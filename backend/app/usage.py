"""Per-agent token usage: the period bucketing, the number words and the budget words.

Pure helpers only. The store reads the ``model_usage`` rows and the engine enforces the
budget; this module owns the period arithmetic (in the workspace timezone), the 万/亿
formatter and the Chinese a confirm card and a ``limited`` error show.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PERIODS = ("day", "week", "month")
# The word one period reads as in a confirm card / error: 今天 / 本周 / 本月.
PERIOD_WORDS = {"day": "今天", "week": "本周", "month": "本月"}
# The purposes ``model_usage.purpose`` stores; ``by_purpose`` always carries these keys.
PURPOSES = ("reply", "compact", "memory-extract", "review", "dispatch", "reaction")


def period_start(period: str, now: datetime, zone: ZoneInfo) -> datetime:
    """The aware moment one period began: local midnight / Monday / the 1st, in ``zone``."""
    local = now.astimezone(zone)
    if period == "week":
        start = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # day
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start


def _combine(day: date, zone: ZoneInfo, hour: int = 0) -> datetime:
    """A wall-clock ``day`` at ``hour`` o'clock in ``zone``, aware and DST-safe."""
    return datetime.combine(day, time(hour, 0, 0), tzinfo=zone)


def _next_month(day: date) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, 1)


def _previous_month(day: date) -> date:
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return date(year, month, 1)


def period_bounds(period: str, now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """``(start, end)``: the half-open period ``[start, end)`` in ``zone``, ``end`` exclusive.

    Boundaries are local wall-clock dates re-attached to ``zone``, so a DST change inside the
    period never skews ``end`` by an hour.
    """
    start = period_start(period, now, zone)
    if period == "week":
        end = _combine(start.date() + timedelta(days=7), zone)
    elif period == "month":
        end = _combine(_next_month(start.date()), zone)
    else:  # day
        end = _combine(start.date() + timedelta(days=1), zone)
    return start, end


def previous_period_start(period: str, now: datetime, zone: ZoneInfo) -> datetime:
    """The moment the period immediately before this one began."""
    start = period_start(period, now, zone)
    if period == "week":
        return _combine(start.date() - timedelta(days=7), zone)
    if period == "month":
        return _combine(_previous_month(start.date()), zone)
    return _combine(start.date() - timedelta(days=1), zone)


def bucket_starts(period: str, now: datetime, zone: ZoneInfo) -> list[datetime]:
    """Ordered local bucket boundaries covering one whole period, first = :func:`period_start`.

    day → 24 hourly buckets, week → 7 daily, month → one per calendar day. Every boundary is
    built from a local wall-clock date, so buckets stay gap-free across DST transitions.
    """
    start = period_start(period, now, zone)
    if period == "day":
        return [_combine(start.date(), zone, hour=h) for h in range(24)]
    if period == "week":
        return [_combine(start.date() + timedelta(days=i), zone) for i in range(7)]
    days = calendar.monthrange(start.year, start.month)[1]
    return [_combine(date(start.year, start.month, day), zone) for day in range(1, days + 1)]


def period_word(period: str) -> str:
    return PERIOD_WORDS[period]


def utc_key(moment: datetime) -> str:
    """The ``created_at`` comparison form: UTC ``...Z``, matching :func:`store.now_iso`."""
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def format_tokens(n: int) -> str:
    """A token count for the user: a plain integer below 1 万, else 万/亿 with one decimal."""
    if n >= 100_000_000:
        return _scaled(n, 100_000_000, "亿")
    # Unit picked on the rounded tenths, so 99 999 500 is 1 亿 rather than 10000 万.
    if n >= 10_000 and (int(n) + 500) // 1_000 >= 100_000:
        return _scaled(n, 100_000_000, "亿")
    if n >= 10_000:
        return _scaled(n, 10_000, "万")
    return str(int(n))


def _scaled(n: int, base: int, unit: str) -> str:
    # Half up, in tenths, the way the web client rounds: 12 500 is 1.3 万 on both sides.
    tenths = (int(n) * 10 + base // 2) // base
    whole, tenth = divmod(tenths, 10)
    text = str(whole) if tenth == 0 else f"{whole}.{tenth}"
    return f"{text} {unit}"
