"""Model providers: field validation, per-agent resolution and live probes.

A provider is one upstream service (an OpenAI-compatible Chat Completions endpoint or an
Anthropic Messages endpoint) plus the models the user picked from it. The engine resolves
one :class:`ResolvedModel` per model call, so a settings change never disturbs a stream
that is already running, and a bot whose picked model disappeared falls back to the
workspace default instead of failing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import model_catalog, permissions
from .config import DEFAULT_CONTEXT_OUTPUT, DEFAULT_CONTEXT_WINDOW
from .errors import UpstreamError

# The HTTP protocols speak to a model service; the CLI ones run a local, already signed-in
# command that brings its own tools (see :mod:`app.cli_engines`).
PROTOCOLS = ("openai-chat", "anthropic-messages", "claude-code", "codex")
CLI_PROTOCOLS = ("claude-code", "codex")
ANTHROPIC_VERSION = "2023-06-01"
# Anthropic requires max_tokens; the resolved model's own limit decides it.
ANTHROPIC_MAX_TOKENS = 8192
# The connection probe only needs to see whether the model emits a tool call.
PROBE_MAX_TOKENS = 32
MAX_NAME_CHARS = 64
MAX_MODEL_CHARS = 200
MAX_MODELS = 200
# A sanity bound on a declared context window or output limit (10M tokens).
MAX_WINDOW_TOKENS = model_catalog.MAX_TOKENS
MAX_PROBE_DETAIL = 300

NO_MODEL = "还没有配置模型服务：请在「设置 → 模型服务」里添加一个。"
PROBE_TOOL_NAME = "ping"
PROBE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"echo": {"type": "string", "description": "要原样回显的文本"}},
    "required": [],
}

# The provider the first start imports from ``backend/.env``.
DEFAULT_ENV_PROVIDER_NAME = "mify"
DEFAULT_ENV_PROTOCOL = "openai-chat"


class ProviderInvalid(RuntimeError):
    """A field the user must fix; the message is shown to the client verbatim."""


class ProviderNameTaken(RuntimeError):
    """A provider name collides with another provider in the workspace."""


class ProviderProbeError(RuntimeError):
    """A probe or model listing failed; reported as 502 with a secret-free sentence."""


@dataclass(frozen=True)
class ModelCaps:
    """What one model can do, resolved field by field, and where each answer came from.

    ``sources`` names the layer each field resolved from: ``user`` (a per-model override),
    ``provider`` (the service's own ``/models`` answer), ``catalog`` (models.dev) or
    ``default`` (the configured fallback).
    """

    images: bool = False
    video: bool = False
    audio: bool = False
    pdf: bool = False
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_output: int = DEFAULT_CONTEXT_OUTPUT
    reasoning: bool = False
    reasoning_levels: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)


# The fields that resolve independently, in the order sources reports them.
CAP_FIELDS = (
    "images",
    "video",
    "audio",
    "pdf",
    "context_window",
    "max_output",
    "reasoning",
)


@dataclass(frozen=True)
class ResolvedModel:
    """Everything one model call needs, resolved from the speaking bot."""

    protocol: str
    base_url: str
    api_key: str
    model: str
    provider_id: str
    provider_name: str
    # Whether this model can see images, after the user / provider / catalog resolution.
    vision: bool = False
    # Whether it can be handed a PDF document (a CLI engine reads it from its own folder).
    pdf: bool = False
    # The context budget this model's requests are built against, in tokens.
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_output: int = DEFAULT_CONTEXT_OUTPUT
    # The folder a CLI engine runs in; empty for the HTTP protocols.
    workdir: str = ""
    # The reasoning effort a CLI engine runs with, when the bot picked one.
    effort: str = ""
    # How wide a CLI engine's own permissions are: its bot's mode, else the workspace's.
    permission_mode: str = "ask"
    # Every folder that mode allows, so ``auto`` can widen the CLI's own sandbox to them.
    folders: tuple[str, ...] = ()


# ------------------------------------------------------------------ validation


def clean_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ProviderInvalid("名称不能为空。")
    if len(name) > MAX_NAME_CHARS:
        raise ProviderInvalid(f"名称不能超过 {MAX_NAME_CHARS} 个字符。")
    return name


def clean_protocol(value: str) -> str:
    protocol = str(value or "").strip()
    if protocol not in PROTOCOLS:
        raise ProviderInvalid(
            "协议无效，只能是 openai-chat、anthropic-messages、claude-code 或 codex。"
        )
    return protocol


def clean_base_url(value: str, protocol: str = "openai-chat") -> str:
    """The service address; the CLI protocols have none, so an empty value is right there."""
    raw = str(value or "").strip().rstrip("/")
    if protocol in CLI_PROTOCOLS:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProviderInvalid("服务地址必须以 http:// 或 https:// 开头。")
    return raw


def clean_api_key(value: str | None) -> str:
    return str(value or "").strip()


def clean_models(values: list[str]) -> list[str]:
    models: list[str] = []
    for raw in values:
        name = str(raw or "").strip()
        if not name:
            continue
        if len(name) > MAX_MODEL_CHARS:
            raise ProviderInvalid(f"模型名称不能超过 {MAX_MODEL_CHARS} 个字符。")
        if name not in models:
            models.append(name)
    if not models:
        raise ProviderInvalid("至少要填写一个模型名称。")
    if len(models) > MAX_MODELS:
        raise ProviderInvalid(f"模型数量不能超过 {MAX_MODELS} 个。")
    return models


def clean_model_name(value: str | None) -> str:
    model = str(value or "").strip()
    if not model:
        raise ProviderInvalid("请先填写要使用的模型名称。")
    return model


def clean_model_ref(ref: dict | None) -> dict | None:
    """A picked model reference, or ``None`` for "follow the default"."""
    if ref is None:
        return None
    cleaned = {
        "provider_id": str(ref.get("provider_id") or "").strip(),
        "model": str(ref.get("model") or "").strip(),
    }
    # Only a CLI engine takes a reasoning effort; an HTTP provider has none to name.
    effort = str(ref.get("effort") or "").strip()
    if effort:
        cleaned["effort"] = effort
    return cleaned


def clean_model_overrides(value: Any) -> dict[str, dict[str, Any]]:
    """``{model|"*": {images, context, output}}``: the user's own answers, all optional.

    ``images`` is ``true``/``false`` for a manual choice; ``context`` and ``output`` are
    positive token counts. A field left out (or ``null``) means "detect it automatically",
    and an entry with nothing left is dropped. Keys the provider does not list are kept: an
    override becomes live again if that model is added back and costs nothing meanwhile.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProviderInvalid("模型覆盖必须是 {模型: {images, context, output}} 这样的对象。")
    cleaned: dict[str, dict[str, Any]] = {}
    for raw_model, entry in value.items():
        model = str(raw_model or "").strip()
        if not model or not isinstance(entry, dict):
            continue
        kept: dict[str, Any] = {}
        if isinstance(entry.get("images"), bool):
            kept["images"] = entry["images"]
        for key in ("context", "output"):
            amount = entry.get(key)
            if amount is None:
                continue
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise ProviderInvalid(f"「{model}」的 {key} 必须是正整数。")
            if amount > MAX_WINDOW_TOKENS:
                raise ProviderInvalid(f"「{model}」的 {key} 不能超过 {MAX_WINDOW_TOKENS}。")
            kept[key] = amount
        if kept:
            cleaned[model] = kept
    return cleaned


def clean_provider_models(values: list[str], protocol: str) -> list[str]:
    """The model names a provider lists; a CLI may leave its list empty and use its default."""
    if protocol in CLI_PROTOCOLS:
        from . import cli_engines

        listed = [str(value or "").strip() for value in values]
        listed = [value for value in listed if value]
        return clean_models(listed or [cli_engines.CLI_DEFAULT_MODEL])
    return clean_models(values)


def clean_create_fields(
    *,
    name: str,
    protocol: str,
    base_url: str,
    api_key: str | None,
    models: list[str],
) -> dict[str, Any]:
    """Every field of a new provider, cleaned in the order the form presents them."""
    cleaned_protocol = clean_protocol(protocol)
    if cleaned_protocol in CLI_PROTOCOLS:
        # A CLI provider has no address and no key: the subscription is the credential.
        return {
            "name": clean_name(name),
            "protocol": cleaned_protocol,
            "base_url": "",
            "api_key": "",
            "models": clean_provider_models(models, cleaned_protocol),
        }
    return {
        "name": clean_name(name),
        "protocol": cleaned_protocol,
        "base_url": clean_base_url(base_url, cleaned_protocol),
        "api_key": clean_api_key(api_key),
        "models": clean_models(models),
    }


def api_key_hint(key: str) -> str | None:
    """A short, non-reversible reminder of a stored key: never the key itself."""
    text = str(key or "").strip()
    if not text:
        return None
    if len(text) < 8:
        return "已设置"
    return f"{text[:3]}…{text[-4:]}"


# ------------------------------------------------------------------ resolution


def _setting_tokens(settings: Any | None, name: str, fallback: int) -> int:
    amount = getattr(settings, name, 0) if settings is not None else 0
    return amount if isinstance(amount, int) and amount > 0 else fallback


def _catalog_caps(entry: dict | None) -> dict[str, Any]:
    """A models.dev entry in the same capability shape the other layers use."""
    caps: dict[str, Any] = {}
    if not isinstance(entry, dict):
        return caps
    inputs = entry.get("input")
    if isinstance(inputs, list):
        names = {str(item).lower() for item in inputs}
        caps["images"] = "image" in names
        caps["video"] = "video" in names
        caps["audio"] = "audio" in names
        caps["pdf"] = "pdf" in names or "file" in names
    context = entry.get("context")
    if isinstance(context, int) and not isinstance(context, bool) and context > 0:
        caps["context_window"] = context
    output = entry.get("output")
    if isinstance(output, int) and not isinstance(output, bool) and output > 0:
        caps["max_output"] = output
    if isinstance(entry.get("reasoning"), bool):
        caps["reasoning"] = entry["reasoning"]
    return caps


def _override_caps(entry: Any) -> dict[str, Any]:
    """A stored user override in capability keys (``context``/``output`` become cap names)."""
    caps: dict[str, Any] = {}
    if not isinstance(entry, dict):
        return caps
    if isinstance(entry.get("images"), bool):
        caps["images"] = entry["images"]
    for key, cap in (("context", "context_window"), ("output", "max_output")):
        amount = entry.get(key)
        if isinstance(amount, int) and not isinstance(amount, bool) and 0 < amount <= MAX_WINDOW_TOKENS:
            caps[cap] = amount
    return caps


def model_caps(provider: dict, model: str, settings: Any | None = None) -> ModelCaps:
    """One model's capabilities, in magpie's order, each field from the first layer that knows.

    The user's own value for this model, then the user's ``*`` value for the provider, then
    the provider's own ``/models`` answer, then models.dev, then the configured default.
    """
    overrides = provider.get("model_overrides") or {}
    layers: list[tuple[str, dict[str, Any]]] = [
        ("user", _override_caps(overrides.get(model))),
        ("user", _override_caps(overrides.get("*"))),
    ]
    protocol = str(provider.get("protocol") or "")
    if protocol in CLI_PROTOCOLS:
        from . import cli_engines

        # A local CLI is not looked up in models.dev: what it accepts is the CLI's own
        # protocol, and for Codex its own model catalog.
        layers.append(("provider", cli_engines.caps(protocol, model)))
    provider_caps = (provider.get("model_caps") or {}).get(model)
    if isinstance(provider_caps, dict):
        layers.append(("provider", provider_caps))
    layers.append(("catalog", _catalog_caps(model_catalog.load(settings).lookup(model))))

    values: dict[str, Any] = {
        "context_window": _setting_tokens(settings, "context_window_default", DEFAULT_CONTEXT_WINDOW),
        "max_output": _setting_tokens(settings, "context_output_default", DEFAULT_CONTEXT_OUTPUT),
    }
    sources: dict[str, str] = {}
    for source, layer in layers:
        for key, value in layer.items():
            if key in CAP_FIELDS and key not in sources:
                values[key] = value
                sources[key] = source
    for key in CAP_FIELDS:
        values.setdefault(key, False)
        sources.setdefault(key, "default")

    levels: tuple[str, ...] = ()
    if isinstance(provider_caps, dict) and isinstance(provider_caps.get("reasoning_levels"), list):
        levels = tuple(str(level) for level in provider_caps["reasoning_levels"])
    return ModelCaps(
        images=bool(values["images"]),
        video=bool(values["video"]),
        audio=bool(values["audio"]),
        pdf=bool(values["pdf"]),
        context_window=int(values["context_window"]),
        max_output=int(values["max_output"]),
        reasoning=bool(values["reasoning"]),
        reasoning_levels=levels,
        sources=sources,
    )


def provider_caps(provider: dict, settings: Any | None = None) -> dict[str, dict[str, Any]]:
    """Every listed model's resolved capabilities, for the settings sheet (wire shape)."""
    shaped: dict[str, dict[str, Any]] = {}
    for model in provider.get("models") or []:
        caps = model_caps(provider, str(model), settings)
        shaped[str(model)] = {
            "images": caps.images,
            "video": caps.video,
            "audio": caps.audio,
            "pdf": caps.pdf,
            "context_window": caps.context_window,
            "max_output": caps.max_output,
            "reasoning": caps.reasoning,
            "reasoning_levels": list(caps.reasoning_levels),
            "sources": dict(caps.sources),
        }
    return shaped


def _resolved(
    provider: dict,
    model: str,
    settings: Any | None = None,
    agent_id: str = "",
    effort: str = "",
) -> ResolvedModel:
    caps = model_caps(provider, model, settings)
    protocol = str(provider["protocol"])
    workdir = ""
    if protocol in CLI_PROTOCOLS and settings is not None:
        from . import cli_engines

        workdir = cli_engines.working_dir(settings, agent_id)
    return ResolvedModel(
        protocol=protocol,
        base_url=str(provider["base_url"]),
        api_key=str(provider.get("api_key") or ""),
        model=model,
        provider_id=str(provider["id"]),
        provider_name=str(provider["name"]),
        vision=caps.images,
        pdf=caps.pdf,
        context_window=caps.context_window,
        max_output=caps.max_output,
        workdir=workdir,
        effort=effort if protocol in CLI_PROTOCOLS else "",
    )


def _ref_resolved(
    store: Any, ref: Any, settings: Any | None = None, agent_id: str = ""
) -> ResolvedModel | None:
    if not isinstance(ref, dict):
        return None
    provider = store.get_provider(str(ref.get("provider_id") or ""))
    model = str(ref.get("model") or "")
    if provider is None or not model:
        return None
    if model not in provider["models"]:
        return None
    return _resolved(
        provider, model, settings, agent_id, str(ref.get("effort") or "").strip()
    )


def resolve_dispatch(store: Any, settings: Any | None = None) -> ResolvedModel | None:
    """The model a group's dispatcher calls: the team's pick, else the fastest configured one.

    ``dispatch_model`` (``{provider_id, model}``) is the team setting. Automatic means the
    backup model when the team has one — it exists to be fast and cheap — and otherwise the
    workspace default. A pick that no longer resolves falls through the same way, so deleting
    a provider never leaves groups unroutable.
    """
    ref = store.team_settings().get("dispatch_model")
    if isinstance(ref, dict) and ref.get("provider_id") and ref.get("model"):
        picked = _ref_resolved(store, ref, settings)
        if picked is not None:
            return picked
    fallback = resolve_fallback(store, settings)
    if fallback is not None:
        return fallback
    return resolve_default(store, settings)


def resolve_default(
    store: Any, settings: Any | None = None, agent_id: str = ""
) -> ResolvedModel | None:
    """The workspace default model, or ``None`` when it is not configured.

    ``agent_id`` only names the working folder when that default is a CLI engine.
    """
    default = store.default_model()
    if default is None:
        return None
    provider = store.get_provider(default["provider_id"])
    if provider is None or default["model"] not in provider["models"]:
        return None
    return _resolved(provider, default["model"], settings, agent_id)


def resolve_fallback(
    store: Any,
    settings: Any | None = None,
    agent_id: str = "",
    *,
    agent: dict | None = None,
) -> ResolvedModel | None:
    """The workspace backup model, or ``None`` when it is off or no longer resolves.

    ``agent`` (not just its id) lets a CLI engine on the backup follow that bot's approval
    mode, exactly as it would on the primary model.
    """
    fallback = store.fallback_model()
    if fallback is None:
        return None
    resolved = _ref_resolved(store, fallback, settings, agent_id)
    return None if resolved is None else _permissioned(resolved, store, agent, settings)


def _permissioned(
    resolved: ResolvedModel, store: Any, agent: dict | None, settings: Any | None
) -> ResolvedModel:
    """Attach the bot's mode and allowed folders for the CLI engines.

    The mode itself was already applied inside Cubes (the tools' cards); a CLI brings its own
    tools, so it needs the same answer on its command line (see ``cli_engines.argv_for``).
    """
    if settings is None:
        return resolved
    return replace(
        resolved,
        permission_mode=permissions.mode_of(store, agent),
        folders=tuple(str(path) for path in permissions.allowed_folders(store, settings)),
    )


def resolve_model(store: Any, agent: dict | None, settings: Any | None = None) -> ResolvedModel:
    """The model that answers for ``agent``: its own pick, else the workspace default."""
    agent_id = str((agent or {}).get("id") or "")
    picked = _ref_resolved(store, (agent or {}).get("model_ref"), settings, agent_id)
    if picked is None:
        picked = resolve_default(store, settings, agent_id)
    if picked is None:
        raise UpstreamError(NO_MODEL)
    return _permissioned(picked, store, agent, settings)


def model_status(store: Any, agent: dict | None) -> str:
    """``custom`` when the pick still resolves, ``fallback`` when it no longer does."""
    ref = (agent or {}).get("model_ref")
    if not isinstance(ref, dict):
        return "default"
    return "custom" if _ref_resolved(store, ref) is not None else "fallback"


def validate_model_ref(store: Any, ref: dict | None) -> None:
    """Reject a model pick that does not point at a listed provider model (422)."""
    if ref is None:
        return
    provider = store.get_provider(str(ref.get("provider_id") or ""))
    if provider is None:
        raise ProviderInvalid("选择的模型服务不存在，可能刚被删除。")
    model = str(ref.get("model") or "")
    if model not in provider["models"]:
        raise ProviderInvalid("选择的模型不在这个模型服务的列表里。")
    effort = str(ref.get("effort") or "")
    if not effort:
        return
    protocol = str(provider["protocol"])
    if protocol not in CLI_PROTOCOLS:
        raise ProviderInvalid("只有 Claude Code / Codex 这类本机命令行引擎可以设置思考强度。")
    from . import cli_engines

    if not cli_engines.effort_allowed(protocol, model, effort):
        levels = "、".join(cli_engines.effort_levels(protocol, model))
        raise ProviderInvalid(f"「{model}」不支持思考强度 {effort}，可选 {levels}。")


# ------------------------------------------------------------------ probes


def auth_headers(protocol: str, api_key: str) -> dict[str, str]:
    """Headers one request needs; an empty key adds nothing (local services have none)."""
    if protocol == "anthropic-messages":
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
        return headers
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _redact(text: str, api_key: str) -> str:
    key = api_key.strip()
    return text.replace(key, "***") if key else text


def _sentence(text: str, api_key: str, limit: int = MAX_PROBE_DETAIL) -> str:
    """A clipped, redacted, secret-free sentence that ends with a full stop."""
    redacted = _redact(text.strip(), api_key)
    if len(redacted) > limit:
        redacted = f"{redacted[:limit]}…"
    return redacted if redacted.endswith("。") else f"{redacted}。"


def _messages_url(protocol: str, base_url: str) -> str:
    if protocol == "anthropic-messages":
        return f"{base_url}/v1/messages"
    return f"{base_url}/chat/completions"


def _models_url(protocol: str, base_url: str) -> str:
    if protocol == "anthropic-messages":
        return f"{base_url}/v1/models"
    return f"{base_url}/models"


def _probe_payload(protocol: str, model: str) -> dict[str, Any]:
    tool_description = "回显一段文本，只用来确认工具调用可用"
    if protocol == "anthropic-messages":
        return {
            "model": model,
            "max_tokens": PROBE_MAX_TOKENS,
            "messages": [{"role": "user", "content": "请调用 ping 工具，参数为空。"}],
            "tools": [
                {
                    "name": PROBE_TOOL_NAME,
                    "description": tool_description,
                    "input_schema": PROBE_TOOL_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": PROBE_TOOL_NAME},
        }
    return {
        "model": model,
        "max_tokens": PROBE_MAX_TOKENS,
        "messages": [{"role": "user", "content": "请调用 ping 工具，参数为空。"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": PROBE_TOOL_NAME,
                    "description": tool_description,
                    "parameters": PROBE_TOOL_SCHEMA,
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": PROBE_TOOL_NAME}},
    }


def _supports_tools(protocol: str, body: Any) -> bool:
    """True when the non-streamed answer really contains a tool call."""
    if not isinstance(body, dict):
        return False
    if protocol == "anthropic-messages":
        blocks = body.get("content")
        return isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in blocks
        )
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and message.get("tool_calls"):
            return True
    return False


async def _post_probe(
    client: httpx.AsyncClient,
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
) -> bool:
    headers = auth_headers(protocol, api_key)
    headers["Accept"] = "application/json"
    try:
        response = await client.post(
            _messages_url(protocol, base_url),
            json=_probe_payload(protocol, model),
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise ProviderProbeError(
            _sentence(f"无法连接模型服务：{exc.__class__.__name__}", api_key)
        ) from exc
    if response.status_code >= 400:
        body = _redact(response.text.strip(), api_key)
        raise ProviderProbeError(
            _sentence(
                f"模型服务返回 HTTP {response.status_code}：{body or '无响应正文'}", api_key
            )
        )
    try:
        parsed = response.json()
    except ValueError as exc:
        raise ProviderProbeError(
            _sentence("模型服务返回的不是 JSON，请检查服务地址。", api_key)
        ) from exc
    return _supports_tools(protocol, parsed)


async def test_provider(
    client: httpx.AsyncClient,
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    supervisor_name: str = "负责人",
) -> dict[str, Any]:
    """One minimal request that also reports whether the model can call tools."""
    if protocol in CLI_PROTOCOLS:
        # A CLI brings its own tools; its availability is the whole answer.
        from . import cli_engines

        return await cli_engines.probe(protocol)
    started = time.monotonic()
    try:
        supports = await _post_probe(
            client,
            protocol=protocol,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
    except ProviderProbeError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "supports_tools": False,
            "detail": str(exc),
        }
    latency = int((time.monotonic() - started) * 1000)
    detail = (
        "连接成功，模型支持工具调用。"
        if supports
        else f"连接成功，但模型没有调用工具，{supervisor_name}和成员都会受影响。"
    )
    return {"ok": True, "latency_ms": latency, "supports_tools": supports, "detail": detail}


def _model_ids(body: Any, caps: dict[str, dict[str, Any]]) -> list[str]:
    """Read model ids from an OpenAI ``data`` list or an Anthropic ``data`` list."""
    if not isinstance(body, dict) and not isinstance(body, list):
        return []
    entries = body if isinstance(body, list) else body.get("data")
    if not isinstance(entries, list) and isinstance(body, dict):
        entries = body.get("models")
    models: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            identifier = entry.get("id") or entry.get("name") or entry.get("model")
        else:
            identifier = entry
        name = str(identifier or "").strip()
        if name and name not in models:
            models.append(name)
    # Capabilities come from the same answer, so one read fills both.
    return models or list(caps)


async def fetch_models(
    client: httpx.AsyncClient,
    *,
    protocol: str,
    base_url: str,
    api_key: str,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """The upstream's own model list and what it said each model can do.

    Failures are a 502 with a secret-free sentence. A CLI names its own models and has no
    capabilities to report.
    """
    if protocol in CLI_PROTOCOLS:
        # The CLI names its own models; 「默认」 still means "whatever the CLI is configured
        # with", and the rest are the real names it says it can run here.
        from . import cli_engines

        options = await cli_engines.model_catalog(protocol)
        names = [cli_engines.CLI_DEFAULT_MODEL]
        names += [option.name for option in options if option.name not in names]
        return names, {}
    headers = auth_headers(protocol, api_key)
    headers["Accept"] = "application/json"
    try:
        response = await client.get(_models_url(protocol, base_url), headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderProbeError(
            _sentence(f"无法连接模型服务：{exc.__class__.__name__}", api_key)
        ) from exc
    if response.status_code >= 400:
        body = _redact(response.text.strip(), api_key)
        raise ProviderProbeError(
            _sentence(
                f"模型服务返回 HTTP {response.status_code}：{body or '无响应正文'}", api_key
            )
        )
    try:
        parsed = response.json()
    except ValueError as exc:
        raise ProviderProbeError(
            _sentence("模型服务返回的不是 JSON，请检查服务地址。", api_key)
        ) from exc
    found = model_catalog.parse_model_list(parsed)
    return _model_ids(parsed, found), found


async def refresh_provider_caps(
    client: httpx.AsyncClient | None, store: Any, provider_id: str
) -> None:
    """Store what the provider's own model list says, best effort.

    Called whenever a provider's models are read or saved, so resolution never needs a
    network call per request. A failed read changes nothing: the previous caps stay and
    creation still succeeds.
    """
    provider = store.get_provider(provider_id)
    if client is None or provider is None:
        return
    if str(provider["protocol"]) in CLI_PROTOCOLS:
        return
    try:
        _, caps = await fetch_models(
            client,
            protocol=str(provider["protocol"]),
            base_url=str(provider["base_url"]),
            api_key=str(provider.get("api_key") or ""),
        )
    except ProviderProbeError:
        return
    store.update_provider(provider_id, model_caps=caps)


def models_json(models: list[str]) -> str:
    """Column encoding for ``providers.models``."""
    return json.dumps(list(models), ensure_ascii=False)


def model_ref_json(ref: dict[str, str]) -> str:
    """Column encoding for ``agents.model_ref``."""
    stored = {"provider_id": ref["provider_id"], "model": ref["model"]}
    if ref.get("effort"):
        stored["effort"] = ref["effort"]
    return json.dumps(stored, ensure_ascii=False)
