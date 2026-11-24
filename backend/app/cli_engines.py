"""Running a bot's reply on the user's own Claude Code or Codex CLI.

Cubes normally speaks HTTP to a model service. A ``claude-code`` or ``codex`` provider
instead hands the whole turn to a CLI that is already installed and signed in on this
machine, so a bot answers on the user's own subscription without an API key.

The CLI is the agent here: it brings its own tools, so nothing comes back to Cubes' tool
loop and no tool schemas are sent to it. This module only turns its JSON event stream into
the same text deltas every other adapter produces, reports the usage it prints, renders the
conversation into one prompt, and maps failures (missing command, not signed in, quota,
non-zero exit) onto the engine's existing errors with Chinese sentences. Each bot runs in
its own working directory, and a stop request kills the whole process group.

Observed non-interactive shapes (checked against the installed CLIs):

``claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages``
    stdin, one line then EOF:
    ``{"type":"user","message":{"role":"user","content":[{"type":"text","text":"…"},
    {"type":"image","source":{"type":"base64","media_type":"image/png","data":"…"}},
    {"type":"document","source":{"type":"base64","media_type":"application/pdf","data":"…"}}]}}``
    stdout:
    ``{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"…"}}}``
    ``{"type":"assistant","message":{"content":[{"type":"text","text":"…"}],"usage":{…}}}``
    ``{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{…}}``

``codex exec --json --image=<path> -``
    ``{"type":"item.completed","item":{"type":"agent_message","text":"…"}}``
    ``{"type":"turn.completed","usage":{"input_tokens":…,"output_tokens":…}}``

Two more things this module owns, both cheaper than a model call: the model catalog each CLI
reports from this machine (``codex debug models``, ``~/.claude/settings.json``; cached a
minute, never fatal) and the CLI session a bot continues per chat (Claude Code resumes by
``--session-id`` / ``--resume``, Codex by ``codex exec resume <thread id>``), so only what is
new since the bot's last turn is sent.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import permissions
from .errors import StreamAborted, UpstreamError
from .model_stream import CompletionResult, Usage

CLAUDE_CODE = "claude-code"
CODEX = "codex"
CLI_PROTOCOLS = (CLAUDE_CODE, CODEX)
CLI_LABELS = {CLAUDE_CODE: "Claude Code", CODEX: "Codex"}
# The one model name that means "let the CLI pick its own model"; anything else is passed as
# ``--model`` / ``-m``. Providers of these kinds list at least this name.
CLI_DEFAULT_MODEL = "默认"
# Codex ``item`` types that mean the CLI used a tool of its own, not just wrote text.
CODEX_TOOL_ITEMS = frozenset(
    ("command_execution", "mcp_tool_call", "file_change", "web_search", "patch_apply")
)
# The model aliases ``claude --model`` accepts, and the effort levels ``claude --effort``
# accepts (per ``claude --help``).
CLAUDE_ALIASES = ("opus", "sonnet", "haiku", "fable")
CLAUDE_ALIAS_LABELS = {
    "opus": "opus（最新 Opus）",
    "sonnet": "sonnet（最新 Sonnet）",
    "haiku": "haiku（最快 Haiku）",
    "fable": "fable（最新 Fable）",
}
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Reasoning levels Codex knows about when a model's own entry does not say. A model found in
# the catalog is validated against exactly its own ``supported_reasoning_levels``.
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# A model catalog costs no model tokens, but it may shell out: read it once a minute.
CATALOG_TTL_SECONDS = 60.0
CLAUDE_SETTINGS_FILE = "settings.json"
CODEX_CACHE_FILE = "models_cache.json"
CODEX_CONFIG_FILE = "config.toml"
# Aliases of the env overrides in ``~/.claude/settings.json``, per alias.
CLAUDE_ENV_MODELS = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}
# Per-bot working folders live under the data directory unless a workspace is configured.
BOTS_DIRNAME = "bots"
# How often a stop request is noticed while the CLI is silent, and how long a turn may run.
POLL_SECONDS = 0.25
STEP_TIMEOUT_SECONDS = 1800.0
TERMINATE_GRACE_SECONDS = 5.0
STATUS_TIMEOUT = 10.0
STATUS_TTL_SECONDS = 30.0
MAX_ERROR_CHARS = 400
STREAM_LIMIT = 4 * 1024 * 1024
# Environment overrides for a CLI installed somewhere unusual (and for the tests).
COMMAND_ENV = {CLAUDE_CODE: "CUBES_CLAUDE_BIN", CODEX: "CUBES_CODEX_BIN"}
INSTALL_HINTS = {
    CLAUDE_CODE: "请先安装 Claude Code（npm install -g @anthropic-ai/claude-code），"
    "再在终端运行 claude 完成登录。",
    CODEX: "请先安装 Codex（npm install -g @openai/codex），再在终端运行 codex login 完成登录。",
}
LOGIN_HINTS = {CLAUDE_CODE: "claude", CODEX: "codex login"}


@dataclass(frozen=True)
class EngineStatus:
    """Whether one CLI can answer right now; every sentence here is shown to the user."""

    kind: str
    label: str
    installed: bool
    ready: bool
    detail: str
    command: str
    version: str


# ----------------------------------------------------------------- working folders


def working_dir(settings: Any, agent_id: str) -> str:
    """The folder one bot's CLI runs in: the configured workspace, else its own data folder.

    The folder is created here, because a CLI refuses to start in a directory that does not
    exist and the engine resolves the model before it knows anything else about the turn.
    """
    base = getattr(settings, "workspace_dir", None)
    if base:
        path = Path(base)
    else:
        database = Path(getattr(settings, "database_path", "data/cubes.sqlite3"))
        path = database.parent / BOTS_DIRNAME / (agent_id.strip() or "shared")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ---------------------------------------------------------------------- commands


def command_name(kind: str) -> str:
    return "claude" if kind == CLAUDE_CODE else "codex"


def command_for(kind: str) -> str | None:
    """The CLI binary to run, or ``None`` when it is not installed."""
    override = os.environ.get(COMMAND_ENV.get(kind, ""), "").strip()
    if override:
        return override if Path(override).is_file() and os.access(override, os.X_OK) else None
    return shutil.which(command_name(kind))


# ------------------------------------------------------------------ model catalog


@dataclass(frozen=True)
class ModelOption:
    """One model this machine's CLI offers, with the wording the pickers show."""

    name: str
    label: str
    description: str = ""
    efforts: tuple[str, ...] = ()
    default_effort: str = ""
    # The input modalities its catalog entry declared, e.g. ``("text", "image")``; empty when
    # the entry said nothing, which means "assume it takes what the CLI always takes".
    input_modalities: tuple[str, ...] = ()
    # Whether the CLI's own configuration currently points at this model.
    current: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "label": self.label,
            "description": self.description,
            "efforts": list(self.efforts),
            "default_effort": self.default_effort,
            "current": self.current,
        }


def claude_home() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".claude"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override) if override else Path.home() / ".codex"


def _efforts_of(entry: dict[str, Any]) -> tuple[str, ...]:
    """The ``effort`` names of one catalog entry's ``supported_reasoning_levels``."""
    levels = entry.get("supported_reasoning_levels")
    efforts: list[str] = []
    for level in levels if isinstance(levels, list) else []:
        effort = str((level or {}).get("effort") or "").strip() if isinstance(level, dict) else ""
        if effort and effort not in efforts:
            efforts.append(effort)
    return tuple(efforts)


def _input_modalities(entry: dict[str, Any]) -> tuple[str, ...]:
    """One catalog entry's input modalities, in any of the shapes Codex has printed."""
    raw = entry.get("input_modalities")
    if isinstance(raw, dict):
        raw = raw.get("input")
    elif raw is None:
        modalities = entry.get("modalities")
        if isinstance(modalities, dict):
            raw = modalities.get("input")
        elif isinstance(modalities, str):
            raw = modalities
    if isinstance(raw, str):
        items = raw.replace("+", ",").split(",")
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        items = []
    found: list[str] = []
    for item in items:
        name = item.strip().lower()
        if name and name not in found:
            found.append(name)
    return tuple(found)


def parse_codex_models(payload: Any) -> list[ModelOption]:
    """The pickable models of one ``codex debug models`` payload (or its cache file).

    Entries whose ``visibility`` is not ``list`` are internal ones Codex itself never offers.
    """
    entries = payload.get("models") if isinstance(payload, dict) else payload
    options: list[ModelOption] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("visibility") or "") != "list":
            continue
        slug = str(entry.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        options.append(
            ModelOption(
                name=slug,
                label=str(entry.get("display_name") or "").strip() or slug,
                description=str(entry.get("description") or "").strip(),
                efforts=_efforts_of(entry),
                default_effort=str(entry.get("default_reasoning_level") or "").strip(),
                input_modalities=_input_modalities(entry),
            )
        )
    return options


def parse_codex_config(text: str) -> str:
    """``model = "…"`` of the root table in ``$CODEX_HOME/config.toml``: its current model."""
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            section = stripped
            continue
        key, sep, value = stripped.partition("=")
        if not sep or section:
            continue
        if key.strip() != "model":
            continue
        return value.split("#")[0].strip().strip("\"'")
    return ""


def parse_claude_models(settings: dict[str, Any]) -> list[ModelOption]:
    """The models ``claude --model`` offers, from its settings: aliases, picker, env mapping.

    ``[1m]`` suffixes are part of the model string, so they are kept verbatim. The alias
    entries carry what this machine's ``env`` maps them to, which is what the CLI will run.
    """
    options: list[ModelOption] = []
    seen: set[str] = set()

    env = settings.get("env")
    env = env if isinstance(env, dict) else {}
    for alias in CLAUDE_ALIASES:
        mapped = str(env.get(CLAUDE_ENV_MODELS.get(alias, ""), "") or "").strip()
        options.append(
            ModelOption(
                name=alias,
                label=CLAUDE_ALIAS_LABELS.get(alias, alias),
                description=(
                    f"本机设置里指向 {mapped}" if mapped else "Claude Code 自带的模型别名"
                ),
                efforts=CLAUDE_EFFORTS,
            )
        )
        seen.add(alias)

    picker = settings.get("modelPicker")
    entries = picker.get("options") if isinstance(picker, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("model") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        options.append(
            ModelOption(
                name=name,
                label=str(entry.get("label") or "").strip() or name,
                description=str(entry.get("description") or "").strip(),
                efforts=CLAUDE_EFFORTS,
            )
        )

    current = str(settings.get("model") or "").strip()
    if current and current not in seen:
        options.append(
            ModelOption(
                name=current,
                label=current,
                description="Claude Code 当前设置的模型",
                efforts=CLAUDE_EFFORTS,
                current=True,
            )
        )
    return _mark_current(options, current)


def _mark_current(options: list[ModelOption], current: str) -> list[ModelOption]:
    return [
        replace(option, current=True) if current and option.name == current else option
        for option in options
    ]


def claude_models() -> list[ModelOption]:
    """The catalog from ``$CLAUDE_CONFIG_DIR/settings.json``; the aliases always exist."""
    path = claude_home() / CLAUDE_SETTINGS_FILE
    try:
        data = json.loads(path.read_text("utf-8", "replace"))
    except (OSError, ValueError):
        data = {}
    return parse_claude_models(data if isinstance(data, dict) else {})


def codex_configured_model() -> str:
    try:
        text = (codex_home() / CODEX_CONFIG_FILE).read_text("utf-8", "replace")
    except OSError:
        return ""
    return parse_codex_config(text)


def _codex_cached_models() -> list[ModelOption]:
    try:
        data = json.loads((codex_home() / CODEX_CACHE_FILE).read_text("utf-8", "replace"))
    except (OSError, ValueError):
        return []
    return parse_codex_models(data)


async def _codex_command_models(command: str, *extra: str) -> list[ModelOption]:
    code, out = await _run_status([command, "debug", "models", *extra])
    if code != 0 or not out.strip():
        return []
    try:
        payload = json.loads(out)
    except ValueError:
        return []
    return parse_codex_models(payload)


async def _read_catalog(kind: str) -> list[ModelOption]:
    """Read one CLI's catalog from this machine; every source is cheap and may be missing."""
    if kind == CLAUDE_CODE:
        return claude_models()
    current = codex_configured_model()
    command = command_for(kind)
    options: list[ModelOption] = []
    if command is not None:
        options = await _codex_command_models(command)
    if not options:
        options = _codex_cached_models()
    if not options and command is not None:
        options = await _codex_command_models(command, "--bundled")
    return _mark_current(options, current)


_CATALOG_CACHE: dict[str, tuple[float, list[ModelOption]]] = {}


async def model_catalog(kind: str, *, force: bool = False) -> list[ModelOption]:
    """The pickable models of one CLI, cached briefly; a failure is an empty list, not an error.

    The settings page and the bot editor read this, so it never raises: a missing command or
    an unreadable settings file only costs the extra names, never the page.
    """
    now = time.monotonic()
    cached = _CATALOG_CACHE.get(kind)
    if not force and cached is not None and now - cached[0] < CATALOG_TTL_SECONDS:
        return cached[1]
    try:
        options = await _read_catalog(kind)
    except Exception:  # a catalog is a convenience; it must not break the settings read
        options = []
    _CATALOG_CACHE[kind] = (now, options)
    return options


def cached_catalog(kind: str) -> list[ModelOption]:
    """The last catalog read, without running anything: what effort validation checks."""
    cached = _CATALOG_CACHE.get(kind)
    return cached[1] if cached is not None else []


def model_modalities(kind: str, model: str) -> tuple[str, ...]:
    """The input modalities one CLI model declared, or ``()`` when nothing says.

    The catalog is read lazily by the settings page; capabilities are resolved on the first
    turn too, so an unread catalog falls back to Codex's own cache file on disk.
    """
    options = cached_catalog(kind)
    if not options and kind == CODEX:
        options = _codex_cached_models()
    for option in options:
        if option.name == model and option.input_modalities:
            return option.input_modalities
    return ()


def caps(kind: str, model: str) -> dict[str, Any]:
    """What a CLI protocol itself accepts from this app, in the capability-layer shape.

    Claude Code reads images and PDFs on stdin, so both always work. Codex takes images as
    files on its command line and reads a PDF from its working folder; whether it can look
    at an image at all comes from its catalog entry, and nothing lists a text-only Codex, so
    an entry that says nothing keeps images on.
    """
    if kind == CLAUDE_CODE:
        return {"images": True, "pdf": True}
    modalities = model_modalities(CODEX, model)
    return {"images": "image" in modalities if modalities else True, "pdf": True}


def effort_levels(kind: str, model: str) -> tuple[str, ...]:
    """The effort levels one model accepts: its own catalog entry, else what the CLI knows."""
    for option in cached_catalog(kind):
        if option.name == model and option.efforts:
            return option.efforts
    return CLAUDE_EFFORTS if kind == CLAUDE_CODE else CODEX_EFFORTS


def effort_allowed(kind: str, model: str, effort: str) -> bool:
    return effort in effort_levels(kind, model)


def new_session_id() -> str:
    """A fresh CLI session id (Claude Code requires a UUID)."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class CLISession:
    """One CLI conversation a turn continues: its id, and whether the CLI already has it."""

    id: str
    resume: bool = False


# ------------------------------------------------------------ conversation diffing


def conversation_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything but the system prompt: the part a CLI session itself remembers."""
    return [message for message in messages if str(message.get("role") or "") != "system"]


def _stable_text(content: Any) -> str:
    """One message's text without this turn's ``<runtime …/>`` block, which changes every turn.

    The block is ``\\n\\n<hints>\\n\\n<tag>``; the hints ride along and must go with the tag,
    or a resumed session's history would never match what it was given last turn.
    """
    text = _text_of(content)
    marker = text.rfind("\n\n<runtime ")
    if marker < 0:
        return text.strip()
    inner = text.rfind("\n\n", 0, marker)
    if inner < 0:
        return text[:marker].strip()
    start = text.rfind("\n\n", 0, inner)
    return text[: start if start >= 0 else inner].strip()


def messages_digest(messages: list[dict[str, Any]]) -> str:
    """A digest of a conversation's messages, stable across turns (the clock is dropped)."""
    hasher = hashlib.sha256()
    for message in messages:
        hasher.update(str(message.get("role") or "").encode("utf-8", "replace"))
        hasher.update(b"\0")
        hasher.update(_stable_text(message.get("content")).encode("utf-8", "replace"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def new_messages(messages: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """What a resumed session has not seen yet: the tail, minus this bot's own answers.

    The CLI already holds the answers it produced, so sending them back would repeat them.
    Everything anyone else said since (the user, other bots) is what the turn really adds.
    """
    return [
        message
        for message in conversation_messages(messages)[count:]
        if str(message.get("role") or "") != "assistant"
    ]


# ------------------------------------------------------------------ status probe

_STATUS_CACHE: dict[str, tuple[float, EngineStatus]] = {}


async def _run_status(argv: list[str]) -> tuple[int, str]:
    """One cheap command: its exit code and its first line of output (or of stderr)."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(process.communicate(), STATUS_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return 124, ""
    text = (out or b"").decode("utf-8", "replace").strip()
    return process.returncode or 0, text


def _claude_logged_in(output: str) -> bool:
    try:
        data = json.loads(output)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("loggedIn") is True


def _codex_custom_provider() -> bool:
    """Whether ``$CODEX_HOME/config.toml`` points at a non-OpenAI model service."""
    try:
        text = (codex_home() / CODEX_CONFIG_FILE).read_text("utf-8", "replace")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("model_provider"):
            continue
        _, _, value = stripped.partition("=")
        name = value.strip().strip("\"'")
        if name and name != "openai":
            return True
    return False


async def engine_status(kind: str) -> EngineStatus:
    """``installed`` / ``ready`` for one CLI, from commands that cost no model tokens."""
    label = CLI_LABELS.get(kind, kind)
    command = command_for(kind)
    if command is None:
        return EngineStatus(
            kind=kind,
            label=label,
            installed=False,
            ready=False,
            detail=f"没有找到 {command_name(kind)} 命令。{INSTALL_HINTS.get(kind, '')}",
            command="",
            version="",
        )
    version = ""
    code, out = await _run_status([command, "--version"])
    if code == 0 and out:
        version = out.splitlines()[0][:120]
    if kind == CLAUDE_CODE:
        code, out = await _run_status([command, "auth", "status"])
        if code == 0 and _claude_logged_in(out):
            return EngineStatus(kind, label, True, True, "已登录，可以直接用你的 Claude 订阅。", command, version)
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return EngineStatus(
                kind, label, True, True, "使用环境变量 ANTHROPIC_API_KEY 调用。", command, version
            )
        return EngineStatus(
            kind,
            label,
            True,
            False,
            "还没有登录：请在终端运行 claude 并完成登录。",
            command,
            version,
        )
    code, _ = await _run_status([command, "login", "status"])
    if code == 0:
        return EngineStatus(kind, label, True, True, "已用 ChatGPT 账号登录。", command, version)
    if _codex_custom_provider():
        return EngineStatus(
            kind, label, True, True, "使用本机 codex 配置的模型服务。", command, version
        )
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return EngineStatus(
            kind, label, True, True, "使用环境变量 OPENAI_API_KEY 调用。", command, version
        )
    return EngineStatus(
        kind,
        label,
        True,
        False,
        "还没有登录：请在终端运行 codex login 并完成登录。",
        command,
        version,
    )


async def engine_statuses(*, force: bool = False) -> list[EngineStatus]:
    """Both statuses, cached briefly so repeated settings reads stay cheap."""
    now = time.monotonic()
    statuses: list[EngineStatus] = []
    for kind in CLI_PROTOCOLS:
        cached = _STATUS_CACHE.get(kind)
        if not force and cached is not None and now - cached[0] < STATUS_TTL_SECONDS:
            statuses.append(cached[1])
            continue
        status = await engine_status(kind)
        _STATUS_CACHE[kind] = (now, status)
        statuses.append(status)
    return statuses


async def probe(kind: str) -> dict[str, Any]:
    """The answer ``POST /providers/test`` gives for a CLI provider.

    A CLI carries its own tools inside its working folder, so the HTTP probe's "can it call
    tools" question does not apply; ``supports_tools`` is true because the CLI does.
    """
    status = await engine_status(kind)
    return {
        "ok": status.ready,
        "latency_ms": 0,
        "supports_tools": status.ready,
        "detail": status.detail,
    }


# --------------------------------------------------------------------- prompting

# Where a turn's PDFs are copied for Codex: inside its own working folder, so the CLI can
# open them with its own file tools. Images go to a throwaway folder on the real filesystem.
ATTACHMENT_DIRNAME = "附件"
IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _text_of(content: Any) -> str:
    """Every text part of a message's content; media parts are skipped.

    The label that stands in for an attachment is itself a text part (see
    :func:`app.agent.history.with_attachments`), so dropping the media keeps the history
    digest stable instead of hashing megabytes of base64.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("text", "input_text") and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part for part in parts if part)


def _data_url(url: str) -> tuple[str, bytes] | None:
    """``(media_type, bytes)`` of one inline ``data:…;base64,`` URL, or ``None``."""
    header, separator, payload = url.partition(",")
    if not separator or not header.startswith("data:") or not header.endswith(";base64"):
        return None
    try:
        return header[len("data:") : -len(";base64")], base64.b64decode(payload)
    except (binascii.Error, ValueError):
        return None


@dataclass(frozen=True)
class RenderedPrompt:
    """One turn as a CLI reads it.

    ``blocks`` is Claude Code's stream-json user content — text, images and documents in the
    order the request had them. ``prompt`` is Codex's plain transcript; ``images`` and
    ``files`` are what Codex gets on disk instead of inline (``--image`` paths and copies in
    ``附件/``), and ``prompt`` already says where a PDF went.
    """

    system: str
    prompt: str
    blocks: list[dict[str, Any]]
    images: list[tuple[str, bytes]]
    files: list[tuple[str, bytes]]


def _segments(content: Any) -> list[tuple[str, Any]]:
    """A message's content as ``(kind, payload)`` in order: ``text``, ``image`` or ``file``."""
    segments: list[tuple[str, Any]] = []
    for part in content if isinstance(content, list) else [content]:
        if isinstance(part, str):
            segments.append(("text", part))
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text") and isinstance(part.get("text"), str):
            segments.append(("text", part["text"]))
        elif kind in ("image_url", "image", "input_image"):
            reference = part.get("image_url")
            url = reference.get("url") if isinstance(reference, dict) else reference
            segments.append(("image", url if isinstance(url, str) else ""))
        elif kind == "file":
            segments.append(("file", part.get("file") if isinstance(part.get("file"), dict) else {}))
    return segments


def _unique_name(name: str, data: bytes, used: dict[str, bytes], *, fallback: str) -> str:
    """A file name that cannot overwrite another attachment with the same original name."""
    candidate = Path(str(name or "")).name
    if candidate in ("", ".", ".."):
        candidate = fallback
    stem, suffix = os.path.splitext(candidate)
    counter = 2
    while candidate in used and used[candidate] != data:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    used[candidate] = data
    return candidate


def render_messages(messages: list[dict[str, Any]], kind: str) -> RenderedPrompt:
    """One turn as a CLI reads it, from the request Cubes already assembled.

    The system prompt keeps its own meaning: Claude Code appends it to its built-in prompt,
    while Codex has no such flag, so it is written at the top of the prompt instead. The
    history becomes a transcript — text with a label where each attachment was — and the
    attachments themselves travel in the form each CLI takes: stdin blocks for Claude Code,
    ``--image`` files and ``附件/`` copies for Codex.
    """
    system: list[str] = []
    transcript: list[str] = []
    blocks: list[dict[str, Any]] = []
    images: list[tuple[str, bytes]] = []
    files: list[tuple[str, bytes]] = []
    used_names: dict[str, bytes] = {}
    for message in messages:
        role = str(message.get("role") or "user")
        segments = _segments(message.get("content"))
        text = "\n".join(value for kind_, value in segments if kind_ == "text").strip()
        if role == "system":
            if text:
                system.append(text)
            continue
        if role == "tool":
            if text:
                transcript.append(f"工具结果：{text}")
            continue
        if not text and not any(kind_ != "text" for kind_, _ in segments):
            continue
        if text:
            # The role prefix is how a transcript stays readable in one flat CLI prompt.
            line = f"{'助手' if role == 'assistant' else '用户'}：{text}"
            transcript.append(line)
            blocks.append({"type": "text", "text": line})
        for kind_, payload in segments:
            if kind_ == "text":
                continue
            if kind_ == "image":
                media = _data_url(str(payload or ""))
                if media is None:
                    continue
                media_type, data = media
                if kind == CLAUDE_CODE:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.b64encode(data).decode("ascii"),
                            },
                        }
                    )
                else:
                    extension = IMAGE_EXTENSIONS.get(media_type, "png")
                    images.append((f"image-{len(images) + 1}.{extension}", data))
                continue
            # A PDF: Claude Code takes the document itself; Codex opens it from its folder.
            file = payload if isinstance(payload, dict) else {}
            name = str(file.get("filename") or "")
            media = _data_url(str(file.get("file_data") or ""))
            if media is None or media[0] != "application/pdf":
                continue
            data = media[1]
            if kind == CLAUDE_CODE:
                blocks.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
                continue
            unique = _unique_name(name, data, used_names, fallback="附件.pdf")
            files.append((unique, data))
            transcript.append(f"PDF 文件已保存到 {ATTACHMENT_DIRNAME}/{unique}，可以直接打开读取。")
    prompt = "\n\n".join(transcript)
    joined_system = "\n\n".join(system)
    if kind == CODEX and joined_system:
        prompt = f"以下是你的身份和规则：\n{joined_system}\n\n以下是对话记录：\n{prompt}"
    if kind == CLAUDE_CODE and not blocks:
        # Stream-json needs at least one content block; an empty turn is a plain nudge.
        blocks = [{"type": "text", "text": prompt or "继续。"}]
    return RenderedPrompt(
        system=joined_system, prompt=prompt, blocks=blocks, images=images, files=files
    )


def claude_stdin(rendered: RenderedPrompt) -> str:
    """The one stream-json user line Claude Code reads, then EOF."""
    payload = {"type": "user", "message": {"role": "user", "content": rendered.blocks}}
    return json.dumps(payload, ensure_ascii=False) + "\n"


def argv_for(
    kind: str,
    resolved: Any,
    command: str,
    system: str,
    *,
    session: CLISession | None = None,
    mcp: list[str] | None = None,
    images: list[str] | None = None,
) -> list[str]:
    """The exact command line one turn runs.

    The CLI brings its own tools, so its own permission switch must answer the same way the
    bot's approval mode does (:mod:`app.permissions`):

    - ``ask``: ``acceptEdits`` (Claude Code) or the CLI's default (Codex), i.e. edits in the
      CLI's own folder but nothing wider: a bot never gets more than the user already gave
      that CLI.
    - ``auto``: the same, plus every allowed folder, so writes beside the working folder go
      through without a second card inside the CLI (Codex runs ``workspace-write``).
    - ``full``: the CLI's own bypass switches (``bypassPermissions`` / ``--dangerously-…``).

    A session id continues one CLI conversation: Claude Code takes a new id up front and
    resumes by it, Codex resumes the id its own event stream reported. Reasoning effort is
    passed only when the user picked one. ``mcp`` is this turn's Cubes MCP server arguments
    (see :mod:`app.cli_tools`); nothing is added when the turn has no tools to offer.
    ``images`` are this turn's staged image files, passed to Codex one ``--image`` flag each;
    Claude Code takes its images inside the stream-json line instead.
    """
    model = str(getattr(resolved, "model", "") or "")
    picked = [] if model in ("", CLI_DEFAULT_MODEL) else (
        ["--model", model] if kind == CLAUDE_CODE else ["-m", model]
    )
    effort = str(getattr(resolved, "effort", "") or "")
    mode = str(getattr(resolved, "permission_mode", "") or permissions.ASK)
    workdir = str(getattr(resolved, "workdir", "") or ".")
    # The working folder is already reachable (it is where the CLI runs); adding it again
    # would only repeat a flag. Every other allowed folder is what ``auto`` opens up.
    extra = [
        str(path)
        for path in getattr(resolved, "folders", ()) or ()
        if str(path) and str(path) != workdir
    ]
    if kind == CLAUDE_CODE:
        argv = [
            command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            "bypassPermissions" if mode == permissions.FULL else "acceptEdits",
            # ``bypassPermissions`` is only accepted when the launch also allows it; without
            # this companion flag Claude Code refuses the mode and falls back to its default.
            *(["--allow-dangerously-skip-permissions"] if mode == permissions.FULL else []),
            *picked,
        ]
        if effort:
            argv += ["--effort", effort]
        if mode == permissions.AUTO and extra:
            argv += ["--add-dir", *extra]
        if session is not None:
            argv += ["--resume", session.id] if session.resume else ["--session-id", session.id]
        if mcp:
            argv += mcp
        if system:
            argv += ["--append-system-prompt", system]
        return argv
    resuming = session is not None and session.resume
    argv = [command, "exec"]
    if resuming:
        argv += ["resume", session.id]
    argv += [
        "--json",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        workdir,
        *picked,
    ]
    if mode == permissions.AUTO:
        # ``codex exec`` has both flags; ``exec resume`` only accepts config overrides, and
        # ``-C``/cwd filtering is how the resumed turn finds its own folder.
        if resuming:
            argv += ["-c", 'sandbox_mode="workspace-write"']
            if extra:
                roots = json.dumps(extra, ensure_ascii=False)
                argv += ["-c", f"sandbox_workspace_write.writable_roots={roots}"]
        else:
            argv += ["--sandbox", "workspace-write"]
            for path in extra:
                argv += ["--add-dir", path]
    elif mode == permissions.FULL:
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    return [*argv, *(mcp or []), *(f"--image={path}" for path in images or []), "-"]


# ----------------------------------------------------------------------- errors


RATE_HINTS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota",
    "too many requests",
    "credit balance",
    "insufficient",
    "402",
    "429",
    "额度",
    "限流",
)
LOGIN_HINTS_TEXT = (
    "not logged in",
    "not signed in",
    "unauthorized",
    "authentication",
    "invalid api key",
    "401",
    "login",
)


def _clip(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit]}…"


def problem_sentence(kind: str, text: str, *, fallback: str) -> str:
    """One Chinese sentence that names the real reason a CLI turn failed."""
    label = CLI_LABELS.get(kind, kind)
    low = text.lower()
    if any(hint in low for hint in RATE_HINTS):
        return f"{label} 的额度用完或触发了限流，等一会儿再试，或到终端检查你的订阅。"
    if any(hint in low for hint in LOGIN_HINTS_TEXT):
        return f"{label} 还没有登录：请在终端运行 {LOGIN_HINTS.get(kind, 'login')} 完成登录。"
    detail = _clip(text.strip())
    if detail:
        return f"{label} 运行失败：{detail}"
    return f"{label} 运行失败：{fallback}"


# ------------------------------------------------------------------------ stream

# What a CLI prints when the session it was asked to resume is gone; the turn then starts a
# fresh session and gives it the whole history once.
RESUME_HINTS = (
    "no conversation found",
    "session not found",
    "could not find session",
    "no such session",
    "session does not exist",
    "unknown session",
    "invalid session",
    "resume",
)


class _ResumeFailed(UpstreamError):
    """The session to resume is gone; one fresh run with the full history follows."""


def _looks_missing_session(text: str) -> bool:
    low = text.lower()
    return any(hint in low for hint in RESUME_HINTS)


class CLIStream:
    """One turn of a local CLI, shaped like every other :class:`ModelStream`.

    ``session`` continues a CLI conversation: a fresh session is given the whole rendered
    history, a resumed one only ``prompt_messages`` (what happened since the bot's last
    turn). A resume the CLI can no longer honour falls back to a fresh session once.
    """

    def __init__(
        self,
        kind: str,
        resolved: Any,
        *,
        messages: list[dict[str, Any]],
        session: CLISession | None = None,
        prompt_messages: list[dict[str, Any]] | None = None,
        cli_tools: Any | None = None,
    ) -> None:
        self._kind = kind
        self._resolved = resolved
        self._messages = messages
        self._prompt_messages = messages if prompt_messages is None else prompt_messages
        self._session = session
        # This turn's bridge to Cubes' own tools (app.cli_tools), or None when the turn
        # offers none. It is closed when the turn ends, and never outlives it.
        self._cli_tools = cli_tools
        # The id this turn's conversation got, once the CLI has named it.
        self.session_id = session.id if session is not None else ""
        self.result = CompletionResult()
        self._error: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr: list[str] = []
        self._stderr_size = 0
        # Claude Code reports the whole reply once more at the end; partial deltas win.
        self._saw_partial = False
        # A resumed run the CLI refused because the session is gone.
        self._missing_session = False
        # Set by the engine before the turn runs: called once when the CLI first reports a
        # tool of its own. The CLI runs those tools, so ``tool_calls`` stays empty and this
        # is the only early sign that streaming text is leading into work.
        self.on_tool_start: Callable[[], None] | None = None
        self.saw_tool_call = False

    # ------------------------------------------------------------------ surface

    @property
    def tool_calls(self) -> list[Any]:
        """A CLI runs its own tools; nothing is ever handed back to the Cubes loop."""
        return []

    async def chunks(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> AsyncIterator[str]:
        stopped = should_stop if should_stop is not None else (lambda: False)
        try:
            if stopped():
                raise StreamAborted("任务已停止。")
            command = command_for(self._kind)
            if command is None:
                raise UpstreamError(
                    f"没有找到 {command_name(self._kind)} 命令。{INSTALL_HINTS.get(self._kind, '')}"
                )
            resuming = self._session is not None and self._session.resume
            try:
                async for item in self._run(command, self._prompt_messages, stopped):
                    yield item
                return
            except _ResumeFailed:
                if not resuming:
                    raise
            # The CLI lost that conversation (cleared, expired, another machine): one new
            # session, given the whole history, answers this turn instead.
            self._reset()
            self._session = CLISession(new_session_id())
            self.session_id = self._session.id
            async for item in self._run(command, self._messages, stopped):
                yield item
        finally:
            # The token dies with the turn, even when the turn failed or was stopped.
            self._close_tools()

    def validate(self, *, allow_tool_calls: bool) -> None:
        """The same verdicts every adapter gives: an honest error instead of a partial answer."""
        label = CLI_LABELS.get(self._kind, self._kind)
        if self._error:
            raise UpstreamError(self._error)
        if not self.result.saw_done:
            raise UpstreamError(f"{label} 没有正常结束，本次回复不完整。")
        if self.result.finish_reason == "length":
            raise UpstreamError("回复被长度限制截断，本次回复不完整。")
        if not self.result.content.strip():
            raise UpstreamError(f"{label} 没有返回任何文字，本次回复未完成。")

    async def aclose(self) -> None:
        await self._stop()
        self._close_tools()

    # ----------------------------------------------------------------- internals

    def _close_tools(self) -> None:
        """Drop this turn's tool token; the CLI cannot reach Cubes with it any more."""
        bridge = self._cli_tools
        if bridge is not None:
            self._cli_tools = None
            bridge.close()

    def _reset(self) -> None:
        """Forget one attempt, so the next one starts from a clean ledger."""
        self.result = CompletionResult()
        self._error = None
        self._process = None
        self._stderr = []
        self._stderr_size = 0
        self._saw_partial = False
        self._missing_session = False

    async def _run(
        self,
        command: str,
        messages: list[dict[str, Any]],
        stopped: Callable[[], bool],
    ) -> AsyncIterator[str]:
        rendered = render_messages(messages, self._kind)
        system = rendered.system
        prompt = rendered.prompt
        mcp = self._cli_tools.argv(self._kind) if self._cli_tools is not None else []
        if self._cli_tools is not None:
            # Claude Code takes it in the appended system prompt (which is sent on every
            # turn, resumed or not); Codex has no system-prompt flag, so it heads the prompt.
            note = self._cli_tools.note()
            if self._kind == CODEX:
                prompt = f"{note}\n\n{prompt}"
            else:
                system = f"{system}\n\n{note}" if system else note
        workdir = str(getattr(self._resolved, "workdir", "") or ".")
        # The staged images live exactly as long as this attempt: the CLI has read them
        # before the process exits, and nothing points at them afterwards.
        staging: Path | None = None
        try:
            paths, staging = self._stage_images(rendered)
            if rendered.files:
                self._place_files(workdir, rendered.files)
            stdin_text = prompt
            if self._kind == CLAUDE_CODE:
                stdin_text = claude_stdin(rendered)
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv_for(
                        self._kind,
                        self._resolved,
                        command,
                        system,
                        session=self._session,
                        mcp=mcp,
                        images=paths,
                    ),
                    cwd=workdir,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    limit=STREAM_LIMIT,
                )
            except OSError as exc:
                raise UpstreamError(
                    problem_sentence(
                        self._kind, str(exc), fallback=f"无法启动（{exc.__class__.__name__}）。"
                    )
                ) from exc
            self._process = process
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            pump = asyncio.create_task(self._pump(process, queue))
            errors = asyncio.create_task(self._drain(process.stderr))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + STEP_TIMEOUT_SECONDS
            try:
                stdin = process.stdin
                if stdin is None:  # pragma: no cover - stdin is always a pipe here
                    raise UpstreamError("无法把对话交给命令行工具。")
                # A CLI that already failed (not signed in, bad flag) closes its pipe first;
                # the exit code and stderr explain why, so an unwritten prompt is no error.
                with contextlib.suppress(OSError):
                    stdin.write(stdin_text.encode("utf-8"))
                    await stdin.drain()
                    stdin.close()
                while True:
                    if stopped():
                        raise StreamAborted("任务已停止。")
                    if loop.time() > deadline:
                        raise UpstreamError(
                            f"{CLI_LABELS.get(self._kind, self._kind)} 执行超过 "
                            f"{int(STEP_TIMEOUT_SECONDS / 60)} 分钟，本次回复已放弃。"
                        )
                    try:
                        item = await asyncio.wait_for(queue.get(), POLL_SECONDS)
                    except asyncio.TimeoutError:
                        continue
                    if item is None:
                        break
                    self.result.content += item
                    yield item
            finally:
                await self._stop()
                for task in (pump, errors):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(pump, errors, return_exceptions=True)
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        if self._missing_session:
            raise _ResumeFailed("This CLI session no longer exists.")

    @staticmethod
    def _stage_images(rendered: RenderedPrompt) -> tuple[list[str], Path | None]:
        """Write this attempt's images to a fresh temp dir; return their paths and that dir."""
        if not rendered.images:
            return [], None
        staging = Path(tempfile.mkdtemp(prefix="cubes-images-"))
        paths: list[str] = []
        for name, data in rendered.images:
            path = staging / name
            path.write_bytes(data)
            paths.append(str(path))
        return paths, staging

    @staticmethod
    def _place_files(workdir: str, files: list[tuple[str, bytes]]) -> None:
        """Copy a turn's PDFs into the bot's own folder so the CLI can open them.

        A path the prompt names must hold this turn's file, so different bytes under the same
        name replace what was there (names collide only across turns; within one turn the
        renderer already made them unique).
        """
        folder = Path(workdir) / ATTACHMENT_DIRNAME
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in files:
            target = folder / name
            try:
                if target.exists() and target.read_bytes() == data:
                    continue
            except OSError:
                pass
            target.write_bytes(data)

    async def _pump(
        self, process: asyncio.subprocess.Process, queue: asyncio.Queue[str | None]
    ) -> None:
        try:
            stdout = process.stdout
            assert stdout is not None  # a pipe, from create_subprocess_exec
            async for raw in stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    # Not one of ours (a banner, a warning): keep it as failure evidence.
                    self._note(line)
                    continue
                if not isinstance(event, dict):
                    continue
                for text in self._texts(event):
                    queue.put_nowait(text)
            await process.wait()
            if process.returncode and not self._error and not self.result.saw_done:
                evidence = "".join(self._stderr)
                resuming = self._session is not None and self._session.resume
                if resuming and not self.result.content.strip() and _looks_missing_session(evidence):
                    self._missing_session = True
                else:
                    self._error = problem_sentence(
                        self._kind,
                        evidence,
                        fallback=f"退出码 {process.returncode}。",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken pipe must surface as an honest error
            if not self._error:
                self._error = problem_sentence(
                    self._kind,
                    str(exc),
                    fallback=f"读取输出失败（{exc.__class__.__name__}）。",
                )
        finally:
            queue.put_nowait(None)

    async def _drain(self, stream: asyncio.subprocess.StreamReader | None) -> None:
        """Keep the last stretch of stderr; a CLI explains its failures there."""
        if stream is None:
            return
        try:
            async for raw in stream:
                self._note(raw.decode("utf-8", "replace"))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _note(self, text: str) -> None:
        """Remember output that is not a JSON event, bounded, for a failure sentence later."""
        self._stderr.append(text)
        self._stderr_size += len(text)
        while self._stderr_size > MAX_ERROR_CHARS * 4 and len(self._stderr) > 1:
            self._stderr_size -= len(self._stderr.pop(0))

    def _texts(self, event: dict[str, Any]) -> list[str]:
        return self._texts_codex(event) if self._kind == CODEX else self._texts_claude(event)

    def _note_tool_start(self) -> None:
        """Fire the engine's hook once, on the CLI's first reported tool of this turn."""
        if self.saw_tool_call:
            return
        self.saw_tool_call = True
        hook = self.on_tool_start
        if hook is not None:
            hook()

    # ------------------------------------------------------------------- claude

    def _texts_claude(self, event: dict[str, Any]) -> list[str]:
        kind = str(event.get("type") or "")
        if kind == "system":
            # ``{"type":"system","subtype":"init","session_id":"…"}``: the id to resume by.
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                self.session_id = session_id.strip()
            return []
        if kind == "stream_event":
            inner = event.get("event")
            if not isinstance(inner, dict):
                return []
            inner_kind = inner.get("type")
            if inner_kind == "content_block_start":
                # The CLI starts a tool_use block before it runs that tool: the moment its
                # text so far stopped being an answer and became narration.
                block = inner.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._note_tool_start()
                text = block.get("text") if isinstance(block, dict) else None
                return [text] if isinstance(text, str) and text else []
            if inner_kind == "message_delta":
                reason = (inner.get("delta") or {}).get("stop_reason")
                if isinstance(reason, str) and reason:
                    self.result.finish_reason = "length" if reason == "max_tokens" else reason
            delta = inner.get("delta")
            if not isinstance(delta, dict):
                return []
            if delta.get("type") == "thinking_delta":
                thought = delta.get("thinking")
                if isinstance(thought, str):
                    self.result.reasoning_chars += len(thought)
                return []
            text = delta.get("text")
            if delta.get("type") == "text_delta" and isinstance(text, str) and text:
                self._saw_partial = True
                return [text]
            return []
        if kind == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                return []
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_use" for block in content
            ):
                # Without partial events this repeated message is the first sign of a tool.
                self._note_tool_start()
            # Usage is read from the final ``result`` only: the CLI repeats the message here
            # with placeholders, and counting both would double the bill.
            if self._saw_partial:
                return []
            return _content_texts(content)
        if kind == "result":
            return self._result_claude(event)
        return []

    def _result_claude(self, event: dict[str, Any]) -> list[str]:
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.result.usage.absorb(claude_usage(usage))
        subtype = str(event.get("subtype") or "")
        message = event.get("result") or event.get("error") or ""
        text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        if event.get("is_error") or subtype.startswith("error"):
            if subtype == "error_max_turns":
                self._error = "Claude Code 达到了单次回合上限，本次回复不完整。"
            else:
                self._error = problem_sentence(
                    self._kind, text or subtype, fallback="命令行工具报告了错误。"
                )
            return []
        self.result.saw_done = True
        if event.get("stop_reason") == "max_tokens":
            self.result.finish_reason = "length"
        return []

    # -------------------------------------------------------------------- codex

    def _texts_codex(self, event: dict[str, Any]) -> list[str]:
        kind = str(event.get("type") or "")
        if kind == "thread.started":
            # ``{"type":"thread.started","thread_id":"…"}``: the id ``exec resume`` takes.
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                self.session_id = thread_id.strip()
            return []
        if kind in ("turn.failed", "error"):
            error = event.get("error") or event.get("message") or ""
            text = error.get("message") if isinstance(error, dict) else error
            resuming = self._session is not None and self._session.resume
            if resuming and not self.result.content.strip() and _looks_missing_session(
                str(text or "")
            ):
                self._missing_session = True
                return []
            self._error = problem_sentence(
                self._kind, str(text or kind), fallback="命令行工具报告了错误。"
            )
            return []
        if kind == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.result.usage.absorb(codex_usage(usage))
            self.result.saw_done = True
            return []
        if kind not in ("item.started", "item.completed"):
            return []
        item = event.get("item")
        if not isinstance(item, dict):
            return []
        if item.get("type") in CODEX_TOOL_ITEMS:
            # The CLI is about to run (or has run) a tool of its own.
            self._note_tool_start()
        if kind == "item.started":
            return []
        if item.get("type") in ("reasoning", "reasoning_summary"):
            self.result.reasoning_chars += len(str(item.get("text") or ""))
            return []
        if item.get("type") != "agent_message":
            return []
        text = item.get("text")
        return [text] if isinstance(text, str) and text else []

    # ------------------------------------------------------------------- process

    async def _stop(self) -> None:
        """Kill the whole process group: the CLI may have started tools of its own."""
        process = self._process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()


def _content_texts(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return texts


def claude_usage(reported: dict[str, Any]) -> Usage:
    """One Claude Code ``usage`` object; the prompt count includes what the cache served."""
    return Usage(
        input_tokens=_count(reported.get("input_tokens"))
        + _count(reported.get("cache_read_input_tokens"))
        + _count(reported.get("cache_creation_input_tokens")),
        output_tokens=_count(reported.get("output_tokens")),
        cached_tokens=_count(reported.get("cache_read_input_tokens")),
    )


def codex_usage(reported: dict[str, Any]) -> Usage:
    """One Codex ``usage`` object: ``input_tokens`` already counts the cached part."""
    return Usage(
        input_tokens=_count(reported.get("input_tokens")),
        output_tokens=_count(reported.get("output_tokens")),
        cached_tokens=_count(reported.get("cached_input_tokens")),
    )


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
