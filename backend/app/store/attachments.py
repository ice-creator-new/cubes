"""Attachment files on disk, bound to the messages that show them."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from .. import attachments as attachment_rules
from .db import new_id, now_iso

class Attachments:
    """Attachment files."""

    def save_attachment(self, data: bytes, mime: str, name: str = "") -> dict:
        """Store one uploaded file (its file once per content) and return its row."""
        sha256 = attachment_rules.write_file(self.attachments_dir, data)
        attachment_id = new_id("att")
        with self._transaction() as tx:
            tx.execute(
                "INSERT INTO attachments (id, mime, size, sha256, name, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (attachment_id, mime, len(data), sha256, name, now_iso()),
            )
        attachment = self.get_attachment(attachment_id)
        assert attachment is not None
        return attachment
    def get_attachment(self, attachment_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
        return None if row is None else dict(row)
    def attachment_path(self, attachment: dict) -> Path:
        return self.attachments_dir / str(attachment["sha256"])
    def unbound_attachments(self, attachment_ids: list[str]) -> list[str] | None:
        """``attachment_ids`` when every one exists and no message carries it yet, else None."""
        if not attachment_ids:
            return []
        conn = self._ensure()
        placeholders = ", ".join("?" for _ in attachment_ids)
        with self._lock:
            rows = conn.execute(
                f"""
                SELECT a.id FROM attachments a
                 WHERE a.id IN ({placeholders})
                   AND NOT EXISTS (SELECT 1 FROM message_attachments m WHERE m.attachment_id = a.id)
                """,
                list(attachment_ids),
            ).fetchall()
        found = {row["id"] for row in rows}
        return list(attachment_ids) if found == set(attachment_ids) else None
    def visible_attachments(self, agent_id: str, attachment_ids: list[str]) -> list[str]:
        """The ids among ``attachment_ids`` this agent has seen: carried by a message in a
        conversation it belongs to. Order follows ``attachment_ids``."""
        if not attachment_ids:
            return []
        conn = self._ensure()
        placeholders = ", ".join("?" for _ in attachment_ids)
        with self._lock:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ma.attachment_id FROM message_attachments ma
                  JOIN messages m ON m.id = ma.message_id
                  JOIN conversation_members cm ON cm.conversation_id = m.conversation_id
                 WHERE cm.agent_id = ? AND ma.attachment_id IN ({placeholders})
                """,
                [agent_id, *attachment_ids],
            ).fetchall()
        seen = {row["attachment_id"] for row in rows}
        return [attachment_id for attachment_id in attachment_ids if attachment_id in seen]
    def attach_to_message(self, message_id: str, attachment_ids: list[str]) -> dict | None:
        """Bind images to one message, in order; return the message as it now reads."""
        if attachment_ids:
            with self._transaction() as tx:
                self._bind_attachments(tx, message_id, attachment_ids)
        return self.get_message(message_id)
    @staticmethod
    def _bind_attachments(conn: sqlite3.Connection, message_id: str, attachment_ids: list[str]) -> None:
        for position, attachment_id in enumerate(dict.fromkeys(attachment_ids)):
            conn.execute(
                "INSERT OR IGNORE INTO message_attachments (message_id, attachment_id, position) "
                "VALUES (?, ?, ?)",
                (message_id, attachment_id, position),
            )
    def _message_attachments(self, conn: sqlite3.Connection, message_id: str) -> list[dict]:
        rows = conn.execute(
            """
            SELECT a.id, a.mime, a.size, a.sha256, a.name FROM message_attachments ma
              JOIN attachments a ON a.id = ma.attachment_id
             WHERE ma.message_id = ?
             ORDER BY ma.position ASC
            """,
            (message_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    def _drop_orphan_attachments(self) -> None:
        """Forget uploads no message took within a day, then files nothing points at."""
        cutoff = (
            datetime.fromtimestamp(
                int(datetime.now(timezone.utc).timestamp()) - attachment_rules.ORPHAN_SECONDS, timezone.utc
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self._transaction() as tx:
            tx.execute(
                """
                DELETE FROM attachments
                 WHERE created_at < ?
                   AND NOT EXISTS (SELECT 1 FROM message_attachments m WHERE m.attachment_id = attachments.id)
                """,
                (cutoff,),
            )
            kept = {row["sha256"] for row in tx.execute("SELECT sha256 FROM attachments").fetchall()}
        directory = self.attachments_dir
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if path.is_file() and path.name not in kept:
                with suppress(OSError):
                    path.unlink()
