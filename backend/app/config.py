"""Runtime configuration for the Cubes supervisor backend.

Configuration is resolved from ``backend/.env`` regardless of the current working
directory; real environment variables always win over dotenv values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "http://model.mify.ai.srv/v1"
DEFAULT_DATABASE_PATH = "data/cubes.sqlite3"
DEFAULT_REQUEST_TIMEOUT = 120.0
DEFAULT_STOP_TIMEOUT = 15.0
# How long a tool may wait for the user to confirm a destructive action before it fails.
DEFAULT_CONFIRM_TIMEOUT = 600.0
# Step budgets are a workspace setting (``app.step_budget``) and unlimited by default. These
# values are the standard preset's numbers, still overridable from the environment, so an
# operator who wants the old fixed ceiling can have exactly it.
DEFAULT_MAX_TURN_MODEL_CALLS = 24
DEFAULT_MAX_TURN_TOOL_CALLS = 32
DEFAULT_MAX_MODEL_CALLS = 64
DEFAULT_MAX_TOOL_CALLS = 128
# A workflow walks up to 30 steps inside one run, each its own bounded turn. The run's cap is
# wider than a chat's for that reason, not because a single turn may do more.
DEFAULT_MAX_WORKFLOW_MODEL_CALLS = 160
DEFAULT_MAX_WORKFLOW_TOOL_CALLS = 256
# Computer use takes many small steps; they have their own budget, apart from other tools, and
# each sandbox tool has its own share: a shell command is cheap, a pixel step is not.
DEFAULT_MAX_COMPUTER_STEPS = 40
DEFAULT_MAX_COMPUTER_SHELL_STEPS = 30
DEFAULT_MAX_COMPUTER_BROWSER_STEPS = 25
DEFAULT_MAX_COMPUTER_SCREEN_STEPS = 8
DEFAULT_MAX_NEW_AGENTS = 8
DEFAULT_MAX_NEW_GROUPS = 4
# Context budget, in tokens, for a model whose provider does not state its own limits.
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_CONTEXT_OUTPUT = 8_192
# Scheduled tasks: the wall-clock timezone rules are written in, and how often the loop
# looks for due rules (a scheduled fire never waits longer than one tick).
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_SCHEDULE_TICK_SECONDS = 30.0
# Memory extraction waits for a conversation to go quiet this long, then reads every turn
# since the last extraction in one call; this many waiting turns trigger it at once.
DEFAULT_MEMORY_IDLE_SECONDS = 120.0
DEFAULT_MEMORY_BATCH_TURNS = 5
# A bot waits this long after the user's last reaction to one of its messages before it
# takes one small look and decides to ignore, react back or say one line.
DEFAULT_REACTION_DEBOUNCE_SECONDS = 2.0
AVATAR_CACHE_DIRNAME = "avatars"
# Search chain: the first provider that answers with usable sources wins.
SEARCH_PROVIDER_NAMES = ("parallel", "exa", "tavily", "brave", "duckduckgo")
DEFAULT_SEARCH_PROVIDERS = SEARCH_PROVIDER_NAMES
DEFAULT_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
DEFAULT_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
DEFAULT_PARALLEL_API_URL = "https://api.parallel.ai/v1beta/search"
DEFAULT_EXA_API_URL = "https://api.exa.ai/search"
DEFAULT_TAVILY_API_URL = "https://api.tavily.com/search"
DEFAULT_BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:1420",
    "http://localhost:1420",
    "app://cubes",
)


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _origins_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Comma-separated exact origins; a wildcard is refused so credentials never go anywhere."""
    raw = _env(name)
    if raw is None:
        return default
    origins = tuple(part.strip().rstrip("/") for part in raw.split(",") if part.strip())
    return tuple(origin for origin in origins if origin != "*") or default


def _workspace_env(name: str) -> Path | None:
    """The working folder override, or ``None`` for the default beside the database.

    The folder is created on first use (:func:`app.permissions.working_folder`), so a path
    that does not exist yet is still accepted: the user may point it at a new folder.
    """
    raw = _env(name)
    if raw is None:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BACKEND_DIR / path
    return path


def _providers_env(name: str) -> tuple[str, ...]:
    """Ordered, de-duplicated search chain; unknown names are dropped."""
    raw = _env(name)
    if raw is None:
        return DEFAULT_SEARCH_PROVIDERS
    picked: list[str] = []
    for part in raw.split(","):
        provider = part.strip().lower()
        if provider in SEARCH_PROVIDER_NAMES and provider not in picked:
            picked.append(provider)
    return tuple(picked) or DEFAULT_SEARCH_PROVIDERS


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings; injectable for tests via ``create_app(settings)``."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = ""
    # Shared secret the desktop shell passes to the embedded backend; empty = no auth.
    auth_token: str = ""
    database_path: Path = BACKEND_DIR / DEFAULT_DATABASE_PATH
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    stop_timeout: float = DEFAULT_STOP_TIMEOUT
    confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT
    # Context window and max output used when a provider does not declare per-model limits.
    context_window_default: int = DEFAULT_CONTEXT_WINDOW
    context_output_default: int = DEFAULT_CONTEXT_OUTPUT
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_workflow_model_calls: int = DEFAULT_MAX_WORKFLOW_MODEL_CALLS
    max_workflow_tool_calls: int = DEFAULT_MAX_WORKFLOW_TOOL_CALLS
    max_turn_model_calls: int = DEFAULT_MAX_TURN_MODEL_CALLS
    max_turn_tool_calls: int = DEFAULT_MAX_TURN_TOOL_CALLS
    max_computer_steps: int = DEFAULT_MAX_COMPUTER_STEPS
    max_computer_shell_steps: int = DEFAULT_MAX_COMPUTER_SHELL_STEPS
    max_computer_browser_steps: int = DEFAULT_MAX_COMPUTER_BROWSER_STEPS
    max_computer_screen_steps: int = DEFAULT_MAX_COMPUTER_SCREEN_STEPS
    max_new_agents: int = DEFAULT_MAX_NEW_AGENTS
    max_new_groups: int = DEFAULT_MAX_NEW_GROUPS
    # IANA timezone of scheduled rules; validated so a typo fails at startup, not at 9:00.
    timezone: str = DEFAULT_TIMEZONE
    schedule_tick_seconds: float = DEFAULT_SCHEDULE_TICK_SECONDS
    memory_idle_seconds: float = DEFAULT_MEMORY_IDLE_SECONDS
    memory_batch_turns: int = DEFAULT_MEMORY_BATCH_TURNS
    reaction_debounce_seconds: float = DEFAULT_REACTION_DEBOUNCE_SECONDS
    # Browser origins allowed to call the API: the Vite dev server and the desktop window.
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    # The working folder relative paths land in; ``None`` means ``workspace/`` beside the
    # database. What the bots may do without asking is each bot's approval mode, not a switch
    # here (see :mod:`app.permissions`).
    workspace_dir: Path | None = None
    # Search chain keys (empty = that provider is skipped) and the ordered chain itself.
    parallel_api_key: str = ""
    exa_api_key: str = ""
    tavily_api_key: str = ""
    brave_api_key: str = ""
    search_providers: tuple[str, ...] = DEFAULT_SEARCH_PROVIDERS
    # Provider endpoints; overridable so tests never leave the machine.
    parallel_mcp_url: str = DEFAULT_PARALLEL_MCP_URL
    parallel_api_url: str = DEFAULT_PARALLEL_API_URL
    exa_mcp_url: str = DEFAULT_EXA_MCP_URL
    exa_api_url: str = DEFAULT_EXA_API_URL
    tavily_api_url: str = DEFAULT_TAVILY_API_URL
    brave_api_url: str = DEFAULT_BRAVE_API_URL
    duckduckgo_url: str = DEFAULT_DUCKDUCKGO_URL

    def __post_init__(self) -> None:
        """Reject a bad TIMEZONE the moment settings are built (i.e. at startup)."""
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"TIMEZONE「{self.timezone}」不是合法的时区名称，请使用 Asia/Shanghai 这样的写法。"
            ) from exc

    # ------------------------------------------------------------------ queries

    @property
    def avatar_cache_dir(self) -> Path:
        """Cache directory for rasterized Blobatar PNGs, next to the database."""
        return self.database_path.parent / AVATAR_CACHE_DIRNAME


def load_settings(dotenv_path: Path | None = None) -> Settings:
    """Build settings from ``backend/.env`` (if present) overridden by real env vars."""
    path = dotenv_path if dotenv_path is not None else BACKEND_DIR / ".env"
    if path.is_file():
        load_dotenv(path, override=False)

    raw_database = _env("DATABASE_PATH") or DEFAULT_DATABASE_PATH
    database_path = Path(raw_database).expanduser()
    if not database_path.is_absolute():
        database_path = BACKEND_DIR / database_path

    return Settings(
        base_url=_env("MODEL_BASE_URL") or DEFAULT_BASE_URL,
        api_key=_env("MODEL_API_KEY") or "",
        model=_env("MODEL_NAME") or "",
        auth_token=_env("CUBES_AUTH_TOKEN") or "",
        database_path=database_path,
        request_timeout=_float_env("REQUEST_TIMEOUT", default=DEFAULT_REQUEST_TIMEOUT),
        stop_timeout=_float_env("STOP_TIMEOUT", default=DEFAULT_STOP_TIMEOUT),
        confirm_timeout_seconds=_float_env(
            "CONFIRM_TIMEOUT_SECONDS", default=DEFAULT_CONFIRM_TIMEOUT
        ),
        context_window_default=_int_env(
            "CONTEXT_WINDOW_DEFAULT", default=DEFAULT_CONTEXT_WINDOW
        ),
        context_output_default=_int_env(
            "CONTEXT_OUTPUT_DEFAULT", default=DEFAULT_CONTEXT_OUTPUT
        ),
        max_model_calls=_int_env("MAX_MODEL_CALLS_PER_RUN", default=DEFAULT_MAX_MODEL_CALLS),
        max_tool_calls=_int_env("MAX_TOOL_CALLS_PER_RUN", default=DEFAULT_MAX_TOOL_CALLS),
        max_workflow_model_calls=_int_env(
            "MAX_WORKFLOW_MODEL_CALLS_PER_RUN", default=DEFAULT_MAX_WORKFLOW_MODEL_CALLS
        ),
        max_workflow_tool_calls=_int_env(
            "MAX_WORKFLOW_TOOL_CALLS_PER_RUN", default=DEFAULT_MAX_WORKFLOW_TOOL_CALLS
        ),
        max_turn_model_calls=_int_env(
            "MAX_MODEL_CALLS_PER_TURN", default=DEFAULT_MAX_TURN_MODEL_CALLS
        ),
        max_turn_tool_calls=_int_env(
            "MAX_TOOL_CALLS_PER_TURN", default=DEFAULT_MAX_TURN_TOOL_CALLS
        ),
        max_computer_steps=_int_env(
            "MAX_COMPUTER_STEPS_PER_RUN", default=DEFAULT_MAX_COMPUTER_STEPS
        ),
        max_computer_shell_steps=_int_env(
            "MAX_COMPUTER_SHELL_STEPS_PER_RUN", default=DEFAULT_MAX_COMPUTER_SHELL_STEPS
        ),
        max_computer_browser_steps=_int_env(
            "MAX_COMPUTER_BROWSER_STEPS_PER_RUN", default=DEFAULT_MAX_COMPUTER_BROWSER_STEPS
        ),
        max_computer_screen_steps=_int_env(
            "MAX_COMPUTER_SCREEN_STEPS_PER_RUN", default=DEFAULT_MAX_COMPUTER_SCREEN_STEPS
        ),
        max_new_agents=_int_env("MAX_NEW_AGENTS_PER_RUN", default=DEFAULT_MAX_NEW_AGENTS),
        max_new_groups=_int_env("MAX_NEW_GROUPS_PER_RUN", default=DEFAULT_MAX_NEW_GROUPS),
        timezone=_env("TIMEZONE") or DEFAULT_TIMEZONE,
        schedule_tick_seconds=_float_env(
            "SCHEDULE_TICK_SECONDS", default=DEFAULT_SCHEDULE_TICK_SECONDS
        ),
        memory_idle_seconds=_float_env("MEMORY_IDLE_SECONDS", default=DEFAULT_MEMORY_IDLE_SECONDS),
        memory_batch_turns=_int_env("MEMORY_BATCH_TURNS", default=DEFAULT_MEMORY_BATCH_TURNS),
        reaction_debounce_seconds=_float_env(
            "REACTION_DEBOUNCE_SECONDS", default=DEFAULT_REACTION_DEBOUNCE_SECONDS
        ),
        cors_origins=_origins_env("CORS_ORIGINS", default=DEFAULT_CORS_ORIGINS),
        workspace_dir=_workspace_env("WORKSPACE_DIR"),
        parallel_api_key=_env("PARALLEL_API_KEY") or "",
        exa_api_key=_env("EXA_API_KEY") or "",
        tavily_api_key=_env("TAVILY_API_KEY") or "",
        brave_api_key=_env("BRAVE_API_KEY") or "",
        search_providers=_providers_env("SEARCH_PROVIDERS"),
        parallel_mcp_url=_env("PARALLEL_MCP_URL") or DEFAULT_PARALLEL_MCP_URL,
        parallel_api_url=_env("PARALLEL_API_URL") or DEFAULT_PARALLEL_API_URL,
        exa_mcp_url=_env("EXA_MCP_URL") or DEFAULT_EXA_MCP_URL,
        exa_api_url=_env("EXA_API_URL") or DEFAULT_EXA_API_URL,
        tavily_api_url=_env("TAVILY_API_URL") or DEFAULT_TAVILY_API_URL,
        brave_api_url=_env("BRAVE_API_URL") or DEFAULT_BRAVE_API_URL,
        duckduckgo_url=_env("DUCKDUCKGO_URL") or DEFAULT_DUCKDUCKGO_URL,
    )
