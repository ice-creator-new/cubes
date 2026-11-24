"""``ask_user``: a bot stops and asks the user up to four questions, and the run waits.

The questions are parked exactly like a confirmation card, so the run pauses instead of
guessing, the sidebar lists it among the things waiting on the user, and the composer shows
the picker. Unlike a card there is no timeout: a question is the user's to answer whenever
they get to it, and silence never decides anything.

The shape is Claude Code's ``AskUserQuestion``: each question owns one choice, every
question automatically ends with 「其他」 for the user's own words, and the bot may name
one option as the recommendation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ToolError
from ..models import MAX_NAME_CHARS, MAX_TOOL_TEXT_CHARS, trim_field
from .args import model_parser
from .names import ASK_USER
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})
ASK_USER_LABEL = "问用户"
MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_HEADER_CHARS = 12
MAX_DESCRIPTION_CHARS = 160
OTHER_LABEL = "其他"
# A run nobody watches has no one to answer; the bot is told to decide for itself instead.
UNATTENDED_REFUSAL = (
    "定时任务在后台运行，没有人能回答这个问题，本次调用未执行；请按你最好的判断继续。"
)
PRIVATE_REFUSAL = "私聊里用户无法回答你的问题，本次调用未执行；请按你最好的判断继续。"
WORKFLOW_REFUSAL = "工作流页面里请在流程中安排提问，本次调用未执行。"
EXPIRED = "提问已失效，本次调用未执行。"
STOPPED = "任务已停止，这个问题没有答案。"
SKIPPED = "用户跳过了这个问题"
SKIPPED_ALL = "用户跳过了这些问题"

ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_USER,
        "description": (
            "停下来问用户 1–4 个问题，运行会暂停，直到用户回答。只在决定权在用户手里时用："
            "要他在几个方案里选、要他补充只有他知道的信息、要他确认一个偏好。"
            "自己能查、能判断的不要问。每个问题给 2–4 个具体选项，把推荐的一个放第一个并标成 "
            "recommended；系统会自动加「其他」，不要自己写。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "一次最多问 4 个问题，每个问题一个选择",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "完整的一句话问题，说明为什么需要他来决定",
                            },
                            "header": {
                                "type": "string",
                                "description": "标签，最多 12 个字，例如「方案」「时间」",
                            },
                            "options": {
                                "type": "array",
                                "description": "2–4 个具体选项",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "简短选项，一两个词",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "这个选择的含义或后果，一句话",
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multi": {
                                "type": "boolean",
                                "description": "是否允许多选；默认单选",
                            },
                            "recommended": {
                                "type": "string",
                                "description": "推荐选项的 label，必须和 options 里的一个完全一致",
                            },
                        },
                        "required": ["question", "header", "options"],
                    },
                }
            },
            "required": ["questions"],
        },
    },
}


class AskOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    description: str = ""

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        label = trim_field(value, field="选项", limit=MAX_NAME_CHARS)
        if label == OTHER_LABEL:
            raise ValueError("「其他」由系统自动提供，不要在选项里自己写。")
        return label

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return trim_field(value, field="选项说明", limit=MAX_DESCRIPTION_CHARS)


class AskQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    header: str
    options: list[AskOption] = Field(default_factory=list)
    multi: bool = False
    recommended: str | None = None

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        return trim_field(value, field="问题", limit=MAX_TOOL_TEXT_CHARS)

    @field_validator("header")
    @classmethod
    def _header(cls, value: str) -> str:
        return trim_field(value, field="问题标签", limit=MAX_HEADER_CHARS)

    @field_validator("recommended")
    @classmethod
    def _recommended(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return trim_field(value, field="推荐选项", limit=MAX_NAME_CHARS)

    @model_validator(mode="after")
    def _answerable(self) -> "AskQuestion":
        if not MIN_OPTIONS <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"每个问题要给 {MIN_OPTIONS}–{MAX_OPTIONS} 个选项。")
        labels = [option.label for option in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError("同一个问题里的选项名字不能重复。")
        if self.recommended is not None and self.recommended not in labels:
            raise ValueError("推荐的选项必须是这个问题选项里的一个。")
        return self


class AskUserArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[AskQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _count(self) -> "AskUserArgs":
        if not 1 <= len(self.questions) <= MAX_QUESTIONS:
            raise ValueError(f"一次要问 1–{MAX_QUESTIONS} 个问题。")
        return self


def handlers() -> list[ToolHandler]:
    return [
        ToolHandler(
            name=ASK_USER,
            label=ASK_USER_LABEL,
            schema=ASK_USER_SCHEMA,
            roles=ROLES,
            parse=model_parser(AskUserArgs),
            run=_ask_user,
            unattended_refusal=UNATTENDED_REFUSAL,
            offered_when=_can_ask,
            callable_when=_can_ask,
        )
    ]


def _can_ask(offer) -> bool:
    """Where a question may be called: the user's chats, and a scheduled run's hidden one.

    The hidden workspace keeps the tool on the list so a scheduled bot that calls it gets the
    policy's own refusal sentence, instead of "unknown tool" it cannot learn anything from.
    Private bot chats and workflow pages answer elsewhere and do not offer it at all.
    """
    if offer.conversation is None:
        return True
    return offer.conversation["kind"] in ("direct", "group", "background", "thread")


def _answer_lines(questions: list[dict], answers: list[dict]) -> list[str]:
    """One line per question: its header, the chosen labels, and any words of their own."""
    lines: list[str] = []
    for index, question in enumerate(questions):
        header = str(question.get("header") or "")
        answer = next(
            (item for item in answers if item.get("question_index") == index),
            {},
        )
        labels = [str(label) for label in answer.get("labels") or []]
        other = str(answer.get("other") or "").strip()
        if not labels and not other:
            lines.append(f"{header}：{SKIPPED}")
            continue
        # The user's own words belong to this question: quoted after the picks, not set off by
        # 「；」, which is also what separates one question from the next in the summary row.
        picked = [*labels, f"「{other}」"] if other else labels
        lines.append(f"{header}：" + "、".join(picked))
    return lines


def _skip_lines(questions: list[dict]) -> list[str]:
    return [f"{str(question.get('header') or '')}：{SKIPPED}" for question in questions]


async def _ask_user(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Park the action row on the questions and wait however long the user needs."""
    if ctx.conversation["kind"] == "private":
        raise ToolError(PRIVATE_REFUSAL)
    if ctx.conversation["kind"] == "workflow":
        raise ToolError(WORKFLOW_REFUSAL)
    questions = payload["questions"]
    prompt = "；".join(str(question["question"]) for question in questions)
    confirmation = {
        "kind": "question",
        "prompt": prompt,
        "confirm_label": "",
        "deny_label": "跳过",
        "questions": questions,
    }
    marked = ctx.store.mark_action_awaiting(
        ctx.action["id"], summary=prompt, confirmation=confirmation
    )
    if marked is None:  # pragma: no cover - the action was created moments ago
        raise ToolError(EXPIRED)
    ctx.publish_message("message_end", marked[1])
    ctx.set_step(
        "等待你回答一个问题" if len(questions) == 1 else f"等待你回答 {len(questions)} 个问题"
    )
    ctx.publish_decisions()
    ctx.refresh_task()

    answer = await ctx.await_answer(ctx.action["id"])
    ctx.refresh_task()
    if answer is None or answer.get("stopped"):
        return ToolOutcome(
            summary="这个问题没有得到回答，任务已停止。",
            result={"ok": False, "stopped": True, "message": STOPPED},
        )
    if answer.get("skip"):
        lines = _skip_lines(questions)
        return ToolOutcome(
            summary=SKIPPED_ALL,
            result={
                "ok": True,
                "skipped": True,
                "questions": questions,
                "message": "\n".join(lines)
                + "\n请按你最好的判断继续，并在回复里说明你假设了什么。",
            },
        )
    answers = [dict(item) for item in answer.get("answers") or []]
    lines = _answer_lines(questions, answers)
    return ToolOutcome(
        summary="；".join(lines),
        result={
            "ok": True,
            "questions": questions,
            "answers": answers,
            "message": "用户的回答：\n" + "\n".join(lines),
        },
    )
