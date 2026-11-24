"""Agents: the roster's create/rename/delete and the direct chat a member opens."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import models, providers, usage as usage_rules
from ..config import Settings
from ..engine import SupervisorEngine
from ..errors import AgentDeleteRefused, AgentNameTaken
from ..models import AgentCreateRequest, AgentPatchRequest
from ..providers import ProviderInvalid
from ..store import Store
from .common import get_engine, get_settings, get_store, publish_sidebar, publish_workspace

router = APIRouter()


@router.post("/agents", status_code=201)
async def create_agent(payload: AgentCreateRequest, store: Store = Depends(get_store)) -> dict:
    try:
        created = store.create_agent(
            name=payload.name, instructions=payload.instructions, kind="member"
        )
    except AgentNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return models.agent_payload(created)


@router.post("/agents/{agent_id}/direct")
async def agent_direct(agent_id: str, store: Store = Depends(get_store)) -> dict:
    agent = store.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    conversation = store.ensure_direct_conversation(agent_id)
    if conversation is None:  # pragma: no cover - existence checked above
        raise HTTPException(status_code=404, detail="成员不存在。")
    return models.conversation_payload(conversation)


@router.get("/agents/{agent_id}/profile")
async def agent_profile(
    agent_id: str,
    period: str = "week",
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> dict:
    """A member's all-time tallies plus one period's usage."""
    if period not in usage_rules.PERIODS:
        raise HTTPException(status_code=422, detail="period 需为 day、week 或 month。")
    if store.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    try:
        zone = ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - validated at startup
        raise HTTPException(status_code=500, detail="时区配置无效。") from exc
    profile = store.agent_profile(
        agent_id, period=period, now=datetime.now(timezone.utc), zone=zone
    )
    if profile is None:  # pragma: no cover - existence checked above
        raise HTTPException(status_code=404, detail="成员不存在。")
    return profile


@router.patch("/agents/{agent_id}")
async def patch_agent(
    agent_id: str,
    payload: AgentPatchRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    agent = store.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    updates: dict[str, Any] = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.instructions is not None:
        updates["instructions"] = payload.instructions
    if "appearance" in payload.model_fields_set:
        updates["appearance"] = (
            None if payload.appearance is None else payload.appearance.stored()
        )
    if "model_ref" in payload.model_fields_set:
        ref = providers.clean_model_ref(
            None if payload.model_ref is None else payload.model_ref.model_dump()
        )
        try:
            providers.validate_model_ref(store, ref)
        except ProviderInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        updates["model_ref"] = ref
    if "token_budget" in payload.model_fields_set:
        updates["token_budget"] = (
            None if payload.token_budget is None else payload.token_budget.model_dump()
        )
    if "tool_permissions" in payload.model_fields_set:
        updates["tool_permissions"] = (
            None if payload.tool_permissions is None else payload.tool_permissions.model_dump()
        )
    if "send_mode" in payload.model_fields_set:
        updates["send_mode"] = payload.send_mode
    if "permission_mode" in payload.model_fields_set:
        updates["permission_mode"] = payload.permission_mode
    try:
        updated = store.update_agent(agent_id, **updates)
    except AgentNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:  # pragma: no cover - existence checked above
        raise HTTPException(status_code=404, detail="成员不存在。")
    if payload.name is not None:
        direct = store.direct_conversation(agent_id)
        if direct is not None and direct["title"] == agent["name"]:
            store.update_conversation(direct["id"], title=updated["name"])
    if (
        "model_ref" in payload.model_fields_set
        or "token_budget" in payload.model_fields_set
        or "tool_permissions" in payload.model_fields_set
        or "send_mode" in payload.model_fields_set
        or "permission_mode" in payload.model_fields_set
    ):
        publish_workspace(store, engine)
    return models.agent_payload(updated)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    """User-initiated delete: the client already asked, so no server-side confirm."""
    try:
        deleted = store.delete_agent(agent_id)
    except AgentDeleteRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if deleted is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    publish_workspace(store, engine)
    publish_sidebar(store, engine)
    return Response(status_code=204)
