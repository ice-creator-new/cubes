"""The workbench's task list."""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import _UNSET, new_id, now_iso

def _task_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "detail": row["detail"],
        "status": row["status"],
        "assignee_id": row["assignee_id"],
        "author_id": row["author_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

class Tasks:
    """Tasks."""

    def create_task(
        self,
        *,
        title: str,
        detail: str = "",
        status: str = "todo",
        assignee_id: str | None = None,
        author_id: str | None = None,
        note: str = "",
    ) -> dict:
        task_id = new_id("t")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO tasks
                    (id, title, detail, status, assignee_id, author_id, note,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, title, detail, status, assignee_id, author_id, note, timestamp, timestamp),
            )
        task = self.get_task(task_id)
        assert task is not None
        return task
    def update_task(
        self,
        task_id: str,
        *,
        status: str | object = _UNSET,
        note: str | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not _UNSET:
            sets.append("status = ?")
            params.append(status)
        if note is not _UNSET:
            sets.append("note = ?")
            params.append(note)
        if not sets:
            return self.get_task(task_id)
        params.extend([now_iso(), task_id])
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(sets)}, updated_at = ? WHERE id = ?", params
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)
    def get_task(self, task_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _task_row(row)
    def list_tasks(self, status: str | None = None) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC, rowid ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at ASC, rowid ASC"
                ).fetchall()
        return [_task_row(row) for row in rows]
