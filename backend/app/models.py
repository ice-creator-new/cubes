"""Pydantic boundaries for the shared snake_case HTTP contract.

Every response goes through a model here so the wire shape is exactly the frozen
contract; request bodies are strict (``extra="forbid"``) so clients cannot smuggle in
fields such as ``kind`` or ``id``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from . import attachments as attachment_rules
from . import approval_rules
from .appearance import EXPRESSIONS, SHAPES, body_color, hex_color, normalize_shape
from .attachments import MAX_ATTACHMENTS_PER_MESSAGE
from .memories import MAX_CONTENT_CHARS as MAX_MEMORY_CHARS
from .memories import MAX_TOPIC_CHARS as MAX_MEMORY_TOPIC
from .memories import SCOPES as MEMORY_SCOPES
from .providers import api_key_hint, provider_caps
from .config import DEFAULT_CONFIRM_TIMEOUT
from .decisions import collect_decisions

MAX_NAME_CHARS = 64
MAX_INSTRUCTIONS_CHARS = 8_000
MAX_TITLE_CHARS = 120
MAX_PURPOSE_CHARS = 4_000
MAX_GOAL_CHARS = 2_000
MAX_TOOL_TEXT_CHARS = 4_000
# A team Markdown file is small by nature; this only keeps a runaway upload out.
MAX_TEAM_FILE_CHARS = 2_000_000
MAX_FOLDER_NAME_CHARS = 24

AgentKind = Literal["supervisor", "member"]
ConversationKind = Literal["direct", "group", "private", "workflow", "thread"]
# ``task`` is the one card a group shows while a member works on a task in its hidden thread;
# ``status`` is the dispatcher's progress card, drawn by the client from structured data.
MessageKind = Literal["user", "agent", "action", "notice", "task", "status"]
MessageStatus = Literal["streaming", "complete", "interrupted", "queued"]
RunStatus = Literal["running", "completed", "stopped", "error", "limited", "queued"]
ActionStatus = Literal["running", "awaiting", "completed", "error"]


def trim_field(value: str, *, field: str, limit: int) -> str:
    """Strip ``value``; reject blank or over-long text with an actionable message."""
    text = value.strip()
    if not text:
        raise ValueError(f"{field}不能为空。")
    if len(text) > limit:
        raise ValueError(f"{field}不能超过 {limit} 个字符。")
    return text


def _catalog_value(value: str, allowed: tuple[str, ...], label: str) -> str:
    """Reject a value outside the appearance catalog with an actionable Chinese message."""
    if value not in allowed:
        raise ValueError(f"{label}「{value}」不在外观目录内。")
    return value


def _body_color(value: str) -> str:
    color = body_color(value)
    if color is None:
        raise ValueError(f"颜色「{value}」不在外观目录内，也不是 #rrggbb 形式的颜色。")
    return color


def _eye_color(value: str) -> str:
    color = hex_color(value)
    if color is None:
        raise ValueError(f"眼睛颜色「{value}」需要是 #rrggbb 形式的颜色。")
    return color


class Appearance(BaseModel):
    """What a member's avatar is drawn from: silhouette, body colour, optional eye colour.

    Faces are automatic — the reply's mood and what the bot is doing pick them — so an
    appearance carries no expression. Without ``eye`` the client picks a high-contrast eye
    for the body; the key is then left out of the payload altogether.
    """

    model_config = ConfigDict(extra="ignore")

    shape: str
    color: str
    eye: str | None = None

    @field_validator("shape")
    @classmethod
    def _shape(cls, value: str) -> str:
        return _catalog_value(normalize_shape(value), SHAPES, "形状")

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return _body_color(value)

    @field_validator("eye")
    @classmethod
    def _eye(cls, value: str | None) -> str | None:
        return None if value is None else _eye_color(value)

    @model_serializer(mode="wrap")
    def _omit_automatic_eye(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("eye") is None:
            data.pop("eye", None)
        return data


class AppearanceInput(BaseModel):
    """The appearance slot of ``PATCH /agents/{id}``.

    ``eye`` is optional: absent or null keeps the automatic eye. A retired ``expression`` is
    still accepted from older clients: it is validated, then dropped. Every field is
    optional so the Chinese "both are required" sentence fires instead of pydantic's
    English field error.
    """

    model_config = ConfigDict(extra="ignore")

    shape: str | None = None
    color: str | None = None
    eye: str | None = None
    expression: str | None = None

    @field_validator("shape")
    @classmethod
    def _shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _catalog_value(normalize_shape(value), SHAPES, "形状")

    @field_validator("color")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        return None if value is None else _body_color(value)

    @field_validator("eye")
    @classmethod
    def _eye(cls, value: str | None) -> str | None:
        return None if value is None else _eye_color(value)

    @field_validator("expression")
    @classmethod
    def _expression(cls, value: str | None) -> str | None:
        # Validated so a client with a typo still hears about it, then ignored: the value
        # never reaches storage and never comes back out.
        return None if value is None else _catalog_value(value, EXPRESSIONS, "表情")

    @model_validator(mode="after")
    def _complete(self) -> "AppearanceInput":
        if self.shape is None or self.color is None:
            raise ValueError("外观需要同时提供 shape 和 color。")
        return self

    def stored(self) -> dict[str, str]:
        """The appearance the store keeps: the live slots, never the retired key."""
        stored = {"shape": self.shape or "", "color": self.color or ""}
        if self.eye:
            stored["eye"] = self.eye
        return stored


# --------------------------------------------------------------------- responses


class ModelRef(BaseModel):
    """One picked model: which provider, and which of its listed models."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model: str
    # The reasoning effort a CLI engine runs with; only Claude Code / Codex take one.
    effort: str | None = None

    @model_serializer(mode="wrap")
    def _shape(self, handler: Any) -> dict[str, Any]:
        """Leave the key out entirely when no effort was picked: every HTTP pick is unchanged."""
        data = handler(self)
        if isinstance(data, dict) and not data.get("effort"):
            data.pop("effort", None)
        return data


class TokenBudget(BaseModel):
    """One bot's token budget: how many tokens per day/week/month it may spend."""

    model_config = ConfigDict(extra="forbid")

    limit: int
    period: Literal["day", "week", "month"]

    @field_validator("limit")
    @classmethod
    def _limit(cls, value: int) -> int:
        if value < 1000 or value > 1_000_000_000:
            raise ValueError("token 预算需在 1000 到 10 亿之间。")
        return value


class ToolPermissions(BaseModel):
    """The four per-member tool switches plus the skill opt-outs; ``null`` on the agent means
    everything allowed."""

    model_config = ConfigDict(extra="forbid")

    code: bool
    files: bool
    computer: bool
    connectors: Literal["all", "read", "none"]
    # Skill names this bot may not use; optional so existing rows and clients that omit it
    # keep validating (a skill is available to every bot unless its name sits here).
    skills_disabled: list[str] = Field(default_factory=list)


class AgentOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    instructions: str
    kind: AgentKind
    avatar_seed: str
    appearance: Appearance | None = None
    # ``None`` follows the workspace default; ``model_status`` says whether the pick still
    # resolves (``custom``), follows the default (``default``) or fell back (``fallback``).
    model_ref: ModelRef | None = None
    model_status: Literal["default", "custom", "fallback"] = "default"
    token_budget: TokenBudget | None = None
    tool_permissions: ToolPermissions | None = None
    # How a send meets this member mid-reply; ``None`` follows the team default.
    send_mode: Literal["steer", "queue", "interrupt"] | None = None
    # This bot's own approval mode; ``None`` follows the workspace.
    permission_mode: Literal["ask", "auto", "full"] | None = None
    created_at: str
    updated_at: str


class ActionOptionOut(BaseModel):
    """One answer a bot offers when it asks the user a question."""

    model_config = ConfigDict(extra="ignore")

    label: str
    description: str = ""


class ActionQuestionOut(BaseModel):
    """One ``ask_user`` question: a header chip, a sentence, and its concrete options."""

    model_config = ConfigDict(extra="ignore")

    question: str
    header: str
    options: list[ActionOptionOut] = Field(default_factory=list)
    multi: bool = False
    recommended: str | None = None


class ActionConfirmationOut(BaseModel):
    """The question a waiting action asks the user, and how it may be answered.

    ``confirm`` is the two-button card every approval uses. ``question`` is ``ask_user``:
    the composer shows a tab per question and its options, each ending in 「其他」.
    """

    model_config = ConfigDict(extra="ignore")

    prompt: str
    confirm_label: str = ""
    deny_label: str = ""
    kind: Literal["confirm", "question"] = "confirm"
    questions: list[ActionQuestionOut] = Field(default_factory=list)
    # What 「总是允许」 on this card remembers, when it is narrower than the tool itself.
    rule: str | None = None

    @model_serializer(mode="wrap")
    def _confirm_shape(self, handler: Any) -> dict:
        """A plain card keeps the exact wire shape older clients know."""
        data = handler(self)
        if data.get("kind", "confirm") == "confirm":
            for key in ("kind", "questions"):
                data.pop(key, None)
        if data.get("rule") is None:
            # Only a narrowed 「总是允许」 names a rule; a plain card keeps the wire shape the
            # older clients already know.
            data.pop("rule", None)
        return data


class ActionDetailItemOut(BaseModel):
    """One source a search returned: enough to recognise it and open it."""

    model_config = ConfigDict(extra="ignore")

    title: str
    url: str
    snippet: str = ""


class ActionStepOut(BaseModel):
    """One parallel subtask a row reports: what it is, and where it got to."""

    model_config = ConfigDict(extra="ignore")

    title: str
    status: Literal["running", "done", "failed"]
    output: str = ""


class ActionDiffLineOut(BaseModel):
    """One line of a written file: added, removed, or unchanged context."""

    model_config = ConfigDict(extra="ignore")

    op: Literal["+", "-", " "]
    text: str


class ActionDetailOut(BaseModel):
    """What a tool actually did, in the user's terms.

    ``search`` carries the query, the provider and its sources; ``page`` a fetched page, its
    element snapshot when a browser read it; ``text`` a short excerpt of whatever the tool
    produced; ``subtasks`` the per-subtask progress of one ``run_subtasks`` call; ``diff`` the
    lines one ``file_write`` changed.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["search", "page", "text", "subtasks", "diff"]
    query: str | None = None
    provider: str | None = None
    items: list[ActionDetailItemOut] = Field(default_factory=list)
    title: str | None = None
    url: str | None = None
    text: str | None = None
    # ``text`` from a terminal step: the whole command as it ran; ``text`` is then its output.
    command: str | None = None
    # ``page`` from a browser step: the numbered element list the model acted on.
    snapshot: str | None = None
    steps: list[ActionStepOut] = Field(default_factory=list)
    # ``diff``: where the write landed, how it changed the file and the lines it touched.
    path: str | None = None
    display_path: str | None = None
    mode: Literal["create", "overwrite", "append"] | None = None
    added: int = 0
    removed: int = 0
    lines: list[ActionDiffLineOut] = Field(default_factory=list)
    truncated: bool = False
    note: str | None = None

    @model_serializer(mode="wrap")
    def _shape(self, handler: Any) -> dict:
        """Only the fields a row's kind means, so every kind keeps its exact wire shape."""
        data = handler(self)
        if not data.get("steps"):
            data.pop("steps", None)
        if data.get("command") is None:
            data.pop("command", None)
        if data.get("snapshot") is None:
            data.pop("snapshot", None)
        if data.get("kind") == "diff":
            for key in ("query", "provider", "items", "title", "url", "text", "command", "snapshot"):
                data.pop(key, None)
            if data.get("note") is None:
                data.pop("note", None)
        else:
            for key in (
                "path",
                "display_path",
                "mode",
                "added",
                "removed",
                "lines",
                "truncated",
                "note",
            ):
                data.pop(key, None)
        return data


class ActionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    status: ActionStatus
    summary: str
    target_agent_id: str | None = None
    target_conversation_id: str | None = None
    error: str | None = None
    confirmation: ActionConfirmationOut | None = None
    detail: ActionDetailOut | None = None


class ApprovalRuleOut(BaseModel):
    """One remembered 「总是允许」: a tool, and the one bot it covers (or every bot)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    tool_name: str
    # The tool's Chinese name, so the settings page never shows a raw identifier.
    title: str
    agent_id: str | None = None
    agent_name: str | None = None
    created_at: str


class RelayOut(BaseModel):
    """Marks a message a bot relayed into a direct chat from a private bot-to-bot chat.

    ``in``/``out`` markers carry no text; they only say who the private chat was with.
    ``summary`` is the caller's own note to the user about that private chat.
    """

    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    direction: Literal["in", "out", "summary"]
    peer_id: str
    peer_name: str


class OriginOut(BaseModel):
    """The user question a bot-to-bot message was sent for, in the conversation it came from."""

    model_config = ConfigDict(extra="ignore")

    message_id: str
    conversation_id: str
    author_name: str
    content: str


class AttachmentOut(BaseModel):
    """One attachment as clients see it; the file itself comes from ``GET /attachments/{id}``."""

    model_config = ConfigDict(extra="ignore")

    id: str
    mime: str
    size: int
    # The original filename; "" for images and for uploads made before names existed.
    name: str = ""


class ScheduleRefOut(BaseModel):
    """The scheduled task a message belongs to: the answer it produced, as the client labels it."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str


class ReactionActorOut(BaseModel):
    """One actor that reacted: the user (``id="user"``) or an agent id, with its name."""

    id: str
    name: str


class ReactionOut(BaseModel):
    """One emoji and everyone who reacted with it, oldest emoji first."""

    emoji: str
    count: int
    mine: bool
    actors: list[ReactionActorOut]


class RouteOut(BaseModel):
    """Who answered one user message in a group, and by which rule.

    Written by the invisible dispatcher (``by="dispatcher"``), by its fallback
    (``"fallback"``), by ``@所有人`` (``"mention"``), or by the user's own re-dispatch
    (``"user"``). The client shows it as a faint line under the bubble.
    """

    model_config = ConfigDict(extra="ignore")

    speakers: list[str] = Field(default_factory=list)
    kind: Literal["answer", "steer", "resume", "status", "none"] = "answer"
    task_id: str | None = None
    reason: str = ""
    by: Literal["dispatcher", "fallback", "mention", "user"] = "dispatcher"


class StatusTaskOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    owner: str
    title: str
    status: str
    step: int = 0
    current: str = ""
    latest: str = ""
    waiting_for: str | None = None


class StatusCardOut(BaseModel):
    """One progress answer: every unfinished task of the group and who is idle."""

    model_config = ConfigDict(extra="ignore")

    tasks: list[StatusTaskOut] = Field(default_factory=list)
    idle: list[str] = Field(default_factory=list)


class MessageOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    conversation_id: str
    author_id: str | None = None
    author_name: str
    avatar_seed: str | None = None
    kind: MessageKind
    content: str
    status: MessageStatus
    mentions: list[str] = Field(default_factory=list)
    reply_to_id: str | None = None
    mood: str | None = None
    relay: RelayOut | None = None
    schedule: ScheduleRefOut | None = None
    seq: int
    created_at: str
    action: ActionOut | None = None
    origin: OriginOut | None = None
    # A user message sent while the previous reply was still running, and cut it off.
    interrupts: bool = False
    attachments: list[AttachmentOut] = Field(default_factory=list)
    # The run that produced this message, when one did.
    run_id: str | None = None
    reactions: list[ReactionOut] = Field(default_factory=list)
    # A self-reminder's trigger: the client shows it as a quiet system row.
    reminder: bool = False
    # The group task a ``kind="task"`` card stands for; the client renders it from live tasks.
    task_id: str | None = None
    # Whether the model response that wrote this text also emitted tool calls. ``None`` on rows
    # written before the field existed and on non-agent rows; the client only guesses for those.
    leads_to_tools: bool | None = None
    # Group routing: who answered this user message, why (kind ``answer``, ``steer``,
    # ``resume``, ``status`` or ``none``), and the progress card a ``kind="status"`` row carries.
    route: RouteOut | None = None
    status_card: StatusCardOut | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: ConversationKind
    title: str
    purpose: str
    member_ids: list[str]
    # A group's standing rules, read into every prompt in that group; "" when none.
    rules: str = ""
    # Frozen contract: the newest message's text preview, ``null`` when empty.
    last_message: str | None = None
    created_at: str
    updated_at: str
    # A bot reply arrived after the newest message the user saw in this chat.
    unread: bool = False


class FinishedRunOut(BaseModel):
    """A completed run the user has not opened yet, for the sidebar's 已完成 list."""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    conversation_id: str
    conversation_title: str
    agent_name: str = ""
    finished_at: str
    message_id: str | None = None
    preview: str = ""


class RunOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    conversation_id: str
    status: RunStatus
    current_step: str
    error: str | None = None
    model_calls: int
    tool_calls: int
    # Tokens the provider reported for this run, summed over its model calls; a provider that
    # reports nothing keeps these at zero.
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # How many times this run compacted its conversation (a summary was written).
    compactions: int = 0
    current_agent_id: str | None = None
    pending_agent_ids: list[str] = Field(default_factory=list)
    hops: int = 0
    schedule_id: str | None = None
    # Some bot's turn in this run stopped at its step budget: 「继续」 picks up from there.
    wrapped: bool = False
    created_at: str
    updated_at: str
    finished_at: str | None = None


class WorkflowStepOut(BaseModel):
    """One node of a definition: what it asks, who runs it, when it waits for the user."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    kind: str
    instruction: str
    gate: str
    on_fail: str
    who: str | None = None
    # A ``branch`` node's options; every outgoing edge names one of them.
    choices: list[str] = Field(default_factory=list)
    # Where the panel put it on the canvas; null means "let the automatic layout decide".
    x: float | None = None
    y: float | None = None


class WorkflowEdgeOut(BaseModel):
    """One connection: ``when`` is the branch choice it belongs to (null for a plain edge).

    ``max_loops`` marks a loop edge: it goes back to an earlier node, and taking it runs the
    nodes in between again, at most that many times. Null on every forward edge.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    when: str | None = None
    max_loops: int | None = None


class WorkflowOut(BaseModel):
    """One workflow as the panel and the ``workflows`` event show it."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str
    owner_id: str
    owner_name: str | None = None
    created_by: str
    steps: list[WorkflowStepOut] = Field(default_factory=list)
    edges: list[WorkflowEdgeOut] = Field(default_factory=list)
    version: int
    enabled: bool
    source: str
    run_count: int
    last_run_at: str | None = None
    created_at: str
    updated_at: str


class WorkflowStepRoundOut(BaseModel):
    """What an earlier round of a looped node produced; the chat still shows its message."""

    model_config = ConfigDict(extra="ignore")

    round: int
    status: str
    output: str = ""
    choice: str | None = None
    message_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class WorkflowStepStateOut(BaseModel):
    """One node of a run: its own status, attempts, and what it produced."""

    model_config = ConfigDict(extra="ignore")

    node_id: str = ""
    index: int
    title: str
    kind: str
    status: str
    attempts: int
    output: str = ""
    # The branch option this node chose, when it is a branch.
    choice: str | None = None
    message_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # 0 on the first pass; each time a loop brings the node back it goes up by one.
    round: int = 0
    history: list[WorkflowStepRoundOut] = Field(default_factory=list)


class WorkflowRunOut(BaseModel):
    """One walk through a workflow: where the cursor is and how each step stands."""

    model_config = ConfigDict(extra="ignore")

    id: str
    workflow_id: str
    workflow_name: str = ""
    workflow_version: int
    conversation_id: str
    run_id: str | None = None
    status: str
    cursor: int
    input: str = ""
    summary: str = ""
    started_by: str
    steps: list[WorkflowStepStateOut] = Field(default_factory=list)
    edges: list[WorkflowEdgeOut] = Field(default_factory=list)
    # Rounds each loop edge has sent this walk back, keyed ``from->to#when``.
    loops: dict[str, int] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    finished_at: str | None = None


class DecisionOut(BaseModel):
    """One thing waiting on the user, wherever the card or question was posted."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: Literal["action", "question", "workflow"]
    conversation_id: str
    message_id: str | None = None
    who: str = ""
    where: str = ""
    prompt: str = ""
    if_unanswered: str = ""


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agents: list[AgentOut] = Field(default_factory=list)
    conversations: list[ConversationOut] = Field(default_factory=list)
    supervisor_id: str
    direct_conversation_id: str
    # False only until the user has named the supervisor on the first-run screen.
    onboarded: bool = True
    active_runs: list[RunOut] = Field(default_factory=list)
    decisions: list[DecisionOut] = Field(default_factory=list)
    # Completed runs the user has not opened, newest first (at most ten).
    finished_runs: list[FinishedRunOut] = Field(default_factory=list)
    # The live task threads under each group's row in the sidebar.
    group_threads: list[dict] = Field(default_factory=list)


class SidebarFolderOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    position: int


class SidebarItemOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    pinned: bool
    pin_position: int
    folder_id: str | None = None
    position: int


class SidebarOut(BaseModel):
    """The whole sidebar layout: folders by position, items by pin then folder position."""

    model_config = ConfigDict(extra="ignore")

    folders: list[SidebarFolderOut] = Field(default_factory=list)
    items: list[SidebarItemOut] = Field(default_factory=list)


class LegacyRoleOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    instructions: str
    avatar_seed: str


class ConversationDetailOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conversation: ConversationOut
    messages: list[MessageOut] = Field(default_factory=list)
    latest_run: RunOut | None = None
    legacy_roles: list[LegacyRoleOut] = Field(default_factory=list)
    has_more: bool = False
    references: list[MessageOut] = Field(default_factory=list)


class SnapshotOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workspace: WorkspaceOut
    conversation: ConversationDetailOut | None = None
    sidebar: SidebarOut
    # The walk the requested chat is serving right now, if one is live: a client
    # that reconnects after missing events gets the cursor back from here, not from a guess.
    workflow_run: WorkflowRunOut | None = None


class HealthOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["ok"] = "ok"
    configured: bool
    model: str
    base_url: str
    provider_name: str = ""
    configuration_error: str | None = None
    # True when the backend demands a launch token, so clients know auth is enforced.
    auth: bool = False


class ModelOverride(BaseModel):
    """The user's own answer for one model (or ``*``); an omitted field stays automatic."""

    model_config = ConfigDict(extra="ignore")

    images: bool | None = None
    context: int | None = None
    output: int | None = None


class ModelCapsOut(BaseModel):
    """One model's resolved capabilities, and which layer answered each field."""

    model_config = ConfigDict(extra="ignore")

    images: bool = False
    video: bool = False
    audio: bool = False
    pdf: bool = False
    context_window: int = 0
    max_output: int = 0
    reasoning: bool = False
    reasoning_levels: list[str] = Field(default_factory=list)
    # Per field: user / provider / catalog / default.
    sources: dict[str, str] = Field(default_factory=dict)


class ModelDetail(BaseModel):
    """One CLI model as the pickers show it: the name it is sent as, and how to word it."""

    model_config = ConfigDict(extra="ignore")

    model: str
    label: str = ""
    description: str = ""
    # The reasoning efforts this model accepts, and the one the CLI defaults to.
    efforts: list[str] = Field(default_factory=list)
    default_effort: str = ""
    # Whether the CLI's own configuration currently points at this model.
    current: bool = False


class ProviderOut(BaseModel):
    """One model service; ``api_key`` itself never leaves the store."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    protocol: Literal["openai-chat", "anthropic-messages", "claude-code", "codex"]
    base_url: str
    api_key_hint: str | None = None
    models: list[str] = Field(default_factory=list)
    # Resolved capabilities for every listed model: images, window, output, reasoning, and
    # the layer each field came from (user / provider / catalog / default).
    model_caps: dict[str, ModelCapsOut] = Field(default_factory=dict)
    # The user's own per-model answers (or ``*``): {model: {images, context, output}}.
    model_overrides: dict[str, ModelOverride] = Field(default_factory=dict)
    # For a CLI provider: what this machine says about each listed model (label, efforts).
    model_details: list[ModelDetail] = Field(default_factory=list)
    created_at: str
    updated_at: str


class EngineOut(BaseModel):
    """One local CLI engine: whether it is installed, and whether it is signed in."""

    model_config = ConfigDict(extra="ignore")

    kind: str
    label: str
    installed: bool
    ready: bool
    # One Chinese sentence for the settings sheet (and the reason when not ready).
    detail: str
    command: str = ""
    version: str = ""


class ProvidersOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    providers: list[ProviderOut] = Field(default_factory=list)
    default: ModelRef | None = None
    # The optional workspace backup model; ``None`` when the user did not pick one.
    fallback: ModelRef | None = None
    # The local CLI engines' availability, so the add form can show it without a second call.
    engines: list[EngineOut] = Field(default_factory=list)


class ProviderTestOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool
    latency_ms: int
    supports_tools: bool
    detail: str


class ProviderModelsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    models: list[str] = Field(default_factory=list)
    # What the provider's answer said about each model, so the form can preview the chips.
    caps: dict[str, ModelCapsOut] = Field(default_factory=dict)


class Skill(BaseModel):
    """One agent skill as the list shows it (``GET /skills``, the ``skills`` event)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str
    source: Literal["user", "agent", "upload", "github", "local"]
    source_url: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    enabled: bool
    has_scripts: bool
    file_count: int
    use_count: int
    last_used_at: str | None = None
    created_at: str
    updated_at: str


class SkillFile(BaseModel):
    """One non-SKILL.md file of a skill, as ``{path, size}``."""

    path: str
    size: int


class SkillDetail(Skill):
    """``Skill`` plus the SKILL.md body (no frontmatter) and every file except SKILL.md."""

    body: str
    files: list[SkillFile] = Field(default_factory=list)


# ---------------------------------------------------------------------- requests


class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    instructions: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return trim_field(value, field="成员名称", limit=MAX_NAME_CHARS)

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: str) -> str:
        return trim_field(value, field="职责指令", limit=MAX_INSTRUCTIONS_CHARS)


class ProviderCreateRequest(BaseModel):
    """``POST /providers``; field rules live in :mod:`app.providers` so errors read well."""

    model_config = ConfigDict(extra="forbid")

    name: str
    protocol: str
    base_url: str
    api_key: str | None = None
    models: list[str] = Field(default_factory=list)
    model_overrides: dict[str, ModelOverride] = Field(default_factory=dict)


class ProviderPatchRequest(BaseModel):
    """``PATCH /providers/{id}``: an omitted ``api_key`` keeps the stored one, ``""`` clears it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    # Omitted keeps the stored overrides; ``{}`` clears them.
    model_overrides: dict[str, ModelOverride] | None = None


class ProviderDefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model: str


class ProviderFallbackRequest(BaseModel):
    """``PUT /providers/fallback``: a pick, or both fields empty to turn the backup off."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str | None = None
    model: str | None = None


class ProviderTestRequest(BaseModel):
    """A form's values; ``id`` (with no ``api_key``) reuses the key already stored."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    protocol: str
    base_url: str
    api_key: str | None = None
    model: str


class ProviderModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    protocol: str
    base_url: str
    api_key: str | None = None


class ConnectorRequest(BaseModel):
    """``POST /connectors`` and ``PATCH /connectors/{id}``; rules live in :mod:`app.connectors`.

    ``secrets`` changes only the fields it names; ``headers``/``env`` replace the whole set
    when present. ``preset`` is required to create and ignored when patching.
    """

    model_config = ConfigDict(extra="forbid")

    preset: str | None = None
    name: str | None = None
    secrets: dict[str, str] | None = None
    allow_writes: bool | None = None
    enabled: bool | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None


class SkillCreateRequest(BaseModel):
    """``POST /skills``: a skill the user writes by hand; enabled immediately."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    body: str = ""


class SkillPatchRequest(BaseModel):
    """``PATCH /skills/{id}``: enable/disable, or rewrite the description/body; name is fixed."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    description: str | None = None
    body: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "SkillPatchRequest":
        if self.enabled is None and self.description is None and self.body is None:
            raise ValueError("至少需要提供 enabled、description 或 body。")
        return self


class SkillGitHubRequest(BaseModel):
    """``POST /skills/github``: one GitHub URL pointing at a skill."""

    model_config = ConfigDict(extra="forbid")

    url: str


class SkillLocalRequest(BaseModel):
    """``POST /skills/local``: one folder path ``GET /skills/local`` listed."""

    model_config = ConfigDict(extra="forbid")

    path: str


class OnboardingRequest(BaseModel):
    """``POST /onboarding``: the user naming the supervisor on the first-run screen.

    ``name`` follows the same rule as a member's; ``appearance`` goes through the same
    :class:`AppearanceInput` validation PATCH /agents/{id} uses. An omitted appearance
    keeps the one the bootstrap drew.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    appearance: AppearanceInput | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return trim_field(value, field="名字", limit=MAX_NAME_CHARS)


class AgentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    instructions: str | None = None
    # An explicit ``null`` resets the stored appearance; ``model_fields_set`` tells an
    # explicit null apart from an omitted field. The retired ``expression`` key is still
    # accepted inside it and then dropped.
    appearance: AppearanceInput | None = None
    # ``null`` follows the workspace default; an omitted key leaves the stored pick alone.
    model_ref: ModelRef | None = None
    # ``null`` clears the budget; an omitted key leaves it alone.
    token_budget: TokenBudget | None = None
    # An object sets all four switches; ``null`` clears them (everything allowed).
    tool_permissions: ToolPermissions | None = None
    # How a send meets this bot mid-reply; ``null`` follows the team default.
    send_mode: Literal["steer", "queue", "interrupt"] | None = None
    # This bot's own approval mode; ``None`` follows the workspace.
    permission_mode: Literal["ask", "auto", "full"] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else trim_field(value, field="成员名称", limit=MAX_NAME_CHARS)

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="职责指令", limit=MAX_INSTRUCTIONS_CHARS)

    @model_validator(mode="after")
    def _at_least_one(self) -> "AgentPatchRequest":
        has_appearance = "appearance" in self.model_fields_set
        has_model = "model_ref" in self.model_fields_set
        has_budget = "token_budget" in self.model_fields_set
        has_permissions = "tool_permissions" in self.model_fields_set
        has_send_mode = "send_mode" in self.model_fields_set
        has_permission_mode = "permission_mode" in self.model_fields_set
        if (
            self.name is None
            and self.instructions is None
            and not has_appearance
            and not has_model
            and not has_budget
            and not has_permissions
            and not has_send_mode
            and not has_permission_mode
        ):
            raise ValueError(
                "至少需要提供 name、instructions、appearance、model_ref、token_budget、"
                "tool_permissions、send_mode 或 permission_mode。"
            )
        return self


TASK_STATUSES = ("todo", "doing", "done", "blocked")


class TaskPatchRequest(BaseModel):
    """``PATCH /tasks/{id}``: the user marking a task done or writing a progress note."""

    model_config = ConfigDict(extra="forbid")

    # Plain ``str`` rather than ``Literal``: the enum check here speaks Chinese, the same
    # sentence the toolkit uses, instead of pydantic's English "Input should be…".
    status: str | None = None
    note: str | None = None

    @field_validator("status")
    @classmethod
    def _status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in TASK_STATUSES:
            raise ValueError("状态只能是 todo、doing、done 或 blocked。")
        return value

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # A blank note is a deliberate clear, so it is allowed where other text is not.
        text = value.strip()
        if len(text) > MAX_TOOL_TEXT_CHARS:
            raise ValueError(f"进度说明不能超过 {MAX_TOOL_TEXT_CHARS} 个字符。")
        return text

    @model_validator(mode="after")
    def _at_least_one(self) -> "TaskPatchRequest":
        if self.status is None and "note" not in self.model_fields_set:
            raise ValueError("至少需要提供 status 或 note。")
        return self


class MemoryCreateRequest(BaseModel):
    """``POST /memories``: one fact the user typed into the panel."""

    model_config = ConfigDict(extra="forbid")

    content: str
    topic: str | None = None
    # Plain ``str`` so the refusal below speaks Chinese, not pydantic's enum wording.
    scope: str = "user"
    agent_id: str | None = None

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        return trim_field(value, field="记忆内容", limit=MAX_MEMORY_CHARS)

    @field_validator("topic")
    @classmethod
    def _topic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split())
        if len(text) > MAX_MEMORY_TOPIC:
            raise ValueError(f"主题不能超过 {MAX_MEMORY_TOPIC} 个字符。")
        return text or None

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: str) -> str:
        if value not in MEMORY_SCOPES:
            raise ValueError("记忆范围只能是 user、agent、team 或 group。")
        return value

    @field_validator("agent_id")
    @classmethod
    def _agent_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _needs_agent(self) -> "MemoryCreateRequest":
        if self.scope == "agent" and self.agent_id is None:
            raise ValueError("属于某个成员的记忆需要 agent_id。")
        if self.scope == "group" and self.agent_id is None:
            raise ValueError("群约定需要用 agent_id 指明是哪个群。")
        return self


class MemoryPatchRequest(BaseModel):
    """``PATCH /memories/{id}``: a new content is a new row; topic or pin alone is not."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    topic: str | None = None
    pinned: bool | None = None

    @field_validator("content")
    @classmethod
    def _content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="记忆内容", limit=MAX_MEMORY_CHARS)

    @field_validator("topic")
    @classmethod
    def _topic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split())
        if len(text) > MAX_MEMORY_TOPIC:
            raise ValueError(f"主题不能超过 {MAX_MEMORY_TOPIC} 个字符。")
        return text

    @model_validator(mode="after")
    def _at_least_one(self) -> "MemoryPatchRequest":
        if self.content is None and self.topic is None and self.pinned is None:
            raise ValueError("至少需要提供 content、topic 或 pinned。")
        return self


class MemorySettingsRequest(BaseModel):
    """``PATCH /memories/settings``: the workspace's two switches."""

    model_config = ConfigDict(extra="forbid")

    paused: bool | None = None
    auto_extract: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "MemorySettingsRequest":
        if self.paused is None and self.auto_extract is None:
            raise ValueError("至少需要提供 paused 或 auto_extract。")
        return self


class ReviewSettingsRequest(BaseModel):
    """The ``review`` slot of ``PATCH /settings/team``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    max_rounds: int | None = None

    @field_validator("max_rounds")
    @classmethod
    def _max_rounds(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1 or value > 3:
            raise ValueError("验收最多打回轮数需在 1 到 3 之间。")
        return value


class DigestSettingsRequest(BaseModel):
    """The ``digest`` slot of ``PATCH /settings/team``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    frequency: Literal["daily", "weekly"] | None = None
    time: str | None = None
    weekday: int | None = None

    @field_validator("time")
    @classmethod
    def _time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.strip().split(":")
        if (
            len(parts) != 2
            or not parts[0].isdigit()
            or not parts[1].isdigit()
            or not 0 <= int(parts[0]) <= 23
            or not 0 <= int(parts[1]) <= 59
            or len(parts[0]) != 2
            or len(parts[1]) != 2
        ):
            raise ValueError("摘要时间需是 HH:MM 的 24 小时格式。")
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"

    @field_validator("weekday")
    @classmethod
    def _weekday(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0 or value > 6:
            raise ValueError("周几需在 0（周一）到 6（周日）之间。")
        return value


class PermissionSettingsRequest(BaseModel):
    """``PATCH /settings/permissions``: the approval mode, the extra folders, or both."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["ask", "auto", "full"] | None = None
    folders: list[str] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "PermissionSettingsRequest":
        if self.mode is None and self.folders is None:
            raise ValueError("至少需要提供 mode 或 folders。")
        return self


class TeamSettingsRequest(BaseModel):
    """``PATCH /settings/team``: a partial body, nested slots partial too."""

    model_config = ConfigDict(extra="forbid")

    review: ReviewSettingsRequest | None = None
    digest: DigestSettingsRequest | None = None
    # The workspace's answer for a send that meets a running reply: ``steer`` (default),
    # ``queue`` or ``interrupt``.
    send_mode: Literal["steer", "queue", "interrupt"] | None = None
    # How many steps one turn and one run may take: ``unlimited`` (default), ``standard``,
    # ``long`` or ``marathon``; the resolved numbers ride back read-only on the response.
    step_budget: Literal["unlimited", "standard", "long", "marathon"] | None = None
    # Whether the supervisor may do hands-on work in a group; off by default, so a group's
    # supervisor delegates instead of redoing the members' job with the same tools.
    supervisor_works_in_groups: bool | None = None
    # How many hidden task threads may execute at once (1..8, default 3).
    max_parallel_tasks: int | None = Field(default=None, ge=1, le=8)
    # The model a group's invisible dispatcher calls; ``null`` (sent explicitly) is automatic:
    # the backup model when one is configured, else the workspace default.
    dispatch_model: ModelRef | None = None


class GroupCreateRequest(BaseModel):
    """``POST /conversations``: the user opens a group of their own."""

    model_config = ConfigDict(extra="forbid")

    title: str
    purpose: str
    member_ids: list[str] = Field(default_factory=list)
    # The supervisor is an optional organiser now: it is added only when asked for here.
    include_supervisor: bool = False
    rules: str = ""

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return trim_field(value, field="群组名称", limit=MAX_TITLE_CHARS)

    @field_validator("rules")
    @classmethod
    def _rules(cls, value: str) -> str:
        text = value.strip()
        if len(text) > MAX_PURPOSE_CHARS:
            raise ValueError(f"群规则不能超过 {MAX_PURPOSE_CHARS} 个字符。")
        return text

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        return trim_field(value, field="群组目的", limit=MAX_PURPOSE_CHARS)


class RerouteRequest(BaseModel):
    """``POST /conversations/{id}/messages/{id}/reroute``: the user's own speakers."""

    model_config = ConfigDict(extra="forbid")

    speakers: list[str] = Field(min_length=1)


class ConversationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    purpose: str | None = None
    member_ids: list[str] | None = None
    # A group's own standing rules; a blank string clears them.
    rules: str | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="会话标题", limit=MAX_TITLE_CHARS)

    @field_validator("rules")
    @classmethod
    def _rules(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if len(text) > MAX_PURPOSE_CHARS:
            raise ValueError(f"群规则不能超过 {MAX_PURPOSE_CHARS} 个字符。")
        return text

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="会话目的", limit=MAX_PURPOSE_CHARS)

    @field_validator("member_ids")
    @classmethod
    def _member_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            text = trim_field(item, field="成员 ID", limit=MAX_NAME_CHARS)
            if text in cleaned:
                raise ValueError(f"成员 ID 不能重复：{text}")
            cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def _at_least_one(self) -> "ConversationPatchRequest":
        if (
            self.title is None
            and self.purpose is None
            and self.member_ids is None
            and self.rules is None
        ):
            raise ValueError("至少需要提供 title、purpose、member_ids 或 rules。")
        return self


class ConversationReadRequest(BaseModel):
    """``POST /conversations/{id}/read``: the newest message sequence the user has seen."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)


class SidebarItemIn(BaseModel):
    """One sidebar entry in a full-layout write; order in the array is the order shown."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    pinned: bool = Field(default=False, strict=True)
    folder_id: str | None = None

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id(cls, value: str) -> str:
        return trim_field(value, field="会话 ID", limit=MAX_NAME_CHARS)

    @field_validator("folder_id")
    @classmethod
    def _folder_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class SidebarPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SidebarItemIn] = Field(default_factory=list)
    folder_ids: list[str] = Field(default_factory=list)


class FolderNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return trim_field(value, field="分组名称", limit=MAX_FOLDER_NAME_CHARS)


SCHEDULE_KINDS = ("once", "interval", "cron")


class ScheduleCreateRequest(BaseModel):
    """``POST /schedules``: one rule, its prompt and where it runs."""

    model_config = ConfigDict(extra="forbid")

    title: str
    prompt: str
    conversation_id: str
    kind: str
    at: str | None = None
    every_minutes: int | None = None
    cron: str | None = None
    timezone: str | None = None
    use_computer: bool = Field(default=False, strict=True)
    enabled: bool = Field(default=True, strict=True)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return trim_field(value, field="定时任务名称", limit=MAX_TITLE_CHARS)

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        return trim_field(value, field="定时任务内容", limit=MAX_GOAL_CHARS)

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id(cls, value: str) -> str:
        return trim_field(value, field="会话 ID", limit=MAX_NAME_CHARS)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        return _schedule_kind(value)

    @field_validator("at", "cron", "timezone")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("every_minutes")
    @classmethod
    def _every_minutes(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("定时间隔必须大于 0 分钟。")
        return value


class SchedulePatchRequest(BaseModel):
    """``PATCH /schedules/{id}``: any field of the same shape, or pause/resume."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    prompt: str | None = None
    conversation_id: str | None = None
    kind: str | None = None
    at: str | None = None
    every_minutes: int | None = None
    cron: str | None = None
    timezone: str | None = None
    use_computer: bool | None = Field(default=None, strict=True)
    enabled: bool | None = Field(default=None, strict=True)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="定时任务名称", limit=MAX_TITLE_CHARS)

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="定时任务内容", limit=MAX_GOAL_CHARS)

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="会话 ID", limit=MAX_NAME_CHARS)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str | None) -> str | None:
        return None if value is None else _schedule_kind(value)

    @field_validator("at", "cron", "timezone")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("every_minutes")
    @classmethod
    def _every_minutes(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("定时间隔必须大于 0 分钟。")
        return value

    @model_validator(mode="after")
    def _at_least_one(self) -> "SchedulePatchRequest":
        if not self.model_fields_set:
            raise ValueError("至少需要修改一个字段。")
        return self


def _schedule_kind(value: str) -> str:
    if value not in SCHEDULE_KINDS:
        raise ValueError("触发方式只能是 once、interval 或 cron。")
    return value


class RunStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = ""
    mentions: list[str] = Field(default_factory=list)
    reply_to_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)
    # What a send does while a reply is already running in this conversation: ``steer``
    # (join the running turn), ``queue`` (send once it ends) or ``interrupt`` (stop it and
    # answer this message instead). Omitted means the bot's own setting, else the team's
    # default; scripts that want a fixed answer still pass it explicitly.
    mode: Literal["steer", "queue", "interrupt"] | None = None

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        # Blank is allowed here: a message may be images alone. The route rejects a message
        # with neither text nor images.
        text = value.strip()
        if len(text) > MAX_GOAL_CHARS:
            return trim_field(value, field="任务内容", limit=MAX_GOAL_CHARS)
        return text

    @field_validator("attachment_ids")
    @classmethod
    def _attachment_ids(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(cleaned) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError(attachment_rules.TOO_MANY)
        return cleaned

    @field_validator("reply_to_id")
    @classmethod
    def _reply_to_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="引用的消息 ID", limit=MAX_NAME_CHARS)

    @field_validator("mentions")
    @classmethod
    def _mentions(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = trim_field(item, field="点名成员 ID", limit=MAX_NAME_CHARS)
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class ActionConfirmRequest(BaseModel):
    """The user's answer to one waiting action: delete it, or keep it.

    ``always`` is the card's 「总是允许」 button: the same yes, plus a rule that covers the
    next identical call so no card is shown for it.
    """

    model_config = ConfigDict(extra="forbid")

    approve: bool = Field(strict=True)
    always: bool = Field(default=False, strict=True)


class ApprovalRuleCreateRequest(BaseModel):
    """``POST /approval-rules``: one tool, and the bot it covers (omitted: every bot)."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    agent_id: str | None = None

    @field_validator("tool_name")
    @classmethod
    def _tool_name(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("工具名不能为空。")
        if len(text) > 120:
            raise ValueError("工具名太长。")
        return text

    @field_validator("agent_id")
    @classmethod
    def _agent_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            raise ValueError("成员 ID 不能为空。")
        return text


class ActionAnswer(BaseModel):
    """One question's answer: its position, the chosen labels, and the user's own words."""

    model_config = ConfigDict(extra="forbid")

    question_index: int = Field(ge=0)
    labels: list[str] = Field(default_factory=list)
    other: str | None = None

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            label = trim_field(item, field="选项", limit=MAX_NAME_CHARS)
            if label not in cleaned:
                cleaned.append(label)
        return cleaned

    @field_validator("other")
    @classmethod
    def _other(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if len(text) > MAX_TOOL_TEXT_CHARS:
            raise ValueError(f"回答不能超过 {MAX_TOOL_TEXT_CHARS} 个字符。")
        return text


class ActionAnswerRequest(BaseModel):
    """The user's answers to an ``ask_user`` call, or one skip for all its questions.

    ``answers`` carries one entry per question (its index, the picked labels, and the words
    typed under 「其他」); ``skip`` means they left every question unanswered.
    """

    model_config = ConfigDict(extra="forbid")

    answers: list[ActionAnswer] = Field(default_factory=list)
    skip: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _answered(self) -> "ActionAnswerRequest":
        if not self.skip and not self.answers:
            raise ValueError("请回答每个问题，或选择跳过。")
        return self


class MessageEditRequest(BaseModel):
    """``POST /conversations/{id}/messages/{mid}/edit``: the corrected text of a sent message.

    The tail after it is removed and a fresh run answers the new text, so the message reads
    the way the user meant it and nothing answers the version they took back.
    """

    model_config = ConfigDict(extra="forbid")

    content: str

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        return trim_field(value, field="消息", limit=MAX_GOAL_CHARS)


class TeamImportRequest(BaseModel):
    """``POST /team/import``: the Markdown file, and whether to really write it."""

    model_config = ConfigDict(extra="forbid")

    markdown: str
    apply: bool = Field(default=False, strict=True)
    # Off by default: an import never rewrites the supervisor's own settings unless asked.
    overwrite_supervisor: bool = Field(default=False, strict=True)

    @field_validator("markdown")
    @classmethod
    def _markdown(cls, value: str) -> str:
        if len(value) > MAX_TEAM_FILE_CHARS:
            raise ValueError(f"团队文件不能超过 {MAX_TEAM_FILE_CHARS // 1024} KB。")
        return value


class WebhookCreateRequest(BaseModel):
    """``POST /webhooks``: which schedule or workflow gets the secret URL."""

    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["schedule", "workflow"]
    target_id: str


class WorkflowStepRequest(BaseModel):
    """One node as a client writes it; the rules module fills in what is missing."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str = ""
    kind: str = "do"
    instruction: str
    gate: str = "auto"
    on_fail: str = "stop"
    choices: list[str] | None = None
    x: float | None = None
    y: float | None = None


class WorkflowEdgeRequest(BaseModel):
    """One connection as a client writes it; ``when`` is a branch node's option.

    ``max_loops`` (1–20) makes it a loop edge back to an earlier node.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    when: str | None = None
    max_loops: int | None = None


class WorkflowCreateRequest(BaseModel):
    """``POST /workflows``: a plan the user built, or an import of a bot's draft."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    owner_agent_id: str | None = None
    steps: list[WorkflowStepRequest] = Field(default_factory=list)
    # Omit ``edges`` to chain the steps in the order they were given.
    edges: list[WorkflowEdgeRequest] | None = None
    enabled: bool = True


class WorkflowPatchRequest(BaseModel):
    """``PATCH /workflows/{id}``: any subset; a changed plan is a new version."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    owner_agent_id: str | None = None
    steps: list[WorkflowStepRequest] | None = None
    edges: list[WorkflowEdgeRequest] | None = None
    enabled: bool | None = None


class WorkflowRunRequest(BaseModel):
    """``POST /workflows/{id}/run``: what the user wants this walk to work on."""

    model_config = ConfigDict(extra="forbid")

    input: str = ""


# ---------------------------------------------------------------------- payloads


def agent_payload(raw: dict) -> dict:
    return AgentOut.model_validate(raw).model_dump(mode="json")


def action_payload(raw: dict) -> dict:
    return ActionOut.model_validate(raw).model_dump(mode="json")


def approval_rule_payload(raw: dict) -> dict:
    """One rule as the settings page reads it, with the tool's Chinese name filled in."""
    return ApprovalRuleOut.model_validate(
        {**raw, "title": approval_rules.tool_title(str(raw["tool_name"]))}
    ).model_dump(mode="json")


def approval_rules_payload(store: Any) -> dict:
    """The whole rule list in the shape ``GET /approval-rules`` and the event share."""
    return {"rules": [approval_rule_payload(rule) for rule in store.list_approval_rules()]}


def message_payload(raw: dict) -> dict:
    return MessageOut.model_validate(raw).model_dump(mode="json")


def conversation_payload(raw: dict) -> dict:
    return ConversationOut.model_validate(raw).model_dump(mode="json")


def run_payload(raw: dict) -> dict:
    return RunOut.model_validate(raw).model_dump(mode="json")


def sidebar_payload(layout: dict) -> dict:
    """Shape one stored sidebar layout for the wire."""
    return SidebarOut.model_validate(layout).model_dump(mode="json")


def skill_payload(raw: dict) -> dict:
    return Skill.model_validate(raw).model_dump(mode="json")


def skill_detail_payload(raw: dict, body: str, files: list[dict]) -> dict:
    return SkillDetail.model_validate({**raw, "body": body, "files": files}).model_dump(
        mode="json"
    )


def skills_payload(store: Any) -> dict:
    """The whole skill list in the shape ``GET /skills`` and the ``skills`` event share."""
    return {"skills": [skill_payload(row) for row in store.list_skills()]}


def provider_shape(raw: dict, settings: Any | None = None) -> dict:
    """One stored provider as the wire shape: the key becomes a hint, never the value."""
    return {
        "id": raw["id"],
        "name": raw["name"],
        "protocol": raw["protocol"],
        "base_url": raw["base_url"],
        "api_key_hint": api_key_hint(raw.get("api_key") or ""),
        "models": list(raw.get("models") or []),
        # Resolution reads the catalog from ``settings``; without one the defaults apply.
        "model_caps": provider_caps(raw, settings),
        "model_overrides": dict(raw.get("model_overrides") or {}),
        # Filled in by ``GET /providers`` for the CLI protocols only; a stored row has none.
        "model_details": list(raw.get("model_details") or []),
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
    }


def provider_payload(raw: dict, settings: Any | None = None) -> dict:
    return ProviderOut.model_validate(provider_shape(raw, settings)).model_dump(mode="json")


def providers_payload(
    providers: list[dict],
    default: dict | None,
    fallback: dict | None = None,
    engines: list[dict] | None = None,
    settings: Any | None = None,
) -> dict:
    shaped = ProvidersOut.model_validate(
        {
            "providers": [provider_shape(item, settings) for item in providers],
            "default": default,
            "fallback": fallback,
            "engines": engines or [],
        }
    )
    return shaped.model_dump(mode="json")


def workspace_payload(store: Any, *, confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT) -> dict:
    """Shape the full workspace payload from a :class:`~app.store.Store`."""
    supervisor = store.supervisor()
    direct = store.direct_conversation()
    workspace = WorkspaceOut(
        agents=[AgentOut.model_validate(agent) for agent in store.list_agents()],
        conversations=[
            ConversationOut.model_validate(conversation)
            for conversation in store.list_conversations()
        ],
        supervisor_id=supervisor["id"] if supervisor is not None else "",
        direct_conversation_id=direct["id"] if direct is not None else "",
        onboarded=store.onboarded(),
        active_runs=[RunOut.model_validate(run) for run in store.active_runs()],
        decisions=[
            DecisionOut.model_validate(row)
            for row in collect_decisions(store, confirm_timeout_seconds=confirm_timeout_seconds)
        ],
        finished_runs=[FinishedRunOut.model_validate(row) for row in store.finished_runs()],
        group_threads=store.list_group_threads(),
    )
    return workspace.model_dump(mode="json")


def conversation_detail_payload(
    store: Any,
    conversation_id: str,
    *,
    limit: int | None = None,
    before: int | None = None,
) -> dict | None:
    """Shape ``ConversationDetail`` for one conversation, or ``None`` when unknown.

    Without ``limit`` every message comes back, which is what older clients read. With it,
    one page comes back instead: the newest ``limit`` messages (those before ``before`` when
    it is given), oldest first, plus whether older ones remain and the messages the page
    quotes although they sit outside it, so their quote lines still have a parent.
    """
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return None
    has_more = False
    if limit is None:
        raw_messages = store.list_messages(conversation_id)
    else:
        raw_messages, has_more = store.list_message_page(
            conversation_id, limit, before
        )
    referenced = {message["id"] for message in raw_messages}
    quoted = {
        message["reply_to_id"]
        for message in raw_messages
        if message.get("reply_to_id") and message["reply_to_id"] not in referenced
    }
    references = sorted(
        (
            parent
            for parent in (store.get_message(message_id) for message_id in quoted)
            if parent is not None
        ),
        key=lambda message: (message["seq"], message["id"]),
    )
    detail = ConversationDetailOut(
        conversation=ConversationOut.model_validate(conversation),
        messages=[MessageOut.model_validate(message) for message in raw_messages],
        latest_run=_optional_run(store.latest_run(conversation_id)),
        legacy_roles=conversation.get("legacy_roles") or [],
        has_more=has_more,
        references=[MessageOut.model_validate(message) for message in references],
    )
    return detail.model_dump(mode="json")


def snapshot_payload(
    store: Any, conversation_id: str | None, *, confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT
) -> dict:
    """Shape ``Snapshot``: the workspace, the requested conversation and the sidebar."""
    detail = conversation_detail_payload(store, conversation_id) if conversation_id else None
    workflow_run = (
        store.workflow_run_by_conversation(conversation_id) if conversation_id else None
    )
    snapshot = SnapshotOut(
        workspace=WorkspaceOut.model_validate(
            workspace_payload(store, confirm_timeout_seconds=confirm_timeout_seconds)
        ),
        conversation=ConversationDetailOut.model_validate(detail) if detail else None,
        sidebar=SidebarOut.model_validate(store.sidebar_layout()),
        workflow_run=(
            WorkflowRunOut.model_validate(workflow_run_payload(store, workflow_run))
            if workflow_run is not None
            else None
        ),
    )
    return snapshot.model_dump(mode="json", by_alias=True)


def _optional_run(raw: dict | None) -> RunOut | None:
    return None if raw is None else RunOut.model_validate(raw)


def _edge_list(edges: Any) -> list[dict]:
    """Stored edges as the wire shape: ``{from, to, when, max_loops}``, in stored order."""
    result: list[dict] = []
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        result.append(
            {
                "from": edge.get("from"),
                "to": edge.get("to"),
                "when": edge.get("when"),
                "max_loops": edge.get("max_loops"),
            }
        )
    return result


def _round_list(raw: Any) -> list[dict]:
    """A node row's ``history`` column (JSON) as the wire list; malformed reads as empty."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def workflow_payload(store: Any, raw: dict) -> dict:
    """One definition with its owner's name, which the row itself does not carry."""
    owner = store.get_agent(raw.get("owner_agent_id") or "")
    return WorkflowOut.model_validate(
        {
            **raw,
            "edges": _edge_list(raw.get("edges")),
            "owner_id": raw.get("owner_agent_id") or "",
            "owner_name": owner["name"] if owner is not None else None,
        }
    ).model_dump(mode="json", by_alias=True)


def workflows_payload(store: Any) -> dict:
    """The whole list, in the shape ``GET /workflows`` and the ``workflows`` event share."""
    return {"workflows": [workflow_payload(store, row) for row in store.list_workflows()]}


def workflow_run_payload(store: Any, raw: dict) -> dict:
    """One run with its step rows and the workflow's current name (the run keeps its own)."""
    workflow = store.get_workflow(raw.get("workflow_id") or "")
    states = store.workflow_step_states(raw["id"])
    return WorkflowRunOut.model_validate(
        {
            **raw,
            "workflow_name": workflow["name"] if workflow is not None else "",
            "edges": _edge_list(raw.get("edges")),
            "steps": [
                {
                    "node_id": state.get("node_id") or "",
                    "index": int(state["step_index"]),
                    "title": state["title"],
                    "kind": state["kind"],
                    "status": state["status"],
                    "attempts": int(state["attempts"]),
                    "output": state["output"],
                    "choice": state.get("choice"),
                    "message_id": state["message_id"],
                    "started_at": state["started_at"],
                    "finished_at": state["finished_at"],
                    "round": int(state.get("rounds") or 0),
                    "history": _round_list(state.get("history")),
                }
                for state in states
            ],
        }
    ).model_dump(mode="json", by_alias=True)
