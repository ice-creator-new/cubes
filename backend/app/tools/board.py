"""The progress board: a working turn's to-do list, and every live lane's board.

``todo_write`` belongs to the lane doing the job. It replaces the whole list at once, so
the board reads as the bot's own current plan instead of an append-only log. ``team_progress``
is the read side: a reply lane answers "做到哪了" from the boards of every live lane in the
conversation, never from memory.
"""

from __future__ import annotations

from typing import Any

from ..errors import ToolError
from ..toolkit import ToolInputError
from .args import parse_json_object
from .names import TEAM_PROGRESS, TODO_WRITE
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})
TODO_STATUSES = ("pending", "doing", "done", "dropped")
MAX_TODOS = 12
MAX_TODO_CHARS = 60
TODO_WRITE_LABEL = "写进度"
TEAM_PROGRESS_LABEL = "看进度"

TODO_WRITE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TODO_WRITE,
        "description": (
            "写下或更新这次工作的待办清单（整张表替换）。开工前先把要做的几步写下来，"
            "每完成一步就更新状态；正在做的那条保持 doing，且只能有一条。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "maxItems": MAX_TODOS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": f"一条待办，一句话，不超过 {MAX_TODO_CHARS} 字",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(TODO_STATUSES),
                                "description": "pending 待做、doing 正在做、done 已完成、dropped 不做了",
                            },
                        },
                        "required": ["text", "status"],
                    },
                }
            },
            "required": ["items"],
        },
    },
}

TEAM_PROGRESS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TEAM_PROGRESS,
        "description": (
            "读取这个会话里每一条仍在进行的工作：谁的、在做什么、待办完成了多少、最近的步骤。"
            "回答「做到哪了」之前先读它，不要凭记忆猜。"
        ),
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
    },
}

def handlers() -> list[ToolHandler]:
    return [
        ToolHandler(
            name=TODO_WRITE,
            label=TODO_WRITE_LABEL,
            schema=TODO_WRITE_SCHEMA,
            roles=ROLES,
            parse=_todo_parser,
            run=_todo_write,
        ),
        ToolHandler(
            name=TEAM_PROGRESS,
            label=TEAM_PROGRESS_LABEL,
            schema=TEAM_PROGRESS_SCHEMA,
            roles=ROLES,
            parse=_progress_parser,
            run=_team_progress,
        ),
    ]


def _todo_parser(raw: str | None) -> dict[str, Any]:
    payload = parse_json_object(raw, label="待办清单")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ToolInputError("待办清单需要 items 数组，本次调用未执行。")
    if not raw_items:
        raise ToolInputError("待办清单不能为空，本次调用未执行。")
    if len(raw_items) > MAX_TODOS:
        raise ToolInputError(f"待办最多 {MAX_TODOS} 条，本次调用未执行。")
    items: list[dict[str, str]] = []
    doing = 0
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ToolInputError("每一条待办都要写 text 和 status，本次调用未执行。")
        text = " ".join(str(raw_item.get("text") or "").split())
        status = str(raw_item.get("status") or "").strip().lower()
        if not text:
            raise ToolInputError("每一条待办都要有一句话，本次调用未执行。")
        if len(text) > MAX_TODO_CHARS:
            raise ToolInputError(
                f"一条待办不超过 {MAX_TODO_CHARS} 字，本次调用未执行。"
            )
        if status not in TODO_STATUSES:
            raise ToolInputError(
                f"待办状态只能是 {'、'.join(TODO_STATUSES)}，本次调用未执行。"
            )
        if status == "doing":
            doing += 1
        items.append({"text": text, "status": status})
    if doing > 1:
        raise ToolInputError("只能有一条待办是 doing，本次调用未执行。")
    return {"items": items}

async def _todo_write(context: Any, arguments: dict[str, Any]) -> ToolOutcome:
    activity_id = context.activity_id
    if not activity_id:
        raise ToolError("现在没有可写进度的工作，本次调用未执行。")
    items = context.store.replace_activity_todos(activity_id, arguments["items"])
    if context.publish_activity is not None:
        context.publish_activity(activity_id)
    done = sum(1 for item in items if item["status"] == "done")
    return ToolOutcome(
        summary=f"更新了进度：{done}/{len(items)} 条已完成",
        result={"ok": True, "todos": items, "done": done, "total": len(items)},
    )


def _progress_parser(raw: str | None) -> dict[str, Any]:
    parse_json_object(raw, label="看进度")
    return {}

async def _team_progress(context: Any, arguments: dict[str, Any]) -> ToolOutcome:
    conversation_id = str(context.conversation["id"])
    activities: list[dict[str, Any]] = []
    for activity in context.store.live_activities(conversation_id):
        activity_id = str(activity["id"])
        todos = context.store.list_activity_todos(activity_id)
        activities.append(
            {
                "activity_id": activity_id,
                "agent": activity["agent_name"],
                "title": activity["title"],
                "status": activity["status"],
                "current_step": activity["current_step"],
                "latest": activity["latest_text"],
                "todos": todos,
                "todo_done": sum(1 for item in todos if item["status"] == "done"),
                "todo_total": len(todos),
                "recent_steps": context.store.activity_recent_steps(activity_id, 3),
            }
        )
    return ToolOutcome(
        summary=f"看了 {len(activities)} 条进行中的工作",
        result={"activities": activities},
    )
