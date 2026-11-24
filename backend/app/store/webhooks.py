"""Webhook triggers: one secret URL per schedule or workflow, stored as a hash.

The plain secret exists only in the answer that created or rotated it; this table keeps a
SHA-256 of it, so reading the database back never reveals a usable URL. ``last_called_at``
is what the panel shows next to the hook, and is written on every accepted call.
"""

from __future__ import annotations

from .db import new_id, now_iso


class Webhooks:
    """The secret URLs that start a schedule or a workflow."""

    def create_webhook(
        self, *, target_kind: str, target_id: str, secret_hash: str
    ) -> dict | None:
        """Insert the one hook a target may have; ``None`` when it already has one."""
        with self._transaction() as tx:
            existing = tx.execute(
                "SELECT 1 FROM webhooks WHERE target_kind = ? AND target_id = ?",
                (target_kind, target_id),
            ).fetchone()
            if existing is not None:
                return None
            webhook_id = new_id("w")
            tx.execute(
                """
                INSERT INTO webhooks
                    (id, target_kind, target_id, secret_hash, created_at, last_called_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (webhook_id, target_kind, target_id, secret_hash, now_iso()),
            )
        return self.get_webhook(webhook_id)

    def get_webhook(self, webhook_id: str) -> dict | None:
        """One hook, hash included: the caller checks the secret against it."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        return None if row is None else dict(row)

    def get_webhook_for(self, target_kind: str, target_id: str) -> dict | None:
        """The hook a schedule or a workflow owns, if it has one."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM webhooks WHERE target_kind = ? AND target_id = ?",
                (target_kind, target_id),
            ).fetchone()
        return None if row is None else dict(row)

    def rotate_webhook(self, webhook_id: str, secret_hash: str) -> dict | None:
        """Replace a hook's secret, keeping its URL, its target and its call count."""
        with self._transaction() as tx:
            cursor = tx.execute(
                "UPDATE webhooks SET secret_hash = ? WHERE id = ?", (secret_hash, webhook_id)
            )
            if cursor.rowcount == 0:
                return None
        return self.get_webhook(webhook_id)

    def touch_webhook(self, webhook_id: str) -> None:
        """Record that the hook was called now, for the panel's 「上次调用」 line."""
        with self._transaction() as tx:
            tx.execute(
                "UPDATE webhooks SET last_called_at = ? WHERE id = ?", (now_iso(), webhook_id)
            )

    def delete_webhook(self, webhook_id: str) -> bool:
        """Delete one hook; ``False`` when it was already gone."""
        with self._transaction() as tx:
            cursor = tx.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        return cursor.rowcount > 0

    def delete_webhooks_for(self, target_kind: str, target_id: str) -> int:
        """Delete the hook of a deleted schedule or workflow; returns how many went."""
        with self._transaction() as tx:
            cursor = tx.execute(
                "DELETE FROM webhooks WHERE target_kind = ? AND target_id = ?",
                (target_kind, target_id),
            )
        return cursor.rowcount
