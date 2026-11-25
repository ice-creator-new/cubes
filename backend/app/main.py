"""FastAPI application: the workspace/agent/conversation/run routes plus SSE.

The database is opened, migrated and bootstrapped inside the lifespan (or lazily on the
first store call), so importing this module never touches a live database. Each
conversation owns at most one run: a new message interrupts the running one before its
own run starts, and different conversations run in parallel.

The routes themselves live in :mod:`app.routes`, one module per resource area; this module
builds the services they share and wires them into the app.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import cli_tools
from .agent.run_state import RunLauncher
from .auth import install_auth
from .avatars import create_avatar_router
from .config import Settings, load_settings
from .engine import SupervisorEngine
from .routes import (
    actions,
    activities,
    agents,
    approval_rules,
    attachments,
    computer,
    connectors,
    conversations,
    events,
    hooks,
    memories,
    providers,
    reactions,
    schedules,
    settings as settings_router,
    sidebar,
    skills,
    team,
    usage,
    workbench,
    workspace,
    workflows,
)
from .scheduler import Scheduler
from .task_scheduler import TaskScheduler
from .store import Store


def _validation_detail(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "请求参数无效。"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg", "请求参数无效"))
    prefix = "Value error, "
    if message.startswith(prefix):
        message = message[len(prefix) :]
    return f"{location or 'body'}：{message}"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    ``settings`` is injectable so tests can point the service at a temporary database and
    an isolated upstream; ``create_app()`` resolves ``backend/.env`` plus real env vars.
    """
    resolved = settings if settings is not None else load_settings()
    # ``backend/.env``'s model is only the provider imported on a database's first start.
    env_key = resolved.api_key.strip()
    store = Store(
        resolved.database_path,
        env_base_url=resolved.base_url,
        env_api_key=env_key,
        env_model=resolved.model,
    )
    engine = SupervisorEngine(resolved, store)
    launcher = RunLauncher(store, engine)
    # The workflow tools start a walk in another conversation; only the app has both halves.
    engine.launcher = launcher
    task_scheduler = TaskScheduler(store, engine)
    engine.task_scheduler = task_scheduler

    scheduler = Scheduler(
        store,
        tick_seconds=resolved.schedule_tick_seconds,
        default_timezone=resolved.timezone,
        fire=launcher.fire_scheduled,
        publish=engine.publish,
    )

    # CLI bots reach Cubes' own tools over MCP on this same loopback listener; each turn gets
    # a token. The session manager lives exactly as long as the app's lifespan.
    hub = cli_tools.Hub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.initialize()
        store.sync_digest_schedule(timezone=resolved.timezone)
        await engine.startup()
        # The loop starts after the engine is ready and is cancelled before it shuts down;
        # its first pass applies the missed-while-down rules and never runs anything due.
        engine.on_run_finished(scheduler.on_run_finished)
        engine.on_run_finished(task_scheduler.on_run_finished)
        # A send still waiting in a queue when the last process ended starts now.
        await launcher.resume_queued()
        # Stale task rows were healed by the store's startup recovery; nothing is queued
        # then, but a pump keeps the two clocks honest if a previous process left work.
        await task_scheduler.pump()
        await scheduler.start()
        try:
            async with hub.serving():
                yield
        finally:
            await scheduler.stop()
            await engine.shutdown()
            store.close()

    app = FastAPI(title="Cubes Team", version="2.0.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.store = store
    app.state.engine = engine
    app.state.launcher = launcher
    app.state.scheduler = scheduler
    app.state.task_scheduler = task_scheduler
    # Installed before CORS so the CORS layer stays outermost and preflights are answered.
    install_auth(app, resolved.auth_token)
    # Credentials stay off (no cookies), and only listed origins may send the launch token.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        # ``X-Attachment-Name`` carries a text/PDF upload's original filename.
        allow_headers=["Content-Type", "Accept", "Authorization", "X-Attachment-Name"],
    )
    app.include_router(create_avatar_router(resolved.avatar_cache_dir))
    for module in (
        workspace,
        attachments,
        workbench,
        memories,
        agents,
        activities,
        providers,
        connectors,
        computer,
        sidebar,
        schedules,
        settings_router,
        skills,
        conversations,
        actions,
        approval_rules,
        events,
        usage,
        reactions,
        workflows,
        hooks,
        team,
    ):
        app.include_router(module.router)

    # A CLI bot reaches Cubes' own tools over MCP on this same loopback listener; the engine
    # opens one token per turn. Mounted last so its prefix never shadows an API route.
    engine.cli_tools = hub
    hub.install(app)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _validation_detail(exc)})

    return app


app = create_app()
