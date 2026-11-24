"""The self-reminder tools every bot can use: set one, and cancel one.

A reminder is a one-time schedule (``kind="reminder"``) owned by the bot that asked for it.
When it is due the scheduler starts that bot again in the same conversation, from a quiet
``提醒：…`` trigger row, so it can say what it needed to say or keep working. It is listed in
the 定时任务 panel under its own heading and can be deleted there like any other rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .. import schedules
from ..errors import ToolError
from ..models import MAX_NAME_CHARS, MAX_TOOL_TEXT_CHARS, trim_field
from .args import model_parser
from .names import CANCEL_REMINDER, REMIND_ME
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})
# One bot may keep this many reminders waiting; past it, a new one is refused.
MAX_PENDING_REMINDERS = 20
MAX_REMIND_MINUTES = 60 * 24 * 366

TOOL_LABELS: dict[str, str] = {
    REMIND_ME: "设置提醒",
    CANCEL_REMINDER: "取消提醒",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": REMIND_ME,
            "description": (
                "给自己设一个提醒：到点后你会在当前会话里被重新唤起，看到「提醒：…」这条消息，"
                "然后接着把该做的事做完或把该说的话说出来。"
                "用 in_minutes 表示多少分钟后，或用 at 表示本地时间（YYYY-MM-DDTHH:MM），二者填一个。"
                f"每个机器人最多同时保留 {MAX_PENDING_REMINDERS} 个提醒。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "in_minutes": {
                        "type": "integer",
                        "description": "多少分钟后提醒，例如 30",
                    },
                    "at": {
                        "type": "string",
                        "description": "本地提醒时间，格式 YYYY-MM-DDTHH:MM，例如 2026-11-21T09:00",
                    },
                    "note": {
                        "type": "string",
                        "description": "提醒的内容：到时你要想起来做什么、说什么",
                    },
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": CANCEL_REMINDER,
            "description": "取消自己设过的一个还没到点的提醒。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reminder_id": {
                        "type": "string",
                        "description": "提醒 ID，可以先用 schedule_list 查看",
                    }
                },
                "required": ["reminder_id"],
            },
        },
    },
]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemindArgs(_Args):
    note: str
    in_minutes: int | None = None
    at: str | None = None

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        return trim_field(value, field="提醒内容", limit=MAX_TOOL_TEXT_CHARS)

    @field_validator("in_minutes")
    @classmethod
    def _minutes(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1 or value > MAX_REMIND_MINUTES:
            raise ValueError(f"分钟数需要在 1 到 {MAX_REMIND_MINUTES} 之间。")
        return int(value)

    @field_validator("at")
    @classmethod
    def _at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def _one_time(self) -> "RemindArgs":
        if (self.in_minutes is None) == (self.at is None):
            raise ValueError("in_minutes 和 at 必须填且只填一个。")
        return self


class CancelRemindArgs(_Args):
    reminder_id: str

    @field_validator("reminder_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return trim_field(value, field="提醒 ID", limit=MAX_NAME_CHARS)


_SCHEMAS: dict[str, dict[str, Any]] = {
    schema["function"]["name"]: schema for schema in TOOL_SCHEMAS
}

_ARG_MODELS: dict[str, type[BaseModel]] = {
    REMIND_ME: RemindArgs,
    CANCEL_REMINDER: CancelRemindArgs,
}


def _due_moment(payload: dict[str, Any], now: datetime, zone) -> tuple[datetime, str]:
    """The reminder's due instant and the local ``YYYY-MM-DDTHH:MM`` that names it."""
    if payload.get("in_minutes") is not None:
        due = now + timedelta(minutes=int(payload["in_minutes"]))
    else:
        text = str(payload["at"])
        try:
            naive = datetime.fromisoformat(text.replace("Z", ""))
        except ValueError as exc:
            raise ValueError("提醒时间格式应为 2026-11-21T09:00。") from exc
        due = naive.replace(tzinfo=zone)
        if due <= now:
            raise ValueError("提醒时间必须晚于现在。")
    return due, due.astimezone(zone).strftime(schedules.AT_FORMAT)


def _reminder_title(note: str) -> str:
    collapsed = " ".join(note.split())
    return collapsed if len(collapsed) <= 36 else collapsed[:35] + "…"


async def _remind(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Store one one-shot reminder owned by the bot that is speaking."""
    if ctx.store.pending_reminder_count(ctx.author["id"]) >= MAX_PENDING_REMINDERS:
        raise ToolError(
            f"你已经有 {MAX_PENDING_REMINDERS} 个待触发的提醒，先取消一个再设新的，本次调用未执行。"
        )
    now = datetime.now(timezone.utc)
    zone = schedules.zone_of(ctx.settings.timezone)
    try:
        due, at_local = _due_moment(payload, now, zone)
    except ValueError as exc:
        raise ToolError(f"{exc}本次调用未执行。") from exc
    note = payload["note"]
    title = _reminder_title(note)
    created = ctx.store.create_schedule(
        title=title,
        prompt=note,
        conversation_id=ctx.conversation["id"],
        conversation_title=ctx.conversation["title"],
        kind="reminder",
        at=at_local,
        every_minutes=None,
        cron=None,
        timezone_name=ctx.settings.timezone,
        use_computer=False,
        next_run_at=schedules.utc_iso(due),
        created_by=ctx.author["name"],
        owner_agent_id=ctx.author["id"],
    )
    ctx.publish_schedules()
    when = schedules.local_time(due, ctx.settings.timezone)
    summary = f"记下了提醒「{title}」"
    text = (
        f"已设好提醒（ID {created['id']}）：{when} 起，我会在这个会话里想起并接着处理：{note}"
    )
    return ToolOutcome(
        summary=summary,
        result={"ok": True, "reminder_id": created["id"], "schedule": schedules.schedule_payload(created)},
        text=text,
    )


async def _cancel(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Delete one reminder this bot owns; another bot's reminder is refused."""
    schedule = ctx.store.get_schedule(str(payload["reminder_id"]))
    if schedule is None or schedule["kind"] != "reminder":
        raise ToolError(
            f"找不到提醒「{payload['reminder_id']}」，先用 schedule_list 查，本次调用未执行。"
        )
    if schedule["owner_agent_id"] != ctx.author["id"]:
        raise ToolError("这个提醒不是你设的，不能取消，本次调用未执行。")
    removed = ctx.store.delete_schedule(schedule["id"])
    if removed is None:  # pragma: no cover - resolved moments ago
        raise ToolError("这个提醒已经不在了，本次调用未执行。")
    ctx.publish_schedules()
    return ToolOutcome(
        summary=f"取消了提醒「{schedule['title']}」",
        result={"ok": True, "reminder_id": schedule["id"]},
        text=f"已取消提醒「{schedule['title']}」，它不会再触发。",
    )


_RUNNERS = {REMIND_ME: _remind, CANCEL_REMINDER: _cancel}


def handlers() -> list[ToolHandler]:
    """Every reminder tool; both roles may use them."""
    return [
        ToolHandler(
            name=name,
            label=TOOL_LABELS[name],
            schema=_SCHEMAS[name],
            roles=ROLES,
            parse=model_parser(_ARG_MODELS[name]),
            run=_RUNNERS[name],
            # A run a schedule or reminder started may not schedule another one.
            offered_when=lambda offer: not offer.scheduled,
        )
        for name in (REMIND_ME, CANCEL_REMINDER)
    ]
