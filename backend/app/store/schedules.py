"""Scheduled tasks: the rule, the next fire, its hidden workspace and its live run."""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import new_id, now_iso

def _schedule_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "prompt": row["prompt"],
        "conversation_id": row["conversation_id"],
        "conversation_title": row["conversation_title"],
        "kind": row["kind"],
        "at": row["at"],
        "every_minutes": row["every_minutes"],
        "cron": row["cron"],
        "timezone": row["timezone"],
        "use_computer": bool(row["use_computer"]),
        "enabled": bool(row["enabled"]),
        "next_run_at": row["next_run_at"],
        "queued": bool(row["queued"]),
        "last_run_at": row["last_run_at"],
        "last_run_id": row["last_run_id"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "run_count": int(row["run_count"]),
        "owner_agent_id": row["owner_agent_id"],
        "workspace_conversation_id": row["workspace_conversation_id"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # The run working in the hidden workspace right now, when the query joined it in.
        "running": _live_run(row),
    }


# A schedule with the run its workspace is busy with, if any: the panel shows that it works.
SCHEDULE_WITH_RUN = """
    SELECT s.*, r.id AS live_id, r.current_step AS live_step, r.created_at AS live_started,
           r.model_calls AS live_model_calls, r.tool_calls AS live_tool_calls
      FROM scheduled_tasks s
      LEFT JOIN runs r ON r.conversation_id = s.workspace_conversation_id AND r.status = 'running'
"""


def _live_run(row: sqlite3.Row) -> dict | None:
    """What the joined running run is doing, or ``None`` (also for queries without the join)."""
    if "live_id" not in row.keys() or row["live_id"] is None:
        return None
    return {
        "run_id": row["live_id"],
        "step": row["live_step"],
        "started_at": row["live_started"],
        "model_calls": row["live_model_calls"],
        "tool_calls": row["live_tool_calls"],
    }

class Schedules:
    """Scheduled tasks and their hidden workspaces."""

    def create_schedule(
        self,
        *,
        title: str,
        prompt: str,
        conversation_id: str,
        conversation_title: str,
        kind: str,
        at: str | None,
        every_minutes: int | None,
        cron: str | None,
        timezone_name: str,
        use_computer: bool,
        next_run_at: str | None,
        created_by: str,
        owner_agent_id: str | None = None,
        enabled: bool = True,
        system: bool = False,
    ) -> dict:
        if owner_agent_id is None:
            supervisor = self.supervisor()
            owner_agent_id = supervisor["id"] if supervisor is not None else None
        schedule_id = new_id("s")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO scheduled_tasks
                    (id, title, prompt, conversation_id, conversation_title, kind, at,
                     every_minutes, cron, timezone, use_computer, enabled, next_run_at, queued,
                     last_run_at, last_run_id, last_status, last_error, run_count,
                     owner_agent_id, created_by, created_at, updated_at, system)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, 0, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    title,
                    prompt,
                    conversation_id,
                    conversation_title,
                    kind,
                    at,
                    every_minutes,
                    cron,
                    timezone_name,
                    int(use_computer),
                    int(enabled),
                    next_run_at,
                    owner_agent_id,
                    created_by,
                    timestamp,
                    timestamp,
                    int(system),
                ),
            )
        schedule = self.get_schedule(schedule_id)
        assert schedule is not None
        return schedule
    def update_schedule(self, schedule_id: str, **fields: Any) -> dict | None:
        """Change any stored field; unknown keys are a programming error, not a request."""
        allowed = {
            "title",
            "prompt",
            "conversation_id",
            "conversation_title",
            "kind",
            "at",
            "every_minutes",
            "cron",
            "timezone",
            "use_computer",
            "enabled",
            "next_run_at",
            "queued",
            "last_run_at",
            "last_run_id",
            "last_status",
            "last_error",
            "run_count",
            "system",
        }
        sets: list[str] = []
        params: list[Any] = []
        for column, value in fields.items():
            if column not in allowed:
                raise KeyError(f"未知的定时任务字段：{column}")
            if column in ("use_computer", "enabled", "queued", "system"):
                value = int(bool(value))
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return self.get_schedule(schedule_id)
        params.extend([now_iso(), schedule_id])
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(
                f"UPDATE scheduled_tasks SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                params,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_schedule(schedule_id)
    def delete_schedule(self, schedule_id: str) -> dict | None:
        """Remove one schedule and the hidden workspace its runs used."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                return None
            tx.execute("DELETE FROM scheduled_tasks WHERE id = ?", (schedule_id,))
            if row["workspace_conversation_id"]:
                self._delete_conversation_rows(tx, str(row["workspace_conversation_id"]))
            return _schedule_row(row)
    def get_schedule(self, schedule_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(f"{SCHEDULE_WITH_RUN} WHERE s.id = ?", (schedule_id,)).fetchone()
        return None if row is None else _schedule_row(row)
    def list_schedules(self) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                f"{SCHEDULE_WITH_RUN} ORDER BY s.created_at ASC, s.rowid ASC"
            ).fetchall()
        return [_schedule_row(row) for row in rows]
    def list_visible_schedules(self) -> list[dict]:
        """Every user-facing schedule; a system schedule (the team digest) stays hidden."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                f"{SCHEDULE_WITH_RUN} WHERE s.system = 0 ORDER BY s.created_at ASC, s.rowid ASC"
            ).fetchall()
        return [_schedule_row(row) for row in rows]
    def digest_schedule(self) -> dict | None:
        """The one system schedule backing the team digest, or ``None``."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                f"{SCHEDULE_WITH_RUN} WHERE s.system = 1 ORDER BY s.rowid ASC LIMIT 1"
            ).fetchone()
        return None if row is None else _schedule_row(row)
    def schedules_for_conversation(self, conversation_id: str) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE conversation_id = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (conversation_id,),
            ).fetchall()
        return [_schedule_row(row) for row in rows]
    def enabled_schedule_count(self, *, exclude_id: str | None = None) -> int:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM scheduled_tasks "
                "WHERE enabled = 1 AND system = 0 AND (? IS NULL OR id != ?)",
                (exclude_id, exclude_id),
            ).fetchone()
        return int(row["total"])
    def pending_reminder_count(self, agent_id: str) -> int:
        """How many reminders one bot still has waiting to fire."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM scheduled_tasks "
                "WHERE kind = 'reminder' AND enabled = 1 AND system = 0 "
                "AND owner_agent_id = ?",
                (agent_id,),
            ).fetchone()
        return int(row["total"])
    def due_schedules(self, now_iso_utc: str) -> list[dict]:
        """Enabled schedules whose next fire is at or before ``now`` (canonical UTC)."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE enabled = 1 AND next_run_at IS NOT NULL "
                "AND next_run_at <= ? ORDER BY next_run_at ASC, rowid ASC",
                (now_iso_utc,),
            ).fetchall()
        return [_schedule_row(row) for row in rows]
    def queued_schedules(self) -> list[dict]:
        """Due-but-waiting schedules, oldest first; pausing one drops its pending fire."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE queued = 1 AND enabled = 1 "
                "ORDER BY updated_at ASC, rowid ASC"
            ).fetchall()
        return [_schedule_row(row) for row in rows]
