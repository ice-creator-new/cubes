"""The four schedule tools: their schemas, models and handlers.

Building, editing and deleting schedules all ask the user first; listing never does. A run a
schedule itself started is offered none of them, so a schedule can never make another one.
"""

from __future__ import annotations

from .. import permissions

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from .. import schedules
from ..approvals import SCHEDULE_UNATTENDED_REFUSAL, Card, ask
from ..errors import ToolError
from ..models import MAX_NAME_CHARS, MAX_TITLE_CHARS, MAX_TOOL_TEXT_CHARS, trim_field
from ..prompts import abstract
from . import resolve
from .args import model_parser
from .names import (
    SCHEDULE_CREATE,
    SCHEDULE_DELETE,
    SCHEDULE_LIST,
    SCHEDULE_TOOLS,
    SCHEDULE_UPDATE,
)
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor"})
# How much of the task prompt the confirmation card quotes.
SCHEDULE_PROMPT_CHARS = 120

TOOL_LABELS: dict[str, str] = {
    SCHEDULE_CREATE: "创建定时任务",
    SCHEDULE_UPDATE: "修改定时任务",
    SCHEDULE_LIST: "查看定时任务",
    SCHEDULE_DELETE: "删除定时任务",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": SCHEDULE_CREATE,
            "description": (
                "建立一个定时任务：到了设定的时间，团队会在后台、用户看不到的地方把 prompt 交给这个"
                "任务的主人去做，用户看不到这条指令和中间步骤；做完由主人在会话里发一小段标着任务名的总结。"
                "触发方式三选一：once（写 at，本地时间 YYYY-MM-DDTHH:MM）、"
                "interval（写 every_minutes，最少 5 分钟）、cron（写 5 段表达式，如 0 9 * * 1-5）。"
                "这个操作会先询问用户，只有用户同意才会真的建立。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "description": "任务名称，简短明确，会显示在定时任务列表里"},
                    "prompt": {
                        "type": "string",
                        "description": "到点后要做的事：写清楚要做什么、最后总结什么给用户",
                    },
                    "kind": {"type": "string", "description": "触发方式：once、interval 或 cron"},
                    "at": {
                        "type": "string",
                        "description": "kind 为 once 时的触发时间，本地时间 YYYY-MM-DDTHH:MM",
                    },
                    "every_minutes": {
                        "type": "integer",
                        "description": "kind 为 interval 时的间隔分钟数，最少 5",
                    },
                    "cron": {
                        "type": "string",
                        "description": "kind 为 cron 时的 5 段表达式：分 时 日 月 周，例如 0 9 * * 1-5",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "规则所在的时区，默认就是提示词里写明的默认时区，一般不用填",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "在哪个会话里触发；默认就是当前会话",
                    },
                    "use_computer": {
                        "type": "boolean",
                        "description": "到点后是否允许直接使用沙盒电脑（不再弹出允许卡片），默认 false",
                    },
                },
                "required": ["title", "prompt", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SCHEDULE_UPDATE,
            "description": (
                "修改或暂停一个已有的定时任务：可以改名称、内容、触发规则、所在会话、"
                "是否用沙盒电脑；enabled 为 false 就是暂停，为 true 就是恢复。"
                "这个操作会先询问用户，只有用户同意才会真的保存。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schedule_id": {"type": "string", "description": "定时任务 ID，用 schedule_list 查"},
                    "title": {"type": "string", "description": "新的名称"},
                    "prompt": {"type": "string", "description": "到点后发出的新消息"},
                    "kind": {"type": "string", "description": "新的触发方式：once、interval 或 cron"},
                    "at": {"type": "string", "description": "kind 为 once 时的触发时间 YYYY-MM-DDTHH:MM"},
                    "every_minutes": {"type": "integer", "description": "kind 为 interval 时的间隔分钟数"},
                    "cron": {"type": "string", "description": "kind 为 cron 时的 5 段表达式"},
                    "timezone": {"type": "string", "description": "规则所在的时区"},
                    "conversation_id": {"type": "string", "description": "改到哪个会话里触发"},
                    "use_computer": {"type": "boolean", "description": "到点后是否允许使用沙盒电脑"},
                    "enabled": {"type": "boolean", "description": "false 暂停，true 恢复"},
                },
                "required": ["schedule_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SCHEDULE_LIST,
            "description": "列出所有定时任务：触发规则、下次触发时间、所在会话、是否启用和上次结果。",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": SCHEDULE_DELETE,
            "description": (
                "删除一个定时任务，之后不再触发。这个操作会先询问用户，只有用户同意才会真的删除；"
                "已经产生的对话记录会保留。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schedule_id": {"type": "string", "description": "定时任务 ID，用 schedule_list 查"},
                    "reason": {"type": "string", "description": "为什么不再需要这个定时任务，会展示给用户看"},
                },
                "required": ["schedule_id", "reason"],
            },
        },
    },
]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleCreateArgs(_Args):
    title: str
    prompt: str
    kind: str
    at: str | None = None
    every_minutes: int | None = None
    cron: str | None = None
    timezone: str | None = None
    conversation_id: str | None = None
    use_computer: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return trim_field(value, field="定时任务名称", limit=MAX_TITLE_CHARS)

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        return trim_field(value, field="定时任务内容", limit=MAX_TOOL_TEXT_CHARS)

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class ScheduleUpdateArgs(_Args):
    schedule_id: str
    title: str | None = None
    prompt: str | None = None
    kind: str | None = None
    at: str | None = None
    every_minutes: int | None = None
    cron: str | None = None
    timezone: str | None = None
    conversation_id: str | None = None
    use_computer: bool | None = None
    enabled: bool | None = None

    @field_validator("schedule_id")
    @classmethod
    def _schedule_id(cls, value: str) -> str:
        return trim_field(value, field="定时任务 ID", limit=MAX_NAME_CHARS)

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
        return trim_field(value, field="定时任务内容", limit=MAX_TOOL_TEXT_CHARS)


class ScheduleListArgs(_Args):
    pass


class ScheduleDeleteArgs(_Args):
    schedule_id: str
    reason: str

    @field_validator("schedule_id")
    @classmethod
    def _schedule_id(cls, value: str) -> str:
        return trim_field(value, field="定时任务 ID", limit=MAX_NAME_CHARS)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return trim_field(value, field="删除原因", limit=MAX_TOOL_TEXT_CHARS)


_SCHEMAS: dict[str, dict[str, Any]] = {
    schema["function"]["name"]: schema for schema in TOOL_SCHEMAS
}

_ARG_MODELS: dict[str, type[BaseModel]] = {
    SCHEDULE_CREATE: ScheduleCreateArgs,
    SCHEDULE_UPDATE: ScheduleUpdateArgs,
    SCHEDULE_LIST: ScheduleListArgs,
    SCHEDULE_DELETE: ScheduleDeleteArgs,
}


def publish_schedules(store, settings, publish) -> None:
    """Push the schedules panel's rows to every subscriber."""
    publish("schedules", schedules.schedules_payload(store.list_visible_schedules(), settings.timezone))


def _conversation(ctx, conversation_id: Any) -> dict:
    """The conversation a schedule targets, defaulting to the one being spoken in."""
    target = str(conversation_id or "").strip() or ctx.run.conversation_id
    conversation = ctx.store.get_conversation(target)
    if conversation is None or conversation["kind"] == "background":
        raise ToolError(f"会话 ID「{target}」不存在，本次调用未执行。")
    if conversation["kind"] == "private":
        raise ToolError("私聊只能旁观，不能作为定时任务的目标，本次调用未执行。")
    return conversation


def _describe_rule(rule: dict) -> str:
    return schedules.describe(
        kind=rule["kind"],
        at=rule["at"],
        every_minutes=rule["every_minutes"],
        cron=rule["cron"],
    )


def _request_text(
    *,
    verb: str,
    title: str,
    rule: dict,
    conversation_title: str,
    prompt: str,
    use_computer: bool,
    owner_name: str,
) -> str:
    """The confirmation card's sentence: how often, when next, where, what, sandbox?"""
    parts = [f"{verb}定时任务「{title}」：{_describe_rule(rule)}"]
    if rule.get("next_run_at"):
        parts.append(f"，下次 {schedules.next_run_text(rule)}")
    parts.append(f"，在会话「{conversation_title}」里。")
    said = abstract(prompt, SCHEDULE_PROMPT_CHARS).rstrip("。．.")
    parts.append(
        f"到点后团队在后台、你看不到的地方去做：{said}。"
        f"做完由「{owner_name}」在会话里发一小段标着任务名的总结。"
    )
    parts.append(
        "到点后可以直接使用沙盒电脑，不再单独询问。"
        if use_computer
        else "到点后不会使用沙盒电脑。"
    )
    return "".join(parts)


def _finish(
    ctx,
    *,
    status: str,
    summary: str,
    text: str,
    result: dict[str, Any] | None = None,
) -> ToolOutcome:
    """Close a schedule action with the outcome the user's answer produced."""
    payload = result if result is not None else {"ok": False, "error": summary}
    ctx.finish_action(
        ctx.action,
        status=status,
        summary=summary,
        error=None if status == "completed" else summary,
        result=text,
    )
    return ToolOutcome(
        summary=summary, result=payload, text=text, handled=True
    )


async def _create(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Ask the user first, then create the rule exactly as the model described it."""
    now = datetime.now(timezone.utc)
    try:
        rule = schedules.normalize_rule(
            kind=payload["kind"],
            at=payload.get("at"),
            every_minutes=payload.get("every_minutes"),
            cron=payload.get("cron"),
            timezone_name=payload.get("timezone") or ctx.settings.timezone,
            now=now,
        )
    except schedules.ScheduleError as exc:
        raise ToolError(f"{exc}本次调用未执行。") from exc
    conversation = _conversation(ctx, payload.get("conversation_id"))
    if ctx.store.enabled_schedule_count() >= schedules.MAX_ENABLED:
        raise ToolError(
            f"最多只能有 {schedules.MAX_ENABLED} 个启用的定时任务，本次调用未执行。"
        )
    title = payload["title"]
    body = payload["prompt"]
    use_computer = bool(payload.get("use_computer"))
    prompt = _request_text(
        verb="要建立",
        title=title,
        owner_name=resolve.supervisor(ctx.store)["name"],
        rule=rule,
        conversation_title=conversation["title"],
        prompt=body,
        use_computer=use_computer,
    )
    decision = await ask(
        ctx,
        Card(
            prompt=prompt,
            confirm_label="创建",
            deny_label="取消",
            summary=f"建立定时任务「{title}」",
            step=f"等待你确认建立「{title}」",
            grant=permissions.SCHEDULE,
        ),
    )
    if decision == "approve":
        created = ctx.store.create_schedule(
            title=title,
            prompt=body,
            conversation_id=conversation["id"],
            conversation_title=conversation["title"],
            kind=rule["kind"],
            at=rule["at"],
            every_minutes=rule["every_minutes"],
            cron=rule["cron"],
            timezone_name=rule["timezone"],
            use_computer=use_computer,
            next_run_at=rule["next_run_at"],
            created_by=resolve.supervisor(ctx.store)["name"],
            owner_agent_id=resolve.supervisor(ctx.store)["id"],
        )
        publish_schedules(ctx.store, ctx.settings, ctx.publish)
        summary = f"建立了定时任务「{title}」"
        text = (
            f"已建立定时任务「{title}」（ID {created['id']}）：{_describe_rule(created)}，"
            f"下次 {schedules.next_run_text(created)}，在会话「{conversation['title']}」里。"
        )
        return _finish(
            ctx,
            status="completed",
            summary=summary,
            text=text,
            result={
                "ok": True,
                "schedule_id": created["id"],
                "schedule": schedules.schedule_payload(created),
            },
        )
    if decision in ("deny", "stop"):
        summary = f"你取消了建立定时任务「{title}」"
        return _finish(
            ctx,
            status="completed",
            summary=summary,
            text=summary,
            result={"ok": False, "denied": True},
        )
    summary = f"等待确认超时，定时任务「{title}」没有建立。"
    return _finish(ctx, status="error", summary=summary, text=summary)


async def _update(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Ask the user first, then save exactly the fields the model changed."""
    schedule = ctx.store.get_schedule(str(payload["schedule_id"]))
    if schedule is None:
        raise ToolError(
            f"找不到定时任务「{payload['schedule_id']}」，先用 schedule_list 查，本次调用未执行。"
        )
    now = datetime.now(timezone.utc)
    updates: dict[str, Any] = {}
    if payload.get("title"):
        updates["title"] = payload["title"]
    if payload.get("prompt"):
        updates["prompt"] = payload["prompt"]
    if payload.get("conversation_id"):
        target = _conversation(ctx, payload["conversation_id"])
        updates["conversation_id"] = target["id"]
        updates["conversation_title"] = target["title"]
    rule_changed = any(
        payload.get(key) is not None
        for key in ("kind", "at", "every_minutes", "cron", "timezone")
    )
    current = schedules.rule_of(schedule)
    if rule_changed:
        try:
            normalized = schedules.normalize_rule(
                kind=payload.get("kind") or schedule["kind"],
                at=payload.get("at") if payload.get("at") is not None else schedule["at"],
                every_minutes=(
                    payload.get("every_minutes")
                    if payload.get("every_minutes") is not None
                    else schedule["every_minutes"]
                ),
                cron=payload.get("cron") if payload.get("cron") is not None else schedule["cron"],
                timezone_name=payload.get("timezone") or schedule["timezone"],
                now=now,
            )
        except schedules.ScheduleError as exc:
            raise ToolError(f"{exc}本次调用未执行。") from exc
        for key in ("kind", "at", "every_minutes", "cron", "timezone", "next_run_at"):
            updates[key] = normalized[key]
        current = {key: normalized[key] for key in ("kind", "at", "every_minutes", "cron", "timezone")}
    if payload.get("use_computer") is not None:
        updates["use_computer"] = bool(payload["use_computer"])
    enabled = payload.get("enabled")
    if enabled is not None:
        if enabled and not schedule["enabled"]:
            if (
                ctx.store.enabled_schedule_count(exclude_id=schedule["id"])
                >= schedules.MAX_ENABLED
            ):
                raise ToolError(
                    f"最多只能有 {schedules.MAX_ENABLED} 个启用的定时任务，本次调用未执行。"
                )
            if not rule_changed:
                try:
                    updates["next_run_at"] = schedules.next_run_on_resume(current, now)
                except schedules.ScheduleError as exc:
                    raise ToolError(f"{exc}本次调用未执行。") from exc
        updates["enabled"] = bool(enabled)
        if not enabled:
            updates["queued"] = False
    if not updates:
        raise ToolError("没有要修改的内容，本次调用未执行。")
    merged = {**schedule, **updates}
    title = merged["title"]
    owner = ctx.store.get_agent(merged["owner_agent_id"] or "") or resolve.supervisor(ctx.store)
    prompt = _request_text(
        verb="要保存",
        title=title,
        owner_name=owner["name"],
        rule={"next_run_at": merged["next_run_at"], **schedules.rule_of(merged)},
        conversation_title=merged["conversation_title"],
        prompt=merged["prompt"],
        use_computer=bool(merged["use_computer"]),
    )
    if merged["enabled"]:
        prompt += "保存后按新的规则继续触发。"
    else:
        prompt += "保存后它处于暂停状态，不会触发。"
    decision = await ask(
        ctx,
        Card(
            prompt=prompt,
            confirm_label="保存",
            deny_label="取消",
            summary=f"修改定时任务「{title}」",
            step=f"等待你确认修改「{title}」",
            grant=permissions.SCHEDULE,
        ),
    )
    if decision == "approve":
        updated = ctx.store.update_schedule(schedule["id"], **updates)
        if updated is None:  # pragma: no cover - resolved moments ago
            summary = "定时任务不存在。"
            return _finish(
                ctx, status="error", summary=summary, text=summary
            )
        publish_schedules(ctx.store, ctx.settings, ctx.publish)
        summary = f"保存了定时任务「{updated['title']}」"
        state = "已启用" if updated["enabled"] else "已暂停"
        text = (
            f"已保存定时任务「{updated['title']}」（ID {updated['id']}）："
            f"{_describe_rule(updated)}，下次 {schedules.next_run_text(updated)}，"
            f"在会话「{updated['conversation_title']}」里，{state}。"
        )
        return _finish(
            ctx,
            status="completed",
            summary=summary,
            text=text,
            result={
                "ok": True,
                "schedule_id": updated["id"],
                "schedule": schedules.schedule_payload(updated),
            },
        )
    if decision in ("deny", "stop"):
        summary = f"你取消了修改定时任务「{title}」"
        return _finish(
            ctx,
            status="completed",
            summary=summary,
            text=summary,
            result={"ok": False, "denied": True},
        )
    summary = f"等待确认超时，定时任务「{title}」没有修改。"
    return _finish(ctx, status="error", summary=summary, text=summary)


async def _list(ctx, payload: dict[str, Any] | None = None) -> ToolOutcome:
    """Every schedule as readable Chinese text, ids included so the model can edit one."""
    rows = ctx.store.list_visible_schedules()
    if not rows:
        return ToolOutcome(
            summary="查看了定时任务",
            result={"ok": True, "schedules": []},
            text="现在没有任何定时任务。",
        )
    status_text = {
        "completed": "已完成",
        "error": "出错",
        "stopped": "已停止",
        "limited": "超出预算",
        "missed": "错过",
    }
    lines: list[str] = []
    for row in rows:
        state = "已启用" if row["enabled"] else "已暂停"
        if row["queued"]:
            state += "，正在排队等会话空闲"
        last = ""
        if row["last_status"]:
            last = f"；上次{status_text.get(str(row['last_status']), str(row['last_status']))}"
            if row["last_error"]:
                last += f"：{abstract(str(row['last_error']), 80)}"
        lines.append(
            f"- ID {row['id']}，{row['title']}，{_describe_rule(row)}，"
            f"下次 {schedules.next_run_text(row)}（{row['timezone']}），"
            f"会话「{row['conversation_title']}」，{state}，已运行 {row['run_count']} 次{last}"
        )
    return ToolOutcome(
        summary=f"查看了 {len(rows)} 个定时任务",
        result={"ok": True, "schedules": [schedules.schedule_payload(row) for row in rows]},
        text="\n".join(lines),
    )


async def _delete(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Ask the user first, then delete the rule; the history it produced stays."""
    schedule = ctx.store.get_schedule(str(payload["schedule_id"]))
    if schedule is None:
        raise ToolError(
            f"找不到定时任务「{payload['schedule_id']}」，先用 schedule_list 查，本次调用未执行。"
        )
    title = schedule["title"]
    prompt = (
        f"要删除定时任务「{title}」：{_describe_rule(schedule)}，"
        f"下次 {schedules.next_run_text(schedule)}，在会话「{schedule['conversation_title']}」里。"
        f"删除后不再触发，已经产生的对话记录会保留。原因：{payload['reason']}"
    )
    decision = await ask(
        ctx,
        Card(
            prompt=prompt,
            confirm_label="删除",
            deny_label="取消",
            summary=f"删除定时任务「{title}」",
            step=f"等待你确认删除「{title}」",
            grant=permissions.SCHEDULE,
        ),
    )
    if decision == "approve":
        removed = ctx.store.delete_schedule(schedule["id"])
        if removed is None:  # pragma: no cover - resolved moments ago
            summary = "定时任务不存在。"
            return _finish(
                ctx, status="error", summary=summary, text=summary
            )
        publish_schedules(ctx.store, ctx.settings, ctx.publish)
        summary = f"已删除定时任务「{title}」"
        return _finish(
            ctx,
            status="completed",
            summary=summary,
            text=summary + "以后不会再触发。",
            result={"ok": True, "schedule_id": schedule["id"], "title": title},
        )
    if decision in ("deny", "stop"):
        summary = f"你取消了删除定时任务「{title}」"
        return _finish(
            ctx,
            status="completed",
            summary=summary,
            text=summary,
            result={"ok": False, "denied": True},
        )
    summary = f"等待确认超时，定时任务「{title}」没有删除。"
    return _finish(ctx, status="error", summary=summary, text=summary)


_RUNNERS = {
    SCHEDULE_CREATE: _create,
    SCHEDULE_UPDATE: _update,
    SCHEDULE_LIST: _list,
    SCHEDULE_DELETE: _delete,
}


def handlers() -> list[ToolHandler]:
    """Every schedule tool, in prompt order."""
    return [_handler(name) for name in SCHEDULE_TOOLS]


def _handler(name: str) -> ToolHandler:
    return ToolHandler(
        name=name,
        label=TOOL_LABELS[name],
        schema=_SCHEMAS[name],
        roles=ROLES,
        parse=model_parser(_ARG_MODELS[name]),
        run=_RUNNERS[name],
        unattended_refusal=SCHEDULE_UNATTENDED_REFUSAL,
        # No recursion: a run a schedule started is never offered these.
        offered_when=lambda offer: not offer.scheduled,
    )
