"""Notebook notes written by the team."""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import _UNSET, _like_escape, new_id, now_iso

def _note_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "author_id": row["author_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

class Notes:
    """Notes."""

    def create_note(self, *, title: str, content: str, author_id: str | None) -> dict:
        note_id = new_id("n")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO notes (id, title, content, author_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (note_id, title, content, author_id, timestamp, timestamp),
            )
        note = self.get_note(note_id)
        assert note is not None
        return note
    def update_note(
        self,
        note_id: str,
        *,
        title: str | object = _UNSET,
        content: str | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        if title is not _UNSET:
            sets.append("title = ?")
            params.append(title)
        if content is not _UNSET:
            sets.append("content = ?")
            params.append(content)
        if not sets:
            return self.get_note(note_id)
        params.extend([now_iso(), note_id])
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(
                f"UPDATE notes SET {', '.join(sets)}, updated_at = ? WHERE id = ?", params
            )
            if cursor.rowcount == 0:
                return None
        return self.get_note(note_id)
    def delete_note(self, note_id: str) -> bool:
        """Remove one note; ``False`` when nothing matched."""
        with self._transaction() as tx:
            cursor = tx.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            return cursor.rowcount > 0
    def get_note(self, note_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return None if row is None else _note_row(row)
    def list_notes(self, query: str | None = None) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            if query:
                pattern = f"%{_like_escape(query)}%"
                rows = conn.execute(
                    """
                    SELECT * FROM notes
                     WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\'
                     ORDER BY updated_at DESC, rowid DESC
                    """,
                    (pattern, pattern),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY updated_at DESC, rowid DESC"
                ).fetchall()
        return [_note_row(row) for row in rows]
