"""The shared computer: its state, its stop switch and the agent's own socket."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from ..agent.run_state import RunLauncher
from ..computer import CLOSE_UNAUTHORIZED
from ..engine import SupervisorEngine
from ..host_computer import host_permissions, request_host_permissions
from ..store import Store
from .common import get_engine, get_launcher, get_store

router = APIRouter()


class ComputerTargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(pattern="^(local|cloud|mixed)$")


@router.get("/computer")
async def get_computer(
    store: Store = Depends(get_store), engine: SupervisorEngine = Depends(get_engine)
) -> dict:
    """The shared computer and the token its agent pairs with (behind the launch token)."""
    return {**engine.computer.state(), "token": store.computer_token()}


@router.patch("/computer/target")
async def set_computer_target(
    body: ComputerTargetBody,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Which machine the bots may drive. ``mixed`` lets a call name ``place``."""
    try:
        store.set_computer_target(body.target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="target 只能是 local、cloud 或 mixed。") from exc
    engine.computer.set_target(body.target)
    return {**engine.computer.state(), "token": store.computer_token()}



@router.get("/computer/permissions")
async def computer_permissions() -> dict:
    """Whether this Mac has granted screen recording and accessibility. No prompt."""
    return host_permissions()


@router.post("/computer/permissions")
async def computer_permissions_request() -> dict:
    """Ask macOS for both, and open System Settings for whichever is still missing."""
    return await asyncio.to_thread(request_host_permissions)


@router.post("/computer/stop")
async def stop_computer(
    engine: SupervisorEngine = Depends(get_engine),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    """Stops whichever run is using the computer right now."""
    run_id = engine.computer.holder_run_id
    if run_id is not None:
        await launcher.finish_stop(run_id)
    return engine.computer.state()


@router.websocket("/computer/agent")
async def computer_agent(socket: WebSocket) -> None:
    """The agent on the driven machine dials in here; HTTP auth never sees this route.

    A wrong token is accepted and closed at once with its own code, so the agent can say
    why instead of seeing a bare HTTP 403.
    """
    engine: SupervisorEngine = socket.app.state.engine
    await socket.accept()
    if not engine.computer.authorized(socket.headers.get("authorization")):
        await socket.close(code=CLOSE_UNAUTHORIZED)
        return
    await engine.computer.serve(socket)
