"""The workspace snapshot, the first-run onboarding and the readiness probe clients start from."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import models, providers
from ..config import Settings
from ..engine import SupervisorEngine
from ..errors import AgentNameTaken
from ..models import HealthOut, OnboardingRequest
from ..store import Store
from .common import get_engine, get_settings, get_store, publish_workspace

router = APIRouter()


@router.get("/health", response_model=HealthOut)
async def health(
    store: Store = Depends(get_store), settings: Settings = Depends(get_settings)
) -> HealthOut:
    default = providers.resolve_default(store)
    return HealthOut(
        configured=default is not None,
        model=default.model if default is not None else "",
        base_url=default.base_url if default is not None else "",
        provider_name=default.provider_name if default is not None else "",
        configuration_error=None if default is not None else providers.NO_MODEL,
        auth=bool(settings.auth_token),
    )


@router.get("/workspace")
async def workspace(
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> dict:
    return models.workspace_payload(store, confirm_timeout_seconds=settings.confirm_timeout_seconds)


@router.post("/onboarding")
async def complete_onboarding(
    payload: OnboardingRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Name the supervisor, keep the look the user picked, and mark the workspace ready.

    The renaming path is the same one PATCH /agents/{id} uses: a trimmed name, an appearance
    validated by :class:`~app.models.AppearanceInput`, and the supervisor's direct thread
    retitled when it still carried the old name.
    """
    supervisor = store.supervisor()
    if supervisor is None:  # pragma: no cover - bootstrap always creates it
        raise HTTPException(status_code=503, detail="工作区还没有负责人。")
    updates: dict[str, Any] = {"name": payload.name}
    if payload.appearance is not None:
        updates["appearance"] = payload.appearance.stored()
    try:
        updated = store.update_agent(supervisor["id"], **updates)
    except AgentNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:  # pragma: no cover - existence checked above
        raise HTTPException(status_code=404, detail="负责人不存在。")
    direct = store.direct_conversation(supervisor["id"])
    if direct is not None and direct["title"] == supervisor["name"]:
        store.update_conversation(direct["id"], title=updated["name"])
    store.set_onboarded()
    return publish_workspace(store, engine)
