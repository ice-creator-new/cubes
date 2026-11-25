"""Team settings: the workspace's review and digest switches.

Stored as ``meta`` keys (like the memory switches) so every change applies from the next
read, and every change publishes the ``team_settings`` SSE event with the same payload the
GET returns.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..engine import SupervisorEngine
from .. import permissions as permission_rules
from .. import providers
from .. import step_budget as step_budget_rules
from ..models import PermissionSettingsRequest, TeamSettingsRequest
from ..scheduler import Scheduler
from ..store import Store
from .common import get_engine, get_scheduler, get_settings, get_store

router = APIRouter()


def permission_state(store: Store, settings) -> dict:
    """The mode, the extra folders, and the working folder relative paths land in."""
    state = store.permission_settings()
    return {
        **state,
        "working_folder": str(permission_rules.working_folder(settings)),
        "home": str(permission_rules.Path.home()),
    }


@router.get("/settings/permissions")
async def get_permission_settings(store: Store = Depends(get_store), settings=Depends(get_settings)) -> dict:
    return permission_state(store, settings)


@router.patch("/settings/permissions")
async def patch_permission_settings(
    payload: PermissionSettingsRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    settings=Depends(get_settings),
) -> dict:
    """Change the approval mode or the folders; a refused folder answers 422 with the reason."""
    folders = None
    if payload.folders is not None:
        try:
            folders = permission_rules.clean_folders(settings, payload.folders)
        except permission_rules.FolderRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    store.set_permission_settings(mode=payload.mode, folders=folders)
    state = permission_state(store, settings)
    engine.publish("permission_settings", state)
    return state


def team_state(store: Store, settings) -> dict:
    """The stored switches plus every step preset's resolved numbers, read-only."""
    return {
        **store.team_settings(),
        "step_budgets": step_budget_rules.all_presets(settings),
        "dispatch_health": store.dispatch_health(),
    }


@router.get("/settings/team")
async def get_team_settings(
    store: Store = Depends(get_store), settings=Depends(get_settings)
) -> dict:
    """The workspace's team switches, with the digest's derived ``schedule_id``."""
    return team_state(store, settings)


@router.patch("/settings/team")
async def patch_team_settings(
    payload: TeamSettingsRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    settings=Depends(get_settings),
) -> dict:
    """Merge a partial body (each nested slot partial too) and broadcast the new state."""
    review: dict = {}
    if payload.review is not None:
        if payload.review.enabled is not None:
            review["enabled"] = payload.review.enabled
        if payload.review.max_rounds is not None:
            review["max_rounds"] = payload.review.max_rounds
    digest: dict = {}
    if payload.digest is not None:
        if payload.digest.enabled is not None:
            digest["enabled"] = payload.digest.enabled
        if payload.digest.frequency is not None:
            digest["frequency"] = payload.digest.frequency
        if payload.digest.time is not None:
            digest["time"] = payload.digest.time
        if payload.digest.weekday is not None:
            digest["weekday"] = payload.digest.weekday
    updates: dict = {}
    if "dispatch_model" in payload.model_fields_set:
        ref = None if payload.dispatch_model is None else payload.dispatch_model.model_dump()
        try:
            providers.validate_model_ref(store, ref)
        except providers.ProviderInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        updates["dispatch_model"] = ref
    store.set_team_settings(
        send_mode=payload.send_mode,
        step_budget=payload.step_budget,
        supervisor_works_in_groups=payload.supervisor_works_in_groups,
        max_parallel_tasks=payload.max_parallel_tasks,
        review=review or None,
        digest=digest or None,
        timezone=settings.timezone,
        **updates,
    )
    state = team_state(store, settings)
    engine.publish("team_settings", state)
    return state


@router.post("/settings/team/digest/run", status_code=202)
async def run_digest_now(
    store: Store = Depends(get_store),
    scheduler: Scheduler = Depends(get_scheduler),
) -> dict:
    """Fire the digest immediately, in its own hidden workspace: ``{"run_id": …}``."""
    schedule = store.digest_schedule()
    if schedule is None or not schedule["enabled"]:
        raise HTTPException(status_code=409, detail="团队简报还没打开。")
    workspace_id = schedule["workspace_conversation_id"]
    if workspace_id is not None and store.active_run(str(workspace_id)) is not None:
        raise HTTPException(status_code=409, detail="团队简报正在生成，稍等它写完。")
    updated = await scheduler.fire_now(schedule["id"])
    if updated is None:  # pragma: no cover - the row exists above
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    return {"run_id": updated["last_run_id"]}
