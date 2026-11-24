"""Group task tools: hand a part of your group task to a member, or hand the whole task over.

Both only work inside a task's own thread, on the task that thread is working on: a plain
group reply has no task to hand on. A part (``group_assign``) is answered in the group by the
member, and its answer comes back to the caller as the tool result; a transfer
(``group_transfer``) ends the caller's turn and the new owner answers next. Limits keep one
request from fanning out without end.
"""

from __future__ import annotations

from typing import Any

from ..agent.run_state import GROUP_TASK
from ..errors import ToolError
from ..models import MAX_TOOL_TEXT_CHARS
from ..store import group_tasks as task_rules
from ..toolkit import ToolInputError
from . import resolve
from .args import parse_json_object
from .names import GROUP_ASSIGN, GROUP_TRANSFER
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})
ASSIGN_LABEL = "分给同事"
TRANSFER_LABEL = "转交任务"
# At most this many parts handed out for one request, and this many parts running at once in
# one group (a part runs inside the caller's turn, so a nested part counts too).
MAX_ASSIGNMENTS_PER_REQUEST = 8
MAX_RUNNING_PARTS = 2

ASSIGN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": GROUP_ASSIGN,
        "description": (
            "把你在这个群里正在做的任务的一部分交给群里另一位成员：对方在群里直接做这一部分，"
            "做完的回答作为工具结果返回给你，你再把各部分合起来回答。只在这一部分确实属于对方的专长时用；"
            f"同一个请求最多分出 {MAX_ASSIGNMENTS_PER_REQUEST} 次。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "agent_id": {"type": "string", "description": "群成员的 ID 或名字"},
                "instruction": {"type": "string", "description": "要对方做的那一部分，写清楚要交回什么"},
            },
            "required": ["agent_id", "instruction"],
        },
    },
}

TRANSFER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": GROUP_TRANSFER,
        "description": (
            "把你在这个群里正在做的整个任务转交给群里另一位成员，由对方负责到底。"
            "调用后你这一轮就结束了，对方接着在群里回答；适合这件事整体更该由对方来做的时候。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "agent_id": {"type": "string", "description": "群成员的 ID 或名字"},
                "note": {"type": "string", "description": "交接时要告诉对方的话（可选）"},
            },
            "required": ["agent_id"],
        },
    },
}


def handlers() -> list[ToolHandler]:
    return [
        ToolHandler(
            name=GROUP_ASSIGN,
            label=ASSIGN_LABEL,
            schema=ASSIGN_SCHEMA,
            roles=ROLES,
            parse=_assign_parser,
            run=_assign,
            offered_when=_in_group,
            callable_when=_in_group,
        ),
        ToolHandler(
            name=GROUP_TRANSFER,
            label=TRANSFER_LABEL,
            schema=TRANSFER_SCHEMA,
            roles=ROLES,
            parse=_transfer_parser,
            run=_transfer,
            offered_when=_in_group,
            callable_when=_in_group,
            hands_off=True,
        ),
    ]


def _in_group(offer) -> bool:
    """Only inside a task's own thread, acting on that thread's group.

    A plain group reply has no task to hand on; offering these there invited the model to
    invent parts of an answer that was one sentence long. The thread is the one place a task
    really exists, so the caller must be ``from_thread`` and the surface must be its group.
    """
    return (
        offer.conversation is not None
        and offer.conversation["kind"] == "group"
        and offer.from_thread
    )


def _agent_arg(payload: dict[str, Any]) -> str:
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ToolInputError("缺少要交给的成员 ID 或名字，本次调用未执行。")
    return agent_id.strip()


def _text_arg(payload: dict[str, Any], key: str, field: str, *, required: bool) -> str:
    value = payload.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolInputError(f"{field}不能为空，本次调用未执行。")
    text = value.strip()
    if len(text) > MAX_TOOL_TEXT_CHARS:
        raise ToolInputError(f"{field}不能超过 {MAX_TOOL_TEXT_CHARS} 个字符，本次调用未执行。")
    return text


def _assign_parser(raw: str | None) -> dict[str, Any]:
    payload = parse_json_object(raw, label=ASSIGN_LABEL)
    return {
        "agent_id": _agent_arg(payload),
        "instruction": _text_arg(payload, "instruction", "要做的部分", required=True),
    }


def _transfer_parser(raw: str | None) -> dict[str, Any]:
    payload = parse_json_object(raw, label=TRANSFER_LABEL)
    return {"agent_id": _agent_arg(payload), "note": _text_arg(payload, "note", "交接说明", required=False)}


def _current_task(ctx) -> dict:
    """The group task the caller's turn works on; a turn with none cannot hand anything on."""
    task_id = GROUP_TASK.get()
    task = ctx.store.get_group_task(task_id) if task_id else None
    if task is None or task["conversation_id"] != ctx.conversation["id"]:
        raise ToolError(
            "分派和转交只在任务线程里可用：你这一轮只是在群里回一句话，"
            "没有可以分出去的任务，本次调用未执行。"
        )
    if task["owner_id"] != ctx.author["id"]:
        raise ToolError("这个任务已经不归你了，本次调用未执行。")
    return task


def _member(ctx, ref: str) -> dict:
    agent = resolve.resolve_agent(ctx.store, ref)
    if agent is None or agent["id"] not in ctx.conversation["member_ids"]:
        raise ToolError(f"「{ref}」不在这个群里，只能交给群成员，本次调用未执行。")
    if agent["id"] == ctx.author["id"]:
        raise ToolInputError("不能交给你自己，本次调用未执行。")
    return agent


def _answers_same_request(ctx, task: dict, agent: dict) -> bool:
    """The member already answers this request in a root task of its own, or is queued to."""
    request = task["request_message_id"]
    if any(agent_id == agent["id"] and reply_to == request for agent_id, reply_to in ctx.run.queued):
        return True
    return any(
        other["owner_id"] == agent["id"] and other["parent_id"] is None and other["id"] != task["id"]
        for other in ctx.store.request_tasks(task["request_message_id"] or "")
    )


async def _assign(ctx, payload: dict[str, Any]) -> ToolOutcome:
    task = _current_task(ctx)
    agent = _member(ctx, payload["agent_id"])
    if agent["kind"] == "supervisor":
        raise ToolError(f"「{agent['name']}」是主管，不能接分出去的部分，本次调用未执行。")
    if _answers_same_request(ctx, task, agent):
        raise ToolError(
            f"「{agent['name']}」已经在自己回答这个请求了，再分给它会让它把同样的话说两遍，本次调用未执行。"
        )
    handed = [
        other for other in ctx.store.request_tasks(task["request_message_id"] or "") if other["parent_id"]
    ]
    if len(handed) >= MAX_ASSIGNMENTS_PER_REQUEST:
        raise ToolError(f"这个请求已经分出去 {MAX_ASSIGNMENTS_PER_REQUEST} 次了，请用已有结果收尾，本次调用未执行。")
    if ctx.store.running_children(ctx.conversation["id"]) >= MAX_RUNNING_PARTS:
        raise ToolError(f"这个群同时最多有 {MAX_RUNNING_PARTS} 个分出去的部分在做，本次调用未执行。")
    if ctx.assign_part is None:  # pragma: no cover - the engine always binds it
        raise ToolError("分派通道没有就绪，本次调用未执行。")
    text, message, child = await ctx.assign_part(ctx.conversation, agent, payload["instruction"], task)
    result: dict[str, Any] = {
        "ok": True,
        "task_id": child["id"],
        "agent_id": agent["id"],
        "message_id": (message or {}).get("id"),
        "content": text,
    }
    if not text:
        result["note"] = f"「{agent['name']}」没有给出文字回答。"
    return ToolOutcome(
        summary=f"把一部分交给了「{agent['name']}」",
        result=result,
        target_agent_id=agent["id"],
    )


async def _transfer(ctx, payload: dict[str, Any]) -> ToolOutcome:
    task = _current_task(ctx)
    agent = _member(ctx, payload["agent_id"])
    if _answers_same_request(ctx, task, agent):
        raise ToolError(
            f"「{agent['name']}」已经在自己回答这个请求了，转给它会让它把同样的话说两遍，本次调用未执行。"
        )
    # The giver is remembered so the new owner is told who handed it over.
    ctx.store.update_group_task(
        task["id"], owner_id=agent["id"], assigned_by=ctx.author["id"], status=task_rules.RUNNING
    )
    ctx.run.transfers.append((task["id"], agent["id"], payload["note"]))
    return ToolOutcome(
        summary=f"把任务转交给了「{agent['name']}」",
        result={"ok": True, "task_id": task["id"], "agent_id": agent["id"], "note": "你这一轮到此结束，对方接着做。"},
        target_agent_id=agent["id"],
    )
