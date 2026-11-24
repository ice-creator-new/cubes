"""Changing a bot's own name and appearance, and the supervisor's power to change any bot.

``update_self`` is how a bot stops being 「助理」 and becomes whatever the work made it; the
supervisor's ``update_agent`` does the same for a member it is setting up. Both validate
exactly like ``PATCH /agents`` — one name for the whole workspace, one catalog of shapes and
colours — and publish the workspace so every sidebar and avatar redraws at once.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .. import appearance as appearance_rules
from ..errors import AgentNameTaken, ToolError
from ..models import AppearanceInput, MAX_NAME_CHARS, trim_field
from ..toolkit import ToolInputError
from . import resolve
from .args import model_parser
from .names import UPDATE_AGENT, UPDATE_SELF
from .types import ToolHandler, ToolOutcome

SELF_ROLES = frozenset({"supervisor", "member"})
SUPERVISOR_ROLES = frozenset({"supervisor"})
UPDATE_SELF_LABEL = "改变自己"
UPDATE_AGENT_LABEL = "调整成员"

_CATALOG = (
    "形状（shape）可选：" + "、".join(appearance_rules.SHAPES) + "；"
    "颜色（color）可选：" + "、".join(appearance_rules.COLORS) + "，也可以是 #rrggbb；"
    "眼睛颜色（eye）是 #rrggbb，不填就自动挑一个清楚的。"
)

_EDIT_SCHEMA_PROPERTIES: dict[str, Any] = {
    "name": {
        "type": "string",
        "description": "新名字，全团队唯一；不改就不填",
    },
    "shape": {"type": "string", "description": "新的外形；改外观时和 color 一起填"},
    "color": {"type": "string", "description": "新的身体颜色；改外观时和 shape 一起填"},
    "eye": {"type": "string", "description": "新的眼睛颜色，#rrggbb；不填保留自动眼睛"},
}

UPDATE_SELF_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": UPDATE_SELF,
        "description": (
            "改变你自己的名字或外观。名字要全团队唯一；外观要同时给出 shape 和 color。"
            "换名字后，用户看到的侧栏和头像会立刻更新。" + _CATALOG
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": _EDIT_SCHEMA_PROPERTIES,
        },
    },
}

UPDATE_AGENT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": UPDATE_AGENT,
        "description": (
            "修改任意一位成员的名字或外观（不能改它的职责指令）。名字要全团队唯一；"
            "外观要同时给出 shape 和 color。" + _CATALOG
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "要修改的成员 ID（团队目录里括号中的 id），也可以写名字",
                },
                "name": _EDIT_SCHEMA_PROPERTIES["name"],
                "shape": _EDIT_SCHEMA_PROPERTIES["shape"],
                "color": _EDIT_SCHEMA_PROPERTIES["color"],
                "eye": _EDIT_SCHEMA_PROPERTIES["eye"],
            },
            "required": ["agent_id"],
        },
    },
}


class _EditFields(BaseModel):
    """The fields both edit tools share, validated exactly like ``PATCH /agents``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    shape: str | None = None
    color: str | None = None
    eye: str | None = None

    @model_validator(mode="after")
    def _something(self) -> "_EditFields":
        if self.name is None and self.shape is None and self.color is None and self.eye is None:
            raise ValueError("至少给出 name 或一套外观（shape 和 color）。")
        if (self.shape is None and self.color is not None) or (
            self.shape is not None and self.color is None
        ):
            raise ValueError("改外观需要同时提供 shape 和 color。")
        if self.shape is None and self.eye is not None:
            raise ValueError("只改眼睛颜色时也要同时提供 shape 和 color。")
        return self


class UpdateSelfArgs(_EditFields):
    pass


class UpdateAgentArgs(_EditFields):
    agent_id: str


def handlers() -> list[ToolHandler]:
    return [
        ToolHandler(
            name=UPDATE_SELF,
            label=UPDATE_SELF_LABEL,
            schema=UPDATE_SELF_SCHEMA,
            roles=SELF_ROLES,
            parse=model_parser(UpdateSelfArgs),
            run=_update_self,
        ),
        ToolHandler(
            name=UPDATE_AGENT,
            label=UPDATE_AGENT_LABEL,
            schema=UPDATE_AGENT_SCHEMA,
            roles=SUPERVISOR_ROLES,
            parse=model_parser(UpdateAgentArgs),
            run=_update_agent,
        ),
    ]


async def _update_self(ctx, payload: dict[str, Any]) -> ToolOutcome:
    return _apply(ctx, ctx.author, payload, own=True)


async def _update_agent(ctx, payload: dict[str, Any]) -> ToolOutcome:
    target = resolve.resolve_agent(ctx.store, payload["agent_id"])
    if target is None:
        raise ToolError(
            f"找不到成员「{payload['agent_id']}」，本次调用未执行；请用团队目录里的名字或 ID。"
        )
    return _apply(ctx, target, payload, own=target["id"] == ctx.author["id"])


def _apply(ctx, target: dict, payload: dict[str, Any], *, own: bool) -> ToolOutcome:
    """Validate and store one edit; the row says what changed in the user's words."""
    updates: dict[str, Any] = {}
    wanted = payload.get("name")
    if wanted is not None:
        updates["name"] = trim_field(wanted, field="成员名称", limit=MAX_NAME_CHARS)
    if payload.get("shape") is not None or payload.get("color") is not None:
        try:
            inputs = AppearanceInput(
                shape=payload.get("shape"), color=payload.get("color"), eye=payload.get("eye")
            )
        except ValidationError as exc:
            raise ToolInputError(_first_error(exc)) from exc
        updates["appearance"] = inputs.stored()

    if not updates:
        raise ToolInputError("没有要改的内容，本次调用未执行。")

    before = target["name"]
    try:
        updated = ctx.store.update_agent(target["id"], **updates)
    except AgentNameTaken as exc:
        raise ToolError(f"{exc}本次调用未执行。") from exc
    if updated is None:  # pragma: no cover - resolved moments ago
        raise ToolError("成员不存在，本次调用未执行。")

    if updates.get("name") is not None:
        direct = ctx.store.direct_conversation(target["id"])
        if direct is not None and direct["title"] == before:
            ctx.store.update_conversation(direct["id"], title=updated["name"])
        # The rest of this run keeps writing under the new name, as the user now finds it.
        if own:
            ctx.author["name"] = updated["name"]
    ctx.publish_workspace()
    ctx.publish_sidebar()

    changes: list[str] = []
    if "name" in updates and own:
        changes.append(f"把名字改成了「{updated['name']}」")
    elif "name" in updates:
        changes.append(f"把「{before}」改名为「{updated['name']}」")
    if "appearance" in updates and own:
        changes.append("换上了新的样子")
    elif "appearance" in updates:
        changes.append(f"给「{updated['name']}」换了新的样子")
    summary = "，".join(changes)
    return ToolOutcome(
        summary=summary,
        result={
            "ok": True,
            "agent_id": updated["id"],
            "name": updated["name"],
            "appearance": updated["appearance"],
            "message": f"{summary}。",
        },
        target_agent_id=updated["id"],
    )


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "外观不符合要求，本次调用未执行。"
    message = str(errors[0].get("msg", "不符合要求"))
    prefix = "Value error, "
    if message.startswith(prefix):
        message = message[len(prefix) :]
    return f"{message}本次调用未执行。"
