"""The local CLI conversation behind one (bot, chat) pair.

A ``claude-code`` or ``codex`` bot keeps one CLI session per conversation, so a turn sends
only what happened since its last one instead of the whole history. The row holds the id to
resume, the model it was started with, and a digest of the messages the CLI has already
seen: a rewind, a compaction or a model change no longer matches, and the next turn starts
a fresh session with the full history.
"""

from __future__ import annotations

from .db import now_iso


class CliSessions:
    """One resumable CLI conversation per bot and chat."""

    def get_cli_session(self, agent_id: str, conversation_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM cli_sessions WHERE agent_id = ? AND conversation_id = ?",
                (agent_id, conversation_id),
            ).fetchone()
        return None if row is None else dict(row)

    def save_cli_session(
        self,
        agent_id: str,
        conversation_id: str,
        *,
        signature: str,
        session_id: str,
        digest: str,
        message_count: int,
    ) -> None:
        """Remember the session and the history it was given; a newer turn overwrites it."""
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO cli_sessions
                    (agent_id, conversation_id, signature, session_id, digest,
                     message_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, conversation_id) DO UPDATE SET
                    signature = excluded.signature,
                    session_id = excluded.session_id,
                    digest = excluded.digest,
                    message_count = excluded.message_count,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id,
                    conversation_id,
                    signature,
                    session_id,
                    digest,
                    message_count,
                    now_iso(),
                ),
            )
