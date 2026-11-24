"""Messages: append, stream updates, paging and search."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import _UNSET, _like_escape, new_id, now_iso

class Messages:
    """Messages and their paging."""

    def append_message(
        self,
        *,
        conversation_id: str,
        author_id: str | None,
        author_name: str,
        avatar_seed: str | None,
        kind: str,
        content: str,
        status: str,
        action_id: str | None = None,
        mentions: list[str] | None = None,
        reply_to_id: str | None = None,
        relay: dict | None = None,
        origin_message_id: str | None = None,
        schedule: dict | None = None,
        mood: str | None = None,
        attachment_ids: list[str] | None = None,
        run_id: str | None = None,
        reminder: bool = False,
        task_id: str | None = None,
        status_card: dict | None = None,
        addresses: list[str] | None = None,
    ) -> dict:
        message_id = new_id("m")
        with self._transaction() as tx:
            self._insert_message(
                tx,
                message_id=message_id,
                conversation_id=conversation_id,
                author_id=author_id,
                author_name=author_name,
                avatar_seed=avatar_seed,
                kind=kind,
                content=content,
                status=status,
                action_id=action_id,
                mentions=mentions,
                reply_to_id=reply_to_id,
                relay=relay,
                origin_message_id=origin_message_id,
                schedule=schedule,
                mood=mood,
                run_id=run_id,
                reminder=reminder,
                task_id=task_id,
                status_card=status_card,
                addresses=addresses,
            )
            self._bind_attachments(tx, message_id, attachment_ids or [])
            self._touch(tx, conversation_id)
        message = self.get_message(message_id)
        assert message is not None
        return message
    def _insert_message(
        self,
        conn: sqlite3.Connection,
        *,
        message_id: str,
        conversation_id: str,
        author_id: str | None,
        author_name: str,
        avatar_seed: str | None,
        kind: str,
        content: str,
        status: str,
        action_id: str | None,
        mentions: list[str] | None = None,
        reply_to_id: str | None = None,
        relay: dict | None = None,
        origin_message_id: str | None = None,
        interrupts: bool = False,
        schedule: dict | None = None,
        mood: str | None = None,
        seq: int | None = None,
        created_at: str | None = None,
        run_id: str | None = None,
        reminder: bool = False,
        task_id: str | None = None,
        status_card: dict | None = None,
        addresses: list[str] | None = None,
    ) -> None:
        if seq is None:
            seq = conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM messages WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()["next"]
        relay = relay or {}
        schedule = schedule or {}
        conn.execute(
            """
            INSERT INTO messages
                (id, conversation_id, author_id, author_name, avatar_seed, kind, content, status,
                 action_id, mentions, reply_to_id, relay_conversation_id, relay_direction,
                 relay_peer_id, relay_peer_name, origin_message_id, interrupts, schedule_id,
                 schedule_title, mood, seq, created_at, run_id, reminder, task_id,
                 status_card, addresses)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                author_id,
                author_name,
                avatar_seed,
                kind,
                content,
                status,
                action_id,
                json.dumps(mentions or [], ensure_ascii=False),
                reply_to_id,
                relay.get("conversation_id"),
                relay.get("direction"),
                relay.get("peer_id"),
                relay.get("peer_name"),
                origin_message_id,
                1 if interrupts else 0,
                schedule.get("id"),
                schedule.get("title"),
                mood,
                seq,
                created_at or now_iso(),
                run_id,
                1 if reminder else 0,
                task_id,
                None if status_card is None else json.dumps(status_card, ensure_ascii=False),
                json.dumps(addresses or [], ensure_ascii=False),
            ),
        )
    def get_message(self, message_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return None if row is None else self._message_row(row)
    def newest_message_seq(self, conversation_id: str) -> int:
        """The seq of the newest message in one conversation; 0 when it is empty."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS newest FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["newest"])

    def list_messages(self, conversation_id: str) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM messages WHERE conversation_id = ?
                 ORDER BY seq ASC, rowid ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [self._message_row(row) for row in rows]
    def list_message_page(
        self, conversation_id: str, limit: int, before: int | None = None
    ) -> tuple[list[dict], bool]:
        """One page of a conversation, oldest first, plus whether older messages remain.

        Reads one row past ``limit`` newest-first to know there is more, then reverses the
        kept ones so the caller can step further back with the ``seq`` of its first row.
        """
        sql = "SELECT * FROM messages WHERE conversation_id = ?"
        params: list[Any] = [conversation_id]
        if before is not None:
            sql += " AND seq < ?"
            params.append(before)
        sql += " ORDER BY seq DESC, rowid DESC LIMIT ?"
        params.append(limit + 1)
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(sql, params).fetchall()
        return [self._message_row(row) for row in reversed(rows[:limit])], len(rows) > limit
    def last_message_preview(self, conversation_id: str) -> str | None:
        """Newest real message text for the conversation directory, or ``None``.

        Relay markers that only announce a bot-to-bot chat carry no text, so they never
        replace the last thing actually said in the conversation.
        """
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT content FROM messages WHERE conversation_id = ?
                   AND NOT (relay_direction IS NOT NULL AND content = '')
                 ORDER BY seq DESC, rowid DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return None if row is None else str(row["content"])
    def update_message(
        self,
        message_id: str,
        *,
        content: str | object = _UNSET,
        status: str | object = _UNSET,
        mentions: list[str] | object = _UNSET,
        mood: str | None | object = _UNSET,
        leads_to_tools: bool | None | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        if content is not _UNSET:
            sets.append("content = ?")
            params.append(content)
        if status is not _UNSET:
            sets.append("status = ?")
            params.append(status)
        if mentions is not _UNSET:
            sets.append("mentions = ?")
            params.append(json.dumps(list(mentions), ensure_ascii=False))  # type: ignore[arg-type]
        if mood is not _UNSET:
            sets.append("mood = ?")
            params.append(mood)
        if leads_to_tools is not _UNSET:
            sets.append("leads_to_tools = ?")
            params.append(None if leads_to_tools is None else (1 if leads_to_tools else 0))
        if not sets:
            return self.get_message(message_id)
        params.append(message_id)
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                return None
        return self.get_message(message_id)
    def set_message_route(self, message_id: str, route: dict | None) -> dict | None:
        """Record who answered a user message (``app.dispatch``) — or clear it."""
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(
                "UPDATE messages SET route = ? WHERE id = ?",
                (None if route is None else json.dumps(route, ensure_ascii=False), message_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_message(message_id)

    def delete_message(self, message_id: str) -> bool:
        """Delete one message row (its attachments and reactions cascade); ``False`` if absent.

        Used for a reply that turned out to be reaction-only: it is removed before any client
        has seen it as a finished message.
        """
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        return cursor.rowcount > 0

    def rewind_conversation(
        self, conversation_id: str, message_id: str, *, inclusive: bool = False
    ) -> list[str] | None:
        """Delete every message after ``message_id`` (or from it too): the ids that went.

        What goes with them: the actions whose row was one of those messages, attachment
        bindings and reactions (both cascade from the message), the summaries that covered
        the removed part, and a run of this conversation that no message refers to any
        more. Memories keep their content and only lose pointers to messages that no
        longer exist. ``None`` means the message is not in this conversation.
        """
        with self._transaction() as tx:
            target = tx.execute(
                "SELECT seq, rowid FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
            if target is None:
                return None
            op = ">=" if inclusive else ">"
            rows = tx.execute(
                f"""
                SELECT id FROM messages
                 WHERE conversation_id = ?
                   AND (seq {op} ? OR (seq = ? AND rowid {op} ?))
                """,
                (conversation_id, target["seq"], target["seq"], target["rowid"]),
            ).fetchall()
            removed = [str(row["id"]) for row in rows]
            if not removed:
                return []
            placeholders = ", ".join("?" for _ in removed)
            # The action behind an action row is useless without the row that shows it.
            tx.execute(f"DELETE FROM actions WHERE message_id IN ({placeholders})", removed)
            tx.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", removed)
            # The work those requests started is taken back with them.
            tx.execute(
                f"DELETE FROM group_tasks WHERE request_message_id IN ({placeholders})", removed
            )
            tx.execute(
                f"DELETE FROM conversation_summaries WHERE conversation_id = ? "
                f"AND upto_seq {op} ?",
                (conversation_id, target["seq"]),
            )
            self._prune_memory_sources(tx, conversation_id, removed)
            # A run that only wrote messages here is gone with them; one still quoted by a
            # surviving message (its trigger) stays. Its tool invocations follow it
            # explicitly: that table has no foreign key.
            stale = [
                str(row["id"])
                for row in tx.execute(
                    """
                    SELECT id FROM runs
                     WHERE conversation_id = ? AND status != 'running'
                       AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.run_id = runs.id)
                    """,
                    (conversation_id,),
                ).fetchall()
            ]
            if stale:
                run_marks = ", ".join("?" for _ in stale)
                tx.execute(
                    f"DELETE FROM tool_invocations WHERE run_id IN ({run_marks})", stale
                )
                tx.execute(f"DELETE FROM runs WHERE id IN ({run_marks})", stale)
            self._touch(tx, conversation_id)
        return removed

    @staticmethod
    def _prune_memory_sources(
        conn: sqlite3.Connection, conversation_id: str, removed: list[str]
    ) -> None:
        """Drop the deleted ids from a memory's ``source_message_ids``, keeping the memory."""
        gone = set(removed)
        rows = conn.execute(
            "SELECT id, source_message_ids FROM memories WHERE source_conversation_id = ?",
            (conversation_id,),
        ).fetchall()
        for row in rows:
            try:
                ids = json.loads(row["source_message_ids"] or "[]")
            except ValueError:  # a hand-edited row must not keep a rewind from happening
                continue
            if not isinstance(ids, list):
                continue
            kept = [item for item in ids if item not in gone]
            if len(kept) != len(ids):
                conn.execute(
                    "UPDATE memories SET source_message_ids = ? WHERE id = ?",
                    (json.dumps(kept, ensure_ascii=False), row["id"]),
                )
    def search_messages(
        self, query: str, *, agent_id: str, is_supervisor: bool, limit: int = 20
    ) -> list[dict]:
        """Messages containing ``query``, inside the conversations the caller may read.

        The supervisor reads every direct and group thread; private threads of other
        members stay invisible to it, and are only readable by their two participants. A
        schedule's hidden workspace is searched by nobody.
        """
        pattern = f"%{_like_escape(query)}%"
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT m.id, m.conversation_id, c.title AS conversation_title,
                       c.kind AS conversation_kind, m.author_name, m.content, m.created_at
                  FROM messages m
                  JOIN conversations c ON c.id = m.conversation_id
                 WHERE m.kind != 'action'
                   AND m.content LIKE ? ESCAPE '\\'
                   AND (
                        (c.kind NOT IN ('private', 'background')
                         AND (? = 1 OR EXISTS (
                                SELECT 1 FROM conversation_members cm
                                 WHERE cm.conversation_id = c.id AND cm.agent_id = ?)))
                     OR (c.kind = 'private'
                         AND EXISTS (
                                SELECT 1 FROM conversation_members cm
                                 WHERE cm.conversation_id = c.id AND cm.agent_id = ?))
                   )
                 ORDER BY m.created_at DESC, m.rowid DESC
                 LIMIT ?
                """,
                (pattern, 1 if is_supervisor else 0, agent_id, agent_id, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "conversation_id": row["conversation_id"],
                "conversation_title": row["conversation_title"],
                "conversation_kind": row["conversation_kind"],
                "author_name": row["author_name"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    def _touch(self, conn: sqlite3.Connection, conversation_id: str) -> None:
        conn.execute(
            "UPDATE conversations SET updated_at = ?, updated_order = ? WHERE id = ?",
            (now_iso(), self._next_order(conn), conversation_id),
        )
    def _next_order(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(updated_order), 0) + 1 AS next FROM conversations"
        ).fetchone()
        return int(row["next"])
    def _message_row(self, row: sqlite3.Row) -> dict:
        conn = self._ensure()
        action: dict | None = None
        if row["action_id"]:
            action_row = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (row["action_id"],)
            ).fetchone()
            if action_row is not None:
                action = self._action_row(action_row)
        origin: dict | None = None
        if row["origin_message_id"]:
            origin_row = conn.execute(
                "SELECT id, conversation_id, author_name, content FROM messages WHERE id = ?",
                (row["origin_message_id"],),
            ).fetchone()
            # A deleted conversation takes the question with it; the message stays as is.
            if origin_row is not None:
                origin = {
                    "message_id": origin_row["id"],
                    "conversation_id": origin_row["conversation_id"],
                    "author_name": origin_row["author_name"],
                    "content": origin_row["content"],
                }
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "author_id": row["author_id"],
            "author_name": row["author_name"],
            "avatar_seed": row["avatar_seed"],
            "kind": row["kind"],
            "content": row["content"],
            "status": row["status"],
            "mentions": json.loads(row["mentions"] or "[]"),
            "reply_to_id": row["reply_to_id"],
            "mood": row["mood"],
            "relay": (
                {
                    "conversation_id": row["relay_conversation_id"],
                    "direction": row["relay_direction"],
                    "peer_id": row["relay_peer_id"],
                    "peer_name": row["relay_peer_name"],
                }
                if row["relay_direction"]
                else None
            ),
            "schedule": (
                {"id": row["schedule_id"], "title": row["schedule_title"] or ""}
                if row["schedule_id"]
                else None
            ),
            "seq": row["seq"],
            "created_at": row["created_at"],
            "interrupts": bool(row["interrupts"]),
            "run_id": row["run_id"],
            "reminder": bool(row["reminder"]),
            "task_id": row["task_id"],
            "route": json.loads(row["route"]) if row["route"] else None,
            "addresses": json.loads(row["addresses"] or "[]"),
            "status_card": json.loads(row["status_card"]) if row["status_card"] else None,
            "leads_to_tools": (
                None if row["leads_to_tools"] is None else bool(row["leads_to_tools"])
            ),
            "origin": origin,
            "action": action,
            "attachments": self._message_attachments(conn, row["id"]),
            "reactions": self._message_reactions(conn, row["id"]),
        }
