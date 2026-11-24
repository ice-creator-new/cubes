"""Actions and tool invocations.

The confirmation state machine, the interrupts that end a run, and the idempotency of a
repeated call.
"""

from __future__ import annotations

import json
import sqlite3

from .db import new_id, now_iso

class Actions:
    """Actions, the confirmation state machine and tool invocations."""

    def create_action(
        self,
        *,
        run_id: str,
        conversation_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: str,
        summary: str,
        author_id: str,
        author_name: str,
        avatar_seed: str,
        status: str = "running",
        confirmation: dict | None = None,
    ) -> tuple[dict, dict]:
        """Persist the pending action *and* its chat message before any side effect."""
        action_id = new_id("x")
        message_id = new_id("m")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO actions
                    (id, run_id, conversation_id, tool_call_id, tool_name, arguments, status, summary,
                     result, error, target_agent_id, target_conversation_id, confirmation, message_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    run_id,
                    conversation_id,
                    tool_call_id,
                    tool_name,
                    arguments,
                    status,
                    summary,
                    json.dumps(confirmation, ensure_ascii=False) if confirmation else None,
                    message_id,
                    timestamp,
                    timestamp,
                ),
            )
            self._insert_message(
                tx,
                message_id=message_id,
                conversation_id=conversation_id,
                author_id=author_id,
                author_name=author_name,
                avatar_seed=avatar_seed,
                kind="action",
                content=summary,
                status="streaming",
                action_id=action_id,
                created_at=timestamp,
                run_id=run_id,
            )
            self._touch(tx, conversation_id)
        action = self.get_action(action_id)
        message = self.get_message(message_id)
        assert action is not None and message is not None
        return action, message
    def get_action(self, action_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return None if row is None else self._action_row(row)

    def awaiting_actions(self) -> list[dict]:
        """Confirmation cards that are still waiting, oldest first, with who asked and where."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT a.id, a.conversation_id, a.tool_name, a.summary, a.confirmation,
                       a.message_id, a.created_at, m.author_name, c.title AS conversation_title
                  FROM actions a
                  JOIN messages m ON m.id = a.message_id
                  JOIN conversations c ON c.id = a.conversation_id
                 WHERE a.status = 'awaiting'
                 ORDER BY a.created_at ASC, a.rowid ASC
                """
            ).fetchall()
        found: list[dict] = []
        for row in rows:
            raw = row["confirmation"]
            try:
                confirmation = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                confirmation = None
            found.append(
                {
                    "id": row["id"],
                    "conversation_id": row["conversation_id"],
                    "tool_name": row["tool_name"],
                    "summary": row["summary"],
                    "confirmation": confirmation if isinstance(confirmation, dict) else None,
                    "message_id": row["message_id"],
                    "created_at": row["created_at"],
                    "author_name": row["author_name"],
                    "conversation_title": row["conversation_title"],
                }
            )
        return found

    def find_action(self, run_id: str, tool_call_id: str) -> dict | None:
        """Audit helper: the first action a run recorded for one ``tool_call_id``."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT * FROM actions WHERE run_id = ? AND tool_call_id = ?
                 ORDER BY rowid ASC LIMIT 1
                """,
                (run_id, tool_call_id),
            ).fetchone()
        return None if row is None else self._action_row(row)
    def finish_action(
        self,
        action_id: str,
        *,
        status: str,
        summary: str,
        message_status: str,
        error: str | None = None,
        result: str | None = None,
        detail: dict | None = None,
        target_agent_id: str | None = None,
        target_conversation_id: str | None = None,
    ) -> tuple[dict, dict] | None:
        """Close an action only after its side effects are persisted, message included."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                return None
            tx.execute(
                """
                UPDATE actions
                   SET status = ?, summary = ?, result = ?, error = ?, confirmation = NULL,
                       detail = ?, target_agent_id = COALESCE(?, target_agent_id),
                       target_conversation_id = COALESCE(?, target_conversation_id),
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    status,
                    summary,
                    result,
                    error,
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                    target_agent_id,
                    target_conversation_id,
                    now_iso(),
                    action_id,
                ),
            )
            tx.execute(
                "UPDATE messages SET content = ?, status = ? WHERE id = ?",
                (summary, message_status, row["message_id"]),
            )
        action = self.get_action(action_id)
        message = self.get_message(str(row["message_id"]))
        if action is None or message is None:  # pragma: no cover - defensive
            return None
        return action, message
    def mark_action_awaiting(
        self, action_id: str, *, summary: str, confirmation: dict
    ) -> tuple[dict, dict] | None:
        """Park one action until the user answers; its message keeps streaming."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                return None
            tx.execute(
                "UPDATE actions SET status = 'awaiting', summary = ?, confirmation = ?, "
                "updated_at = ? WHERE id = ?",
                (summary, json.dumps(confirmation, ensure_ascii=False), now_iso(), action_id),
            )
            tx.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (summary, row["message_id"]),
            )
        action = self.get_action(action_id)
        message = self.get_message(str(row["message_id"]))
        if action is None or message is None:  # pragma: no cover - defensive
            return None
        return action, message
    def resume_action(self, action_id: str, *, summary: str) -> tuple[dict, dict] | None:
        """Put an approved action back to running while its real work happens."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                return None
            tx.execute(
                "UPDATE actions SET status = 'running', summary = ?, confirmation = NULL, "
                "updated_at = ? WHERE id = ?",
                (summary, now_iso(), action_id),
            )
            tx.execute(
                "UPDATE messages SET content = ? WHERE id = ?", (summary, row["message_id"])
            )
        action = self.get_action(action_id)
        message = self.get_message(str(row["message_id"]))
        if action is None or message is None:  # pragma: no cover - defensive
            return None
        return action, message
    def action_for_target(self, tool_name: str, target_conversation_id: str) -> dict | None:
        """The newest row one run left as the way into ``target_conversation_id``."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM actions WHERE tool_name = ? AND target_conversation_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (tool_name, target_conversation_id),
            ).fetchone()
        return None if row is None else self._action_row(row)

    def update_action_summary(self, action_id: str, *, summary: str) -> tuple[dict, dict] | None:
        """Rewrite what a finished row says, e.g. a walk's entry line once it has ended."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                return None
            tx.execute(
                "UPDATE actions SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, now_iso(), action_id),
            )
            tx.execute(
                "UPDATE messages SET content = ? WHERE id = ?", (summary, row["message_id"])
            )
        action = self.get_action(action_id)
        message = self.get_message(str(row["message_id"]))
        if action is None or message is None:  # pragma: no cover - defensive
            return None
        return action, message

    def update_action_progress(
        self, action_id: str, *, summary: str, detail: dict | None = None
    ) -> tuple[dict, dict] | None:
        """Rewrite a still-running row while it works, e.g. its subtasks' progress.

        The row keeps its status: only the line and the expandable detail change, so a
        client redrawing the conversation shows the step that just settled.
        """
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                return None
            if detail is None:
                tx.execute(
                    "UPDATE actions SET summary = ?, updated_at = ? WHERE id = ?",
                    (summary, now_iso(), action_id),
                )
            else:
                tx.execute(
                    "UPDATE actions SET summary = ?, detail = ?, updated_at = ? WHERE id = ?",
                    (
                        summary,
                        json.dumps(detail, ensure_ascii=False),
                        now_iso(),
                        action_id,
                    ),
                )
            tx.execute(
                "UPDATE messages SET content = ? WHERE id = ?", (summary, row["message_id"])
            )
        action = self.get_action(action_id)
        message = self.get_message(str(row["message_id"]))
        if action is None or message is None:  # pragma: no cover - defensive
            return None
        return action, message

    def set_action_target(
        self, action_id: str, *, target_agent_id: str | None, target_conversation_id: str
    ) -> tuple[dict, dict] | None:
        """Point a still-running action at what it is working on, so clients can follow it."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                return None
            tx.execute(
                "UPDATE actions SET target_agent_id = ?, target_conversation_id = ?, "
                "updated_at = ? WHERE id = ?",
                (target_agent_id, target_conversation_id, now_iso(), action_id),
            )
        action = self.get_action(action_id)
        message = self.get_message(str(row["message_id"]))
        if action is None or message is None:  # pragma: no cover - defensive
            return None
        return action, message
    def count_created(self, run_id: str, tool_name: str) -> int:
        """How many entities one run already created with ``tool_name``."""
        column = "target_conversation_id" if tool_name == "create_group" else "target_agent_id"
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                f"""
                SELECT COUNT(DISTINCT {column}) AS total FROM actions
                 WHERE run_id = ? AND tool_name = ? AND status = 'completed'
                   AND {column} IS NOT NULL
                """,
                (run_id, tool_name),
            ).fetchone()
        return int(row["total"])
    def used_tools(self, run_id: str, tool_names: tuple[str, ...]) -> bool:
        """Whether this run completed a call to any of ``tool_names``."""
        if not tool_names:
            return False
        conn = self._ensure()
        marks = ", ".join("?" for _ in tool_names)
        with self._lock:
            row = conn.execute(
                f"SELECT 1 FROM actions WHERE run_id = ? AND status = 'completed'"
                f" AND tool_name IN ({marks}) LIMIT 1",
                (run_id, *tool_names),
            ).fetchone()
        return row is not None
    def interrupt_messages(self, message_ids: list[str]) -> list[dict]:
        """Mark the given streaming messages interrupted; return the updated messages."""
        if not message_ids:
            return []
        placeholders = ", ".join("?" for _ in message_ids)
        with self._transaction() as tx:
            ids = [
                str(row["id"])
                for row in tx.execute(
                    f"SELECT id FROM messages WHERE status = 'streaming' "
                    f"AND id IN ({placeholders}) ORDER BY rowid ASC",
                    list(message_ids),
                ).fetchall()
            ]
            if ids:
                tx.execute(
                    f"UPDATE messages SET status = 'interrupted' WHERE id IN "
                    f"({', '.join('?' for _ in ids)})",
                    ids,
                )
        messages = [self.get_message(message_id) for message_id in ids]
        return [message for message in messages if message is not None]
    def interrupt_actions(self, run_id: str, reason: str) -> int:
        """Fail the actions one run left running or waiting; other runs stay untouched."""
        with self._transaction() as tx:
            rows = tx.execute(
                "SELECT id, message_id FROM actions WHERE run_id = ? "
                "AND status IN ('running', 'awaiting')",
                (run_id,),
            ).fetchall()
            if not rows:
                return 0
            tx.execute(
                "UPDATE actions SET status = 'error', error = ?, confirmation = NULL, "
                "updated_at = ? WHERE run_id = ? AND status IN ('running', 'awaiting')",
                (reason, now_iso(), run_id),
            )
            message_ids = [str(row["message_id"]) for row in rows if row["message_id"]]
            if message_ids:
                tx.execute(
                    f"UPDATE messages SET status = 'interrupted' WHERE status = 'streaming' "
                    f"AND id IN ({', '.join('?' for _ in message_ids)})",
                    message_ids,
                )
        return len(rows)
    def streaming_message_ids(self, conversation_id: str) -> list[str]:
        """Messages of one conversation currently marked streaming, oldest first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND status = 'streaming' "
                "ORDER BY rowid ASC",
                (conversation_id,),
            ).fetchall()
        return [str(row["id"]) for row in rows]
    def get_invocation(self, run_id: str, tool_call_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM tool_invocations WHERE run_id = ? AND tool_call_id = ?",
                (run_id, tool_call_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "tool_call_id": row["tool_call_id"],
            "tool_name": row["tool_name"],
            "arguments": row["arguments"],
            "result": row["result"],
            "action_id": row["action_id"],
        }
    def record_invocation(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: str,
        result: str,
        action_id: str,
    ) -> None:
        conn = self._ensure()
        with self._lock:
            conn.execute(
                """
                INSERT OR REPLACE INTO tool_invocations
                    (run_id, tool_call_id, tool_name, arguments, result, action_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, tool_call_id, tool_name, arguments, result, action_id),
            )
    @staticmethod
    def _action_row(row: sqlite3.Row) -> dict:
        confirmation = row["confirmation"]
        detail = row["detail"]
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "tool_call_id": row["tool_call_id"],
            "tool_name": row["tool_name"],
            "status": row["status"],
            "summary": row["summary"],
            "target_agent_id": row["target_agent_id"],
            "target_conversation_id": row["target_conversation_id"],
            "error": row["error"],
            "confirmation": json.loads(confirmation) if confirmation else None,
            "detail": json.loads(detail) if detail else None,
        }
