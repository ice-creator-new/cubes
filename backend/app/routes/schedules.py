"""Scheduled tasks: their rules, their fires and the workspace a fire runs in."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import models
from ..agent.run_state import RunLauncher
from ..config import Settings
from ..constants import USER_AUTHOR_NAME
from ..engine import SupervisorEngine
from ..models import ScheduleCreateRequest, SchedulePatchRequest
from ..scheduler import Scheduler
from ..schedules import (
    MAX_ENABLED,
    ScheduleError,
    next_run_on_resume,
    normalize_rule,
    rule_of,
    schedule_payload,
    schedules_payload,
)
from ..store import Store
from .common import get_engine, get_launcher, get_scheduler, get_settings, get_store

router = APIRouter()


def schedules_state(store: Store, settings: Settings) -> dict:
    return schedules_payload(store.list_visible_schedules(), settings.timezone)


def publish_schedules(
    store: Store, engine: SupervisorEngine, settings: Settings
) -> dict:
    """Shape the schedule list, push it to every window and return it."""
    payload = schedules_state(store, settings)
    engine.publish("schedules", payload)
    return payload


def schedule_target(store: Store, conversation_id: str) -> dict:
    conversation = store.get_conversation(conversation_id)
    # A hidden workspace does not exist as far as any client is concerned.
    if conversation is None or conversation["kind"] == "background":
        raise HTTPException(status_code=404, detail="会话不存在。")
    if conversation["kind"] == "private":
        raise HTTPException(status_code=422, detail="私聊只能旁观，不能作为定时任务的目标。")
    return conversation


def rule_now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/schedules")
async def list_schedules(
    store: Store = Depends(get_store), settings: Settings = Depends(get_settings)
) -> dict:
    return schedules_state(store, settings)


@router.post("/schedules", status_code=201)
async def create_schedule(
    payload: ScheduleCreateRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    settings: Settings = Depends(get_settings),
    scheduler: Scheduler = Depends(get_scheduler),
) -> dict:
    """A schedule the user wrote in the client: no confirmation card, it is their own."""
    conversation = schedule_target(store, payload.conversation_id)
    try:
        rule = normalize_rule(
            kind=payload.kind,
            at=payload.at,
            every_minutes=payload.every_minutes,
            cron=payload.cron,
            timezone_name=payload.timezone or settings.timezone,
            now=rule_now(),
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.enabled and store.enabled_schedule_count() >= MAX_ENABLED:
        raise HTTPException(
            status_code=409,
            detail=f"最多只能有 {MAX_ENABLED} 个启用的定时任务，请先暂停或删除一个。",
        )
    created = store.create_schedule(
        title=payload.title,
        prompt=payload.prompt,
        conversation_id=conversation["id"],
        conversation_title=conversation["title"],
        kind=rule["kind"],
        at=rule["at"],
        every_minutes=rule["every_minutes"],
        cron=rule["cron"],
        timezone_name=rule["timezone"],
        use_computer=payload.use_computer,
        next_run_at=rule["next_run_at"],
        created_by=USER_AUTHOR_NAME,
        enabled=payload.enabled,
    )
    scheduler.wake()
    publish_schedules(store, engine, settings)
    return schedule_payload(created)


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: str,
    payload: SchedulePatchRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    settings: Settings = Depends(get_settings),
    scheduler: Scheduler = Depends(get_scheduler),
) -> dict:
    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    updates: dict[str, Any] = {}
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.prompt is not None:
        updates["prompt"] = payload.prompt
    if payload.conversation_id is not None:
        target = schedule_target(store, payload.conversation_id)
        updates["conversation_id"] = target["id"]
        updates["conversation_title"] = target["title"]
    rule_changed = bool(
        payload.model_fields_set & {"kind", "at", "every_minutes", "cron", "timezone"}
    )
    current = rule_of(schedule)
    if rule_changed:
        try:
            normalized = normalize_rule(
                kind=payload.kind or schedule["kind"],
                at=payload.at if payload.at is not None else schedule["at"],
                every_minutes=(
                    payload.every_minutes
                    if payload.every_minutes is not None
                    else schedule["every_minutes"]
                ),
                cron=payload.cron if payload.cron is not None else schedule["cron"],
                timezone_name=payload.timezone or schedule["timezone"],
                now=rule_now(),
            )
        except ScheduleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for key in ("kind", "at", "every_minutes", "cron", "timezone", "next_run_at"):
            updates[key] = normalized[key]
        current = {key: normalized[key] for key in current}
    if payload.use_computer is not None:
        updates["use_computer"] = payload.use_computer
    if payload.enabled is not None:
        if payload.enabled and not schedule["enabled"]:
            if store.enabled_schedule_count(exclude_id=schedule["id"]) >= MAX_ENABLED:
                raise HTTPException(
                    status_code=409,
                    detail=f"最多只能有 {MAX_ENABLED} 个启用的定时任务，请先暂停或删除一个。",
                )
            if not rule_changed:
                try:
                    updates["next_run_at"] = next_run_on_resume(current, rule_now())
                except ScheduleError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
        updates["enabled"] = payload.enabled
        if not payload.enabled:
            updates["queued"] = False
    if not updates:
        raise HTTPException(status_code=422, detail="没有需要修改的内容。")
    updated = store.update_schedule(schedule_id, **updates)
    if updated is None:  # pragma: no cover - existence checked above
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    scheduler.wake()
    publish_schedules(store, engine, settings)
    return schedule_payload(updated)


@router.post("/schedules/{schedule_id}/run", status_code=202)
async def run_schedule_now(
    schedule_id: str, scheduler: Scheduler = Depends(get_scheduler)
) -> dict:
    """Start it now, or queue it behind the reply already running in its conversation."""
    updated = await scheduler.fire_now(schedule_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    return schedule_payload(updated)


@router.get("/schedules/{schedule_id}/run")
async def schedule_run_detail(schedule_id: str, store: Store = Depends(get_store)) -> dict:
    """What the schedule's newest fire did (or is doing): read-only, from its workspace.

    The workspace holds exactly one fire at a time (each fire starts it afresh), so
    its messages are that run's whole story: the prompt, every step and the answer.
    """
    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    workspace_id = schedule["workspace_conversation_id"]
    run = store.latest_run(workspace_id) if workspace_id else None
    if run is None or workspace_id is None:
        return {"schedule_id": schedule_id, "run": None, "messages": []}
    return {
        "schedule_id": schedule_id,
        "run": models.run_payload(run),
        "messages": [
            models.message_payload(message) for message in store.list_messages(workspace_id)
        ],
    }


@router.post("/schedules/{schedule_id}/stop")
async def stop_schedule_run(
    schedule_id: str,
    store: Store = Depends(get_store),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    """Stop the fire that is working now; a stopped scheduled run posts no summary."""
    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    live = schedule.get("running")
    if live is not None:
        await launcher.finish_stop(live["run_id"])
    return schedule_payload(store.get_schedule(schedule_id) or schedule)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    settings: Settings = Depends(get_settings),
    scheduler: Scheduler = Depends(get_scheduler),
) -> Response:
    if store.delete_schedule(schedule_id) is None:
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    scheduler.wake()
    publish_schedules(store, engine, settings)
    return Response(status_code=204)
