"""The one server-sent event stream: a snapshot first, then every run's events."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from .. import models
from ..config import Settings
from ..engine import SupervisorEngine
from ..store import Store
from .common import get_engine, get_settings, get_store

HEARTBEAT_SECONDS = 15.0
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

router = APIRouter()


def _sse(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/events")
async def events(
    conversation_id: str | None = None,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    # Subscribe, build the snapshot and drain any already-reflected events in one
    # synchronous block: with no await in between, nothing live can be lost and no
    # event can arrive after socket control was yielded.
    queue = engine.subscribe()
    snapshot = models.snapshot_payload(
        store, conversation_id, confirm_timeout_seconds=settings.confirm_timeout_seconds
    )
    engine.drain(queue)

    async def event_stream():
        try:
            yield _sse("snapshot", snapshot)
            # The stream reports every run for as long as the client keeps it open;
            # idle time is a heartbeat, never an end. Only a shutting-down server ends it
            # (``None`` from ``engine.close_streams``).
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    return
                event, payload = item
                yield _sse(event, payload)
        finally:
            engine.unsubscribe(queue)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=SSE_HEADERS
    )
