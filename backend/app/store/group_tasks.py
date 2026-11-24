"""Work a group took on: one task per member answering a request, and the parts handed on."""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from .db import _UNSET, new_id, now_iso

RUNNING = "running"
QUEUED = "queued"
# The thread's run is parked on an action that waits for the user (a confirm card, a question).
AWAITING = "awaiting"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
TASK_STATUSES = (QUEUED, RUNNING, AWAITING, DONE, FAILED, STOPPED)
# A task that is still occupying a thread slot, or still visible as unfinished work.
UNFINISHED = (QUEUED, RUNNING, AWAITING)
OUTCOME = (STOPPED, FAILED)
# A task title is the opening of the request, enough to tell tasks apart in a list.
TITLE_CHARS = 60


def task_title(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= TITLE_CHARS else f"{flat[: TITLE_CHARS - 1]}…"


def _row(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


class GroupTasks:
    """Group tasks."""

    def create_group_task(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        title: str,
        instruction: str = "",
        request_message_id: str | None = None,
        parent_id: str | None = None,
        assigned_by: str | None = None,
        run_id: str | None = None,
        status: str = RUNNING,
    ) -> dict:
        task_id = new_id("gt")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO group_tasks
                    (id, conversation_id, request_message_id, parent_id, owner_id, assigned_by,
                     title, instruction, status, run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, conversation_id, request_message_id, parent_id, owner_id,
                    assigned_by, task_title(title), instruction, status, run_id,
                    timestamp, timestamp,
                ),
            )
        task = self.get_group_task(task_id)
        assert task is not None
        return task

    def get_group_task(self, task_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM group_tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _row(row)

    def update_group_task(
        self,
        task_id: str,
        *,
        status: str | object = _UNSET,
        owner_id: str | object = _UNSET,
        assigned_by: str | object = _UNSET,
        run_id: str | object = _UNSET,
        result_message_id: str | None | object = _UNSET,
        thread_id: str | None | object = _UNSET,
        card_message_id: str | None | object = _UNSET,
        current_step: str | object = _UNSET,
        started_at: str | None | object = _UNSET,
        latest_text: str | object = _UNSET,
        title: str | object = _UNSET,
        instruction: str | object = _UNSET,
        end_reason: str | None | object = _UNSET,
        stopped_by: str | None | object = _UNSET,
        dismissed_at: str | None | object = _UNSET,
        seen_at: str | None | object = _UNSET,
        another_round: bool = False,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("owner_id", owner_id),
            ("assigned_by", assigned_by),
            ("run_id", run_id),
            ("result_message_id", result_message_id),
            ("thread_id", thread_id),
            ("card_message_id", card_message_id),
            ("current_step", current_step),
            ("started_at", started_at),
            ("latest_text", latest_text),
            ("title", title),
            ("instruction", instruction),
            ("end_reason", end_reason),
            ("stopped_by", stopped_by),
            ("dismissed_at", dismissed_at),
            ("seen_at", seen_at),
        ):
            if value is not _UNSET:
                sets.append(f"{column} = ?")
                params.append(value)
        if another_round:
            sets.append("rounds = rounds + 1")
        if not sets:
            return self.get_group_task(task_id)
        params.extend([now_iso(), task_id])
        with self._transaction() as tx:
            tx.execute(f"UPDATE group_tasks SET {', '.join(sets)}, updated_at = ? WHERE id = ?", params)
        return self.get_group_task(task_id)

    def list_group_tasks(self, conversation_id: str, limit: int = 50) -> list[dict]:
        """The newest tasks of one group, newest first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM group_tasks WHERE conversation_id = ?
                 ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (conversation_id, max(1, int(limit))),
            ).fetchall()
        return [_row(row) for row in rows]

    def run_group_tasks(self, run_id: str) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM group_tasks WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
            ).fetchall()
        return [_row(row) for row in rows]

    def settle_group_tasks(
        self, run_id: str, status: str, *, end_reason: str | None = None, stopped_by: str | None = None
    ) -> list[dict]:
        """Every task this run left unfinished takes the run's own ending; answers them all."""
        sets = "status = ?, updated_at = ?"
        params: list[Any] = [status, now_iso()]
        if end_reason is not None:
            sets += ", end_reason = ?"
            params.append(end_reason)
        if stopped_by is not None:
            sets += ", stopped_by = ?"
            params.append(stopped_by)
        params.extend([run_id, RUNNING, AWAITING])
        with self._transaction() as tx:
            tx.execute(
                f"UPDATE group_tasks SET {sets} WHERE run_id = ? AND status IN (?, ?)",
                params,
            )
        return self.run_group_tasks(run_id)

    def open_tasks(self, conversation_id: str) -> list[dict]:
        """Tasks a new user message may steer or resume: running, waiting, queued or stopped."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM group_tasks
                 WHERE conversation_id = ? AND parent_id IS NULL
                   AND status IN (?, ?, ?, ?) AND dismissed_at IS NULL
                 ORDER BY created_at, rowid
                """,
                (conversation_id, RUNNING, AWAITING, QUEUED, STOPPED),
            ).fetchall()
        return [_row(row) for row in rows]

    def mark_task_dismissed(self, task_id: str) -> dict | None:
        """「不做了」: put a stopped/failed task down, out of the sidebar and the chip list."""
        return self.update_group_task(task_id, dismissed_at=now_iso())

    def mark_task_seen(self, task_id: str) -> dict | None:
        """A finished task whose result the user has seen leaves the sidebar."""
        return self.update_group_task(task_id, seen_at=now_iso())

    def task_answered_by(self, message_id: str) -> dict | None:
        """The task whose result is ``message_id``, if any."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM group_tasks WHERE result_message_id = ? ORDER BY rowid DESC LIMIT 1",
                (message_id,),
            ).fetchone()
        return None if row is None else _row(row)

    def task_of_thread(self, thread_id: str) -> dict | None:
        """The task whose hidden thread is ``thread_id``, if any."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM group_tasks WHERE thread_id = ? ORDER BY rowid DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return None if row is None else _row(row)

    def bump_group_task_step(self, task_id: str, step: str) -> dict | None:
        """One tool call counted, and the short step it is on now."""
        with self._transaction() as tx:
            tx.execute(
                "UPDATE group_tasks SET steps = steps + 1, current_step = ?, updated_at = ? "
                "WHERE id = ?",
                (step, now_iso(), task_id),
            )
        return self.get_group_task(task_id)

    def request_tasks(self, request_message_id: str) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM group_tasks WHERE request_message_id = ? ORDER BY created_at, rowid",
                (request_message_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def running_children(self, conversation_id: str) -> int:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM group_tasks
                 WHERE conversation_id = ? AND parent_id IS NOT NULL AND status IN (?, ?, ?)
                """,
                (conversation_id, RUNNING, QUEUED, AWAITING),
            ).fetchone()
        return int(row["n"])

    # --------------------------------------------------------------- task threads

    def queued_tasks(self, *, group_id: str | None = None) -> list[dict]:
        """Tasks waiting for a thread slot, oldest first; the scheduler starts them in order."""
        conn = self._ensure()
        with self._lock:
            if group_id is None:
                rows = conn.execute(
                    "SELECT * FROM group_tasks WHERE status = ? AND thread_id IS NOT NULL "
                    "ORDER BY created_at, rowid",
                    (QUEUED,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM group_tasks WHERE status = ? AND thread_id IS NOT NULL "
                    "AND conversation_id = ? ORDER BY created_at, rowid",
                    (QUEUED, group_id),
                ).fetchall()
        return [_row(row) for row in rows]

    def running_thread_task(self, owner_id: str, *, exclude: str | None = None) -> dict | None:
        """The one thread task this member is working on right now, if any."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT * FROM group_tasks
                 WHERE owner_id = ? AND thread_id IS NOT NULL AND status IN (?, ?)
                   AND (? IS NULL OR id != ?)
                 ORDER BY created_at, rowid LIMIT 1
                """,
                (owner_id, RUNNING, AWAITING, exclude, exclude),
            ).fetchone()
        return None if row is None else _row(row)

    def running_thread_tasks(self) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM group_tasks WHERE status IN (?, ?) AND thread_id IS NOT NULL "
                "ORDER BY created_at, rowid",
                (RUNNING, AWAITING),
            ).fetchall()
        return [_row(row) for row in rows]

    def task_of_run(self, run_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM group_tasks WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return None if row is None else _row(row)

    def update_task_text(self, task_id: str, text: str) -> dict | None:
        """The thread's newest prose line, for the card's live window."""
        value = " ".join(str(text).split())[-200:]
        with self._transaction() as tx:
            tx.execute(
                "UPDATE group_tasks SET latest_text = ?, updated_at = ? WHERE id = ?",
                (value, now_iso(), task_id),
            )
        return self.get_group_task(task_id)

    def recent_action_summaries(self, conversation_id: str, limit: int = 3) -> list[str]:
        """The newest finished tool steps in one conversation, oldest first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT summary FROM actions
                 WHERE conversation_id = ? AND status IN ('completed', 'error')
                 ORDER BY rowid DESC LIMIT ?
                """,
                (conversation_id, max(1, int(limit))),
            ).fetchall()
        return [str(row["summary"]) for row in reversed(rows)]

    def awaiting_action_id(self, conversation_id: str | None) -> str | None:
        if not conversation_id:
            return None
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT id FROM actions WHERE conversation_id = ? AND status = 'awaiting' "
                "ORDER BY rowid LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return None if row is None else str(row["id"])

    def task_payload(self, task: dict) -> dict:
        """One task with the contract's live fields (all derived at read time)."""
        view = dict(task)
        thread_id = view.get("thread_id")
        view["awaiting_action_id"] = self.awaiting_action_id(thread_id)
        view["recent_steps"] = (
            self.recent_action_summaries(thread_id, limit=3) if thread_id else []
        )
        waiting_for = None
        if view.get("run_id"):
            run = self.get_run(str(view["run_id"]))
            step = str((run or {}).get("current_step") or "")
            if step.startswith("等「"):
                waiting_for = step
        view.setdefault("waiting_for", waiting_for)
        view["waiting_for"] = waiting_for
        view.setdefault("latest_text", "")
        view.setdefault("started_at", None)
        return view

    def group_tasks_payload(self, conversation_id: str, limit: int = 50) -> list[dict]:
        return [
            self.task_payload(task)
            for task in self.list_group_tasks(conversation_id, limit=limit)
        ]

    def list_group_threads(self) -> list[dict]:
        """The task threads the sidebar shows: live work, un-dismissed failures, unseen results.

        A done task leaves once its result has been seen; a stopped/failed one stays until the
        user puts it down with 「不做了」. An archived (dismissed) row is hidden everywhere but
        its card in the group's timeline, which renders greyed.
        """
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT t.* FROM group_tasks t
                  JOIN conversations c ON c.id = t.thread_id AND c.kind = 'thread'
                 WHERE t.thread_id IS NOT NULL AND t.dismissed_at IS NULL
                   AND (
                     t.status IN (?, ?, ?, ?, ?)
                     OR (t.status = ? AND t.seen_at IS NULL)
                   )
                 ORDER BY t.created_at, t.rowid
                """,
                (QUEUED, RUNNING, AWAITING, STOPPED, FAILED, DONE),
            ).fetchall()
        return [
            self._thread_entry(self.task_payload(_row(row))) for row in rows
        ]

    @staticmethod
    def _thread_entry(view: dict) -> dict:
        """One sidebar row, under the contract's names."""
        return {
            "group_id": view["conversation_id"],
            "task_id": view["id"],
            "thread_id": view["thread_id"],
            "owner_id": view["owner_id"],
            "title": view["title"],
            "status": view["status"],
            "steps": int(view.get("steps") or 0),
            "current_step": view.get("current_step") or "",
            "awaiting_action_id": view.get("awaiting_action_id"),
            "latest_text": view.get("latest_text") or "",
            "waiting_for": view.get("waiting_for"),
            "end_reason": view.get("end_reason"),
            "stopped_by": view.get("stopped_by"),
            "dismissed_at": view.get("dismissed_at"),
            "seen_at": view.get("seen_at"),
        }

    def delete_group_tasks(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        with self._transaction() as tx:
            tx.execute(f"DELETE FROM group_tasks WHERE id IN ({marks})", list(ids))
