"""The conversation's working set: which files this team touched, and how it last left them.

One row per ``(conversation, path)``, overwritten by the newest touch, so a long coding
session keeps a bounded, current picture of its artifacts instead of asking every bot to
re-read the same files. The rows hold no content: the hint re-reads the file it inlines, so
what a bot is shown can never be stale.
"""

from __future__ import annotations

from typing import Any

from .db import now_iso

# A conversation only ever needs its recent artifacts; older rows are pruned on write.
MAX_ROWS = 200


class WorkingFiles:
    """Per-conversation file state, one row per path."""

    def touch_working_file(
        self,
        conversation_id: str,
        *,
        path: str,
        author_id: str,
        author_name: str,
        action: str,
        lines: int | None = None,
        size: int | None = None,
        sha: str | None = None,
        added: int | None = None,
        removed: int | None = None,
    ) -> None:
        """Record one touch of ``path``; the newest touch replaces that path's row."""
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO working_files
                    (conversation_id, path, author_id, author_name, action, lines, size, sha,
                     added, removed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (conversation_id, path) DO UPDATE SET
                    author_id = excluded.author_id,
                    author_name = excluded.author_name,
                    action = excluded.action,
                    lines = COALESCE(excluded.lines, working_files.lines),
                    size = COALESCE(excluded.size, working_files.size),
                    sha = COALESCE(excluded.sha, working_files.sha),
                    added = COALESCE(excluded.added, working_files.added),
                    removed = COALESCE(excluded.removed, working_files.removed),
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    path,
                    author_id,
                    author_name,
                    action,
                    lines,
                    size,
                    sha,
                    added,
                    removed,
                    timestamp,
                ),
            )
            tx.execute(
                """
                DELETE FROM working_files
                 WHERE conversation_id = ?
                   AND path NOT IN (
                       SELECT path FROM working_files
                        WHERE conversation_id = ?
                        ORDER BY updated_at DESC, rowid DESC LIMIT ?
                   )
                """,
                (conversation_id, conversation_id, MAX_ROWS),
            )

    def list_working_files(self, conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """The conversation's files, newest touch first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM working_files WHERE conversation_id = ?
                 ORDER BY updated_at DESC, rowid DESC LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
