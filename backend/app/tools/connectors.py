"""MCP connector tools as registry handlers.

A connector's tool list is known only while its session is connected, so the handlers are
built from the manager each time a registry is assembled: one handler per offered tool, and
every one that may change data parks on a card before it runs.
"""

from __future__ import annotations

from .. import permissions

import json
from typing import Any

from ..approvals import UNATTENDED_REFUSAL, Card, ask
from ..connectors import ConnectorTool, argument_preview
from ..toolkit import text_detail
from .args import json_parser
from .types import Runner, ToolHandler, ToolOutcome, cap_tool_text

ROLES = frozenset({"supervisor", "member"})
# The sandbox and the connectors both park writes on a card: unattended runs refuse them.


def handlers(connectors) -> list[ToolHandler]:
    """One handler per tool a connected connector offers right now."""
    offered: list[ToolHandler] = []
    for schema in connectors.schemas():
        name = schema["function"]["name"]
        target = connectors.resolve(name)
        if target is None:  # pragma: no cover - schemas and resolve read the same records
            continue
        offered.append(_handler(connectors, target, schema))
    return offered


def _handler(connectors, target: ConnectorTool, schema: dict) -> ToolHandler:
    return ToolHandler(
        name=target.model_name,
        label=target.label,
        schema=schema,
        roles=ROLES,
        parse=json_parser(target.label),
        run=_runner(connectors, target),
        unattended_refusal=None if target.read_only else UNATTENDED_REFUSAL,
    )


def _runner(connectors, target: ConnectorTool) -> Runner:
    async def run(ctx, payload: dict[str, Any]) -> ToolOutcome:
        return await _call(ctx, connectors, target, payload)

    return run


async def _call(ctx, connectors, target: ConnectorTool, payload: dict[str, Any]) -> ToolOutcome:
    """Run one connector tool; one that may change data waits for the user first."""
    shown = target.title or target.name
    if not target.read_only:
        card = Card(
            prompt=(
                f"要调用 {target.connector_name} 的「{shown}」，它可能会修改这个服务里的数据。"
                f"参数：{argument_preview(payload)}"
            ),
            confirm_label="允许",
            deny_label="拒绝",
            summary=f"调用 {target.connector_name}「{shown}」",
            step=f"等待你允许调用 {target.connector_name}「{shown}」",
            resume_summary=f"正在调用 {target.connector_name}「{shown}」",
            resume_step=f"正在调用 {target.connector_name}「{shown}」",
            grant=permissions.CONNECTOR_WRITE,
        )
        decision = await ask(ctx, card)
        if decision != "approve":
            denied = decision in ("deny", "stop")
            summary = (
                f"你拒绝了调用 {target.connector_name}「{shown}」"
                if denied
                else f"等待确认超时，没有调用 {target.connector_name}「{shown}」"
            )
            result = {"ok": False, "denied": True} if denied else {"ok": False, "error": summary}
            text = json.dumps(result, ensure_ascii=False)
            ctx.finish_action(
                ctx.action,
                status="completed" if denied else "error",
                summary=summary,
                error=None if denied else summary,
                result=text,
            )
            return ToolOutcome(summary=summary, result=result, text=text, handled=True)
    text = await connectors.call(target, payload)
    return ToolOutcome(
        summary=f"已调用 {target.connector_name}「{shown}」",
        result={},
        text=cap_tool_text(text),
        detail=text_detail(text),
    )
