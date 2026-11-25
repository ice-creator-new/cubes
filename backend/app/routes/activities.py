"""Activities: one bot turn's record, its steps, and the work log a bot keeps.

``GET /agents/{agent_id}/activities`` is the bot's 「工作记录」 (newest first, every
conversation); ``GET /activities/{activity_id}`` is one turn's page: its answer and its
steps grouped by whose turn they belong to (this bot first, then each turn it caused).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import models
from ..store import Store
from .common import get_store

router = APIRouter()


@router.get("/agents/{agent_id}/activities")
async def agent_activities(
    agent_id: str,
    limit: int = 50,
    before: str | None = None,
    store: Store = Depends(get_store),
) -> dict:
    """One bot's activities across every conversation, newest first."""
    if store.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="「limit」需要在 1 到 200 之间。")
    rows, has_more = store.list_activities(agent_id, limit=limit, before=before)
    return {
        "activities": [models.activity_payload(store.activity_view(row)) for row in rows],
        "has_more": has_more,
    }


@router.get("/activities/{activity_id}")
async def activity_detail(activity_id: str, store: Store = Depends(get_store)) -> dict:
    detail = store.activity_detail(activity_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="这条记录不存在。")
    return models.activity_detail_payload(detail)
