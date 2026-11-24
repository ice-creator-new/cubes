"""The supervisor's management tools: their schemas, models and handlers.

Only the supervisor receives these: they change the team itself. Arguments are validated
server-side against models with ``extra="forbid"`` so a malformed or over-reaching call
becomes a real tool result the model can correct, never a silent no-op. The two that
destroy something park on a confirmation card first.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import schedules
from ..approvals import UNATTENDED_REFUSAL, UNATTENDED_TOOLS, Card, ask
from ..attachments import MAX_ATTACHMENTS_PER_MESSAGE
from ..errors import RunLimited, ToolError
from ..mentions import parse_mentions
from ..models import (
    MAX_INSTRUCTIONS_CHARS,
    MAX_NAME_CHARS,
    MAX_PURPOSE_CHARS,
    MAX_TITLE_CHARS,
    MAX_TOOL_TEXT_CHARS,
    trim_field,
)
from ..errors import AgentDeleteRefused, AgentNameTaken, GroupDissolveRefused
from . import resolve
from .args import clean_ids, model_parser
from .names import (
    ASK_AGENT,
    CREATE_AGENT,
    CREATE_GROUP,
    DELETE_AGENT,
    DISSOLVE_GROUP,
    POST_GROUP_MESSAGE,
    SET_GROUP_MEMBERS,
    TEAM_REPORT,
)
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor"})

TOOL_LABELS: dict[str, str] = {
    CREATE_AGENT: "创建成员",
    CREATE_GROUP: "建立群组",
    SET_GROUP_MEMBERS: "调整群组成员",
    POST_GROUP_MESSAGE: "发布群消息",
    ASK_AGENT: "咨询成员",
    DELETE_AGENT: "删除成员",
    DISSOLVE_GROUP: "解散群组",
    TEAM_REPORT: "团队数据",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": CREATE_AGENT,
            "description": "创建一个新的团队成员。只有调用本工具才代表成员真的被创建。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "description": "成员名称，简短明确，体现这个成员的专业方向"},
                    "instructions": {
                        "type": "string",
                        "description": "该成员的职责指令：它擅长什么、依据什么工作、输出什么",
                    },
                },
                "required": ["name", "instructions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": CREATE_GROUP,
            "description": (
                "建立一个群组，用于让几位成员围绕同一个目标反复协作。先用 list_groups 现有群组里找一找："
                "成员相同、名字相近的群组已经存在时，本工具会直接返回它（existing 为 true），不会重复建。"
                "一次性的问题不要建群，用 ask_agent 或 ask_group。"
                "群里有一条看不见的分发决定每条消息由谁回答；主管默认不在群里，需要它整理进度、检查结果时"
                "才把 include_supervisor 设成 true。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "description": "群组名称，简短明确，体现这个群组要做什么"},
                    "purpose": {"type": "string", "description": "这个群组要达成的目的"},
                    "member_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要加入群组的成员，写名字或 ID 都行；至少一位",
                    },
                    "rules": {
                        "type": "string",
                        "description": "这个群的长期规则（可选），每位成员发言前都会读到",
                    },
                    "include_supervisor": {
                        "type": "boolean",
                        "description": "是否把主管也加进群（默认否）；主管不干活，只整理进度、检查结果、催办任务",
                    },
                    "first_message": {
                        "type": "string",
                        "description": "建好后以主管名义发到群里的第一条消息（可选），用来开场",
                    },
                },
                "required": ["title", "purpose", "member_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SET_GROUP_MEMBERS,
            "description": (
                "调整某个群组的成员：写名字或 ID 都行，主管也可以加进来或移出去，"
                "但群组不能一个人都不剩。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "group_id": {"type": "string", "description": "群组会话 ID"},
                    "member_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "调整后该群组的全部成员（至少一位），名字或 ID",
                    },
                },
                "required": ["group_id", "member_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": POST_GROUP_MESSAGE,
            "description": "以主管身份向某个群组发布任务说明或阶段结论，群成员之后会读到它。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "group_id": {"type": "string", "description": "群组会话 ID"},
                    "content": {"type": "string", "description": "要发布的正文"},
                    "image_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "要附带的图片 ID（att_ 开头，最多 4 个，只能用对话里出现过的）。"
                            "任务和图片有关时一定要附上原图，不要只用文字描述图片"
                        ),
                    },
                },
                "required": ["group_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ASK_AGENT,
            "description": (
                "把一件需要对方专长的具体任务交给某个群组里的一位成员；该成员会真实调用模型，"
                "它的回复会写入该群组，并在工具结果里返回给你。"
                "不要用它分派确认、点赞、表情或寒暄，也不要把同一个任务逐个发给每位成员。"
                "任务和图片有关时，先用 post_group_message 带 image_ids 把原图发到群里，再调用本工具。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "group_id": {"type": "string", "description": "成员所在的群组会话 ID"},
                    "agent_id": {"type": "string", "description": "被咨询的成员 ID"},
                    "task": {"type": "string", "description": "交给该成员的具体任务与期望产出"},
                },
                "required": ["group_id", "agent_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": DELETE_AGENT,
            "description": (
                "删除一名团队成员。这个操作会先询问用户，只有用户同意才会真的删除；"
                "删除后它和用户的私聊、它参与的机器人私聊会一起删除，群组里的历史发言会保留。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "要删除的成员 ID（团队目录里括号中的 id）；也可以直接写名字",
                    },
                    "reason": {"type": "string", "description": "为什么不再需要这个成员，会展示给用户看"},
                },
                "required": ["agent_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": DISSOLVE_GROUP,
            "description": (
                "解散一个群组。这个操作会先询问用户，只有用户同意才会真的解散；"
                "解散后群组和它的聊天记录会一起删除，成员和他们的私聊都会保留。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "要解散的群组 ID（群组目录里括号中的 id）；也可以直接写群组名称",
                    },
                    "reason": {"type": "string", "description": "为什么不再需要这个群组，会展示给用户看"},
                },
                "required": ["group_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TEAM_REPORT,
            "description": (
                "读取团队一段时间的数据，用于写晨报或周报：每个成员的回复数、参与的任务数、失败数、"
                "验收通过/打回次数、token 用量，以及任务完成情况和失败的任务。只读，不会改动任何数据。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "统计周期：day（本日）或 week（本周）",
                    },
                },
                "required": ["period"],
            },
        },
    },
]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateAgentArgs(_Args):
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


class CreateGroupArgs(_Args):
    title: str
    purpose: str
    member_ids: list[str] = Field(min_length=1)
    rules: str = ""
    include_supervisor: bool = False
    first_message: str = ""

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return trim_field(value, field="群组名称", limit=MAX_TITLE_CHARS)

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        return trim_field(value, field="群组目的", limit=MAX_PURPOSE_CHARS)

    @field_validator("member_ids")
    @classmethod
    def _member_ids(cls, value: list[str]) -> list[str]:
        return clean_ids(value)

    @field_validator("rules")
    @classmethod
    def _rules(cls, value: str) -> str:
        text = str(value or "").strip()
        if len(text) > MAX_PURPOSE_CHARS:
            raise ValueError(f"群规则不能超过 {MAX_PURPOSE_CHARS} 个字符。")
        return text

    @field_validator("first_message")
    @classmethod
    def _first_message(cls, value: str) -> str:
        text = str(value or "").strip()
        if len(text) > MAX_TOOL_TEXT_CHARS:
            raise ValueError(f"第一条消息不能超过 {MAX_TOOL_TEXT_CHARS} 个字符。")
        return text


class SetGroupMembersArgs(_Args):
    group_id: str
    member_ids: list[str]

    @field_validator("group_id")
    @classmethod
    def _group_id(cls, value: str) -> str:
        return trim_field(value, field="群组 ID", limit=MAX_NAME_CHARS)

    @field_validator("member_ids")
    @classmethod
    def _member_ids(cls, value: list[str]) -> list[str]:
        return clean_ids(value)


class PostGroupMessageArgs(_Args):
    group_id: str
    content: str
    image_ids: list[str] = Field(default_factory=list)

    @field_validator("group_id")
    @classmethod
    def _group_id(cls, value: str) -> str:
        return trim_field(value, field="群组 ID", limit=MAX_NAME_CHARS)

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        return trim_field(value, field="消息正文", limit=MAX_TOOL_TEXT_CHARS)

    @field_validator("image_ids")
    @classmethod
    def _image_ids(cls, value: list[str]) -> list[str]:
        ids = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError("一次最多附带 4 张图片。")
        return ids


class AskAgentArgs(_Args):
    group_id: str
    agent_id: str
    task: str

    @field_validator("group_id")
    @classmethod
    def _group_id(cls, value: str) -> str:
        return trim_field(value, field="群组 ID", limit=MAX_NAME_CHARS)

    @field_validator("agent_id")
    @classmethod
    def _agent_id(cls, value: str) -> str:
        return trim_field(value, field="成员 ID", limit=MAX_NAME_CHARS)

    @field_validator("task")
    @classmethod
    def _task(cls, value: str) -> str:
        return trim_field(value, field="成员任务", limit=MAX_TOOL_TEXT_CHARS)


class DeleteAgentArgs(_Args):
    agent_id: str
    reason: str

    @field_validator("agent_id")
    @classmethod
    def _agent_id(cls, value: str) -> str:
        return trim_field(value, field="成员 ID", limit=MAX_NAME_CHARS)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return trim_field(value, field="删除原因", limit=MAX_TOOL_TEXT_CHARS)


class DissolveGroupArgs(_Args):
    group_id: str
    reason: str

    @field_validator("group_id")
    @classmethod
    def _group_id(cls, value: str) -> str:
        return trim_field(value, field="群组 ID", limit=MAX_NAME_CHARS)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return trim_field(value, field="解散原因", limit=MAX_TOOL_TEXT_CHARS)


class TeamReportArgs(_Args):
    period: str

    @field_validator("period")
    @classmethod
    def _period(cls, value: str) -> str:
        if value not in ("day", "week"):
            raise ValueError("period 只能是 day 或 week。")
        return value


_SCHEMAS: dict[str, dict[str, Any]] = {
    schema["function"]["name"]: schema for schema in TOOL_SCHEMAS
}

_ARG_MODELS: dict[str, type[BaseModel]] = {
    CREATE_AGENT: CreateAgentArgs,
    CREATE_GROUP: CreateGroupArgs,
    SET_GROUP_MEMBERS: SetGroupMembersArgs,
    POST_GROUP_MESSAGE: PostGroupMessageArgs,
    ASK_AGENT: AskAgentArgs,
    DELETE_AGENT: DeleteAgentArgs,
    DISSOLVE_GROUP: DissolveGroupArgs,
    TEAM_REPORT: TeamReportArgs,
}

async def _create_agent(ctx, payload: dict[str, Any]) -> ToolOutcome:
    limit = ctx.settings.max_new_agents
    if ctx.store.count_created(ctx.run.run_id, CREATE_AGENT) >= limit:
        raise RunLimited(f"已达本次任务的成员创建上限（{limit}），任务已结束。")
    try:
        agent = ctx.store.create_agent(
            name=payload["name"], instructions=payload["instructions"], kind="member"
        )
    except AgentNameTaken as exc:
        raise ToolError(f"{exc}本次调用未执行。") from exc
    ctx.publish_workspace()
    return ToolOutcome(
        summary=f"创建了成员「{agent['name']}」",
        result={
            "ok": True,
            "agent_id": agent["id"],
            "name": agent["name"],
            "avatar_seed": agent["avatar_seed"],
        },
        target_agent_id=agent["id"],
    )


def _same_group(existing: dict, title: str, members: list[str]) -> bool:
    """A group with the same members and a similar title: reuse it instead of a twin."""
    if set(existing["member_ids"]) != set(members):
        return False

    def key(text: str) -> str:
        return "".join(ch for ch in str(text).casefold() if ch.isalnum())

    mine, theirs = key(title), key(existing["title"])
    return mine == theirs or (bool(mine) and bool(theirs) and (mine in theirs or theirs in mine))


async def _create_group(ctx, payload: dict[str, Any]) -> ToolOutcome:
    members = resolve.group_members(ctx.store, payload["member_ids"])
    if payload.get("include_supervisor"):
        boss = resolve.supervisor(ctx.store)
        if boss["id"] not in members:
            members.insert(0, boss["id"])
    for existing in ctx.store.list_conversations():
        if existing["kind"] != "group" or not _same_group(existing, payload["title"], members):
            continue
        # The same group already exists: say so instead of making a duplicate.
        return ToolOutcome(
            summary=f"群组「{existing['title']}」已经存在，直接复用它",
            result={
                "ok": True,
                "existing": True,
                "conversation_id": existing["id"],
                "title": existing["title"],
                "purpose": existing["purpose"],
                "member_ids": existing["member_ids"],
            },
            target_conversation_id=existing["id"],
        )
    limit = ctx.settings.max_new_groups
    if ctx.store.count_created(ctx.run.run_id, CREATE_GROUP) >= limit:
        raise RunLimited(f"已达本次任务的群组创建上限（{limit}），任务已结束。")
    group = ctx.store.create_conversation(
        kind="group",
        title=payload["title"],
        purpose=payload["purpose"],
        member_ids=members,
    )
    if payload.get("rules"):
        group = ctx.store.update_conversation(group["id"], rules=payload["rules"]) or group
    first_message = payload.get("first_message") or ""
    if first_message:
        group = _kick_off(ctx, group, first_message) or group
    ctx.publish_workspace()
    names = "、".join(resolve.agent_name(ctx.store, member_id) for member_id in group["member_ids"])
    return ToolOutcome(
        summary=f"建立了群组「{group['title']}」，成员：{names}",
        result={
            "ok": True,
            "existing": False,
            "conversation_id": group["id"],
            "title": group["title"],
            "purpose": group["purpose"],
            "member_ids": group["member_ids"],
        },
        target_conversation_id=group["id"],
    )


def _kick_off(ctx, group: dict, first_message: str) -> dict:
    """Post the supervisor's opening in the new group, then let the members answer it.

    The run is started without a new user message: the opening is the trigger, and the
    members' queue is the group's own members (side by side), not a dispatcher call — there
    is no user message to route yet.
    """
    boss = resolve.supervisor(ctx.store)
    roster = resolve.roster(ctx.store, group)
    message = ctx.store.append_message(
        conversation_id=group["id"],
        author_id=boss["id"],
        author_name=boss["name"],
        avatar_seed=boss["avatar_seed"],
        kind="agent",
        content=first_message,
        status="complete",
        mentions=parse_mentions(first_message, roster),
        run_id=ctx.run.run_id,
    )
    ctx.publish_message("message_start", message)
    ctx.publish_message("message_end", message)
    kickoff = ctx.kick_off
    if kickoff is not None:
        kickoff(group, message)
    return group


async def _set_group_members(ctx, payload: dict[str, Any]) -> ToolOutcome:
    group = resolve.require_group(ctx.store, payload["group_id"])
    members = resolve.group_members(ctx.store, payload["member_ids"])
    # The supervisor may be removed now; only an empty group is refused (``group_members``).
    updated = ctx.store.update_conversation(group["id"], member_ids=members)
    if updated is None:  # pragma: no cover - group existence checked above
        raise ToolError("群组不存在，本次调用未执行。")
    ctx.publish_workspace()
    names = "、".join(resolve.agent_name(ctx.store, member_id) for member_id in updated["member_ids"])
    return ToolOutcome(
        summary=f"更新了群组「{updated['title']}」的成员：{names}",
        result={
            "ok": True,
            "conversation_id": updated["id"],
            "member_ids": updated["member_ids"],
        },
        target_conversation_id=updated["id"],
    )


async def _post_group_message(ctx, payload: dict[str, Any]) -> ToolOutcome:
    group = resolve.require_group(ctx.store, payload["group_id"])
    supervisor = resolve.supervisor(ctx.store)
    roster = resolve.roster(ctx.store, group)
    image_ids = resolve.passable_images(ctx.store, supervisor, payload.get("image_ids") or [])
    message = ctx.store.append_message(
        conversation_id=group["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content=payload["content"],
        status="complete",
        mentions=parse_mentions(payload["content"], roster),
        run_id=ctx.run.run_id,
    )
    message = ctx.store.attach_to_message(message["id"], image_ids) or message
    ctx.publish_message("message_start", message)
    ctx.publish_message("message_end", message)
    result: dict[str, Any] = {
        "ok": True,
        "conversation_id": group["id"],
        "message_id": message["id"],
    }
    if image_ids:
        result["image_ids"] = image_ids
        blind = [
            resolve.agent_name(ctx.store, member)
            for member in group["member_ids"]
            if member != supervisor["id"] and not resolve.sees_images_by_id(ctx.store, member)
        ]
        if blind:
            result["note"] = f"{'、'.join(blind)}用的模型看不到图片内容，只能看到图片 ID。"
    return ToolOutcome(
        summary=f"向「{group['title']}」发布了消息",
        result=result,
        target_conversation_id=group["id"],
    )


async def _ask_agent(ctx, payload: dict[str, Any]) -> ToolOutcome:
    group = resolve.require_group(ctx.store, payload["group_id"])
    agent = ctx.store.get_agent(payload["agent_id"])
    if agent is None:
        raise ToolError(f"成员 ID「{payload['agent_id']}」不存在，本次调用未执行。")
    if agent["kind"] == "supervisor":
        boss = resolve.supervisor(ctx.store)
        raise ToolError(f"「{boss['name']}」不能作为被咨询的成员，本次调用未执行。")
    if agent["id"] not in group["member_ids"]:
        raise ToolError(f"「{agent['name']}」不在群组「{group['title']}」中，本次调用未执行。")
    if ctx.consult_member is None:  # pragma: no cover - the engine always binds it
        raise ToolError("咨询成员的通道没有就绪，本次调用未执行。")
    text, message, review = await ctx.consult_member(
        group, agent, payload["task"], reply_to_id=ctx.action_message_id
    )
    result: dict[str, Any] = {
        "ok": True,
        "agent_id": agent["id"],
        "conversation_id": group["id"],
        # A member may answer with only a reaction: no message is left behind then.
        "message_id": (message or {}).get("id"),
        "content": text,
    }
    if not (message or {}).get("id"):
        limited = getattr(ctx.run, "turn_limited", {}).get(agent["id"])
        if limited:
            result["note"] = f"「{agent['name']}」这一轮的调用预算用完了，没有给出回复（{limited}）。"
        else:
            result["note"] = f"「{agent['name']}」只贴了一个表情，没有发文字回复。"
    if review:
        result["review"] = review
    return ToolOutcome(
        summary=f"「{agent['name']}」已回复",
        result=result,
        target_agent_id=agent["id"],
        target_conversation_id=group["id"],
    )


async def _delete_agent(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Ask the user first, then delete the member exactly as they decided."""
    target = resolve.resolve_agent(ctx.store, payload["agent_id"])
    if target is None:
        raise ToolError(
            f"找不到成员「{payload['agent_id']}」，请用团队目录里的名字或 ID，本次调用未执行。"
        )
    if target["kind"] == "supervisor":
        raise ToolError(f"「{target['name']}」不能删除，本次调用未执行。")
    name = target["name"]
    prompt = (
        f"{ctx.author['name']}想删除成员「{name}」：{payload['reason']}。"
        "删除后，它和你的私聊、它参与的机器人私聊会一起删除，群组里的历史发言会保留。"
    )
    decision = await ask(
        ctx,
        Card(
            prompt=prompt,
            confirm_label="删除",
            deny_label="保留",
            summary=f"删除成员「{name}」",
            step=f"等待你确认删除「{name}」",
        ),
    )
    if decision == "approve":
        try:
            deleted = ctx.store.delete_agent(target["id"])
        except AgentDeleteRefused as exc:
            reason = str(exc)
            return _finish_delete(ctx, name, status="error", summary=reason, error=reason)
        if deleted is None:  # pragma: no cover - resolved moments ago
            return _finish_delete(ctx, name, status="error", summary="成员不存在。", error="成员不存在。")
        ctx.publish_workspace()
        ctx.publish_sidebar()
        return _finish_delete(
            ctx,
            name,
            status="completed",
            summary=f"已删除成员「{name}」",
            result={
                "ok": True,
                "agent_id": target["id"],
                "name": name,
                "message": f"已删除成员「{name}」，它参与的群组历史发言仍然保留。",
            },
        )
    if decision in ("deny", "stop"):
        return _finish_delete(
            ctx,
            name,
            status="completed",
            summary=f"你保留了成员「{name}」",
            result={"ok": False, "denied": True},
        )
    reason = f"等待确认超时，成员「{name}」没有删除。"
    return _finish_delete(ctx, name, status="error", summary=reason, error=reason)


def _finish_delete(
    ctx,
    name: str,
    *,
    status: str,
    summary: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> ToolOutcome:
    """Close the delete action with the outcome the user's answer produced."""
    payload = result if result is not None else {"ok": False, "error": error}
    text = json.dumps(payload, ensure_ascii=False)
    ctx.finish_action(ctx.action, status=status, summary=summary, error=error, result=text)
    return ToolOutcome(summary=f"删除成员「{name}」", result=payload, text=text, handled=True)


async def _dissolve_group(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Ask the user first, then dissolve the group exactly as they decided."""
    group = resolve.resolve_group(ctx.store, payload["group_id"])
    if group is None:
        raise ToolError(
            f"找不到群组「{payload['group_id']}」，请用群组目录里的名字或 ID，本次调用未执行。"
        )
    if group["id"] == ctx.run.conversation_id:
        # Dissolving the group the supervisor is speaking in would delete this very run.
        raise ToolError("不能在要解散的群组里发起解散，本次调用未执行。")
    title = group["title"]
    prompt = (
        f"{ctx.author['name']}想解散群组「{title}」：{payload['reason']}。"
        "解散后群组和它的聊天记录会一起删除，成员和他们的私聊都会保留。"
    )
    decision = await ask(
        ctx,
        Card(
            prompt=prompt,
            confirm_label="解散",
            deny_label="保留",
            summary=f"解散群组「{title}」",
            step=f"等待你确认解散「{title}」",
        ),
    )
    if decision == "approve":
        try:
            dissolved = ctx.store.dissolve_group(group["id"])
        except GroupDissolveRefused as exc:
            reason = str(exc)
            return _finish_dissolve(ctx, title, status="error", summary=reason, error=reason)
        if dissolved is None:  # pragma: no cover - resolved moments ago
            return _finish_dissolve(ctx, title, status="error", summary="群组不存在。", error="群组不存在。")
        ctx.publish_workspace()
        ctx.publish_sidebar()
        return _finish_dissolve(
            ctx,
            title,
            status="completed",
            summary=f"已解散群组「{title}」",
            result={
                "ok": True,
                "conversation_id": group["id"],
                "title": title,
                "message": f"已解散群组「{title}」，成员和他们的私聊都还在。",
            },
        )
    if decision in ("deny", "stop"):
        return _finish_dissolve(
            ctx,
            title,
            status="completed",
            summary=f"你保留了群组「{title}」",
            result={"ok": False, "denied": True},
        )
    reason = f"等待确认超时，群组「{title}」没有解散。"
    return _finish_dissolve(ctx, title, status="error", summary=reason, error=reason)


def _finish_dissolve(
    ctx,
    title: str,
    *,
    status: str,
    summary: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> ToolOutcome:
    """Close the dissolve action with the outcome the user's answer produced."""
    payload = result if result is not None else {"ok": False, "error": error}
    text = json.dumps(payload, ensure_ascii=False)
    ctx.finish_action(ctx.action, status=status, summary=summary, error=error, result=text)
    return ToolOutcome(summary=f"解散群组「{title}」", result=payload, text=text, handled=True)


async def _team_report(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Read one period's team facts for the supervisor's 晨报/周报."""
    period = payload["period"]
    try:
        zone = schedules.zone_of(ctx.settings.timezone)
    except schedules.ScheduleError as exc:  # pragma: no cover - validated at startup
        raise ToolError(str(exc)) from exc
    report = ctx.store.team_report(period=period, now=datetime.now(timezone.utc), zone=zone)
    word = "本日" if period == "day" else "本周"
    return ToolOutcome(
        summary=f"读取了{word}团队数据",
        result={"ok": True, **report},
        text=json.dumps({"ok": True, **report}, ensure_ascii=False),
    )


_BY_NAME = {
    CREATE_AGENT: _create_agent,
    CREATE_GROUP: _create_group,
    SET_GROUP_MEMBERS: _set_group_members,
    POST_GROUP_MESSAGE: _post_group_message,
    ASK_AGENT: _ask_agent,
    DELETE_AGENT: _delete_agent,
    DISSOLVE_GROUP: _dissolve_group,
    TEAM_REPORT: _team_report,
}


def handlers() -> list[ToolHandler]:
    """Every management tool, in prompt order."""
    return [_handler(name) for name in _ARG_MODELS]


def _handler(name: str) -> ToolHandler:
    return ToolHandler(
        name=name,
        label=TOOL_LABELS[name],
        schema=_SCHEMAS[name],
        roles=ROLES,
        parse=model_parser(_ARG_MODELS[name]),
        run=_BY_NAME[name],
        # Both destructive tools park on a card: nobody can answer one in a scheduled run.
        unattended_refusal=UNATTENDED_REFUSAL if name in UNATTENDED_TOOLS else None,
    )
