"""The tiny tool framework shared by every pack.

One :class:`ToolSpec` describes a tool completely: its name, the Chinese label shown in the
action row, the model-facing description, a JSON schema for the arguments, who may call it
and the coroutine that runs it. The engine only ever sees this interface, so adding a tool
never touches the run loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..agent.budget import clip_head_tail

# Every tool result is capped before it ever reaches the model.
MAX_RESULT_CHARS = 12_000
SEARCH_RESULT_CHARS = 6_000

# The user-facing detail under an action row is deliberately smaller than the model's text.
MAX_DETAIL_ITEMS = 10
DETAIL_SNIPPET_CHARS = 200
DETAIL_TEXT_CHARS = 1_200
DETAIL_PAGE_CHARS = 600


class ToolInputError(Exception):
    """A call the tool refuses; the Chinese message becomes the tool result verbatim."""


@dataclass(frozen=True)
class Gate:
    """What one call needs the user's yes for, before it runs.

    ``grant`` names the permission a mode may already give (see :mod:`app.permissions`);
    ``rule`` is the key a 「总是允许」 remembers, so a yes for writing inside the working folder
    never also covers writing anywhere else.
    """

    grant: str
    rule: str
    prompt: str
    summary: str
    resume: str


@dataclass
class ToolContext:
    """Everything a tool may touch during one run."""

    store: Any
    settings: Any
    run_id: str
    conversation_id: str
    agent: dict
    http: httpx.AsyncClient
    publish: Callable[[str, Any], None]
    # The run this call belongs to (the engine's ``ActiveRun``). Tools that must remember
    # something per run — the write guard's ledger of files this run has seen — keep it here.
    run: Any = None
    # The message this call is written against: the reply being composed, or the user
    # message that started the run. Tools that record provenance store it.
    message_id: str | None = None
    # The action row this call is rendering into. A tool that produces a file (a screenshot,
    # a viewed picture) hangs its attachment here so the row itself can show it.
    action_message_id: str | None = None
    # The user (or the bot's approval mode) allowed this call to reach outside the folders.
    allow_outside: bool = False

    @property
    def is_supervisor(self) -> bool:
        return self.agent.get("kind") == "supervisor"

    @property
    def agent_id(self) -> str:
        return str(self.agent.get("id") or "")

    @property
    def agent_name(self) -> str:
        return str(self.agent.get("name") or "")


@dataclass(frozen=True)
class ToolResult:
    """A tool's model-facing text plus the short detail the user sees under its action row.

    Tools that can say what they actually did return this; every other tool still returns a
    plain string, so the model contract is unchanged. ``summary`` overrides the spec's
    arg-only summary when the tool can phrase it better at run time — a file path resolved
    against the working folder, for instance.
    """

    text: str
    detail: dict | None = None
    summary: str | None = None
    # Attachment ids the tool produced; the engine turns them into one follow-up user turn,
    # which is how a tool's pixels reach a model that only accepts media in message content.
    images: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool: metadata for the model plus the coroutine that does the work."""

    name: str
    label: str
    description: str
    parameters: dict
    roles: frozenset[str]
    run: Callable[[ToolContext, dict], Awaitable[str | ToolResult]]
    summary: Callable[[dict], str]
    available: Callable[[Any], bool] = lambda settings: True
    # The runtime half of availability: ``(offer, store) -> bool``. It answers what the
    # settings alone cannot — which conversation this is, and a switch that lives in the
    # database — so a pack can hide a tool in a private bot chat or while memory is paused.
    usable: Callable[[Any, Any], bool] = lambda offer, store: True
    # ``(settings, store, args) -> Gate | None``: whether this call must be approved first.
    gate: Callable[[Any, Any, dict], "Gate | None"] | None = None

    def offered_to(self, settings: Any, role: str) -> bool:
        return role in self.roles and self.available(settings)


def cap_text(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Trim ``text`` to ``limit`` characters, head and tail, saying so instead of cutting.

    A file listing, a search result page and a script's output all carry their most useful
    line at the end as often as at the start, so both ends survive the cut.
    """
    return clip_head_tail(text, limit, marker="\n…（内容已截断，共 {total} 字符）\n")


def split_result(value: str | ToolResult) -> tuple[str, dict | None]:
    """``(text, detail)`` for whatever a tool returned."""
    if isinstance(value, ToolResult):
        return value.text, value.detail
    return str(value), None


def _clip(text: Any, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def text_detail(text: Any, *, limit: int = DETAIL_TEXT_CHARS) -> dict:
    """Detail for a tool whose value to the user is a short block of text."""
    body = str(text or "").strip()
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    return {"kind": "text", "text": body}


def search_detail(query: str, provider: str, results: list[dict]) -> dict:
    """Detail for a search: the query, who answered it and the sources it returned."""
    items = [
        {
            "title": _clip(item.get("title"), 200),
            "url": str(item.get("url") or ""),
            "snippet": _clip(item.get("snippet"), DETAIL_SNIPPET_CHARS),
        }
        for item in results[:MAX_DETAIL_ITEMS]
    ]
    return {"kind": "search", "query": query, "provider": provider, "items": items}


def page_detail(url: str, title: str, text: str) -> dict:
    """Detail for a page read: where it landed, what it is called and its opening text."""
    return {
        "kind": "page",
        "url": url,
        "title": _clip(title, 200),
        "text": _clip(text, DETAIL_PAGE_CHARS),
    }


def schemas(specs: list[ToolSpec]) -> list[dict]:
    """OpenAI ``tools`` array for the given specs."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in specs
    ]
