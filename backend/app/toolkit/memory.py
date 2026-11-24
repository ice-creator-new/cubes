"""Pack B (part 1) — memory: scoped history search and conversation reading."""

from __future__ import annotations

from typing import Any

from .base import (
    MAX_RESULT_CHARS,
    ToolContext,
    ToolInputError,
    ToolResult,
    ToolSpec,
    cap_text,
    text_detail,
)

SNIPPET_PAD = 60
SNIPPET_CHARS = 200
DEFAULT_HISTORY = 8
MAX_HISTORY = 20
DEFAULT_READ = 30
MAX_READ = 50

SEARCH_HISTORY_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "description": "要回忆的关键词，尽量具体。"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_HISTORY,
            "description": f"返回条数，默认 {DEFAULT_HISTORY}，最多 {MAX_HISTORY}。",
        },
    },
    "required": ["query"],
}

READ_CONVERSATION_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "conversation_id": {"type": "string", "description": "会话 ID，可从工作区或搜索结果得到。"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_READ,
            "description": f"读取最近多少条消息，默认 {DEFAULT_READ}，最多 {MAX_READ}。",
        },
    },
    "required": ["conversation_id"],
}


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _when(value: str) -> str:
    return _collapse(value)[:16].replace("T", " ")


def _string(args: dict, key: str, *, limit: int, field: str) -> str:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ToolInputError(f"{field}不能为空。")
    text = raw.strip()
    if len(text) > limit:
        raise ToolInputError(f"{field}不能超过 {limit} 个字符。")
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


def may_read(ctx: ToolContext, conversation: dict) -> bool:
    """Private threads are only readable by their two participants; the rest by members."""
    if conversation.get("kind") == "private":
        return ctx.agent_id in ctx.store.member_ids(conversation["id"])
    if ctx.is_supervisor:
        return True
    return ctx.agent_id in ctx.store.member_ids(conversation["id"])


def snippet_around(content: str, needle: str) -> str:
    """A one-line window around the first match, with ellipses on the cut sides."""
    flat = _collapse(content)
    lowered = flat.lower()
    index = lowered.find(needle.lower())
    if index < 0:
        return flat[:SNIPPET_CHARS] + ("…" if len(flat) > SNIPPET_CHARS else "")
    start = max(0, index - SNIPPET_PAD)
    end = min(len(flat), index + len(needle) + SNIPPET_PAD)
    piece = flat[start:end]
    if start > 0:
        piece = "…" + piece
    if end < len(flat):
        piece = piece + "…"
    return piece[:SNIPPET_CHARS + 2]


async def run_search_history(ctx: ToolContext, args: dict) -> str:
    query = _string(args, "query", limit=200, field="关键词")
    limit = _count(args, "limit", default=DEFAULT_HISTORY, maximum=MAX_HISTORY, field="返回条数")
    found = ctx.store.search_messages(
        query, agent_id=ctx.agent_id, is_supervisor=ctx.is_supervisor, limit=limit
    )
    if not found:
        return f"没有找到包含「{query}」的历史消息。"
    lines = [f"找到 {len(found)} 条相关消息："]
    for index, row in enumerate(found, start=1):
        lines.append(
            f"{index}. 「{row['conversation_title']}」{row['author_name']}（{_when(row['created_at'])}）"
        )
        lines.append(f" {snippet_around(row['content'], query)}")
    text = cap_text("\n".join(lines), MAX_RESULT_CHARS)
    return ToolResult(text, text_detail(text))


async def run_read_conversation(ctx: ToolContext, args: dict) -> str:
    conversation_id = _string(args, "conversation_id", limit=80, field="会话 ID")
    limit = _count(args, "limit", default=DEFAULT_READ, maximum=MAX_READ, field="消息条数")
    conversation = ctx.store.get_conversation(conversation_id)
    if conversation is None:
        raise ToolInputError(f"会话「{conversation_id}」不存在。")
    if not may_read(ctx, conversation):
        raise ToolInputError("只能读取你参与的会话。")
    messages = [
        message
        for message in ctx.store.list_messages(conversation_id)
        if message.get("kind") != "action" and (message.get("content") or "").strip()
    ]
    recent = messages[-limit:]
    if not recent:
        return f"会话「{conversation['title']}」还没有消息。"
    kind_label = {"group": "群聊", "direct": "私聊", "private": "两位成员的私聊"}.get(
        conversation.get("kind", ""), "会话"
    )
    lines = [f"会话「{conversation['title']}」（{kind_label}），最近 {len(recent)} 条："]
    for index, message in enumerate(recent, start=1):
        body = _collapse(message.get("content") or "")[:600]
        lines.append(f"{index}. {message.get('author_name') or '你'}：{body}")
    text = cap_text("\n".join(lines), MAX_RESULT_CHARS)
    return ToolResult(text, text_detail(text))


def _summary(value: Any, limit: int) -> str:
    text = _collapse(str(value or ""))
    return text if len(text) <= limit else text[: limit - 1] + "…"


MEMORY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_history",
        label="搜索历史",
        description=(
            "在自己的会话历史里按关键词搜索旧消息。用户提到之前说过的事、需要沿用早先的决定时使用。"
            "只会搜到你有权查看的会话。"
        ),
        parameters=SEARCH_HISTORY_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_search_history,
        summary=lambda args: f"搜索历史「{_summary(args.get('query'), 30)}」",
    ),
    ToolSpec(
        name="read_conversation",
        label="读取会话",
        description=(
            "读取一个会话最近的消息，用于回顾上下文或别人的讨论。"
            "不属于你的会话会被拒绝。"
        ),
        parameters=READ_CONVERSATION_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_read_conversation,
        summary=lambda args: "读取会话记录",
    ),
)
