"""Approving or refusing the action a run is parked on."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import models
from ..engine import SupervisorEngine
from ..errors import ActionNotAwaiting, ActionUnknown
from ..models import ActionAnswerRequest, ActionConfirmRequest
from ..store import Store
from .common import get_engine, get_store

router = APIRouter()


@router.post("/actions/{action_id}/answer")
async def answer_action(
    action_id: str,
    payload: ActionAnswerRequest,
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Answer an ``ask_user`` question; the parked run resumes with this answer."""
    try:
        action = await engine.answer(
            action_id,
            {
                "answers": [
                    {
                        "question_index": answer.question_index,
                        "labels": answer.labels,
                        "other": answer.other,
                    }
                    for answer in payload.answers
                ],
                "skip": payload.skip,
            },
        )
    except ActionUnknown as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActionNotAwaiting as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return models.action_payload(action)


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: str,
    payload: ActionConfirmRequest,
    engine: SupervisorEngine = Depends(get_engine),
    store: Store = Depends(get_store),
) -> dict:
    """Answer a waiting action; the run applies it before this returns.

    ``always`` is the card's third button: besides this yes, it remembers a rule for the
    tool and the bot that asked, so the next identical call goes ahead without a card.
    """
    # Read before answering: once the run resumes, the card (and the rule it names) is gone.
    asked = (store.get_action(action_id) or {}).get("confirmation") or {}
    rule_key = asked.get("rule") if isinstance(asked, dict) else None
    try:
        action = await engine.confirm(action_id, payload.approve)
    except ActionUnknown as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActionNotAwaiting as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if payload.always and payload.approve:
        message = store.action_message(action_id)
        asked_by = (message or {}).get("author_id")
        if asked_by:
            # A card names what its yes covers (writing inside the folders, say), so the rule
            # is never wider than what the user saw.
            store.add_approval_rule(tool_name=str(rule_key or action["tool_name"]), agent_id=asked_by)
            engine.publish("approval_rules", models.approval_rules_payload(store))
    return models.action_payload(action)
