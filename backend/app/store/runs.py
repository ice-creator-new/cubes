"""Runs: beginning one under its conversation, its progress and its usage counters."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..constants import STEP_INITIAL, STEP_QUEUED, USER_AUTHOR_NAME
from .db import _UNSET, new_id, now_iso

class Runs:
    """A run's row and its counters."""

    def begin_run(
        self,
        conversation_id: str,
        goal: str,
        mentions: list[str] | None = None,
        *,
        reply_to_id: str | None = None,
        interrupts: bool = False,
        attachment_ids: list[str] | None = None,
        schedule: dict | None = None,
        reminder: bool = False,
        addresses: list[str] | None = None,
        lane: str = "work",
    ) -> tuple[dict, dict]:
        """Atomically append the user's goal message and create the running run.

        ``schedule`` (``{"id","title"}``) marks a message a scheduled task sent, and links
        the run back to that schedule for the client and for outcome recording. ``reminder``
        marks the trigger of a bot's own reminder, which the client shows as a quiet row.
        ``lane`` is ``work`` for the job itself and ``reply`` for a lane answering beside it.
        """
        run_id = new_id("r")
        message_id = new_id("m")
        timestamp = now_iso()
        with self._transaction() as tx:
            self._insert_message(
                tx,
                message_id=message_id,
                conversation_id=conversation_id,
                author_id=None,
                author_name=USER_AUTHOR_NAME,
                avatar_seed=None,
                kind="user",
                content=goal,
                status="complete",
                action_id=None,
                mentions=mentions,
                reply_to_id=reply_to_id,
                interrupts=interrupts,
                schedule=schedule,
                created_at=timestamp,
                run_id=run_id,
                reminder=reminder,
                addresses=addresses,
            )
            self._bind_attachments(tx, message_id, attachment_ids or [])
            tx.execute(
                """
                INSERT INTO runs
                    (id, conversation_id, goal, status, current_step, error, model_calls,
                     tool_calls, schedule_id, lane, created_at, updated_at, finished_at)
                VALUES (?, ?, ?, 'running', ?, NULL, 0, 0, ?, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    conversation_id,
                    goal,
                    STEP_INITIAL,
                    (schedule or {}).get("id"),
                    lane,
                    timestamp,
                    timestamp,
                ),
            )
            self._touch(tx, conversation_id)
        run = self.get_run(run_id)
        message = self.get_message(message_id)
        assert run is not None and message is not None
        return run, message
    def begin_bare_run(self, conversation_id: str, goal: str, *, lane: str = "work") -> dict:
        """A running run with no user message: a workflow walk that is not an answer.

        The steps' own posts are the conversation; inventing a user line for "start the
        workflow" would put words in the user's mouth.
        """
        run_id = new_id("r")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO runs
                    (id, conversation_id, goal, status, current_step, error, model_calls,
                     tool_calls, schedule_id, lane, created_at, updated_at, finished_at)
                VALUES (?, ?, ?, 'running', ?, NULL, 0, 0, NULL, ?, ?, ?, NULL)
                """,
                (run_id, conversation_id, goal, STEP_INITIAL, lane, timestamp, timestamp),
            )
            self._touch(tx, conversation_id)
        run = self.get_run(run_id)
        assert run is not None
        return run
    def get_run(self, run_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else self._run_row(row)
    def active_run(self, conversation_id: str | None = None) -> dict | None:
        conn = self._ensure()
        with self._lock:
            if conversation_id is None:
                row = conn.execute(
                    "SELECT * FROM runs WHERE status = 'running' ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs WHERE status = 'running' AND conversation_id = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
        return None if row is None else self._run_row(row)
    def active_runs(self) -> list[dict]:
        """Every run still marked running, newest first; a hidden workspace's never counts."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT r.* FROM runs r
                  JOIN conversations c ON c.id = r.conversation_id
                 WHERE r.status = 'running' AND c.kind NOT IN ('background', 'thread')
                 ORDER BY r.rowid DESC
                """
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def active_runs_in(self, conversation_id: str) -> list[dict]:
        """Every live lane of one conversation, oldest first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status = 'running' AND conversation_id = ? "
                "ORDER BY rowid ASC",
                (conversation_id,),
            ).fetchall()
        return [self._run_row(row) for row in rows]
    # ------------------------------------------------------------------ queued sends

    def begin_queued_run(
        self,
        conversation_id: str,
        goal: str,
        mentions: list[str] | None = None,
        *,
        reply_to_id: str | None = None,
        attachment_ids: list[str] | None = None,
        addresses: list[str] | None = None,
        lane: str = "work",
    ) -> tuple[dict, dict]:
        """Write the message and a ``queued`` run, without starting anything.

        The message is in the transcript at once, marked ``queued``: the user sees what they
        sent and can take it back, and when the run before it ends the launcher promotes it
        to a normal run that answers that same message.
        """
        run_id = new_id("r")
        message_id = new_id("m")
        timestamp = now_iso()
        with self._transaction() as tx:
            self._insert_message(
                tx,
                message_id=message_id,
                conversation_id=conversation_id,
                author_id=None,
                author_name=USER_AUTHOR_NAME,
                avatar_seed=None,
                kind="user",
                content=goal,
                status="queued",
                action_id=None,
                mentions=mentions,
                reply_to_id=reply_to_id,
                interrupts=False,
                created_at=timestamp,
                run_id=run_id,
                addresses=addresses,
            )
            self._bind_attachments(tx, message_id, attachment_ids or [])
            tx.execute(
                """
                INSERT INTO runs
                    (id, conversation_id, goal, status, current_step, error, model_calls,
                     tool_calls, schedule_id, lane, created_at, updated_at, finished_at)
                VALUES (?, ?, ?, 'queued', ?, NULL, 0, 0, NULL, ?, ?, ?, NULL)
                """,
                (run_id, conversation_id, goal, STEP_QUEUED, lane, timestamp, timestamp),
            )
            self._touch(tx, conversation_id)
        run = self.get_run(run_id)
        message = self.get_message(message_id)
        assert run is not None and message is not None
        return run, message

    def queued_conversation_ids(self) -> list[str]:
        """Every chat with a send waiting for its run slot, including hidden task threads."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT DISTINCT conversation_id FROM runs WHERE status = 'queued' "
                "ORDER BY rowid"
            ).fetchall()
        return [str(row["conversation_id"]) for row in rows]

    def queued_run(self, conversation_id: str) -> dict | None:
        """The oldest send waiting for this conversation's slot, if one is waiting."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM runs WHERE conversation_id = ? AND status = 'queued' "
                "ORDER BY rowid ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._run_row(row)

    def queued_runs(self, conversation_id: str) -> list[dict]:
        """Every send waiting for this conversation's slots, oldest first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM runs WHERE conversation_id = ? AND status = 'queued' "
                "ORDER BY rowid ASC",
                (conversation_id,),
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def promote_queued_run(self, run_id: str) -> tuple[dict, dict] | None:
        """Turn a queued send into a running one; the message stops reading as queued."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT * FROM runs WHERE id = ? AND status = 'queued'", (run_id,)
            ).fetchone()
            if row is None:
                return None
            timestamp = now_iso()
            tx.execute(
                "UPDATE runs SET status = 'running', current_step = ?, updated_at = ? WHERE id = ?",
                (STEP_INITIAL, timestamp, run_id),
            )
            tx.execute(
                "UPDATE messages SET status = 'complete' WHERE run_id = ? AND status = 'queued'",
                (run_id,),
            )
        run = self.get_run(run_id)
        message = self.queued_message_of(run_id)
        assert run is not None
        return run, message

    def queued_message_of(self, run_id: str) -> dict:
        """The user message a queued or freshly promoted run answers."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM messages WHERE run_id = ? ORDER BY rowid ASC LIMIT 1", (run_id,)
            ).fetchone()
        assert row is not None
        return self._message_row(row)

    def discard_run(self, run_id: str) -> list[str]:
        """Remove a run that never started and the message it wrote; answer the message ids."""
        with self._transaction() as tx:
            ids = [
                str(row["id"])
                for row in tx.execute("SELECT id FROM messages WHERE run_id = ?", (run_id,)).fetchall()
            ]
            tx.execute("DELETE FROM messages WHERE run_id = ?", (run_id,))
            tx.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return ids

    def cancel_queued_run(self, run_id: str) -> dict | None:
        """Remove a send still waiting: its message and its run. ``None`` unless queued."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT * FROM runs WHERE id = ? AND status = 'queued'", (run_id,)
            ).fetchone()
            if row is None:
                return None
            message = tx.execute(
                "SELECT * FROM messages WHERE run_id = ? ORDER BY rowid ASC LIMIT 1", (run_id,)
            ).fetchone()
            removed = None if message is None else self._message_row(message)
            tx.execute("DELETE FROM messages WHERE run_id = ?", (run_id,))
            tx.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return removed

    def latest_run(self, conversation_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM runs WHERE conversation_id = ? ORDER BY rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._run_row(row)
    def previous_run(self, conversation_id: str, run_id: str) -> dict | None:
        """The run of this conversation just before ``run_id``; ``None`` when there is none."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT * FROM runs
                 WHERE conversation_id = ?
                   AND rowid < (SELECT rowid FROM runs WHERE id = ?)
                 ORDER BY rowid DESC LIMIT 1
                """,
                (conversation_id, run_id),
            ).fetchone()
        return None if row is None else self._run_row(row)

    def list_conversation_runs(self, conversation_id: str) -> list[dict]:
        """Every run of one conversation, newest first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM runs WHERE conversation_id = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (conversation_id,),
            ).fetchall()
        return [self._run_row(row) for row in rows]
    def update_run(
        self,
        run_id: str,
        *,
        status: str | object = _UNSET,
        current_step: str | object = _UNSET,
        error: str | None | object = _UNSET,
        model_calls: int | object = _UNSET,
        tool_calls: int | object = _UNSET,
        current_agent_id: str | None | object = _UNSET,
        pending_agent_ids: list[str] | object = _UNSET,
        hops: int | object = _UNSET,
        wrapped: int | object = _UNSET,
        lane: str | object = _UNSET,
        finished_at: str | None | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("current_step", current_step),
            ("error", error),
            ("model_calls", model_calls),
            ("tool_calls", tool_calls),
            ("current_agent_id", current_agent_id),
            ("pending_agent_ids", pending_agent_ids),
            ("hops", hops),
            ("wrapped", wrapped),
            ("lane", lane),
            ("finished_at", finished_at),
        ):
            if value is _UNSET:
                continue
            sets.append(f"{column} = ?")
            params.append(
                json.dumps(list(value), ensure_ascii=False)  # type: ignore[arg-type]
                if column == "pending_agent_ids"
                else value
            )
        if not sets:
            return self.get_run(run_id)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(run_id)
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                return None
        return self.get_run(run_id)
    def add_run_usage(
        self,
        run_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> dict | None:
        """Add one model call's reported usage to its run's running totals."""
        if not (input_tokens or output_tokens or cached_tokens):
            return self.get_run(run_id)
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(
                """
                UPDATE runs SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,
                                cached_tokens = cached_tokens + ?, updated_at = ?
                 WHERE id = ?
                """,
                (max(0, input_tokens), max(0, output_tokens), max(0, cached_tokens), now_iso(), run_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_run(run_id)
    def count_compaction(self, run_id: str) -> None:
        """Record that this run compacted its conversation once."""
        conn = self._ensure()
        with self._lock:
            conn.execute(
                "UPDATE runs SET compactions = compactions + 1, updated_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )
    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "status": row["status"],
            "current_step": row["current_step"],
            "error": row["error"],
            "model_calls": row["model_calls"],
            "tool_calls": row["tool_calls"],
            "current_agent_id": row["current_agent_id"],
            "pending_agent_ids": json.loads(row["pending_agent_ids"] or "[]"),
            "hops": row["hops"],
            "schedule_id": row["schedule_id"],
            "lane": row["lane"] or "work",
            "wrapped": bool(row["wrapped"]),
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cached_tokens": row["cached_tokens"],
            "compactions": row["compactions"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }
