"""Long-term memory: rows, scopes, retirement and the extraction bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from ..memories import (
    DEFAULT_CORE_CHARS,
    ORIGIN_EXTRACT,
    OWNED_SCOPES,
    SCOPE_AGENT,
    STATUS_ACTIVE,
    STATUS_RETIRED,
    bigrams,
    derive_topic,
    fts_query,
)
from .db import _UNSET, _like_escape, new_id, now_iso

# Memory switches live in ``meta`` so the panel's two toggles survive a restart.
MEMORY_PAUSED_KEY = "memory_paused"
MEMORY_AUTO_EXTRACT_KEY = "memory_auto_extract"
MEMORY_CORE_CHARS_KEY = "memory_core_chars"
MEMORY_LAST_ERROR_KEY = "memory_last_error"

# A failure is shown in the panel's shadow, not in a log viewer; keep it to one line.
MEMORY_ERROR_CHARS = 300


def _memory_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "scope": row["scope"],
        "agent_id": row["agent_id"],
        "topic": row["topic"],
        "content": row["content"],
        "pinned": bool(row["pinned"]),
        "status": row["status"],
        "superseded_by": row["superseded_by"],
        "origin": row["origin"],
        "author_id": row["author_id"],
        "source_conversation_id": row["source_conversation_id"],
        "source_message_ids": json.loads(row["source_message_ids"] or "[]"),
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

class Memories:
    """Long-term memory rows."""

    def memory_settings(self) -> dict[str, Any]:
        """The workspace's two switches and the prompt budget, with their defaults."""
        conn = self._ensure()
        keys = (MEMORY_PAUSED_KEY, MEMORY_AUTO_EXTRACT_KEY, MEMORY_CORE_CHARS_KEY)
        with self._lock:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key IN (?, ?, ?)", keys
            ).fetchall()
        stored = {str(row["key"]): str(row["value"]) for row in rows}
        try:
            core_chars = int(stored.get(MEMORY_CORE_CHARS_KEY) or DEFAULT_CORE_CHARS)
        except ValueError:
            core_chars = DEFAULT_CORE_CHARS
        return {
            # Nothing is read or written: the bots answer without memories.
            "paused": stored.get(MEMORY_PAUSED_KEY) == "1",
            "auto_extract": stored.get(MEMORY_AUTO_EXTRACT_KEY) != "0",
            "core_chars": core_chars if core_chars > 0 else DEFAULT_CORE_CHARS,
        }
    def update_memory_settings(
        self,
        *,
        paused: bool | object = _UNSET,
        auto_extract: bool | object = _UNSET,
    ) -> dict[str, Any]:
        """Store the two switches; an omitted one is left exactly as it was."""
        with self._transaction() as tx:
            if paused is not _UNSET:
                self._meta_set(tx, MEMORY_PAUSED_KEY, "1" if paused else "0")
            if auto_extract is not _UNSET:
                self._meta_set(tx, MEMORY_AUTO_EXTRACT_KEY, "1" if auto_extract else "0")
        return self.memory_settings()
    def record_memory_error(self, message: str) -> None:
        """Keep the last extraction failure for the panel; the user never sees it otherwise."""
        text = " ".join(str(message).split())[:MEMORY_ERROR_CHARS]
        with self._transaction() as tx:
            self._meta_set(tx, MEMORY_LAST_ERROR_KEY, text)
    def memory_last_error(self) -> str | None:
        conn = self._ensure()
        with self._lock:
            return self._meta(conn, MEMORY_LAST_ERROR_KEY)
    def list_memories(self) -> list[dict]:
        """Every row, active and retired, newest first: what ``GET /memories`` answers."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [_memory_row(row) for row in rows]
    def get_memory(self, memory_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return None if row is None else _memory_row(row)
    def visible_memories(self, *, agent_id: str, group_id: str | None = None) -> list[dict]:
        """What one bot may see: the user's facts, the team's decisions and its own notes.

        Speaking in a group (``group_id``) adds that group's own facts. Retired rows are
        left out; pinned rows come first, then the newest.
        """
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM memories
                 WHERE status = ?
                   AND (scope IN ('user', 'team') OR (scope = 'agent' AND agent_id = ?)
                        OR (scope = 'group' AND agent_id = ?))
                 ORDER BY pinned DESC, created_at DESC, rowid DESC
                """,
                (STATUS_ACTIVE, agent_id, group_id),
            ).fetchall()
        return [_memory_row(row) for row in rows]
    def search_memories(
        self,
        query: str,
        *,
        agent_id: str,
        limit: int = 8,
        exclude_ids: Sequence[str] = (),
        group_id: str | None = None,
    ) -> list[dict]:
        """Active memories matching ``query``, best match first, within one bot's scopes.

        Chinese and Latin both go through the bigram index and rank by BM25; a query too
        short to have a pair (one character, or a single emoji) falls back to a substring
        scan, which is the only way such a query can match at all.
        """
        text = " ".join(str(query).split())
        if not text:
            return []
        exclusion = list(dict.fromkeys(str(value) for value in exclude_ids))
        wanted = max(1, int(limit)) + len(exclusion)
        scopes = (
            "m.scope IN ('user', 'team') OR (m.scope = 'agent' AND m.agent_id = ?)"
            " OR (m.scope = 'group' AND m.agent_id = ?)"
        )
        match = fts_query(text) if len(text) > 1 else None
        conn = self._ensure()
        with self._lock:
            if match is not None:
                rows = conn.execute(
                    f"""
                    SELECT m.* FROM memories_fts
                      JOIN memories m ON m.id = memories_fts.memory_id
                     WHERE memories_fts MATCH ?
                       AND m.status = ?
                       AND ({scopes})
                     ORDER BY bm25(memories_fts), m.created_at DESC, m.rowid DESC
                     LIMIT ?
                    """,
                    (match, STATUS_ACTIVE, agent_id, group_id, wanted),
                ).fetchall()
            else:
                pattern = f"%{_like_escape(text)}%"
                rows = conn.execute(
                    f"""
                    SELECT m.* FROM memories m
                     WHERE m.status = ?
                       AND (m.content LIKE ? ESCAPE '\\' OR m.topic LIKE ? ESCAPE '\\')
                       AND ({scopes})
                     ORDER BY m.pinned DESC, m.created_at DESC, m.rowid DESC
                     LIMIT ?
                    """,
                    (STATUS_ACTIVE, pattern, pattern, agent_id, group_id, wanted),
                ).fetchall()
        found = [_memory_row(row) for row in rows]
        return [row for row in found if row["id"] not in exclusion][: max(1, int(limit))]
    def create_memory(
        self,
        *,
        content: str,
        scope: str,
        topic: str | None = None,
        agent_id: str | None = None,
        origin: str,
        author_id: str | None = None,
        source_conversation_id: str | None = None,
        source_message_ids: Sequence[str] = (),
        pinned: bool = False,
        supersede_topic: bool = False,
    ) -> dict:
        """Insert one memory; ``supersede_topic`` retires the active row of the same topic."""
        text = " ".join(str(content).split())
        label = " ".join(str(topic or "").split()) or derive_topic(text)
        memory_id = new_id("mem")
        timestamp = now_iso()
        with self._transaction() as tx:
            if supersede_topic:
                self._supersede_topic(
                    tx,
                    scope=scope,
                    agent_id=agent_id if scope in OWNED_SCOPES else None,
                    topic=label,
                    timestamp=timestamp,
                    keeper=memory_id,
                )
            self._insert_memory(
                tx,
                memory_id=memory_id,
                scope=scope,
                agent_id=agent_id if scope in OWNED_SCOPES else None,
                topic=label,
                content=text,
                pinned=pinned,
                origin=origin,
                author_id=author_id,
                source_conversation_id=source_conversation_id,
                source_message_ids=source_message_ids,
                timestamp=timestamp,
            )
        created = self.get_memory(memory_id)
        assert created is not None
        return created
    def update_memory(
        self,
        memory_id: str,
        *,
        content: str | object = _UNSET,
        topic: str | object = _UNSET,
        pinned: bool | object = _UNSET,
    ) -> dict | None:
        """Edit one row; a changed content is a new row that replaces the old one.

        The replacement keeps the retired row's scope, agent, origin and source, so the
        panel still says where the fact came from, and it carries whatever pin the caller
        asked for.
        """
        row = self.get_memory(memory_id)
        if row is None:
            return None
        text = None if content is _UNSET else " ".join(str(content).split())
        label = (
            row["topic"]
            if topic is _UNSET
            else " ".join(str(topic).split()) or derive_topic(text or row["content"])
        )
        if text is not None and text and text != row["content"]:
            return self._replace_memory(
                row,
                content=text,
                topic=label,
                pinned=row["pinned"] if pinned is _UNSET else bool(pinned),
            )
        sets: list[str] = []
        params: list[Any] = []
        if topic is not _UNSET and label != row["topic"]:
            sets.append("topic = ?")
            params.append(label)
        if pinned is not _UNSET and bool(pinned) != row["pinned"]:
            sets.append("pinned = ?")
            params.append(1 if pinned else 0)
        if not sets:
            return row
        params.extend([now_iso(), memory_id])
        with self._transaction() as tx:
            tx.execute(
                f"UPDATE memories SET {', '.join(sets)}, updated_at = ? WHERE id = ?", params
            )
            if "topic = ?" in sets:
                self._index_memory(tx, memory_id, label, row["content"])
        return self.get_memory(memory_id)
    def retire_memory(self, memory_id: str, *, superseded_by: str | None = None) -> dict | None:
        """Forget a row softly: it leaves search and prompts but the panel may still show it."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return None
            if str(row["status"]) == STATUS_ACTIVE:
                self._retire_row(tx, memory_id, superseded_by=superseded_by, timestamp=now_iso())
        return self.get_memory(memory_id)
    def delete_memory(self, memory_id: str) -> bool:
        """Delete one row for good, FTS entry included; ``False`` when nothing matched."""
        with self._transaction() as tx:
            cursor = tx.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._unindex_memory(tx, memory_id)
            return cursor.rowcount > 0
    def apply_memory_ops(
        self,
        ops: Sequence[dict],
        *,
        author_id: str,
        source_conversation_id: str | None,
        source_message_ids: Sequence[str] = (),
    ) -> list[dict]:
        """Apply a model's add/update/delete ops in one transaction; return the new rows.

        An update replaces the named row with a fresh one (the old keeps its timeline), a
        delete forgets it softly, and an add supersedes an active row of the same topic so
        one topic never has two live rows in the same scope.
        """
        created: list[str] = []
        with self._transaction() as tx:
            for op in ops:
                kind = str(op.get("op") or "")
                if kind == "add":
                    scope = str(op.get("scope") or "user")
                    topic = " ".join(str(op.get("topic") or "").split())
                    content = " ".join(str(op.get("content") or "").split())
                    if not content:
                        continue
                    label = topic or derive_topic(content)
                    memory_id = new_id("mem")
                    timestamp = now_iso()
                    self._supersede_topic(
                        tx,
                        scope=scope,
                        agent_id=None,
                        topic=label,
                        timestamp=timestamp,
                        keeper=memory_id,
                    )
                    self._insert_memory(
                        tx,
                        memory_id=memory_id,
                        scope=scope,
                        agent_id=None,
                        topic=label,
                        content=content,
                        pinned=False,
                        origin=ORIGIN_EXTRACT,
                        author_id=author_id,
                        source_conversation_id=source_conversation_id,
                        source_message_ids=source_message_ids,
                        timestamp=timestamp,
                    )
                    created.append(memory_id)
                    continue
                memory_id = str(op.get("id") or "")
                if not memory_id:
                    continue
                if kind == "delete":
                    self._retire_row(tx, memory_id, superseded_by=None, timestamp=now_iso())
                    continue
                if kind != "update":
                    continue
                row = tx.execute(
                    "SELECT * FROM memories WHERE id = ? AND status = ?",
                    (memory_id, STATUS_ACTIVE),
                ).fetchone()
                if row is None:
                    continue
                content = " ".join(str(op.get("content") or "").split())
                if not content:
                    continue
                topic = " ".join(str(op.get("topic") or "").split()) or str(row["topic"])
                if content == str(row["content"]) and topic == str(row["topic"]):
                    continue
                replacement = new_id("mem")
                timestamp = now_iso()
                self._retire_row(
                    tx, memory_id, superseded_by=replacement, timestamp=timestamp
                )
                self._insert_memory(
                    tx,
                    memory_id=replacement,
                    scope=str(row["scope"]),
                    agent_id=row["agent_id"],
                    topic=topic,
                    content=content,
                    pinned=bool(row["pinned"]),
                    origin=ORIGIN_EXTRACT,
                    author_id=author_id,
                    source_conversation_id=source_conversation_id,
                    source_message_ids=source_message_ids,
                    timestamp=timestamp,
                )
                created.append(replacement)
        rows = [self.get_memory(memory_id) for memory_id in created]
        return [row for row in rows if row is not None]
    def claim_memory_run(self, run_id: str) -> bool:
        """Reserve one run for extraction; ``False`` when it was already claimed."""
        with self._transaction() as tx:
            cursor = tx.execute(
                "INSERT OR IGNORE INTO memory_runs (run_id, created_at) VALUES (?, ?)",
                (run_id, now_iso()),
            )
            return cursor.rowcount > 0
    def queue_memory_turn(
        self,
        run_id: str,
        *,
        conversation_id: str,
        agent_id: str,
        trigger_message_id: str,
        reply_ids: Sequence[str],
    ) -> int | None:
        """Claim this run and park it for its conversation's next batch.

        Returns how many turns that conversation now has waiting, or ``None`` when the run
        was already claimed (a run is never extracted twice, before or after a restart).
        """
        stamp = now_iso()
        with self._transaction() as tx:
            claimed = tx.execute(
                "INSERT OR IGNORE INTO memory_runs (run_id, created_at) VALUES (?, ?)",
                (run_id, stamp),
            ).rowcount
            if not claimed:
                return None
            tx.execute(
                """
                INSERT INTO memory_pending
                    (run_id, conversation_id, agent_id, trigger_message_id, reply_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, conversation_id, agent_id, trigger_message_id,
                 json.dumps(list(reply_ids)), stamp),
            )
            row = tx.execute(
                "SELECT COUNT(*) AS total FROM memory_pending WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            return int(row["total"])
    def pending_memory_turns(self, conversation_id: str) -> list[dict]:
        """The turns waiting in one conversation, oldest first."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM memory_pending WHERE conversation_id = ? ORDER BY created_at, rowid",
                (conversation_id,),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "agent_id": row["agent_id"],
                "trigger_message_id": row["trigger_message_id"],
                "reply_ids": json.loads(row["reply_ids"]),
            }
            for row in rows
        ]
    def memory_pending_conversations(self) -> list[str]:
        """Every conversation with turns still waiting (what a restart picks up again)."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute("SELECT DISTINCT conversation_id FROM memory_pending").fetchall()
        return [str(row["conversation_id"]) for row in rows]
    def clear_memory_turns(self, run_ids: Sequence[str]) -> None:
        if not run_ids:
            return
        marks = ", ".join("?" for _ in run_ids)
        with self._transaction() as tx:
            tx.execute(f"DELETE FROM memory_pending WHERE run_id IN ({marks})", tuple(run_ids))
    def _insert_memory(
        self,
        tx: sqlite3.Connection,
        *,
        memory_id: str,
        scope: str,
        agent_id: str | None,
        topic: str,
        content: str,
        pinned: bool,
        origin: str,
        author_id: str | None,
        source_conversation_id: str | None,
        source_message_ids: Sequence[str],
        timestamp: str,
    ) -> None:
        tx.execute(
            """
            INSERT INTO memories (
                id, scope, agent_id, topic, content, pinned, status, superseded_by,
                origin, author_id, source_conversation_id, source_message_ids,
                valid_from, valid_to, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                memory_id,
                scope,
                agent_id,
                topic,
                content,
                1 if pinned else 0,
                STATUS_ACTIVE,
                origin,
                author_id,
                source_conversation_id,
                json.dumps(list(source_message_ids), ensure_ascii=False),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        self._index_memory(tx, memory_id, topic, content)
    @staticmethod
    def _retire_row(
        tx: sqlite3.Connection,
        memory_id: str,
        *,
        superseded_by: str | None,
        timestamp: str,
    ) -> None:
        tx.execute(
            """
            UPDATE memories
               SET status = ?, superseded_by = ?, valid_to = ?, updated_at = ?
             WHERE id = ? AND status = ?
            """,
            (STATUS_RETIRED, superseded_by, timestamp, timestamp, memory_id, STATUS_ACTIVE),
        )
    @staticmethod
    def _supersede_topic(
        tx: sqlite3.Connection,
        *,
        scope: str,
        agent_id: str | None,
        topic: str,
        timestamp: str,
        keeper: str,
    ) -> None:
        """Retire every live row of one topic before its replacement is inserted."""
        tx.execute(
            """
            UPDATE memories
               SET status = ?, superseded_by = ?, valid_to = ?, updated_at = ?
             WHERE status = ? AND scope = ? AND agent_id IS ? AND topic = ?
            """,
            (STATUS_RETIRED, keeper, timestamp, timestamp, STATUS_ACTIVE, scope, agent_id, topic),
        )
    def _replace_memory(self, row: dict, *, content: str, topic: str, pinned: bool) -> dict:
        """Insert the successor of ``row`` and retire it, in one transaction."""
        replacement = new_id("mem")
        timestamp = now_iso()
        with self._transaction() as tx:
            self._retire_row(tx, row["id"], superseded_by=replacement, timestamp=timestamp)
            self._insert_memory(
                tx,
                memory_id=replacement,
                scope=row["scope"],
                agent_id=row["agent_id"],
                topic=topic,
                content=content,
                pinned=pinned,
                origin=row["origin"],
                author_id=row["author_id"],
                source_conversation_id=row["source_conversation_id"],
                source_message_ids=row["source_message_ids"],
                timestamp=timestamp,
            )
        created = self.get_memory(replacement)
        assert created is not None
        return created
    @staticmethod
    def _index_memory(
        tx: sqlite3.Connection, memory_id: str, topic: str, content: str
    ) -> None:
        """Rewrite this row's shadow text; the index holds one row per memory."""
        tx.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        tx.execute(
            "INSERT INTO memories_fts (memory_id, body) VALUES (?, ?)",
            (memory_id, bigrams(f"{topic} {content}")),
        )
    @staticmethod
    def _unindex_memory(tx: sqlite3.Connection, memory_id: str) -> None:
        tx.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
