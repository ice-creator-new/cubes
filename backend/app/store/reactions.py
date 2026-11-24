"""Emoji reactions on messages: add, remove, replace and the wire shape.

One aggregate over the same connection: ``add_reaction`` enforces the per-message rules (a
bot holds at most one emoji, at most 20 distinct emoji total), and ``reactions_payload``
builds the ordered ``[{"emoji", "count", "mine", "actors"}]`` list every message carries.
"""

from __future__ import annotations

import sqlite3

from .db import now_iso

# A message accepts at most this many distinct emoji; the next one is refused.
MAX_DISTINCT_EMOJI = 20
# The user's actor id in a reaction row.
USER_ACTOR_ID = "user"
USER_ACTOR_NAME = "你"


class Reactions:
    """Reactions and the ordered list a message carries."""

    def add_reaction(self, message_id: str, emoji: str, actor_id: str, actor_name: str) -> bool:
        """Add (or replace) one actor's reaction; ``False`` when it would be the 21st emoji.

        A bot holds at most one emoji per message: a new one replaces the old. The user may
        hold several. Re-adding the same emoji is a no-op (and keeps its ``created_at``).
        """
        conn = self._ensure()
        with self._lock:
            current = conn.execute(
                "SELECT emoji FROM reactions WHERE message_id = ? AND actor_id = ?",
                (message_id, actor_id),
            ).fetchone()
            if current is not None and str(current["emoji"]) == emoji:
                return True
            distinct = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT emoji) FROM reactions WHERE message_id = ?",
                    (message_id,),
                ).fetchone()[0]
            )
            # A bot replacing its one emoji frees that emoji's slot if nobody else holds it.
            replaced: str | None = None
            if actor_id != USER_ACTOR_ID and current is not None:
                replaced = str(current["emoji"])
            if replaced is not None and replaced != emoji:
                other = conn.execute(
                    "SELECT 1 FROM reactions WHERE message_id = ? AND emoji = ? AND actor_id != ?",
                    (message_id, replaced, actor_id),
                ).fetchone()
                if other is None:
                    distinct -= 1
            already = conn.execute(
                "SELECT 1 FROM reactions WHERE message_id = ? AND emoji = ?",
                (message_id, emoji),
            ).fetchone()
            if already is None and distinct >= MAX_DISTINCT_EMOJI:
                return False
            if actor_id != USER_ACTOR_ID:
                conn.execute(
                    "DELETE FROM reactions WHERE message_id = ? AND actor_id = ?",
                    (message_id, actor_id),
                )
            conn.execute(
                "INSERT INTO reactions (message_id, emoji, actor_id, actor_name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, emoji, actor_id, actor_name, now_iso()),
            )
        return True

    def remove_reaction(self, message_id: str, emoji: str, actor_id: str) -> bool:
        """Remove one actor's reaction; ``False`` when it was not there (idempotent caller)."""
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM reactions WHERE message_id = ? AND emoji = ? AND actor_id = ?",
                (message_id, emoji, actor_id),
            )
        return cursor.rowcount > 0

    def reactions_payload(self, message_id: str) -> list[dict]:
        """The ordered wire list for one message: oldest emoji first, actors by ``created_at``."""
        conn = self._ensure()
        with self._lock:
            return self._message_reactions(conn, message_id)

    @staticmethod
    def _message_reactions(conn: sqlite3.Connection, message_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT emoji, actor_id, actor_name, created_at FROM reactions "
            "WHERE message_id = ? ORDER BY created_at ASC, rowid ASC",
            (message_id,),
        ).fetchall()
        by_emoji: dict[str, list[tuple[str, str]]] = {}
        first: dict[str, str] = {}
        for row in rows:
            emoji = str(row["emoji"])
            if emoji not in by_emoji:
                by_emoji[emoji] = []
                first[emoji] = str(row["created_at"])
            by_emoji[emoji].append((str(row["actor_id"]), str(row["actor_name"])))
        ordered = sorted(by_emoji, key=lambda emoji: (first[emoji], emoji))
        return [
            {
                "emoji": emoji,
                "count": len(by_emoji[emoji]),
                "mine": any(actor_id == USER_ACTOR_ID for actor_id, _ in by_emoji[emoji]),
                "actors": [
                    {"id": actor_id, "name": name} for actor_id, name in by_emoji[emoji]
                ],
            }
            for emoji in ordered
        ]
