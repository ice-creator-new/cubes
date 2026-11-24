"""The workbench: notebook notes and the shared task list, both pushed on every change."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from ..engine import SupervisorEngine
from ..models import TaskPatchRequest
from ..store import Store
from ..toolkit.workbench import note_payload, notes_payload, tasks_payload
from .common import get_engine, get_store

router = APIRouter()


@router.get("/notes")
async def list_notes(store: Store = Depends(get_store)) -> dict:
    return notes_payload(store)


@router.get("/notes/{note_id}")
async def get_note(note_id: str, store: Store = Depends(get_store)) -> dict:
    note = store.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在。")
    return note_payload(note)


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(
    note_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    """The user's own delete: the workbench already asked for confirmation."""
    if not store.delete_note(note_id):
        raise HTTPException(status_code=404, detail="笔记不存在。")
    engine.publish("notes", notes_payload(store))
    return Response(status_code=204)


@router.get("/tasks")
async def list_tasks(store: Store = Depends(get_store)) -> dict:
    return tasks_payload(store)


@router.patch("/tasks/{task_id}")
async def patch_task(
    task_id: str,
    payload: TaskPatchRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Hand-edited status or note; a blank note clears it, an omitted one is untouched."""
    updates: dict[str, Any] = {}
    if payload.status is not None:
        updates["status"] = payload.status
    if "note" in payload.model_fields_set:
        updates["note"] = payload.note or ""
    updated = store.update_task(task_id, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    engine.publish("tasks", tasks_payload(store))
    return updated
