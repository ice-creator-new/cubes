"""Pack B (part 3) — long-term memory: remember, recall_memory and forget.

These three tools write the ``memories`` table the panel shows: one self-contained
sentence per row, in one of three scopes. A bot may record what the user is like, its own
working experience, and — the supervisor only — a decision the whole team must know.
"""

from __future__ import annotations

from typing import Any

from ..memories import (
    MAX_CONTENT_CHARS,
    MAX_TOPIC_CHARS,
    ORIGIN_AGENT,
    SCOPE_AGENT,
    SCOPE_GROUP,
    SCOPE_TEAM,
    SCOPE_USER,
    memories_payload,
)
from .base import ToolContext, ToolInputError, ToolResult, ToolSpec, text_detail

DEFAULT_RECALL = 5
MAX_RECALL = 10

SCOPE_LABELS = {
    SCOPE_USER: "关于用户",
    SCOPE_AGENT: "你的经验",
    SCOPE_TEAM: "团队决定",
    SCOPE_GROUP: "群约定",
}
GROUP_WRITE_REFUSED = "group 记忆只能在群组里记，记下的是这个群自己的约定。"
PAUSED_WRITE = "记忆已暂停，现在不记录也不修改。"
PAUSED_READ = "记忆已暂停，现在不读取。"
TEAM_WRITE_REFUSED = "只有主管能写团队记忆。"

REMEMBER_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "content": {
            "type": "string",
            "description": "一句自足的中文事实，不要出现「他」「那个」这类指代，例如「用户不吃香菜」。",
        },
        "topic": {
            "type": "string",
            "description": "短标签，例如「饮食偏好」；同一主题的旧条目会被新条目取代。",
        },
        "scope": {
            "type": "string",
            "enum": [SCOPE_USER, SCOPE_AGENT, SCOPE_TEAM, SCOPE_GROUP],
            "description": (
                "user 关于用户（默认）；agent 只给自己看；team 全团队可见，仅限主管；"
                "group 只在当前群里生效的约定（只能在群里用）。"
            ),
        },
    },
    "required": ["content"],
}

RECALL_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "description": "要回忆的内容，用关键词，两三个字也能搜到。"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_RECALL,
            "description": f"返回条数，默认 {DEFAULT_RECALL}，最多 {MAX_RECALL}。",
        },
    },
    "required": ["query"],
}

FORGET_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "memory_id": {"type": "string", "description": "要忘记的记忆 id，可从 recall_memory 得到。"},
        "topic": {"type": "string", "description": "按主题忘记；和 memory_id 二选一。"},
    },
}


def _summary(value: Any, limit: int = 40) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _optional(args: dict, key: str, *, limit: int, field: str) -> str | None:
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ToolInputError(f"{field}必须是文字。")
    text = " ".join(raw.split())
    if not text:
        return None
    if len(text) > limit:
        raise ToolInputError(f"{field}不能超过 {limit} 个字符。")
    return text


def _required(args: dict, key: str, *, limit: int, field: str) -> str:
    text = _optional(args, key, limit=limit, field=field)
    if text is None:
        raise ToolInputError(f"{field}不能为空。")
    return text


def _count(args: dict, key: str, *, default: int, maximum: int, field: str) -> int:
    raw = args.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolInputError(f"{field}必须是整数。")
    value = int(raw)
    if value < 1 or value > maximum:
        raise ToolInputError(f"{field}必须在 1 到 {maximum} 之间。")
    return value


def _scope(args: dict) -> str:
    raw = args.get("scope")
    if raw is None:
        return SCOPE_USER
    scope = str(raw).strip().lower()
    if scope not in SCOPE_LABELS:
        raise ToolInputError("scope 只能是 user、agent、team 或 group。")
    return scope


def _may_touch(ctx: ToolContext, memory: dict) -> bool:
    """Which rows a call may forget: its own, the user's, and — the supervisor's — the team's."""
    if memory["status"] != "active":
        return False
    if memory["scope"] == SCOPE_AGENT:
        return memory["agent_id"] == ctx.agent_id
    if memory["scope"] == SCOPE_TEAM:
        return ctx.is_supervisor
    if memory["scope"] == SCOPE_GROUP:
        return memory["agent_id"] is not None and memory["agent_id"] == _group_id(ctx)
    return True


def _group_id(ctx: ToolContext) -> str | None:
    """The group this call speaks in, or None outside a group."""
    conversation = ctx.store.get_conversation(ctx.conversation_id)
    return conversation["id"] if conversation is not None and conversation["kind"] == "group" else None


def _label(memory: dict) -> str:
    return SCOPE_LABELS.get(str(memory["scope"]), "记忆")


def _line(index: int, memory: dict) -> str:
    return (
        f"{index}. [{_label(memory)}] {memory['content']}"
        f"（主题：{memory['topic']}，id {memory['id']}）"
    )


async def run_remember(ctx: ToolContext, args: dict) -> ToolResult:
    settings = ctx.store.memory_settings()
    if settings["paused"]:
        raise ToolInputError(PAUSED_WRITE)
    content = _required(args, "content", limit=MAX_CONTENT_CHARS, field="记忆内容")
    topic = _optional(args, "topic", limit=MAX_TOPIC_CHARS, field="主题")
    scope = _scope(args)
    agent_id: str | None = None
    if scope == SCOPE_AGENT:
        agent_id = ctx.agent_id
    elif scope == SCOPE_TEAM and not ctx.is_supervisor:
        raise ToolInputError(TEAM_WRITE_REFUSED)
    elif scope == SCOPE_GROUP:
        agent_id = _group_id(ctx)
        if agent_id is None:
            raise ToolInputError(GROUP_WRITE_REFUSED)
    memory = ctx.store.create_memory(
        content=content,
        topic=topic,
        scope=scope,
        agent_id=agent_id,
        origin=ORIGIN_AGENT,
        author_id=ctx.agent_id,
        source_conversation_id=ctx.conversation_id,
        source_message_ids=[ctx.message_id] if ctx.message_id else [],
        supersede_topic=True,
    )
    ctx.publish("memories", memories_payload(ctx.store))
    text = (
        f"已记住（{_label(memory)}）：{memory['content']}"
        f"（主题：{memory['topic']}，id {memory['id']}）"
    )
    return ToolResult(text, text_detail(text))


async def run_recall_memory(ctx: ToolContext, args: dict) -> ToolResult:
    settings = ctx.store.memory_settings()
    if settings["paused"]:
        raise ToolInputError(PAUSED_READ)
    query = _required(args, "query", limit=200, field="要回忆的内容")
    limit = _count(args, "limit", default=DEFAULT_RECALL, maximum=MAX_RECALL, field="返回条数")
    found = ctx.store.search_memories(
        query, agent_id=ctx.agent_id, group_id=_group_id(ctx), limit=limit
    )
    if not found:
        return ToolResult(f"没有找到「{query}」相关的记忆。")
    lines = [f"找到 {len(found)} 条记忆："]
    lines.extend(_line(index, memory) for index, memory in enumerate(found, start=1))
    text = "\n".join(lines)
    return ToolResult(text, text_detail(text))


async def run_forget(ctx: ToolContext, args: dict) -> ToolResult:
    settings = ctx.store.memory_settings()
    if settings["paused"]:
        raise ToolInputError(PAUSED_WRITE)
    memory_id = _optional(args, "memory_id", limit=80, field="记忆 id")
    topic = _optional(args, "topic", limit=MAX_TOPIC_CHARS, field="主题")
    if memory_id is None and topic is None:
        raise ToolInputError("要指定 memory_id 或 topic。")
    targets: list[dict] = []
    if memory_id is not None:
        memory = ctx.store.get_memory(memory_id)
        if memory is None or not _may_touch(ctx, memory):
            raise ToolInputError(f"没有找到可以忘记的记忆 id「{memory_id}」。")
        targets.append(memory)
    else:
        assert topic is not None
        candidates = [
            memory
            for memory in ctx.store.list_memories()
            if _may_touch(ctx, memory)
        ]
        exact = [memory for memory in candidates if memory["topic"] == topic]
        targets = exact or [memory for memory in candidates if topic in memory["content"]]
        if not targets:
            raise ToolInputError(f"没有找到主题「{topic}」的记忆。")
    for memory in targets:
        ctx.store.retire_memory(memory["id"])
    ctx.publish("memories", memories_payload(ctx.store))
    listed = "；".join(f"「{memory['topic']}」{memory['content']}" for memory in targets)
    text = f"已忘记 {len(targets)} 条：{listed}"
    return ToolResult(text, text_detail(text))


def _usable(offer: Any, store: Any) -> bool:
    """Not in a private bot chat (nothing there may be remembered), not while paused."""
    conversation = offer.conversation
    if conversation is not None and conversation.get("kind") == "private":
        return False
    return not store.memory_settings()["paused"]


MEMORY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="remember",
        label="记住",
        description=(
            "把一件长期有用的事实记下来：用户的偏好与情况、你自己干活的经验、"
            "或（仅主管）全团队都该知道的决定。一条只记一件事，写成一句自足的话。"
            "同一主题已有条目时会被新内容取代。"
        ),
        parameters=REMEMBER_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_remember,
        summary=lambda args: f"记住：{_summary(args.get('content'))}",
        usable=_usable,
    ),
    ToolSpec(
        name="recall_memory",
        label="回忆",
        description=(
            "在自己的长期记忆里检索：用户的事实与偏好、你自己的经验、团队决定。"
            "记忆区块里被省略的条目、或用户提到以前的事时使用。"
        ),
        parameters=RECALL_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_recall_memory,
        summary=lambda args: f"回忆「{_summary(args.get('query'), 30)}」",
        usable=_usable,
    ),
    ToolSpec(
        name="forget",
        label="忘记",
        description=(
            "忘掉一条记忆：已经不对的事实、用户明确要求不要记的东西。"
            "忘记是软删除，旧条目会留在面板的历史里。"
        ),
        parameters=FORGET_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_forget,
        summary=lambda args: "忘记：" + (
            f"{_summary(args.get('topic'))}"
            if args.get("topic")
            else f"{_summary(args.get('memory_id'))}"
        ),
        usable=_usable,
    ),
)
