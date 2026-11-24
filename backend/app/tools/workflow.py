"""Workflows as registry handlers: the six tools a bot uses to write, edit and start plans.

A bot writes plans for itself; the supervisor may write plans for anyone. Editing or deleting
someone else's plan is refused in the handler (the tool stays offered), which keeps the tool
list identical between turns and puts the reason where the model reads it. A plan a bot wrote
starts disabled, exactly like a bot-written skill: the user reviews it in the panel, and
``workflow_run`` refuses a disabled one until they do.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .. import models
from .. import workflows as rules
from ..errors import ToolError, WorkflowBusy
from ..toolkit import ToolInputError
from .names import (
    WORKFLOW_CREATE,
    WORKFLOW_DELETE,
    WORKFLOW_LIST,
    WORKFLOW_READ,
    WORKFLOW_RUN,
    WORKFLOW_UPDATE,
)
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})

STEP_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {
            "type": "string",
            "description": "节点 id（小写字母数字、- 或 _）；留空会自动取名，连线要引用的就写这里",
        },
        "title": {"type": "string", "description": "这一步的短标题"},
        "instruction": {"type": "string", "description": "这一步要做的事，写给执行这一步的自己看"},
        "kind": {
            "type": "string",
            "enum": list(rules.STEP_KINDS),
            "description": (
                "do 是去做；ask_user 是把 instruction 当问题问用户；"
                "branch 是让模型在 choices 里挑一条路"
            ),
        },
        "choices": {
            "type": "array",
            "items": {"type": "string"},
            "description": "branch 节点的选项（至少两个）；出去的连线要写上对应选项",
        },
        "gate": {
            "type": "string",
            "enum": list(rules.GATES),
            "description": "confirm 表示开始这一步前先让用户确认",
        },
        "on_fail": {
            "type": "string",
            "enum": list(rules.FAIL_POLICIES),
            "description": "这一步失败时：stop 停下，retry 重试一次，skip 跳过",
        },
    },
    "required": ["instruction"],
}

EDGE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "from": {"type": "string", "description": "起点节点 id"},
        "to": {"type": "string", "description": "终点节点 id"},
        "when": {"type": "string", "description": "只有分支节点才写：这条连线属于哪个选项"},
        "max_loops": {
            "type": "integer",
            "minimum": 1,
            "maximum": rules.MAX_LOOPS,
            "description": (
                "只有绕回前面节点的连线才写：最多循环几轮。走这条线会把它和终点之间的节点重新跑一遍，"
                "用完次数后这条线失效，流程从起点的其他出线继续。常见写法：分支「不合格」连回「修改」，设 3。"
            ),
        },
    },
    "required": ["from", "to"],
}

WORKFLOW_LIST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WORKFLOW_LIST,
        "description": "列出团队里的工作流：名字、谁的、几步、有没有启用、最近跑过几次。",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
    },
}

WORKFLOW_READ_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WORKFLOW_READ,
        "description": "读一个工作流的全部步骤。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "description": "工作流名字或 ID"}},
            "required": ["name"],
        },
    },
}

WORKFLOW_CREATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WORKFLOW_CREATE,
        "description": (
            "新建一个属于你的工作流。写好就已经启用、可以直接运行；主管可以停用任何人的工作流。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "工作流名字，同一个机器人名下不能重名"},
                "description": {"type": "string", "description": "一两句话说明这个工作流做什么"},
                "steps": {
                    "type": "array",
                    "items": STEP_ITEM_SCHEMA,
                    "description": f"节点列表，最多 {rules.MAX_NODES} 个",
                },
                "edges": {
                    "type": "array",
                    "items": EDGE_ITEM_SCHEMA,
                    "description": (
                        "节点之间的连线；不写就连成一条直线。往前走的连线不能成环，"
                        "要重复做就连回前面的节点并写 max_loops。分支节点的每条出线要写 when"
                    ),
                },
            },
            "required": ["name", "steps"],
        },
    },
}

WORKFLOW_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WORKFLOW_UPDATE,
        "description": (
            "改你自己（主管可以改任何人的）工作流：改名、改说明、换整套步骤、启用或停用。"
            "enabled 传 false 就停用一条工作流，传 true 重新启用；主管用它开关别人的工作流也行。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "要改的工作流名字或 ID"},
                "new_name": {"type": "string", "description": "改名时的新名字"},
                "description": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": STEP_ITEM_SCHEMA,
                    "description": "整套节点（不是增量）",
                },
                "edges": {
                    "type": "array",
                    "items": EDGE_ITEM_SCHEMA,
                    "description": "整套连线（不是增量）；不写就按节点顺序连成一条线",
                },
                "enabled": {"type": "boolean", "description": "启用或停用"},
            },
            "required": ["name"],
        },
    },
}

WORKFLOW_DELETE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WORKFLOW_DELETE,
        "description": "删除你自己（主管可以删任何人的）工作流。已经跑过的运行记录会一起删掉。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "description": "工作流名字或 ID"}},
            "required": ["name"],
        },
    },
}

WORKFLOW_RUN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WORKFLOW_RUN,
        "description": (
            "开跑一个工作流：它会开一个自己的页面，由负责这个工作流的成员一步一步做，"
            "用户在那个页面里看着它流转，可以暂停；负责人自己的聊天里会留一行入口。"
            "它立刻就开始，你现在不会被告知结果。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "工作流名字或 ID"},
                "input": {"type": "string", "description": "这次要做的具体内容（可以留空）"},
            },
            "required": ["name"],
        },
    },
}


def handlers(store: Any, run_workflow: Callable[..., Any]) -> list[ToolHandler]:
    """The six workflow tools; ``run_workflow`` is the launcher the engine binds per call."""

    async def _list(ctx, payload: dict[str, Any]) -> ToolOutcome:
        rows = []
        for workflow in store.list_workflows():
            rows.append(
                {
                    "name": workflow["name"],
                    "id": workflow["id"],
                    "owner": _owner_name(store, workflow),
                    "description": workflow["description"],
                    "steps": len(workflow["steps"]),
                    "enabled": workflow["enabled"],
                    "runs": workflow["run_count"],
                    "last_run_at": workflow["last_run_at"],
                    "mine": workflow["owner_agent_id"] == ctx.author["id"],
                }
            )
        return ToolOutcome(
            summary=f"列出了 {len(rows)} 个工作流",
            result={"ok": True, "workflows": rows},
        )

    async def _read(ctx, payload: dict[str, Any]) -> ToolOutcome:
        workflow = _require(ctx, payload)
        return ToolOutcome(
            summary=f"读了工作流「{workflow['name']}」",
            result={
                "ok": True,
                "name": workflow["name"],
                "owner": _owner_name(store, workflow),
                "description": workflow["description"],
                "enabled": workflow["enabled"],
                "steps": [
                    {
                        "title": step["title"],
                        "kind": step["kind"],
                        "instruction": step["instruction"],
                        "gate": step["gate"],
                        "on_fail": step["on_fail"],
                    }
                    for step in workflow["steps"]
                ],
            },
        )

    async def _create(ctx, payload: dict[str, Any]) -> ToolOutcome:
        try:
            name = rules.validate_name(payload.get("name") or "")
            description = rules.validate_description(payload.get("description") or "")
            nodes, edges = rules.normalize_graph(
                payload.get("steps"), payload.get("edges")
            )
        except rules.WorkflowInvalid as exc:
            raise ToolInputError(str(exc)) from exc
        if len(nodes) > rules.MAX_NODES:
            raise ToolInputError(f"一个工作流最多 {rules.MAX_NODES} 个节点。")
        if store.count_workflows_of(ctx.author["id"]) >= rules.MAX_WORKFLOWS_PER_BOT:
            raise ToolError(
                f"「{ctx.author['name']}」名下已经有 {rules.MAX_WORKFLOWS_PER_BOT} 个工作流了，"
                "先删掉一些再建。"
            )
        if store.workflow_by_name(name, owner_agent_id=ctx.author["id"]) is not None:
            raise ToolInputError(f"你名下已经有叫「{name}」的工作流了，用 workflow_update 改它。")
        workflow = store.create_workflow(
            name=name,
            description=description,
            owner_agent_id=ctx.author["id"],
            created_by=ctx.author["id"],
            nodes=nodes,
            edges=edges,
            source="agent",
            enabled=True,
        )
        ctx.publish("workflows", models.workflows_payload(store))
        return ToolOutcome(
            summary=f"写了工作流「{name}」",
            result={
                "ok": True,
                "name": workflow["name"],
                "id": workflow["id"],
                "nodes": len(workflow["steps"]),
                "edges": len(workflow["edges"]),
                "note": "它已经启用，可以直接 workflow_run；要停用就用 workflow_update 的 enabled=false。",
            },
        )

    async def _update(ctx, payload: dict[str, Any]) -> ToolOutcome:
        workflow = _require(ctx, payload, must_own=True)
        changes: dict[str, Any] = {}
        try:
            if isinstance(payload.get("new_name"), str):
                changes["name"] = rules.validate_name(payload["new_name"])
            if isinstance(payload.get("description"), str):
                changes["description"] = rules.validate_description(payload["description"])
            if payload.get("steps") is not None:
                nodes, edges = rules.normalize_graph(
                    payload.get("steps"), payload.get("edges")
                )
                changes["nodes"] = nodes
                changes["edges"] = edges
        except rules.WorkflowInvalid as exc:
            raise ToolInputError(str(exc)) from exc
        if isinstance(payload.get("enabled"), bool):
            changes["enabled"] = payload["enabled"]
        if not changes:
            raise ToolInputError(
                "没有要改的内容：至少给 new_name、description、steps（可带 edges）或 enabled。"
            )
        updated = store.update_workflow(workflow["id"], **changes)
        ctx.publish("workflows", models.workflows_payload(store))
        return ToolOutcome(
            summary=f"改了工作流「{updated['name'] if updated else workflow['name']}」",
            result={
                "ok": True,
                "name": updated["name"] if updated else workflow["name"],
                "version": updated["version"] if updated else workflow["version"],
            },
        )

    async def _delete(ctx, payload: dict[str, Any]) -> ToolOutcome:
        workflow = _require(ctx, payload, must_own=True)
        store.delete_workflow(workflow["id"])
        ctx.publish("workflows", models.workflows_payload(store))
        return ToolOutcome(
            summary=f"删了工作流「{workflow['name']}」",
            result={"ok": True, "name": workflow["name"]},
        )

    async def _run(ctx, payload: dict[str, Any]) -> ToolOutcome:
        workflow = _require(ctx, payload)
        if not workflow["enabled"]:
            raise ToolError(
                f"工作流「{workflow['name']}」还没启用，先请用户在「工作流」里启用它。"
            )
        raw_input = payload.get("input")
        text = " ".join(str(raw_input or "").split())
        try:
            conversation_id, workflow_run_id = await run_workflow(workflow, text, ctx.author["id"])
        except WorkflowBusy as exc:
            raise ToolError(str(exc)) from exc
        ctx.publish("workflows", models.workflows_payload(store))
        return ToolOutcome(
            summary=f"开始了工作流「{workflow['name']}」",
            result={
                "ok": True,
                "name": workflow["name"],
                "conversation_id": conversation_id,
                "workflow_run_id": workflow_run_id,
                "note": (
                    "它开在自己的页面里一步一步跑，这一轮到这里就结束了："
                    "不要再自己搜索、读写或私信别人去替它做，也不要去问它进度。"
                ),
            },
            target_conversation_id=conversation_id,
        )

    return [
        ToolHandler(
            name=WORKFLOW_LIST,
            label="列出工作流",
            schema=WORKFLOW_LIST_SCHEMA,
            roles=ROLES,
            parse=_object_parser("列出工作流"),
            run=_list,
        ),
        ToolHandler(
            name=WORKFLOW_READ,
            label="读取工作流",
            schema=WORKFLOW_READ_SCHEMA,
            roles=ROLES,
            parse=_object_parser("读取工作流"),
            run=_read,
        ),
        ToolHandler(
            name=WORKFLOW_CREATE,
            label="新建工作流",
            schema=WORKFLOW_CREATE_SCHEMA,
            roles=ROLES,
            parse=_object_parser("新建工作流"),
            run=_create,
        ),
        ToolHandler(
            name=WORKFLOW_UPDATE,
            label="修改工作流",
            schema=WORKFLOW_UPDATE_SCHEMA,
            roles=ROLES,
            parse=_object_parser("修改工作流"),
            run=_update,
        ),
        ToolHandler(
            name=WORKFLOW_DELETE,
            label="删除工作流",
            schema=WORKFLOW_DELETE_SCHEMA,
            roles=ROLES,
            parse=_object_parser("删除工作流"),
            run=_delete,
        ),
        ToolHandler(
            name=WORKFLOW_RUN,
            label="运行工作流",
            schema=WORKFLOW_RUN_SCHEMA,
            roles=ROLES,
            parse=_object_parser("运行工作流"),
            run=_run,
            hands_off=True,
        ),
    ]


def _object_parser(label: str):
    """A JSON-object parser that reports a bad argument body as a correctable mistake."""

    def parse(raw: str | None) -> dict[str, Any]:
        if raw is None or not str(raw).strip():
            return {}
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ToolInputError(f"{label}的参数不是合法 JSON。") from exc
        if not isinstance(value, dict):
            raise ToolInputError(f"{label}需要 JSON 对象参数。")
        return value

    return parse


def _owner_name(store: Any, workflow: dict) -> str:
    owner = store.get_agent(workflow["owner_agent_id"])
    return owner["name"] if owner is not None else "（已离开的成员）"


def _require(ctx, payload: dict[str, Any], *, must_own: bool = False) -> dict:
    """The named workflow, if this bot may read it — and own it, when ``must_own``.

    Everyone may read a plan (the panel shows the whole team's); only its owner edits or
    deletes it. The supervisor is the exception: it writes and edits plans for the team.
    """
    text = str(payload.get("name") or "").strip()
    if not text:
        raise ToolInputError("需要 name（工作流名字或 ID）。")
    workflow = ctx.store.get_workflow(text) or ctx.store.workflow_by_name(text)
    if workflow is None:
        raise ToolError(f"没有叫「{text}」的工作流，本次调用未执行。")
    if not must_own or ctx.author["kind"] == "supervisor":
        return workflow
    if workflow["owner_agent_id"] != ctx.author["id"]:
        boss = ctx.store.supervisor()
        boss_name = boss["name"] if boss is not None else "负责人"
        raise ToolError(
            f"工作流「{workflow['name']}」是{_owner_name(ctx.store, workflow)}的，"
            f"只能改自己的；要改它请那个成员自己来，或者找{boss_name}。"
        )
    return workflow
