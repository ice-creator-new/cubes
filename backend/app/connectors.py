"""Connectors: account-wide MCP servers whose tools every bot may call.

Each enabled connector keeps one live MCP session. The session is owned by its own task,
because the SDK's transports are cancel scopes that must be entered and left in the same
task; tool calls from runs on other tasks share it freely. A connector is only saved, or a
change to how it connects only applied, after a fresh session connected and listed tools,
so a stored connector always describes a configuration that worked at least once.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from . import connector_presets as catalog
from .connector_presets import (
    CUSTOM_HTTP,
    CUSTOM_STDIO,
    HTTP,
    PRESETS,
    PRESETS_BY_ID,
    Connection,
    Preset,
    connection_for,
    preset_payload,
)
from .errors import ToolError
from .providers import api_key_hint

HTTP_CONNECT_SECONDS = 30.0
# A local preset may download its npm package on first start.
STDIO_CONNECT_SECONDS = 120.0
CALL_SECONDS = 120.0
CLOSE_SECONDS = 10.0
MAX_TOOL_PAGES = 20
MAX_NAME_CHARS = 64
MAX_SLUG_CHARS = 20
MAX_ENTRIES = 32
MAX_VALUE_CHARS = 4096
MAX_ARGS = 64
# OpenAI-compatible endpoints accept ``^[a-zA-Z0-9_-]{1,64}$`` for function names.
MAX_TOOL_NAME_CHARS = 64
SEPARATOR = "__"
MAX_SCHEMA_DESCRIPTION = 1000
MAX_UI_DESCRIPTION = 300
MAX_ARGUMENT_PREVIEW = 400
MAX_STDERR_CHARS = 200

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# Servers built on the Python SDK wrap every tool failure in this English prefix.
SDK_ERROR_PREFIX = re.compile(r"^Error executing tool [^:]+:\s*")

# Where Node and friends usually live. A GUI-launched app inherits only the system PATH, so
# ``npx`` from Homebrew or nvm would otherwise never be found.
EXTRA_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "~/.cargo/bin",
)
NVM_GLOB = "~/.nvm/versions/node/*/bin"
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


class ConnectorInvalid(RuntimeError):
    """A field the user must fix; shown verbatim (422)."""


class ConnectorNameTaken(RuntimeError):
    """Another connector already has this name (409)."""


class ConnectorUnavailable(RuntimeError):
    """The server could not be reached or refused us; a Chinese sentence (502)."""


# ---------------------------------------------------------------------- tools


@dataclass(frozen=True)
class ConnectorTool:
    """One tool of one connector, as the model and the action row see it."""

    connector_id: str
    connector_name: str
    model_name: str
    name: str
    title: str
    read_only: bool

    @property
    def label(self) -> str:
        """``正在{label}`` / ``{label}失败`` read naturally with this."""
        return f"调用 {self.connector_name}「{self.title or self.name}」"


def _model_names(slug: str, names: list[str]) -> list[str]:
    """A model-safe, unique function name per tool: ``<slug>__<tool>``."""
    taken: set[str] = set()
    result: list[str] = []
    for name in names:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", name) or "tool"
        base = f"{slug}{SEPARATOR}{safe}"[:MAX_TOOL_NAME_CHARS]
        candidate, index = base, 2
        while candidate in taken:
            suffix = f"_{index}"
            candidate = base[: MAX_TOOL_NAME_CHARS - len(suffix)] + suffix
            index += 1
        taken.add(candidate)
        result.append(candidate)
    return result


def _stored_tool(tool: Any, preset: Preset, allow_writes: bool) -> dict:
    """The parts of an MCP ``Tool`` we keep, with the read/write decision made once."""
    annotations = tool.annotations
    if preset.read_only_mode_is_enforced and not allow_writes:
        # The vendor removed every write tool from this session itself.
        read_only = True
    elif preset.id == "feishu":
        # Feishu's annotations are unreliable (a plain read is marked destructive).
        read_only = tool.name in catalog.FEISHU_READ_TOOLS
    else:
        read_only = bool(annotations is not None and annotations.read_only_hint is True)
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    title = tool.title or (annotations.title if annotations is not None else None) or ""
    return {
        "name": tool.name,
        "title": title,
        "description": tool.description or "",
        "input_schema": schema,
        "read_only": read_only,
    }


# ---------------------------------------------------------------------- results


def result_text(result: Any) -> str:
    """Everything a tool returned, as text the model can read."""
    parts: list[str] = []
    for item in result.content:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(item.text)
        elif kind in ("image", "audio"):
            parts.append(f"[{'图片' if kind == 'image' else '音频'}：{item.mime_type}，未显示]")
        elif kind == "resource":
            resource = item.resource
            text = getattr(resource, "text", None)
            parts.append(text if text is not None else f"[资源：{resource.uri}]")
        elif kind == "resource_link":
            parts.append(f"[链接：{item.title or item.name} {item.uri}]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(part for part in parts if part) or "（没有返回内容）"


# ---------------------------------------------------------------------- local commands


def search_path() -> str:
    """The PATH a local server runs with: the inherited one plus the usual tool homes."""
    dirs: list[str] = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    candidates = [os.path.expanduser(d) for d in EXTRA_PATH_DIRS]
    # Newest nvm-installed Node first.
    candidates += sorted(glob.glob(os.path.expanduser(NVM_GLOB)), reverse=True)
    candidates += list(SYSTEM_PATH)
    for candidate in candidates:
        if candidate not in dirs and Path(candidate).is_dir():
            dirs.append(candidate)
    return os.pathsep.join(dirs)


def _missing_command(command: str) -> str:
    if command in ("npx", "node", "npm"):
        return f"找不到命令「{command}」，请先安装 Node.js。"
    if command in ("uvx", "uv"):
        return f"找不到命令「{command}」，请先安装 uv。"
    return f"找不到命令「{command}」，请确认它已安装，或填写完整路径。"


# ---------------------------------------------------------------------- feishu


class FeishuAuth(httpx2.Auth):
    """Adds a tenant access token, minted from the app credentials and renewed before expiry."""

    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            try:
                async with httpx2.AsyncClient(timeout=15.0) as client:
                    response = await client.post(
                        catalog.FEISHU_TOKEN_URL,
                        json={"app_id": self._app_id, "app_secret": self._app_secret},
                    )
                data = response.json()
            except (httpx2.HTTPError, ValueError) as exc:
                raise ConnectorUnavailable("连不上飞书开放平台，请检查网络。") from exc
            if not isinstance(data, dict) or data.get("code") != 0 or not data.get("tenant_access_token"):
                message = str(data.get("msg") or "未知错误") if isinstance(data, dict) else "未知错误"
                raise ConnectorUnavailable(
                    f"飞书拒绝了应用凭据（{message}），请检查 App ID 和 App Secret。"
                )
            self._token = str(data["tenant_access_token"])
            # Renew five minutes early; never cache for less than a minute.
            self._expires = time.monotonic() + max(60.0, float(data.get("expire") or 7200) - 300.0)
            return self._token

    async def async_auth_flow(self, request):  # type: ignore[override]
        request.headers["X-Lark-MCP-TAT"] = await self.token()
        yield request


# ---------------------------------------------------------------------- one session


def _leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from _leaves(inner)
    else:
        yield error


def _sentence(text: str) -> str:
    text = " ".join(text.split())[:MAX_STDERR_CHARS].rstrip("。.;；,， ")
    return f"{text}。" if text else ""


class Session:
    """One live MCP connection, owned by a dedicated task."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.client: Client | None = None
        self.last_status: int | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None
        self._errlog: Any = None
        self._auth: FeishuAuth | None = None

    @property
    def is_stdio(self) -> bool:
        return self.connection.transport != HTTP

    async def start(self) -> None:
        """Connect; raises :class:`ConnectorUnavailable` with a readable reason."""
        if self.connection.feishu is not None:
            self._auth = FeishuAuth(*self.connection.feishu)
            await self._auth.token()  # a credential error reads better before the session
        self._task = asyncio.create_task(self._own())
        limit = STDIO_CONNECT_SECONDS if self.is_stdio else HTTP_CONNECT_SECONDS
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), limit)
        except asyncio.TimeoutError:
            await self.close()
            if self.is_stdio:
                raise ConnectorUnavailable(
                    "本地服务启动超时，可能还在下载依赖，请稍后点「重新连接」。"
                ) from None
            raise ConnectorUnavailable("连接超时，服务没有响应。") from None
        except BaseException:
            await self.close()
            raise

    async def list_tools(self) -> list[Any]:
        assert self.client is not None
        tools: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            page = await self.client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        return tools

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), CLOSE_SECONDS)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None

    async def _own(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(
                    Client(await self._transport(stack), read_timeout_seconds=CALL_SECONDS)
                )
                self.client = client
                self._ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported through the ready future
            if not self._ready.done():
                self._ready.set_exception(ConnectorUnavailable(self._describe(exc)))
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.client = None

    async def _transport(self, stack: AsyncExitStack):
        connection = self.connection
        if connection.transport == HTTP:

            async def remember(response: httpx2.Response) -> None:
                self.last_status = response.status_code

            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=connection.headers,
                    auth=self._auth,
                    timeout=httpx2.Timeout(HTTP_CONNECT_SECONDS, read=CALL_SECONDS + 30.0),
                    follow_redirects=True,
                    event_hooks={"response": [remember]},
                )
            )
            return streamable_http_client(connection.url, http_client=http)
        path = search_path()
        command = connection.command
        resolved = command if os.sep in command else shutil.which(command, path=path)
        if not resolved or not os.access(resolved, os.X_OK):
            raise ConnectorUnavailable(_missing_command(command))
        self._errlog = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
        params = StdioServerParameters(
            command=resolved,
            args=list(connection.args),
            env={"PATH": path, **connection.env},
        )
        return stdio_client(params, errlog=self._errlog)

    def stderr_tail(self) -> str:
        if self._errlog is None:
            return ""
        try:
            self._errlog.seek(0)
            lines = [line.strip() for line in self._errlog.read().splitlines() if line.strip()]
        except (OSError, ValueError):
            return ""
        return lines[-1] if lines else ""

    def _describe(self, error: BaseException) -> str:
        leaves = list(_leaves(error))
        for leaf in leaves:
            if isinstance(leaf, ConnectorUnavailable):
                return str(leaf)
        status = self.last_status
        if status in (401, 403):
            return f"服务拒绝了凭据（HTTP {status}），请检查 token 是否正确、是否过期、权限是否足够。"
        if status == 404:
            return "地址不存在（HTTP 404），请检查 MCP 地址。"
        if status is not None and status >= 400:
            return f"服务返回了错误（HTTP {status}）。"
        for leaf in leaves:
            if isinstance(leaf, (httpx2.ConnectError, httpx2.ConnectTimeout)):
                return "连不上服务，请检查网络或地址。"
            if isinstance(leaf, FileNotFoundError):
                return _missing_command(self.connection.command)
            if isinstance(leaf, PermissionError):
                return f"没有权限运行「{self.connection.command}」。"
        if self.is_stdio:
            tail = _sentence(self.stderr_tail())
            return f"本地服务启动后退出了：{tail}" if tail else "本地服务启动后退出了。"
        leaf = leaves[-1] if leaves else error
        if isinstance(leaf, MCPError):
            return f"服务返回了错误：{_sentence(leaf.message) or '未知错误。'}"
        return f"连接失败：{leaf.__class__.__name__}。"


# ---------------------------------------------------------------------- input cleaning


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ConnectorInvalid("请填写连接器名称。")
    if len(name) > MAX_NAME_CHARS:
        raise ConnectorInvalid(f"连接器名称不能超过 {MAX_NAME_CHARS} 个字。")
    return name


def _clean_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_VALUE_CHARS:
        raise ConnectorInvalid(f"{label}太长了。")
    return text


def _clean_url(value: Any, label: str = "地址") -> str:
    url = _clean_text(value, label)
    if not re.match(r"^https?://[^\s/]+", url, re.IGNORECASE):
        raise ConnectorInvalid(f"请填写以 http:// 或 https:// 开头的{label}。")
    return url


def _clean_map(value: Any, *, kind: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConnectorInvalid(f"{kind}格式不正确。")
    if len(value) > MAX_ENTRIES:
        raise ConnectorInvalid(f"{kind}最多 {MAX_ENTRIES} 项。")
    pattern = HEADER_NAME if kind == "请求头" else ENV_NAME
    cleaned: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name).strip()
        if not pattern.match(name):
            raise ConnectorInvalid(f"{kind}名称「{name}」无效。")
        text = str(raw_value if raw_value is not None else "")
        if len(text) > MAX_VALUE_CHARS or "\n" in text or "\r" in text:
            raise ConnectorInvalid(f"{kind}「{name}」的值无效。")
        cleaned[name] = text.strip()
    return cleaned


def _clean_args(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ARGS:
        raise ConnectorInvalid(f"参数最多 {MAX_ARGS} 个。")
    return [_clean_text(arg, "参数") for arg in value if str(arg).strip()]


def _merge_values(preset: Preset, stored: dict[str, str], sent: Any) -> dict[str, str]:
    """Credential fields after an edit: sent keys replace, an empty optional value clears."""
    if sent is None:
        sent = {}
    if not isinstance(sent, dict):
        raise ConnectorInvalid("凭据格式不正确。")
    known = {f.id: f for f in preset.fields}
    values = {key: value for key, value in stored.items() if key in known}
    for key, raw in sent.items():
        if key not in known:
            raise ConnectorInvalid("凭据里有这个连接器不认识的字段。")
        text = _clean_text(raw, known[key].label)
        if text:
            values[key] = text
        else:
            values.pop(key, None)
    for f in preset.fields:
        if f.required and not values.get(f.id):
            raise ConnectorInvalid(f"请填写{f.label}。")
    if preset.id == "gitlab" and values.get("host"):
        values["host"] = _clean_url(values["host"], "实例地址")
    return values


def _clean_config(preset: Preset, stored: dict, body: dict) -> dict:
    """The stored config after applying ``body`` (create or patch semantics)."""
    if preset.id == CUSTOM_HTTP:
        config = {"url": stored.get("url", ""), "headers": dict(stored.get("headers") or {})}
        if "url" in body or not config["url"]:
            config["url"] = _clean_url(body.get("url"))
        if body.get("headers") is not None:
            config["headers"] = _clean_map(body["headers"], kind="请求头")
        return config
    if preset.id == CUSTOM_STDIO:
        config = {
            "command": stored.get("command", ""),
            "args": list(stored.get("args") or []),
            "env": dict(stored.get("env") or {}),
        }
        if "command" in body or not config["command"]:
            command = _clean_text(body.get("command"), "命令")
            if not command:
                raise ConnectorInvalid("请填写要运行的命令。")
            config["command"] = command
        if body.get("args") is not None:
            config["args"] = _clean_args(body["args"])
        if body.get("env") is not None:
            config["env"] = _clean_map(body["env"], kind="环境变量")
        return config
    return {"values": _merge_values(preset, dict(stored.get("values") or {}), body.get("secrets"))}


def _slug_base(preset: Preset, name: str) -> str:
    if not preset.custom:
        return preset.id
    ascii_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (ascii_name or "mcp")[:MAX_SLUG_CHARS].strip("_") or "mcp"


# ---------------------------------------------------------------------- the manager


@dataclass
class _Runtime:
    status: str = "idle"
    error: str | None = None
    session: Session | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConnectorManager:
    """Owns every connector's live session and turns their tools into model tools."""

    def __init__(self, store: Any, publish: Callable[[str, Any], None]) -> None:
        self._store = store
        self._publish = publish
        self._runtimes: dict[str, _Runtime] = {}
        self._background: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        """Reconnect every enabled connector in the background; startup never waits."""
        for record in self._store.list_connectors():
            runtime = self._runtime(record["id"])
            if record["enabled"]:
                runtime.status = "connecting"
                task = asyncio.create_task(self._connect_stored(record["id"]))
                self._background.add(task)
                task.add_done_callback(self._background.discard)

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        sessions = [r.session for r in self._runtimes.values() if r.session is not None]
        self._runtimes.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)

    def _runtime(self, connector_id: str) -> _Runtime:
        return self._runtimes.setdefault(connector_id, _Runtime())

    # ------------------------------------------------------------------ state

    def state(self) -> dict:
        return {
            "presets": [preset_payload(preset) for preset in PRESETS],
            "connectors": self.payloads(),
        }

    def payloads(self) -> list[dict]:
        return [self._payload(record) for record in self._store.list_connectors()]

    def payload(self, connector_id: str) -> dict | None:
        record = self._store.get_connector(connector_id)
        return None if record is None else self._payload(record)

    def _payload(self, record: dict) -> dict:
        preset = PRESETS_BY_ID[record["preset"]]
        config = record["config"]
        connection = connection_for(record["preset"], config, record["allow_writes"])
        values = config.get("values") or {}
        runtime = self._runtimes.get(record["id"]) or _Runtime()
        status = runtime.status if record["enabled"] else "disabled"
        return {
            "id": record["id"],
            "preset": record["preset"],
            "name": record["name"],
            "slug": record["slug"],
            "transport": preset.transport,
            "url": connection.url,
            "command": connection.command,
            "args": list(connection.args),
            "header_names": sorted(config.get("headers") or {}) if preset.id == CUSTOM_HTTP else [],
            "env_names": sorted(config.get("env") or {}) if preset.id == CUSTOM_STDIO else [],
            "secret_hints": {
                f.id: api_key_hint(values[f.id]) for f in preset.fields if f.secret and values.get(f.id)
            },
            "values": {
                f.id: values[f.id] for f in preset.fields if not f.secret and values.get(f.id)
            },
            "allow_writes": record["allow_writes"],
            "enabled": record["enabled"],
            "status": status,
            "error": runtime.error if status == "error" else None,
            "tools": [
                {
                    "name": tool["name"],
                    "title": tool["title"],
                    "description": tool["description"][:MAX_UI_DESCRIPTION],
                    "read_only": tool["read_only"],
                }
                for tool in record["tools"]
            ],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def _announce(self) -> None:
        self._publish("connectors", {"connectors": self.payloads()})

    # ------------------------------------------------------------------ CRUD

    async def create(self, body: dict) -> dict:
        preset = PRESETS_BY_ID.get(str(body.get("preset") or ""))
        if preset is None:
            raise ConnectorInvalid("未知的连接器类型。")
        name = _clean_name(body.get("name") or preset.name)
        if self._store.connector_name_taken(name):
            raise ConnectorNameTaken(f"连接器名称「{name}」已被使用，请换一个。")
        config = _clean_config(preset, {}, body)
        allow_writes = bool(body.get("allow_writes")) if preset.writes_toggle else False
        session, tools = await self._open(preset, config, allow_writes)
        # Re-checked after the slow connect: another request may have taken the name.
        if self._store.connector_name_taken(name):
            await session.close()
            raise ConnectorNameTaken(f"连接器名称「{name}」已被使用，请换一个。")
        record = self._store.create_connector(
            preset=preset.id,
            name=name,
            slug=self._unique_slug(_slug_base(preset, name)),
            config=config,
            allow_writes=allow_writes,
            tools=tools,
        )
        runtime = self._runtime(record["id"])
        runtime.session, runtime.status, runtime.error = session, "connected", None
        self._announce()
        return self._payload(record)

    async def update(self, connector_id: str, body: dict) -> dict | None:
        record = self._store.get_connector(connector_id)
        if record is None:
            return None
        preset = PRESETS_BY_ID[record["preset"]]
        updates: dict[str, Any] = {}
        if "name" in body and body["name"] is not None:
            name = _clean_name(body["name"])
            if self._store.connector_name_taken(name, exclude_id=connector_id):
                raise ConnectorNameTaken(f"连接器名称「{name}」已被使用，请换一个。")
            updates["name"] = name
        connection_keys = ("secrets", "url", "headers", "command", "args", "env")
        config = record["config"]
        reconnect = False
        if any(body.get(key) is not None for key in connection_keys):
            config = _clean_config(preset, record["config"], body)
            reconnect = config != record["config"]
        allow_writes = record["allow_writes"]
        if preset.writes_toggle and body.get("allow_writes") is not None:
            allow_writes = bool(body["allow_writes"])
            reconnect = reconnect or allow_writes != record["allow_writes"]
        enabled = record["enabled"] if body.get("enabled") is None else bool(body["enabled"])

        runtime = self._runtime(connector_id)
        async with runtime.lock:
            if enabled and (reconnect or not record["enabled"]):
                session, tools = await self._open(preset, config, allow_writes)
                old, runtime.session = runtime.session, session
                runtime.status, runtime.error = "connected", None
                updates.update(config=config, allow_writes=allow_writes, tools=tools)
                if old is not None:
                    await old.close()
            elif reconnect:
                # A disabled connector keeps the edit; it is verified when switched back on.
                updates.update(config=config, allow_writes=allow_writes)
            if not enabled and runtime.session is not None:
                old, runtime.session = runtime.session, None
                await old.close()
            if not enabled:
                runtime.status, runtime.error = "disabled", None
            updates["enabled"] = enabled
            updated = self._store.update_connector(connector_id, **updates)
        self._announce()
        return None if updated is None else self._payload(updated)

    async def delete(self, connector_id: str) -> bool:
        if not self._store.delete_connector(connector_id):
            return False
        runtime = self._runtimes.pop(connector_id, None)
        if runtime is not None and runtime.session is not None:
            await runtime.session.close()
        self._announce()
        return True

    async def reconnect(self, connector_id: str) -> dict | None:
        """Try the stored settings again; a failure is reported on the connector itself."""
        if self._store.get_connector(connector_id) is None:
            return None
        await self._connect_stored(connector_id)
        return self.payload(connector_id)

    def _unique_slug(self, base: str) -> str:
        taken = {record["slug"] for record in self._store.list_connectors()}
        slug, index = base, 2
        while slug in taken:
            suffix = f"_{index}"
            slug = base[: MAX_SLUG_CHARS - len(suffix)] + suffix
            index += 1
        return slug

    # ------------------------------------------------------------------ sessions

    async def _open(self, preset: Preset, config: dict, allow_writes: bool) -> tuple[Session, list[dict]]:
        """A connected session and its tools, or :class:`ConnectorUnavailable`."""
        session = Session(connection_for(preset.id, config, allow_writes))
        await session.start()
        try:
            tools = await session.list_tools()
        except BaseException as exc:
            await session.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ConnectorUnavailable(session._describe(exc)) from exc
        return session, [_stored_tool(tool, preset, allow_writes) for tool in tools]

    async def _connect_stored(self, connector_id: str) -> None:
        record = self._store.get_connector(connector_id)
        if record is None or not record["enabled"]:
            return
        preset = PRESETS_BY_ID[record["preset"]]
        runtime = self._runtime(connector_id)
        async with runtime.lock:
            old, runtime.session = runtime.session, None
            if old is not None:
                await old.close()
            runtime.status, runtime.error = "connecting", None
            self._announce()
            try:
                session, tools = await self._open(preset, record["config"], record["allow_writes"])
            except ConnectorUnavailable as exc:
                runtime.status, runtime.error = "error", str(exc)
            else:
                runtime.session, runtime.status = session, "connected"
                self._store.update_connector(connector_id, tools=tools)
        self._announce()

    # ------------------------------------------------------------------ model tools

    def _offered(self) -> list[tuple[dict, list[str]]]:
        offered: list[tuple[dict, list[str]]] = []
        for record in self._store.list_connectors():
            runtime = self._runtimes.get(record["id"])
            if not record["enabled"] or runtime is None or runtime.status != "connected":
                continue
            names = _model_names(record["slug"], [tool["name"] for tool in record["tools"]])
            offered.append((record, names))
        return offered

    def schemas(self) -> list[dict]:
        schemas: list[dict] = []
        for record, names in self._offered():
            for tool, model_name in zip(record["tools"], names):
                summary = tool["title"] or tool["name"]
                description = f"{record['name']}：{summary}。{tool['description']}".strip()
                if not tool["read_only"]:
                    description += "（会修改数据，调用前会先请用户允许。）"
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": model_name,
                            "description": description[:MAX_SCHEMA_DESCRIPTION],
                            "parameters": tool["input_schema"],
                        },
                    }
                )
        return schemas

    def resolve(self, model_name: str) -> ConnectorTool | None:
        if SEPARATOR not in model_name:
            return None
        for record, names in self._offered():
            for tool, candidate in zip(record["tools"], names):
                if candidate == model_name:
                    return ConnectorTool(
                        connector_id=record["id"],
                        connector_name=record["name"],
                        model_name=model_name,
                        name=tool["name"],
                        title=tool["title"],
                        read_only=tool["read_only"],
                    )
        return None

    def prompt_hint(self) -> str:
        offered = self._offered()
        if not offered:
            return ""
        listed = "、".join(f"{record['name']}（工具名以 {record['slug']}{SEPARATOR} 开头）" for record, _ in offered)
        return (
            f"\n\n已连接的外部服务：{listed}。"
            "用户问到这些服务里的数据时，直接调用对应工具查询，不要凭印象回答；"
            "会修改数据的工具调用前会先请用户允许，被拒绝时不要换个工具绕过去。"
        )

    async def call(self, target: ConnectorTool, arguments: dict) -> str:
        runtime = self._runtimes.get(target.connector_id)
        session = runtime.session if runtime is not None else None
        if session is None or session.client is None:
            raise ToolError(f"{target.connector_name} 当前没有连接，本次调用未执行。")
        try:
            result = await session.client.call_tool(target.name, arguments)
        except MCPError as exc:
            if exc.code == REQUEST_TIMEOUT:
                raise ToolError(
                    f"{target.connector_name} 在 {int(CALL_SECONDS)} 秒内没有返回结果。"
                ) from exc
            if exc.code == CONNECTION_CLOSED:
                await self._lost(target.connector_id, session)
                raise ToolError(f"和 {target.connector_name} 的连接断开了，本次调用没有完成。") from exc
            raise ToolError(f"{target.connector_name} 返回了错误：{_sentence(exc.message) or '未知错误。'}") from exc
        except httpx2.HTTPError as exc:
            await self._lost(target.connector_id, session)
            raise ToolError(f"连不上 {target.connector_name}，本次调用没有完成。") from exc
        text = result_text(result)
        if result.is_error:
            reason = SDK_ERROR_PREFIX.sub("", text).strip()[:MAX_ARGUMENT_PREVIEW]
            raise ToolError(f"{target.connector_name} 返回了错误：{reason or '未知错误。'}")
        return text

    async def _lost(self, connector_id: str, session: Session) -> None:
        """A broken session stops offering tools; the next reconnect brings it back."""
        runtime = self._runtimes.get(connector_id)
        if runtime is None or runtime.session is not session:
            return
        runtime.session, runtime.status = None, "error"
        tail = _sentence(session.stderr_tail()) if session.is_stdio else ""
        runtime.error = f"连接断开了：{tail}" if tail else "连接断开了，请点「重新连接」。"
        self._announce()
        await session.close()


def argument_preview(arguments: dict) -> str:
    text = json.dumps(arguments, ensure_ascii=False)
    return text if len(text) <= MAX_ARGUMENT_PREVIEW else text[: MAX_ARGUMENT_PREVIEW - 1] + "…"
