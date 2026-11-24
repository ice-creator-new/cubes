"""Long-term memory: the panel's rows, its two switches and the manual writes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from ..engine import SupervisorEngine
from ..memories import ORIGIN_USER, memories_payload, memory_payload
from ..models import MemoryCreateRequest, MemoryPatchRequest, MemorySettingsRequest
from ..store import Store
from .common import get_engine, get_store

router = APIRouter()


@router.get("/memories")
async def list_memories(store: Store = Depends(get_store)) -> dict:
    """Every row the workspace kept, active and retired, plus the two switches."""
    return memories_payload(store)


@router.patch("/memories/settings")
async def patch_memory_settings(
    payload: MemorySettingsRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Pausing stops both reading and writing until it is switched back on."""
    updates: dict[str, Any] = {}
    if payload.paused is not None:
        updates["paused"] = payload.paused
    if payload.auto_extract is not None:
        updates["auto_extract"] = payload.auto_extract
    store.update_memory_settings(**updates)
    state = memories_payload(store)
    engine.publish("memories", state)
    return state


@router.post("/memories", status_code=201)
async def create_memory(
    payload: MemoryCreateRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    if payload.scope == "agent" and store.get_agent(payload.agent_id or "") is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    if payload.scope == "group":
        group = store.get_conversation(payload.agent_id or "")
        if group is None or group["kind"] != "group":
            raise HTTPException(status_code=404, detail="群组不存在。")
    memory = store.create_memory(
        content=payload.content,
        topic=payload.topic,
        scope=payload.scope,
        agent_id=payload.agent_id,
        origin=ORIGIN_USER,
    )
    engine.publish("memories", memories_payload(store))
    return memory_payload(memory)


@router.patch("/memories/{memory_id}")
async def patch_memory(
    memory_id: str,
    payload: MemoryPatchRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """A changed content answers a new row; the old one is retired in its place."""
    updates: dict[str, Any] = {}
    if payload.content is not None:
        updates["content"] = payload.content
    if payload.topic is not None:
        updates["topic"] = payload.topic
    if payload.pinned is not None:
        updates["pinned"] = payload.pinned
    updated = store.update_memory(memory_id, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="记忆不存在。")
    engine.publish("memories", memories_payload(store))
    return memory_payload(updated)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    """The user's own delete is physical: nothing of the row stays behind."""
    if not store.delete_memory(memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在。")
    engine.publish("memories", memories_payload(store))
    return Response(status_code=204)
