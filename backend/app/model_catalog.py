"""Model capabilities: what a provider's own answer says, and the models.dev catalog.

A provider's ``GET /models`` answer is the best source for what a model can do, because it
describes the service's own deployment. Not every gateway says much: an OpenAI-style list
names models only. The open models.dev catalog fills those gaps. Resolution between the
layers lives in :mod:`app.providers`; this module parses and stores them.

The bundled snapshot is a trimmed copy of https://models.dev/api.json (id, provider, input
modalities, context and output limits, reasoning) so a fresh install knows real windows
without a network call. At most once a day a background task replaces the data directory's
copy with a fresh download; every failure leaves the previous copy in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# A sanity bound shared with providers: a declared window or output past this is ignored.
MAX_TOKENS = 10_000_000

CATALOG_URL = "https://models.dev/api.json"
CATALOG_FILENAME = "models-dev.json"
# How long the data directory's copy is trusted before a background refresh is started.
CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 120.0
SNAPSHOT_PATH = Path(__file__).with_name("models_dev.json")

# Capability keys, in the shape every layer stores: only keys an answer actually declared
# are present, so a missing key means "that layer has no opinion".
CAP_KEYS = (
    "images",
    "video",
    "audio",
    "pdf",
    "context_window",
    "max_output",
    "reasoning",
    "reasoning_levels",
)

# First-party providers win a duplicate model id in the bundled snapshot: an aggregator's
# copy sometimes carries a smaller window or another routing layer's output cap.
FIRST_PARTY = (
    "anthropic",
    "openai",
    "google",
    "deepseek",
    "moonshotai",
    "alibaba",
    "xai",
    "meta",
    "mistral",
    "zhipuai",
    "minimax",
)

_DATE_SUFFIX = re.compile(r"[-_](?:20\d{2}[-_]?\d{2}[-_]?\d{2}|20\d{6}|\d{4})$")
_TAG_SUFFIX = re.compile(r"(?::(?:free|latest|nitro))$")


# ------------------------------------------------------------------ provider answers


def _tokens(value: Any) -> int | None:
    """A positive token count, or ``None`` for anything else (never trusts a bool)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= MAX_TOKENS else None


def _first_int(source: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        amount = _tokens(source.get(key))
        if amount is not None:
            return amount
    return None


def _name_set(value: Any) -> set[str]:
    """Modality names from a list, or a ``"text+image"`` style string."""
    if isinstance(value, str):
        parts = re.split(r"[+,]", value)
    elif isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        return set()
    return {part.strip().lower() for part in parts if part.strip()}


def _input_modalities(entry: dict) -> set[str] | None:
    """The input modalities an answer declared, in any of the shapes seen in the wild."""
    found: set[str] | None = None
    modalities = entry.get("modalities")
    if isinstance(modalities, dict):
        names = _name_set(modalities.get("input"))
        if names:
            found = names
    elif isinstance(modalities, str):
        names = _name_set(modalities)
        if names:
            found = names
    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        names = _name_set(architecture.get("input_modalities"))
        modality = architecture.get("modality")
        if isinstance(modality, str):
            names |= _name_set(modality.split("->", 1)[0])
        if names:
            found = (found or set()) | names
    return found


def _capabilities(entry: dict) -> dict[str, bool]:
    """Image / PDF / video / audio support from a nested ``capabilities`` object or flags."""
    found: dict[str, bool] = {}
    nested = entry.get("capabilities")
    if isinstance(nested, dict):
        pairs = (
            ("image_input", "images"),
            ("image", "images"),
            ("vision", "images"),
            ("pdf_input", "pdf"),
            ("pdf", "pdf"),
            ("video_input", "video"),
            ("audio_input", "audio"),
        )
        for key, cap in pairs:
            item = nested.get(key)
            if isinstance(item, bool):
                found[cap] = item
            elif isinstance(item, dict) and isinstance(item.get("supported"), bool):
                found[cap] = item["supported"]
    flat = (
        ("vision", "images"),
        ("supports_vision", "images"),
        ("supports_images", "images"),
        ("supports_video", "video"),
        ("supports_audio", "audio"),
        ("supports_pdf", "pdf"),
    )
    for key, cap in flat:
        if isinstance(entry.get(key), bool):
            found[cap] = entry[key]
    return found


def _reasoning_levels(entry: dict) -> list[str]:
    raw = entry.get("reasoning_levels")
    if not isinstance(raw, list):
        raw = entry.get("supported_reasoning_levels")
    levels: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        level = item.get("effort") if isinstance(item, dict) else item
        text = str(level or "").strip()
        if text and text not in levels:
            levels.append(text)
    return levels


def parse_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Capabilities one ``/models`` entry declared, in :data:`CAP_KEYS` shape.

    Covers OpenAI-style entries (usually nothing), OpenRouter, magpie/LiteLLM gateways and
    Anthropic's ``/v1/models``. An undeclared capability stays absent.
    """
    caps: dict[str, Any] = {}
    inputs = _input_modalities(entry)
    if inputs is not None:
        caps["images"] = "image" in inputs
        caps["video"] = "video" in inputs
        caps["audio"] = "audio" in inputs
        caps["pdf"] = "pdf" in inputs or "file" in inputs
    caps.update(_capabilities(entry))

    context = _first_int(entry, ("context_window", "context_length", "max_input_tokens"))
    output = _first_int(entry, ("max_output_tokens", "max_completion_tokens"))
    top = entry.get("top_provider")
    if isinstance(top, dict):
        if context is None:
            context = _first_int(top, ("context_length", "max_input_tokens"))
        if output is None:
            output = _first_int(top, ("max_completion_tokens", "max_output_tokens"))
    if context is not None:
        caps["context_window"] = context
    if output is not None:
        caps["max_output"] = output

    reasoning = entry.get("reasoning")
    if isinstance(reasoning, bool):
        caps["reasoning"] = reasoning
    else:
        supported = entry.get("supported_parameters")
        if isinstance(supported, list) and any(
            str(item).lower() in ("reasoning", "reasoning_effort", "thinking")
            for item in supported
        ):
            caps["reasoning"] = True
    levels = _reasoning_levels(entry)
    if levels:
        caps["reasoning_levels"] = levels
        caps.setdefault("reasoning", True)
    return caps


def _entries(body: Any) -> list[dict]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("data", "models"):
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def parse_model_list(body: Any) -> dict[str, dict[str, Any]]:
    """``{model id: capabilities}`` from a provider's own model list answer."""
    parsed: dict[str, dict[str, Any]] = {}
    for entry in _entries(body):
        identifier = entry.get("id") or entry.get("name") or entry.get("model")
        name = str(identifier or "").strip()
        if not name or name in parsed:
            continue
        parsed[name] = parse_entry(entry)
    return parsed


def clean_caps(raw: Any) -> dict[str, dict[str, Any]]:
    """Stored provider-sourced capabilities with anything malformed dropped."""
    cleaned: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return cleaned
    for raw_model, entry in raw.items():
        model = str(raw_model or "").strip()
        if not model or not isinstance(entry, dict):
            continue
        caps: dict[str, Any] = {}
        for key in ("images", "video", "audio", "pdf", "reasoning"):
            if isinstance(entry.get(key), bool):
                caps[key] = entry[key]
        for key in ("context_window", "max_output"):
            amount = _tokens(entry.get(key))
            if amount is not None:
                caps[key] = amount
        levels = [
            str(level).strip()
            for level in entry.get("reasoning_levels") or []
            if str(level).strip()
        ] if isinstance(entry.get("reasoning_levels"), list) else []
        if levels:
            caps["reasoning_levels"] = levels
        if caps:
            cleaned[model] = caps
    return cleaned


# ------------------------------------------------------------------ models.dev catalog


def normalize_model_id(model: str) -> str:
    """The conservative name match: lowercase, no provider prefix, no date/tag suffix."""
    text = str(model or "").strip().lower()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    previous = None
    while text and text != previous:
        previous = text
        text = _TAG_SUFFIX.sub("", text)
        text = _DATE_SUFFIX.sub("", text)
    return text


@dataclass(frozen=True)
class Catalog:
    """The models.dev entries, indexed the three ways a lookup is allowed to try."""

    entries: dict[str, dict] = field(default_factory=dict)
    by_short: dict[str, dict] = field(default_factory=dict)
    by_name: dict[str, dict] = field(default_factory=dict)

    def lookup(self, model: str) -> dict | None:
        """Exact id, then the id after the last ``/``, then the normalized name."""
        key = str(model or "").strip().lower()
        if not key:
            return None
        if key in self.entries:
            return self.entries[key]
        short = key.rsplit("/", 1)[-1]
        if short in self.by_short:
            return self.by_short[short]
        return self.by_name.get(normalize_model_id(key))


def build_catalog(entries: list[dict]) -> Catalog:
    """Index a snapshot; a duplicate key keeps the first entry, which the builder sorted."""
    exact: dict[str, dict] = {}
    short: dict[str, dict] = {}
    names: dict[str, dict] = {}
    for entry in entries:
        identifier = str(entry.get("id") or "").strip()
        if not identifier:
            continue
        key = identifier.lower()
        exact.setdefault(key, entry)
        short.setdefault(key.rsplit("/", 1)[-1], entry)
    for entry in exact.values():
        name = normalize_model_id(str(entry.get("id") or ""))
        if name:
            names.setdefault(name, entry)
    return Catalog(entries=exact, by_short=short, by_name=names)


def _trim_entry(model_id: str, provider: str, entry: Any) -> dict | None:
    if not isinstance(entry, dict):
        return None
    identifier = str(entry.get("id") or model_id or "").strip()
    if not identifier:
        return None
    trimmed: dict[str, Any] = {"id": identifier, "provider": provider}
    modalities = entry.get("modalities")
    inputs = _name_set((modalities or {}).get("input") if isinstance(modalities, dict) else None)
    trimmed["input"] = sorted(inputs)
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    context = _tokens(limit.get("context"))
    output = _tokens(limit.get("output"))
    if context is not None:
        trimmed["context"] = context
    if output is not None:
        trimmed["output"] = output
    if isinstance(entry.get("reasoning"), bool):
        trimmed["reasoning"] = entry["reasoning"]
    return trimmed


def trim_catalog(payload: Any) -> list[dict]:
    """The compact snapshot: one entry per model id, four capability fields at most."""
    if isinstance(payload, list):
        # A list already in snapshot shape keeps its fields as they are.
        return [
            {
                key: entry[key]
                for key in ("id", "provider", "input", "context", "output", "reasoning")
                if key in entry
            }
            for entry in payload
            if isinstance(entry, dict) and entry.get("id")
        ]
    if not isinstance(payload, dict):
        return []
    order = sorted(
        (key for key in payload if isinstance(payload[key], dict)),
        key=lambda key: (
            FIRST_PARTY.index(key) if key in FIRST_PARTY else len(FIRST_PARTY),
            key,
        ),
    )
    picked: dict[str, dict] = {}
    for provider in order:
        models = payload[provider].get("models")
        if not isinstance(models, dict):
            continue
        for model_id, entry in models.items():
            trimmed = _trim_entry(str(model_id), provider, entry)
            if trimmed is not None:
                picked.setdefault(trimmed["id"], trimmed)
    return sorted(picked.values(), key=lambda entry: entry["id"])


def catalogs_from_snapshot(entries: Any) -> Catalog:
    if not isinstance(entries, list):
        return Catalog()
    return build_catalog([entry for entry in entries if isinstance(entry, dict)])


# ------------------------------------------------------------------ loading / refresh

_cache: dict[Path, tuple[float, Catalog]] = {}
_refresh_task: asyncio.Task[None] | None = None
# A failed download is retried at most once an hour, so opening 设置 never hammers it.
RETRY_AFTER_SECONDS = 60 * 60
_last_attempt = 0.0


def data_dir(settings: Any | None) -> Path | None:
    path = getattr(settings, "database_path", None) if settings is not None else None
    return Path(path).parent if path is not None else None


def catalog_path(settings: Any | None) -> Path | None:
    directory = data_dir(settings)
    return None if directory is None else directory / CATALOG_FILENAME


def _read(path: Path) -> Catalog | None:
    try:
        stamp = path.stat().st_mtime
        cached = _cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    catalog = catalogs_from_snapshot(entries)
    _cache[path] = (stamp, catalog)
    return catalog if catalog.entries else None


def seed(settings: Any | None) -> None:
    """Copy the bundled snapshot beside the database once, so its age can be tracked."""
    target = catalog_path(settings)
    if target is None or target.exists() or not SNAPSHOT_PATH.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(SNAPSHOT_PATH.read_bytes())
        os.replace(temporary, target)
    except OSError:
        pass


def load(settings: Any | None) -> Catalog:
    """The catalog to answer from: the data directory's copy, else the bundled snapshot."""
    if settings is not None:
        seed(settings)
        target = catalog_path(settings)
        if target is not None:
            catalog = _read(target)
            if catalog is not None:
                return catalog
    return _read(SNAPSHOT_PATH) or Catalog()


async def refresh(settings: Any) -> None:
    """Download a fresh catalog and replace the data directory's copy atomically."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS, connect=10.0), trust_env=False
        ) as client:
            response = await client.get(CATALOG_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            entries = trim_catalog(response.json())
        if not entries:
            return
        target = catalog_path(settings)
        if target is None:
            return
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(entries, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(temporary, target)
    except Exception as exc:  # noqa: BLE001 - a stale catalog is better than a failed request
        logger.debug("models.dev refresh failed: %s", exc)


def maybe_refresh(settings: Any | None) -> None:
    """Start one background refresh per process when the data directory's copy is old."""
    global _refresh_task, _last_attempt
    if settings is None:
        return
    if _refresh_task is not None and not _refresh_task.done():
        return
    seed(settings)
    target = catalog_path(settings)
    if target is not None and target.exists():
        try:
            if time.time() - target.stat().st_mtime < CATALOG_TTL_SECONDS:
                return
        except OSError:
            return
    if time.time() - _last_attempt < RETRY_AFTER_SECONDS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _last_attempt = time.time()
    _refresh_task = loop.create_task(refresh(settings))
