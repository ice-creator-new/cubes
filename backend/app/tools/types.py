"""Value types of the tool layer: one handler shape, one outcome, one run context.

The registry, every family module and the engine share these, so a tool never needs the
engine's internals to describe itself and the engine never needs a table per family.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..agent import budget as budget_rules

# Every tool result is capped before it ever reaches the model.
MAX_TOOL_RESULT_CHARS = budget_rules.LIVE_RESULT_CHARS


def cap_tool_text(text: str) -> str:
    """Trim a tool's answer to the budget, head and tail, saying so instead of cutting it.

    The tail is the half that matters: a shell command's error, a browser click's failure and
    a stack trace all end the text, and a head-only cut threw exactly that away.
    """
    return budget_rules.clip_head_tail(
        text, MAX_TOOL_RESULT_CHARS, marker=budget_rules.LIVE_RESULT_MARKER
    )


def error_result(message: str) -> str:
    """The JSON every refused or failed call returns to the model."""
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


@dataclass
class ToolOutcome:
    """Result of one real tool execution, ready to be replayed to the model."""

    summary: str
    result: dict[str, Any]
    target_agent_id: str | None = None
    target_conversation_id: str | None = None
    # Toolkit packs return model-facing text directly; management tools return JSON.
    text: str | None = None
    # The user-facing summary of what a toolkit tool did, shown under its action row.
    detail: dict | None = None
    # Set when the tool already finished its own action (e.g. after a confirmation).
    handled: bool = False
    # Images the other side sent back (a private reply); the caller sees them next.
    images: list[str] = field(default_factory=list)

    @property
    def result_json(self) -> str:
        if self.text is not None:
            return self.text
        return json.dumps(self.result, ensure_ascii=False)


@dataclass(frozen=True)
class ToolOffer:
    """Who is asking for a tool list: the role, where, and whether a human can answer.

    ``scheduled`` marks a run a scheduled task started: nobody watches its workspace, so
    the tools that would park on a card are refused rather than offered.
    """

    role: str
    conversation: dict | None = None
    author: dict | None = None
    scheduled: bool = False
    # The supervisor is running a group's conversation, not doing hands-on work: tools that
    # execute on the machine (see ``ToolRegistry.EXECUTION_TOOL_NAMES``) stay out of reach.
    coordinating: bool = False
    # This turn is already working a task's hidden thread: starting another task is not offered.
    from_thread: bool = False


Parser = Callable[[str | None], dict[str, Any]]
Runner = Callable[["ToolRunContext", dict[str, Any]], Awaitable["ToolOutcome"]]


@dataclass
class ToolRunContext:
    """Everything one tool call may touch, assembled by the engine for that call.

    A handler names exactly what it needs instead of reaching into the engine: the run's
    own state, the action row it was given, and the run-level services the loop still owns
    (a nested reply, a card parked on the user's decision, step and event publishing).
    """

    store: Any
    settings: Any
    run: Any
    conversation: dict
    author: dict
    action: dict
    action_message_id: str
    http: httpx.AsyncClient | None
    computer: Any
    connectors: Any
    publish: Callable[[str, Any], None]
    publish_message: Callable[[str, dict], None]
    publish_workspace: Callable[[], None]
    publish_sidebar: Callable[[], None]
    publish_schedules: Callable[[], None]
    publish_decisions: Callable[[], None]
    set_step: Callable[[str], None]
    finish_action: Callable[..., None]
    # Push the current task's live fields after a card starts or stops waiting.
    refresh_task: Callable[[], None]
    await_decision: Callable[[str], Awaitable[str]]
    # ``ask_user`` waits for an arbitrary answer, with no deadline; ``None`` on a stopped run.
    await_answer: Callable[[str], Awaitable[dict | None]]
    roster: Callable[[dict], list[dict]]
    # The chat tools run a nested reply inside this very run; the loop owns that.
    supervisor_reply: Callable[..., Awaitable[dict | None]] | None = None
    member_context: Callable[..., list[dict[str, Any]]] | None = None
    member_call: Callable[..., Awaitable[tuple[str, dict | None]]] | None = None
    consult_member: Callable[..., Awaitable[tuple[str, dict, dict]]] | None = None
    # ``ask_group``: run one group's discussion of a posted question inside this run, and say
    # whether a group already has a conversation going of its own.
    group_discussion: Callable[..., Awaitable[list[dict]]] | None = None
    group_busy: Callable[[str], bool] | None = None
    # ``group_assign``: one member answers a part of the caller's group task, in its own thread.
    assign_part: Callable[..., Awaitable[tuple[str, dict | None, dict]]] | None = None
    # ``start_task``: turn the caller's group turn into a hidden task thread and run it there.
    start_task: Callable[..., Awaitable[dict]] | None = None
    review_member: Callable[..., Awaitable[tuple[str, dict, dict]]] | None = None
    origin_message_id: Callable[[], str | None] | None = None
    # The message a write is made against: the reply being composed, else the trigger.
    source_message_id: Callable[[], str | None] | None = None
    # ``run_subtasks``: this bot's own focused turns, side by side, reporting as they settle.
    run_subtasks: Callable[[list[dict], list[dict], Callable[[], None]], Awaitable[list[dict]]] | None = None


@dataclass(frozen=True)
class ToolHandler:
    """One tool: how the model sees it, who may call it, and what it does.

    ``unattended_refusal`` is the answer a run nobody watches gets instead of its
    confirmation card; ``None`` means the tool is safe there. ``offered_when`` and
    ``callable_when`` add the environment's own rules on top of the role: a sandbox tool
    needs a live sandbox, and ``notify_user`` only exists inside a private bot chat.
    """

    name: str
    label: str
    schema: dict[str, Any]
    roles: frozenset[str]
    run: Runner
    parse: Parser
    unattended_refusal: str | None = None
    offered_when: Callable[[ToolOffer], bool] = lambda offer: True
    callable_when: Callable[[ToolOffer], bool] = lambda offer: True
    # A sandbox step, counted against its own budget rather than the tool budget.
    computer_step: bool = False
    # The call hands the work to someone else (a workflow's own page): the bot's turn ends
    # with what it wrote, instead of it going on to do the same work itself.
    hands_off: bool = False

    def offered(self, offer: ToolOffer) -> bool:
        """Whether this handler's schema goes into this model call's tool list."""
        return offer.role in self.roles and self.offered_when(offer)

    def callable(self, offer: ToolOffer) -> bool:
        """Whether a call of this name may reach ``run`` at all."""
        return offer.role in self.roles and self.callable_when(offer)
