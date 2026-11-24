"""The rolling summary a compacted conversation reads and the tool traces it folds."""

from __future__ import annotations

from .db import new_id, now_iso

class Summaries:
    """Rolling summaries and tool traces."""

    def latest_summary(self, conversation_id: str) -> dict | None:
        """The newest rolling summary of one conversation, or ``None`` before the first."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT * FROM conversation_summaries WHERE conversation_id = ?
                 ORDER BY upto_seq DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "upto_seq": row["upto_seq"],
            "summary": row["summary"],
            "tokens": row["tokens"],
            "created_at": row["created_at"],
        }
    def save_summary(
        self, conversation_id: str, *, upto_seq: int, summary: str, tokens: int
    ) -> dict:
        """Store one compaction step; a resumed step at the same ``upto_seq`` replaces it."""
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO conversation_summaries
                    (id, conversation_id, upto_seq, summary, tokens, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (conversation_id, upto_seq) DO UPDATE
                    SET summary = excluded.summary, tokens = excluded.tokens
                """,
                (new_id("s"), conversation_id, upto_seq, summary, tokens, now_iso()),
            )
        saved = self.latest_summary(conversation_id)
        assert saved is not None
        return saved
    def list_tool_traces(self, conversation_id: str) -> list[dict]:
        """Every tool call made in one conversation, with its result and where it happened.

        The action row already holds the call id, the arguments and the result, and its own
        chat message carries the author and the ``seq`` — so the trace needs no second write.
        A call still waiting for the user, or never finished, is left out.
        """
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT a.tool_call_id, a.tool_name, a.arguments, a.status, a.result,
                       m.id AS message_id, m.seq AS seq, m.author_id AS author_id
                  FROM actions a
                  JOIN messages m ON m.id = a.message_id
                 WHERE a.conversation_id = ? AND m.kind = 'action'
                   AND a.status NOT IN ('awaiting', 'running')
                 ORDER BY m.seq ASC, a.rowid ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [
            {
                "message_id": row["message_id"],
                "seq": row["seq"],
                "author_id": row["author_id"],
                "tool_call_id": row["tool_call_id"],
                "tool_name": row["tool_name"],
                "arguments": row["arguments"],
                "status": row["status"],
                "result": row["result"] or "",
            }
            for row in rows
        ]
