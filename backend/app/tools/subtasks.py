"""``run_subtasks``: split one job into up to five small ones and do them side by side.

Each subtask is a real, separate turn of the calling bot — the same instructions and tools,
a focused brief, its own small budget — run as its own task so two of them really overlap.
The caller gets every subtask's answer back as one tool result, and the one action row
reports each subtask as it settles. A nested subtask may not start subtasks of its own.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ToolError
from ..models import MAX_TITLE_CHARS, trim_field
from .args import model_parser
from .names import RUN_SUBTASKS
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})
RUN_SUBTASKS_LABEL = "并行做小任务"
MAX_SUBTASKS = 5
MAX_BRIEF_CHARS = 2_000
# One answer is a reply, not an essay: what the caller replays is the gist of each subtask.
MAX_ANSWER_CHARS = 2_000
NESTED_REFUSAL = "小任务里不能再开小任务，本次调用未执行；请直接做完这一部分。"

RUN_SUBTASKS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": RUN_SUBTASKS,
        "description": (
            "把一件可以拆开的事分成最多 5 个小任务同时去做，每个小任务是一次独立的专注尝试，"
            "用你同样的身份和工具，各自有自己的一小份调用预算。适合彼此独立、能同时进行的部分；"
            "前后依赖的工作不要拆。每个小任务的结果会一起返回给你，由你汇总。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subtasks": {
                    "type": "array",
                    "description": "小任务列表，1 到 5 个",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string", "description": "一句话说明这部分做什么"},
                            "brief": {
                                "type": "string",
                                "description": "交给这次尝试的完整交代：目标、已知信息、输出成什么样",
                            },
                        },
                        "required": ["title", "brief"],
                    },
                }
            },
            "required": ["subtasks"],
        },
    },
}


class SubtaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    brief: str

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return trim_field(value, field="小任务标题", limit=MAX_TITLE_CHARS)

    @field_validator("brief")
    @classmethod
    def _brief(cls, value: str) -> str:
        return trim_field(value, field="小任务说明", limit=MAX_BRIEF_CHARS)


class RunSubtasksArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtasks: list[SubtaskItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded(self) -> "RunSubtasksArgs":
        if not self.subtasks:
            raise ValueError("至少给出一个小任务。")
        if len(self.subtasks) > MAX_SUBTASKS:
            raise ValueError(f"一次最多 {MAX_SUBTASKS} 个小任务。")
        return self


def handlers() -> list[ToolHandler]:
    return [
        ToolHandler(
            name=RUN_SUBTASKS,
            label=RUN_SUBTASKS_LABEL,
            schema=RUN_SUBTASKS_SCHEMA,
            roles=ROLES,
            parse=model_parser(RunSubtasksArgs),
            run=_run_subtasks,
        )
    ]


async def _run_subtasks(ctx, payload: dict[str, Any]) -> ToolOutcome:
    if ctx.run.subtask_depth:
        raise ToolError(NESTED_REFUSAL)
    if ctx.run_subtasks is None:  # pragma: no cover - the engine always wires it
        raise ToolError("这次运行不能拆小任务，本次调用未执行。")
    items = payload["subtasks"]
    states: list[dict] = [
        {"title": item["title"], "status": "running", "output": ""} for item in items
    ]

    def progress() -> None:
        done = sum(1 for state in states if state["status"] != "running")
        summary = f"正在并行做 {len(states)} 个小任务"
        if done:
            summary += f"，已完成 {done} 个"
        updated = ctx.store.update_action_progress(
            ctx.action["id"],
            summary=summary,
            detail={"kind": "subtasks", "steps": [dict(state) for state in states]},
        )
        if updated is not None:
            ctx.publish_message("message_end", updated[1])

    progress()
    results = await ctx.run_subtasks(items, states, progress)
    failed = sum(1 for result in results if result["status"] == "failed")
    summary = (
        f"并行完成了 {len(results)} 个小任务"
        if failed == 0
        else f"并行做了 {len(results)} 个小任务，{failed} 个没成功"
    )
    answer_rows = [
        {
            "title": result["title"],
            "status": result["status"],
            "answer": _clip(result.get("answer") or result.get("error") or ""),
        }
        for result in results
    ]
    return ToolOutcome(
        summary=summary,
        result={"ok": failed == 0, "subtasks": answer_rows},
        detail={
            "kind": "subtasks",
            "steps": [
                {
                    "title": result["title"],
                    "status": result["status"],
                    "output": _clip(result.get("answer") or result.get("error") or ""),
                }
                for result in results
            ],
        },
    )


def _clip(text: str) -> str:
    text = str(text).strip()
    if len(text) <= MAX_ANSWER_CHARS:
        return text
    return text[:MAX_ANSWER_CHARS] + "\n…（小任务的回答过长，已截断）"
