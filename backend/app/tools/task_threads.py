"""``start_task``: the bot itself decides that a request needs its own hidden thread.

In a group, a member answers a question, an opinion or one quick lookup there; when the job
will take several tool steps or a while, it calls this instead. The call ends the bot's turn
with whatever lead-in it already wrote, and the real work runs as a normal run in the task's
thread, side by side with the chat.

A direct chat works the same way, with one difference: the card appears in the chat itself,
and the task waits for the bot's live work lane unless the user starts it right away. The
supervisor may also name an owner (a member), so handing work out never queues behind the
supervisor's own lane.
"""

from __future__ import annotations

from typing import Any

from ..agent.run_state import GROUP_TASK
from ..errors import ToolError
from ..models import MAX_TOOL_TEXT_CHARS
from ..toolkit import ToolInputError
from . import resolve
from .args import parse_json_object
from .names import START_TASK
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})
LABEL = "开一个任务"
MAX_TITLE_CHARS = 30

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": START_TASK,
        "description": (
            "开一个正式任务，交给你的后台工作线程去做。要做几步、要花一会儿的活"
            "（写代码、查资料、操作电脑、做出一个东西）先调用它；一句话能答完的问题、看法或"
            "一次简单查询不要用。调用后你这一轮就结束：你之前写的话作为开场，"
            "任务卡片会立刻出现在当前聊天里，进度也显示在那里。"
            "它默认排在当前聊天里正在做的那件后面。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {
                    "type": "string",
                    "description": f"任务名，卡片上显示，{MAX_TITLE_CHARS} 字以内",
                },
                "plan": {
                    "type": "string",
                    "description": "你打算怎么做（可选），写清步骤和交付什么",
                },
                "owner": {
                    "type": "string",
                    "description": (
                        "交给团队里的哪位成员做（可选，ID 或名字）。只有主管在私聊里分派新活时用；"
                        "不写就由你自己做。"
                    ),
                },
            },
            "required": ["title"],
        },
    },
}


def handlers() -> list[ToolHandler]:
    return [
        ToolHandler(
            name=START_TASK,
            label=LABEL,
            schema=SCHEMA,
            roles=ROLES,
            parse=_parser,
            run=_start,
            offered_when=_in_chat,
            callable_when=_in_chat,
            hands_off=True,
        )
    ]


def _in_chat(offer) -> bool:
    """A group or a direct chat, never a thread that already works a task.

    In a group only a member starts tasks (the supervisor coordinates); in a direct chat the
    one bot there may start one, and the supervisor may hand it to a member by name.
    """
    conversation = offer.conversation
    if conversation is None or offer.from_thread:
        return False
    if conversation["kind"] == "group":
        return offer.author is not None and offer.author["kind"] == "member"
    return conversation["kind"] == "direct"


def _text(value: Any, field: str, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolInputError(f"{field}不能为空，本次调用未执行。")
    text = value.strip()
    if len(text) > MAX_TOOL_TEXT_CHARS:
        raise ToolInputError(f"{field}不能超过 {MAX_TOOL_TEXT_CHARS} 个字符，本次调用未执行。")
    return text


def _parser(raw: str | None) -> dict[str, Any]:
    payload = parse_json_object(raw, label=LABEL)
    title = _text(payload.get("title"), "任务名", required=True)
    if len(title) > MAX_TITLE_CHARS:
        raise ToolInputError(f"任务名不能超过 {MAX_TITLE_CHARS} 个字符，本次调用未执行。")
    return {
        "title": title,
        "plan": _text(payload.get("plan"), "计划", required=False),
        "owner": _text(payload.get("owner"), "要交给的成员", required=False),
    }


def _owner(ctx, ref: str) -> dict:
    """Who does the work: the caller, or the member a supervisor names."""
    if not ref:
        return ctx.author
    if ctx.author["kind"] != "supervisor":
        raise ToolError("只有主管可以把新活交给别的成员，本次调用未执行。")
    agent = resolve.resolve_agent(ctx.store, ref)
    if agent is None:
        raise ToolError(f"成员「{ref}」不存在，本次调用未执行。")
    if agent["kind"] != "member":
        raise ToolError("只能把任务交给团队成员，本次调用未执行。")
    return agent


async def _start(ctx, payload: dict[str, Any]) -> ToolOutcome:
    task_id = GROUP_TASK.get()
    task = ctx.store.get_group_task(task_id) if task_id else None
    # A plain chat turn makes no task row (only this call does), so ``None`` is the normal
    # case; a row exists only when a thread turn calls it, which the tool is not offered in.
    if task is not None:
        if task["conversation_id"] != ctx.conversation["id"]:
            raise ToolError("你这一轮没有在开这个聊天里的任务，无法开任务，本次调用未执行。")
        if task["owner_id"] != ctx.author["id"]:
            raise ToolError("这个任务已经不归你了，本次调用未执行。")
    if ctx.start_task is None:  # pragma: no cover - the engine always binds it
        raise ToolError("任务通道没有就绪，本次调用未执行。")
    owner = _owner(ctx, payload["owner"])
    started = await ctx.start_task(
        task, payload["title"], payload["plan"], owner_id=owner["id"]
    )
    return ToolOutcome(
        summary=f"开了一个任务「{started['title']}」",
        result={
            "ok": True,
            "task_id": started["id"],
            "thread_id": started["thread_id"],
            "title": started["title"],
            "owner": owner["name"],
            "message": "任务已开，你这一轮到此结束；接下来的工作会在任务线程里进行。",
        },
        target_conversation_id=started["thread_id"],
    )
