"""Scheduled tasks: rule validation, next fire times, Chinese descriptions and payloads.

A rule is one of three shapes — a single local wall time (``once``), a fixed interval
(``interval``), or a five-field cron expression (``cron``). Every rule carries the IANA
timezone the user typed it in, so ``09:00`` keeps meaning 09:00 across restarts and DST;
``next_run_at`` is always stored and served as ISO-8601 UTC with an explicit offset.

This module is pure: it never touches the database, so the store and the HTTP boundary
share exactly one interpretation of a rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

KINDS = ("once", "interval", "cron")
MAX_ENABLED = 20
MIN_INTERVAL_MINUTES = 5
MIN_CRON_GAP = timedelta(minutes=MIN_INTERVAL_MINUTES)
AT_FORMAT = "%Y-%m-%dT%H:%M"
AT_HINT = "2026-10-24T09:00"
DEFAULT_TIMEZONE = "Asia/Shanghai"
# A rule may never produce more than this many skipped occurrences in a row.
MAX_SKIP = 1_000


class ScheduleError(ValueError):
    """A schedule request that cannot be applied; the message is a Chinese sentence."""


def zone_of(name: str | None) -> ZoneInfo:
    """The IANA zone ``name``, or a Chinese error naming the bad value."""
    text = (name or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"时区「{text}」不存在，请使用 Asia/Shanghai 这样的时区名称。") from exc


def utc_iso(moment: datetime) -> str:
    """Canonical stored form: ISO-8601 UTC with an offset, e.g. ``2026-10-24T01:00:00+00:00``."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Read a stored timestamp; a trailing ``Z`` is the same instant as ``+00:00``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_at(value: str, zone: ZoneInfo) -> datetime:
    """``YYYY-MM-DDTHH:MM`` as a wall time in ``zone``."""
    try:
        naive = datetime.strptime(value.strip(), AT_FORMAT)
    except (AttributeError, ValueError) as exc:
        raise ScheduleError(f"触发时间格式应为 {AT_HINT}。") from exc
    return naive.replace(tzinfo=zone)


def rule_of(schedule: dict) -> dict:
    """The rule fields of one stored schedule row."""
    return {
        "kind": schedule["kind"],
        "at": schedule["at"],
        "every_minutes": schedule["every_minutes"],
        "cron": schedule["cron"],
        "timezone": schedule["timezone"],
    }


def normalize_rule(
    *,
    kind: str,
    at: str | None,
    every_minutes: int | None,
    cron: str | None,
    timezone_name: str | None,
    now: datetime,
    require_future: bool = True,
) -> dict:
    """Validate one rule and compute its first fire; raises :class:`ScheduleError`."""
    zone = zone_of(timezone_name)
    if kind not in KINDS:
        raise ScheduleError("触发方式只能是 once／interval／cron。")
    at = (at or "").strip() or None
    cron = (cron or "").strip() or None
    minutes = every_minutes
    if kind == "once":
        if at is None:
            raise ScheduleError(f"一次性任务需要填写触发时间，格式是 {AT_HINT}。")
        moment = parse_at(at, zone)
        cron = None
        minutes = None
        if require_future and moment <= now:
            raise ScheduleError("触发时间必须晚于现在。")
    elif kind == "interval":
        if minutes is None:
            raise ScheduleError("按间隔触发需要填写间隔分钟数。")
        if int(minutes) < MIN_INTERVAL_MINUTES:
            raise ScheduleError(f"定时间隔不能小于 {MIN_INTERVAL_MINUTES} 分钟。")
        minutes = int(minutes)
        at = None
        cron = None
    else:
        if cron is None:
            raise ScheduleError("按 cron 触发需要填写 5 段的 cron 表达式。")
        if len(cron.split()) != 5 or not croniter.is_valid(cron):
            raise ScheduleError(f"cron 表达式「{cron}」无效，需要 5 段：分 时 日 月 周。")
        first = croniter(cron, now.astimezone(zone)).get_next(datetime)
        second = croniter(cron, first).get_next(datetime)
        if second - first < MIN_CRON_GAP:
            raise ScheduleError(f"cron 的两次触发至少相隔 {MIN_INTERVAL_MINUTES} 分钟。")
        at = None
        minutes = None
    rule = {
        "kind": kind,
        "at": at,
        "every_minutes": minutes,
        "cron": cron,
        "timezone": str(zone),
    }
    moment = next_fire_after(rule, now)
    return {**rule, "next_run_at": None if moment is None else utc_iso(moment)}


def next_fire_after(rule: dict, after: datetime) -> datetime | None:
    """The next fire strictly after ``after``, or ``None`` for a spent ``once`` rule."""
    zone = zone_of(rule["timezone"])
    kind = rule["kind"]
    if kind == "once":
        moment = parse_at(str(rule["at"] or ""), zone)
        return moment if moment > after else None
    if kind == "interval":
        return after.astimezone(zone) + timedelta(minutes=int(rule["every_minutes"]))
    return croniter(str(rule["cron"]), after.astimezone(zone)).get_next(datetime)


def next_future(rule: dict, now: datetime) -> datetime | None:
    """The first fire after ``now``, skipping occurrences missed while nothing was running."""
    moment = next_fire_after(rule, now)
    for _ in range(MAX_SKIP):
        if moment is None or moment > now:
            return moment
        moment = next_fire_after(rule, moment)
    return None  # pragma: no cover - a valid interval or cron always advances


def next_run_on_resume(rule: dict, now: datetime) -> str:
    """The next fire when a paused rule is switched back on; a spent ``once`` cannot."""
    moment = next_fire_after(rule, now)
    if moment is None:
        raise ScheduleError("触发时间已经过去，请重新设置一个时间。")
    return utc_iso(moment)


def describe(*, kind: str, at: str | None, every_minutes: int | None, cron: str | None) -> str:
    """The human rule in Chinese, e.g.「每天 09:00」/「每 30 分钟」/「2026-10-24 09:00 一次」."""
    if kind == "once":
        return f"{(at or '').replace('T', ' ')} 一次"
    if kind == "reminder":
        return f"{(at or '').replace('T', ' ')} 的提醒"
    if kind == "interval":
        minutes = int(every_minutes or 0)
        if minutes >= 60 and minutes % 60 == 0:
            return f"每 {minutes // 60} 小时"
        return f"每 {minutes} 分钟"
    text = (cron or "").strip()
    fields = text.split()
    if len(fields) == 5 and fields[2:] == ["*", "*", "*"]:
        minute, hour = fields[0], fields[1]
        if minute.isdigit() and hour.isdigit():
            return f"每天 {int(hour):02d}:{int(minute):02d}"
    return f"cron {text}"


def local_time(moment: datetime, timezone_name: str) -> str:
    """``YYYY-MM-DD HH:MM`` in the rule's own timezone, as the user would read it."""
    return moment.astimezone(zone_of(timezone_name)).strftime("%Y-%m-%d %H:%M")


def next_run_text(schedule: dict) -> str:
    """The next fire in the schedule's timezone, for confirmation prompts and tool results."""
    if not schedule.get("next_run_at"):
        return "不再触发"
    return local_time(parse_iso(str(schedule["next_run_at"])), str(schedule["timezone"]))


def schedule_payload(schedule: dict) -> dict:
    """One stored schedule as the wire shape shared by HTTP, SSE and tool results."""
    return {
        "id": schedule["id"],
        "title": schedule["title"],
        "prompt": schedule["prompt"],
        "conversation_id": schedule["conversation_id"],
        "conversation_title": schedule["conversation_title"],
        "kind": schedule["kind"],
        "at": schedule["at"],
        "every_minutes": schedule["every_minutes"],
        "cron": schedule["cron"],
        "timezone": schedule["timezone"],
        "use_computer": bool(schedule["use_computer"]),
        "enabled": bool(schedule["enabled"]),
        "next_run_at": schedule["next_run_at"],
        "queued": bool(schedule["queued"]),
        "last_run_at": schedule["last_run_at"],
        "last_run_id": schedule["last_run_id"],
        "last_status": schedule["last_status"],
        "last_error": schedule["last_error"],
        "run_count": int(schedule["run_count"]),
        # A bot's own reminder, listed under its own heading and fired in the chat it was made in.
        "reminder": schedule["kind"] == "reminder",
        # Working right now in its hidden workspace: the run, its step and its counters.
        "running": schedule.get("running"),
        "created_by": schedule["created_by"],
        "created_at": schedule["created_at"],
        "updated_at": schedule["updated_at"],
        "describe": describe(
            kind=schedule["kind"],
            at=schedule["at"],
            every_minutes=schedule["every_minutes"],
            cron=schedule["cron"],
        ),
    }


def schedules_payload(schedules: list[dict], timezone_name: str) -> dict:
    """``GET /schedules``: every schedule, the default timezone and the hard limits."""
    return {
        "schedules": [schedule_payload(schedule) for schedule in schedules],
        "timezone": timezone_name,
        "limits": {"max_enabled": MAX_ENABLED, "min_interval_minutes": MIN_INTERVAL_MINUTES},
    }
