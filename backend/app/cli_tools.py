"""Cubes' own tools, offered to a bot whose engine is the user's own CLI.

A Claude Code or Codex bot does its thinking inside that CLI: it never hands a tool call back
to :mod:`app.engine`, so on its own it cannot create members, ask a teammate, start a workflow
or remember anything. This module closes that gap. Every CLI turn gets a short-lived MCP
endpoint whose token is bound to one (run, bot, conversation); the endpoint lists exactly the
tools that turn was offered, and a call goes through the same engine path an API model's tool
call does — an action row in the chat, the run and turn budgets, confirmation cards, stop.

The endpoint is mounted on the app the backend already runs on loopback:
``/cli-tools/<token>``. Its own token is the only credential, verified in constant time, so
:mod:`app.auth` lets that prefix through like ``/hooks/``.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)

from .errors import RunLimited, RunStopped, ToolError, TurnHandedOff, TurnLimited, UpstreamError
from .model_stream import ToolCallFragment
from .tools.types import error_result

# Where the endpoint is mounted, and the name the CLI sees it under (``mcp__cubes__<tool>``).
PREFIX = "/cli-tools"
SERVER_NAME = "cubes"
# A turn may park on a card for a while, but a token nobody closed must still die.
TOKEN_TTL_SECONDS = 3600.0
# The engine-side state slot: the turn's bridge, placed on the request's ``scope``.
SCOPE_KEY = "cubes_cli_tools"

# The bridge of the request being served. Set by the guard around the MCP app; asyncio copies
# the context into the task the transport spawns, so the handlers on the other side see it.
_REQUEST_BRIDGE: ContextVar["ToolBridge | None"] = ContextVar(
    "cubes_cli_tool_bridge", default=None
)

NOTE = (
    "本轮你可以直接使用 Cubes 的团队工具：它们由 Cubes 以 MCP 服务器「"
    + SERVER_NAME
    + "」提供，工具名与 Cubes 里的完全一样（例如 create_agent、ask_agent、run_workflow、"
    "remind_me、team_report）。需要管理团队、把任务交给成员、安排定时任务或记住事情时，"
    "就调用这些工具，而不是只在回答里描述。每次调用都会在对话里显示成一条操作记录，"
    "需要确认的工具会停下来等用户回答。"
)


def _shape(schema: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """One engine tool schema as ``(name, description, parameters)``."""
    function = schema.get("function")
    if not isinstance(function, dict):
        function = schema
    name = str(function.get("name") or "")
    description = str(function.get("description") or "")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return name, description, parameters


def _was_error(text: str) -> bool:
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("ok") is False


@dataclass
class ToolBridge:
    """One CLI turn's Cubes tools: its token, its offered tools, and how a call runs.

    ``turn`` is the engine's own turn budget of the task that opened the bridge. The MCP call
    arrives on another task, so it is installed there while the call runs; the shared counter
    object then reads the same to both sides.
    """

    hub: "Hub"
    token: str
    engine: Any
    active: Any
    conversation: dict
    author: dict
    schemas: list[dict[str, Any]]
    turn: Any = None
    expires_at: float = 0.0
    # Set once the run ended (budget, stop, hand-off): further calls are refused cheaply.
    closed_reason: str | None = None

    # ------------------------------------------------------------------ surface

    @property
    def url(self) -> str:
        return f"{self.hub.origin}{PREFIX}/{self.token}"

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in (_shape(schema) for schema in self.schemas)]

    def offers(self, name: str) -> bool:
        return any(name == offered for offered, _, _ in (_shape(s) for s in self.schemas))

    def argv(self, kind: str) -> list[str]:
        """The CLI arguments that point this turn at the bridge (empty when none)."""
        if kind == "codex":
            return [
                "-c",
                f'mcp_servers.{SERVER_NAME}.url="{self.url}"',
                "-c",
                f'mcp_servers.{SERVER_NAME}.http_headers={{Authorization="Bearer {self.token}"}}',
            ]
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {self.token}"},
                }
            }
        }
        return [
            "--mcp-config",
            json.dumps(config, ensure_ascii=False, separators=(",", ":")),
            # Only Cubes' own server: the user's MCP servers would add tools this turn did
            # not offer, and the CLI's permission prompts cannot be answered in print mode.
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(f"mcp__{SERVER_NAME}__{name}" for name in self.names),
        ]

    def note(self) -> str:
        return NOTE

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run one MCP call; return the text handed back and whether it is an error."""
        if self.closed_reason is not None:
            return error_result(self.closed_reason), True
        if not self.offers(name):
            reason = f"本轮没有提供工具「{name}」，本次调用未执行。"
            return error_result(reason), True
        call = ToolCallFragment(
            index=0,
            id=f"mcp-{uuid.uuid4().hex}",
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
        previous = self.active.turn
        self.active.turn = self.turn
        try:
            text, _images = await self.engine._handle_tool_call(
                self.active, self.conversation, call, self.author
            )
            return text, _was_error(text)
        except (RunStopped, RunLimited) as exc:
            # The run is over on the engine side: the CLI process is being killed, and any
            # later call would only create rows nobody can act on.
            reason = str(exc) or "任务已停止。"
            self.closed_reason = reason
            return error_result(reason), True
        except TurnHandedOff:
            reason = "这项工作的页面已经打开，本轮到此结束。"
            self.closed_reason = reason
            return error_result(reason), True
        except TurnLimited as exc:
            return error_result(str(exc)), True
        except (UpstreamError, ToolError) as exc:
            return error_result(str(exc)), True
        finally:
            self.active.turn = previous

    def close(self) -> None:
        self.hub.bridges.pop(self.token, None)


class Hub:
    """One app's CLI tool endpoint: the live turn tokens, the MCP server, and its origin."""

    def __init__(self) -> None:
        self.bridges: dict[str, ToolBridge] = {}
        # ``http://127.0.0.1:<port>``, learned from the first request the app answered.
        self.origin = ""
        # The low-level server, not ``MCPServer``: its ``list_tools`` takes no request context,
        # but what a turn may call depends on the token in the URL, so listing and calling are
        # resolved per request here. The protocol surface is otherwise the SDK's own.
        self.server = Server(
            SERVER_NAME,
            instructions="Cubes 的团队工具。每次调用都会作为一条操作记录出现在对话里。",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )
        self.manager = StreamableHTTPSessionManager(
            app=self.server, stateless=True, json_response=True
        )
        self.endpoint = _Guarded(self)

    # ------------------------------------------------------------------ mounting

    def install(self, app: FastAPI) -> None:
        app.state.cli_tools = self
        app.mount(PREFIX, self.endpoint)
        app.add_middleware(_Origin, hub=self)

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[None]:
        """Hold the session manager open for the app's lifetime; mounts get no lifespan."""
        async with self.manager.run():
            yield

    # ------------------------------------------------------------------ booking

    def begin(
        self,
        engine: Any,
        active: Any,
        conversation: dict,
        author: dict,
        schemas: list[dict[str, Any]],
    ) -> ToolBridge | None:
        """Open this turn's token; ``None`` when there is nothing to offer yet."""
        if not self.origin or not schemas:
            return None
        now = time.monotonic()
        # A stream that failed before it could close would otherwise leave a row here until
        # the process ends; each new turn sweeps the ones whose hour is up.
        for token in [token for token, old in self.bridges.items() if old.expires_at <= now]:
            self.bridges.pop(token, None)
        token = secrets.token_urlsafe(32)
        bridge = ToolBridge(
            hub=self,
            token=token,
            engine=engine,
            active=active,
            conversation=conversation,
            author=author,
            schemas=list(schemas),
            turn=active.turn,
            expires_at=now + TOKEN_TTL_SECONDS,
        )
        self.bridges[token] = bridge
        return bridge

    def bridge_for(self, token: str) -> ToolBridge | None:
        """The live bridge a presented token names, compared in constant time."""
        found: ToolBridge | None = None
        now = time.monotonic()
        for bridge in tuple(self.bridges.values()):
            if hmac.compare_digest(bridge.token, token):
                if bridge.expires_at <= now:
                    self.bridges.pop(bridge.token, None)
                    return None
                found = bridge
        return found

    def remember(self, scope: dict[str, Any]) -> None:
        """Keep the address the backend is really listening on, for this turn's CLI config."""
        server = scope.get("server")
        if not server:
            return
        host, port = server[0], server[1]
        if not host or not port:
            return
        self.origin = f"{scope.get('scheme') or 'http'}://{host}:{port}"

    # ------------------------------------------------------------------ handlers

    async def _list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        bridge = _request_bridge(ctx)
        if bridge is None:  # pragma: no cover - the guard answered 401 already
            return types.ListToolsResult(tools=[])
        return types.ListToolsResult(
            tools=[
                types.Tool(name=name, description=description, input_schema=parameters)
                for name, description, parameters in (
                    _shape(schema) for schema in bridge.schemas
                )
                if name
            ]
        )

    async def _call_tool(self, ctx: Any, params: Any) -> types.CallToolResult:
        bridge = _request_bridge(ctx)
        if bridge is None:  # pragma: no cover - the guard answered 401 already
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=error_result("工具端点已失效。"))],
                is_error=True,
            )
        arguments = params.arguments if isinstance(params.arguments, dict) else {}
        text, failed = await bridge.call(str(params.name or ""), arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=failed
        )


def _request_bridge(ctx: Any) -> ToolBridge | None:
    """The bridge of this request: the context the guard set, else the request's scope."""
    bridge = _REQUEST_BRIDGE.get()
    if bridge is not None:
        return bridge
    request = getattr(ctx, "request", None)
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        return scope.get("state", {}).get(SCOPE_KEY)
    return None


def _token_of(path: str) -> str:
    """The token is the last path segment of ``/cli-tools/<token>``."""
    return path.rstrip("/").rsplit("/", 1)[-1]


class _Guarded:
    """The ASGI door in front of the MCP app: origin, token, then the transport."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.inner = StreamableHTTPASGIApp(hub.manager)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.inner(scope, receive, send)
            return
        self.hub.remember(scope)
        bridge = self.hub.bridge_for(_token_of(str(scope.get("path") or "")))
        if bridge is None:
            response = JSONResponse(
                status_code=401, content={"detail": "这个工具端点不存在或已失效。"}
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})[SCOPE_KEY] = bridge
        reset = _REQUEST_BRIDGE.set(bridge)
        try:
            await self.inner(scope, receive, send)
        finally:
            _REQUEST_BRIDGE.reset(reset)


class _Origin:
    """Records the port every ordinary API request arrived on, for the CLI's MCP config."""

    def __init__(self, app: Any, hub: Hub) -> None:
        self.app = app
        self.hub = hub

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            self.hub.remember(scope)
        await self.app(scope, receive, send)
