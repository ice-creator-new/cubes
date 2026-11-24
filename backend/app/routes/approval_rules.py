"""The 「总是允许」 rules: list, remember one by hand, and delete one.

A rule's other entrance is a confirmation card's third button (``POST /actions/{id}/confirm``
with ``always``); both write the same store row, and the settings page reads this list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import models
from ..engine import SupervisorEngine
from ..models import ApprovalRuleCreateRequest
from ..store import Store
from .common import get_engine, get_store

router = APIRouter()


@router.get("/approval-rules")
async def list_approval_rules(store: Store = Depends(get_store)) -> dict:
    return models.approval_rules_payload(store)


@router.post("/approval-rules", status_code=201)
async def create_approval_rule(
    payload: ApprovalRuleCreateRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Remember one rule. The card sites offer it themselves; this is the manual entry."""
    if payload.agent_id is not None and store.get_agent(payload.agent_id) is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    rule = store.add_approval_rule(tool_name=payload.tool_name, agent_id=payload.agent_id)
    engine.publish("approval_rules", models.approval_rules_payload(store))
    return models.approval_rule_payload(rule)


@router.delete("/approval-rules/{rule_id}", status_code=204)
async def delete_approval_rule(
    rule_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    if not store.delete_approval_rule(rule_id):
        raise HTTPException(status_code=404, detail="这条规则不存在。")
    engine.publish("approval_rules", models.approval_rules_payload(store))
    return Response(status_code=204)
