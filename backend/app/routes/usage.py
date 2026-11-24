"""Token usage: one roll-up of every model call per agent, bucketed by period."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import usage as usage_rules
from ..config import Settings
from ..store import Store
from .common import get_settings, get_store

router = APIRouter()


@router.get("/usage")
async def get_usage(
    period: str = Query("week"),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> dict:
    if period not in usage_rules.PERIODS:
        raise HTTPException(status_code=422, detail="period 需为 day、week 或 month。")
    try:
        zone = ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - validated at startup
        raise HTTPException(status_code=500, detail="时区配置无效。") from exc
    return store.usage_report(period=period, now=datetime.now(timezone.utc), zone=zone)
