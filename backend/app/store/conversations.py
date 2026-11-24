"""Conversations, their members and the sidebar layout over them.

A background conversation is a scheduled run's hidden workspace: no client may read it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import GROUP_BUSY, GroupDissolveRefused, NOT_A_GROUP
from ..sidebar import FolderNameTaken, folder_name_key, shape_layout
from .db import _UNSET, new_id, now_iso

class Conversations:
    """Conversations, members and the sidebar."""

    def dissolve_group(self, conversation_id: str) -> dict | None:
        """Delete one group and everything that belongs only to it; return the removed row.

        Only ``group`` conversations dissolve: the supervisor's thread, member chats and
        private bot chats are refused, as is a group that is replying right now. Members,
        their chats, notes and tasks stay; history rows elsewhere that point here stay as
        they are.
        """
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                return None
            if row["kind"] != "group":
                raise GroupDissolveRefused(NOT_A_GROUP)
            # A task thread belongs only to its group's task: it goes with the group. Threads
            # have no foreign key to the group, so they are collected before the rows vanish.
            threads = [
                str(thread["thread_id"])
                for thread in tx.execute(
                    "SELECT DISTINCT thread_id FROM group_tasks "
                    "WHERE conversation_id = ? AND thread_id IS NOT NULL",
                    (conversation_id,),
                ).fetchall()
            ]
            # Busy means anything still replying for this group, including a member working in
            # one of its task threads: deleting a live run's rows would fail it mid-write.
            marks = ",".join("?" for _ in range(1 + len(threads)))
            running = tx.execute(
                f"SELECT 1 FROM runs WHERE status = 'running' "
                f"AND conversation_id IN ({marks}) LIMIT 1",
                [conversation_id, *threads],
            ).fetchone()
            if running is not None:
                raise GroupDissolveRefused(GROUP_BUSY)
            removed = self._conversation_row(row)
            # The group's own facts only ever applied inside it: they go with it.
            tx.execute(
                "DELETE FROM memories_fts WHERE memory_id IN "
                "(SELECT id FROM memories WHERE scope = 'group' AND agent_id = ?)",
                (conversation_id,),
            )
            tx.execute(
                "DELETE FROM memories WHERE scope = 'group' AND agent_id = ?", (conversation_id,)
            )
            self._delete_conversation_rows(tx, conversation_id)
            for thread_id in threads:
                self._delete_conversation_rows(tx, thread_id)
            return removed
    @staticmethod
    def _delete_conversation_content(conn: sqlite3.Connection, conversation_id: str) -> None:
        """Remove everything said and done in one conversation, keeping the conversation."""
        conn.execute(
            "DELETE FROM tool_invocations WHERE run_id IN "
            "(SELECT id FROM runs WHERE conversation_id = ?)",
            (conversation_id,),
        )
        conn.execute("DELETE FROM actions WHERE conversation_id = ?", (conversation_id,))
        conn.execute(
            "DELETE FROM conversation_summaries WHERE conversation_id = ?", (conversation_id,)
        )
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM runs WHERE conversation_id = ?", (conversation_id,))
    def _delete_conversation_rows(self, conn: sqlite3.Connection, conversation_id: str) -> None:
        """Remove a conversation and every row that belongs only to it.

        Messages, runs, membership and sidebar rows cascade from the conversation, and an
        action cascades from its run. A tool invocation has no foreign key, so it follows
        its run explicitly. Memories the conversation produced keep their content and only
        lose the pointer, so what the team learned outlives the chat it learned it in.
        """
        self._delete_conversation_content(conn, conversation_id)
        self._detach_memories(conn, conversation_id)
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    @staticmethod
    def _detach_memories(conn: sqlite3.Connection, conversation_id: str) -> None:
        """Forget which conversation a memory came from, keeping the memory itself."""
        conn.execute(
            "UPDATE memories SET source_conversation_id = NULL WHERE source_conversation_id = ?",
            (conversation_id,),
        )
    def create_conversation(
        self,
        *,
        kind: str,
        title: str,
        purpose: str,
        member_ids: list[str],
        legacy_roles: list[dict] | None = None,
    ) -> dict:
        conversation_id = new_id("c")
        with self._transaction() as tx:
            self._insert_conversation(
                tx,
                conversation_id=conversation_id,
                kind=kind,
                title=title,
                purpose=purpose,
                member_ids=member_ids,
                legacy_roles=legacy_roles or [],
            )
        conversation = self.get_conversation(conversation_id)
        assert conversation is not None
        return conversation
    def get_conversation(self, conversation_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return None if row is None else self._conversation_row(row)
    def list_conversations(self) -> list[dict]:
        """Every conversation a client may see.

        A schedule's hidden workspace and a group task's thread are both left out: the
        workspace panel, the sidebar and the team directories list chats the user opens by
        name, and a thread is reached through its task card instead.
        """
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM conversations WHERE kind NOT IN ('background', 'thread')
                 ORDER BY CASE kind WHEN 'direct' THEN 0 ELSE 1 END, updated_order DESC, rowid DESC
                """
            ).fetchall()
        return [self._conversation_row(row) for row in rows]
    def direct_conversation(self, agent_id: str | None = None) -> dict | None:
        """The supervisor's direct thread, or the direct thread of one agent."""
        conn = self._ensure()
        with self._lock:
            if agent_id is None:
                row = conn.execute(
                    "SELECT * FROM conversations WHERE kind = 'direct' ORDER BY rowid ASC LIMIT 1"
                ).fetchone()
                return None if row is None else self._conversation_row(row)
            row = conn.execute(
                """
                SELECT c.* FROM conversations c
                  JOIN conversation_members m ON m.conversation_id = c.id
                 WHERE c.kind = 'direct' AND m.agent_id = ?
                 ORDER BY c.rowid ASC LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return None if row is None else self._conversation_row(row)
    def ensure_direct_conversation(self, agent_id: str) -> dict | None:
        """Idempotently return the one-to-one chat with ``agent_id``, creating it once."""
        existing = self.direct_conversation(agent_id)
        if existing is not None:
            return existing
        agent = self.get_agent(agent_id)
        if agent is None:
            return None
        if agent["kind"] == "supervisor":
            return self.direct_conversation()
        conversation_id = new_id("c")
        with self._transaction() as tx:
            self._insert_conversation(
                tx,
                conversation_id=conversation_id,
                kind="direct",
                title=agent["name"],
                purpose=f"与「{agent['name']}」的私聊",
                member_ids=[agent_id],
                legacy_roles=[],
            )
        return self.get_conversation(conversation_id)

    def private_conversation(self, agent_a: str, agent_b: str) -> dict | None:
        """The private chat between exactly these two bots, regardless of who spoke first."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT c.* FROM conversations c
                 WHERE c.kind = 'private'
                   AND (SELECT COUNT(*) FROM conversation_members m
                         WHERE m.conversation_id = c.id) = 2
                   AND EXISTS (SELECT 1 FROM conversation_members m
                                WHERE m.conversation_id = c.id AND m.agent_id = ?)
                   AND EXISTS (SELECT 1 FROM conversation_members m
                                WHERE m.conversation_id = c.id AND m.agent_id = ?)
                 ORDER BY c.rowid ASC LIMIT 1
                """,
                (agent_a, agent_b),
            ).fetchone()
        return None if row is None else self._conversation_row(row)
    def ensure_private_conversation(self, agent_a: str, agent_b: str) -> dict | None:
        """Idempotently return the private chat between two bots, creating it once.

        Title order follows the pair's first caller, so the name reads the way the
        conversation actually started; later calls from either side reuse the same row.
        """
        if agent_a == agent_b:
            return None
        first = self.get_agent(agent_a)
        second = self.get_agent(agent_b)
        if first is None or second is None:
            return None
        existing = self.private_conversation(agent_a, agent_b)
        if existing is not None:
            return existing
        conversation_id = new_id("c")
        with self._transaction() as tx:
            self._insert_conversation(
                tx,
                conversation_id=conversation_id,
                kind="private",
                title=f"{first['name']} ⇄ {second['name']}",
                purpose=f"「{first['name']}」与「{second['name']}」的私聊",
                member_ids=[agent_a, agent_b],
                legacy_roles=[],
            )
        return self.get_conversation(conversation_id)
    def is_background_conversation(self, conversation_id: str) -> bool:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT kind FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return row is not None and row["kind"] == "background"
    def ensure_background_conversation(
        self, schedule: dict, *, owner_id: str, purpose: str
    ) -> dict:
        """The schedule's hidden workspace, created once and kept in step with its title."""
        title = f"定时任务「{schedule['title']}」"
        with self._transaction() as tx:
            row = None
            if schedule.get("workspace_conversation_id"):
                row = tx.execute(
                    "SELECT id FROM conversations WHERE id = ? AND kind = 'background'",
                    (schedule["workspace_conversation_id"],),
                ).fetchone()
            if row is None:
                conversation_id = new_id("c")
                self._insert_conversation(
                    tx,
                    conversation_id=conversation_id,
                    kind="background",
                    title=title,
                    purpose=purpose,
                    member_ids=[owner_id],
                    legacy_roles=[],
                )
            else:
                conversation_id = str(row["id"])
                tx.execute(
                    "UPDATE conversations SET title = ?, purpose = ? WHERE id = ?",
                    (title, purpose, conversation_id),
                )
                self._replace_members(tx, conversation_id, [owner_id])
            tx.execute(
                "UPDATE scheduled_tasks SET workspace_conversation_id = ?, updated_at = ? "
                "WHERE id = ?",
                (conversation_id, now_iso(), schedule["id"]),
            )
        conversation = self.get_conversation(conversation_id)
        assert conversation is not None
        return conversation
    def reset_conversation(self, conversation_id: str) -> None:
        """Empty one background workspace: its messages, runs and steps all go."""
        with self._transaction() as tx:
            self._delete_conversation_content(tx, conversation_id)

    def sweep_background_conversations(self) -> int:
        """Delete workspaces whose schedule is gone; returns how many were removed."""
        with self._transaction() as tx:
            kept = {
                str(row["workspace_conversation_id"])
                for row in tx.execute(
                    "SELECT workspace_conversation_id FROM scheduled_tasks "
                    "WHERE workspace_conversation_id IS NOT NULL"
                ).fetchall()
            }
            removed = 0
            for row in tx.execute("SELECT id FROM conversations WHERE kind = 'background'").fetchall():
                if str(row["id"]) in kept:
                    continue
                self._delete_conversation_rows(tx, str(row["id"]))
                removed += 1
        return removed
    def last_agent_message(self, conversation_id: str) -> dict | None:
        """The newest finished bot reply in one conversation, complete or cut off."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT * FROM messages
                 WHERE conversation_id = ? AND kind = 'agent'
                   AND status IN ('complete', 'interrupted')
                 ORDER BY seq DESC, rowid DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._message_row(row)

    # ------------------------------------------------------------------------ inbox

    def conversation_read_seq(self, conversation_id: str) -> int:
        """How far the user has read one conversation; 0 when it was never opened."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT last_read_seq FROM conversation_reads WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return 0 if row is None else int(row["last_read_seq"])

    def mark_conversation_read(self, conversation_id: str, seq: int) -> bool:
        """Remember the newest message the user has seen; the marker only ever moves forward."""
        with self._transaction() as tx:
            exists = tx.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                return False
            tx.execute(
                """
                INSERT INTO conversation_reads (conversation_id, last_read_seq, read_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    last_read_seq = MAX(last_read_seq, excluded.last_read_seq),
                    read_at = excluded.read_at
                """,
                (conversation_id, max(0, int(seq)), now_iso()),
            )
        return True

    def conversation_unread(self, conversation_id: str) -> bool:
        """Whether news arrived after the last message the user saw there.

        Everything a bot says counts, except inside a task's thread: that chat belongs to the
        task and is not listed, so its own unread never reaches the sidebar. A task card counts
        only when it ended badly (``interrupted``): a card that merely went away is replaced by
        the result reply the group already got.
        """
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM messages m
                     WHERE m.conversation_id = ?
                       AND (m.kind = 'agent'
                            OR (m.kind = 'task' AND m.status = 'interrupted'))
                       AND m.status IN ('complete', 'interrupted')
                       AND m.seq > COALESCE(
                           (SELECT last_read_seq FROM conversation_reads
                             WHERE conversation_id = ?), 0)
                ) AS unread
                """,
                (conversation_id, conversation_id),
            ).fetchone()
        return bool(row["unread"])

    def finished_runs(self, limit: int = 10) -> list[dict]:
        """Recently completed runs the user has not opened yet, newest first.

        A run counts as opened once the conversation's read marker has reached its last
        message: opening the chat after the run cleared it. Private and workflow pages are
        left out because the sidebar does not list them.
        """
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT r.id AS run_id, r.conversation_id, c.title AS conversation_title,
                       r.finished_at,
                       (SELECT m.id FROM messages m WHERE m.run_id = r.id
                         ORDER BY m.seq DESC, m.rowid DESC LIMIT 1) AS message_id,
                       (SELECT m.content FROM messages m WHERE m.run_id = r.id
                         ORDER BY m.seq DESC, m.rowid DESC LIMIT 1) AS preview,
                       (SELECT m.author_name FROM messages m
                         WHERE m.run_id = r.id AND m.kind = 'agent'
                         ORDER BY m.seq DESC, m.rowid DESC LIMIT 1) AS agent_name
                  FROM runs r
                  JOIN conversations c ON c.id = r.conversation_id
                 WHERE r.status = 'completed' AND r.finished_at IS NOT NULL
                   AND c.kind IN ('direct', 'group')
                   AND (SELECT COALESCE(MAX(m.seq), 0) FROM messages m WHERE m.run_id = r.id)
                       > COALESCE(
                           (SELECT last_read_seq FROM conversation_reads cr
                             WHERE cr.conversation_id = r.conversation_id), 0)
                 ORDER BY r.finished_at DESC, r.rowid DESC
                 LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        found: list[dict] = []
        for row in rows:
            preview = " ".join(str(row["preview"] or "").split())
            if len(preview) > 80:
                preview = preview[:79] + "…"
            found.append(
                {
                    "run_id": row["run_id"],
                    "conversation_id": row["conversation_id"],
                    "conversation_title": row["conversation_title"],
                    "agent_name": row["agent_name"] or "",
                    "finished_at": row["finished_at"],
                    "message_id": row["message_id"],
                    "preview": preview,
                }
            )
        return found
    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | object = _UNSET,
        purpose: str | object = _UNSET,
        member_ids: list[str] | object = _UNSET,
        rules: str | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        if rules is not _UNSET:
            sets.append("rules = ?")
            params.append(rules)
        if title is not _UNSET:
            sets.append("title = ?")
            params.append(title)
        if purpose is not _UNSET:
            sets.append("purpose = ?")
            params.append(purpose)
        with self._transaction() as tx:
            exists = tx.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                return None
            if sets:
                params.extend([now_iso(), self._next_order(tx), conversation_id])
                tx.execute(
                    f"UPDATE conversations SET {', '.join(sets)}, updated_at = ?, updated_order = ? "
                    "WHERE id = ?",
                    params,
                )
            if member_ids is not _UNSET:
                self._replace_members(tx, conversation_id, list(member_ids))  # type: ignore[arg-type]
                self._touch(tx, conversation_id)
        return self.get_conversation(conversation_id)

    def _insert_conversation(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        kind: str,
        title: str,
        purpose: str,
        member_ids: list[str],
        legacy_roles: list[dict],
        created_at: str | None = None,
        updated_at: str | None = None,
        updated_order: int | None = None,
    ) -> None:
        timestamp = created_at or now_iso()
        conn.execute(
            """
            INSERT INTO conversations
                (id, kind, title, purpose, legacy_roles, created_at, updated_at, updated_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                kind,
                title,
                purpose,
                json.dumps(legacy_roles, ensure_ascii=False),
                timestamp,
                updated_at or timestamp,
                self._next_order(conn) if updated_order is None else updated_order,
            ),
        )
        for position, agent_id in enumerate(member_ids):
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_members (conversation_id, agent_id, position)
                VALUES (?, ?, ?)
                """,
                (conversation_id, agent_id, position),
            )
    def _replace_members(
        self, conn: sqlite3.Connection, conversation_id: str, member_ids: list[str]
    ) -> None:
        conn.execute(
            "DELETE FROM conversation_members WHERE conversation_id = ?", (conversation_id,)
        )
        for position, agent_id in enumerate(member_ids):
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_members (conversation_id, agent_id, position)
                VALUES (?, ?, ?)
                """,
                (conversation_id, agent_id, position),
            )
    def member_ids(self, conversation_id: str) -> list[str]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT agent_id FROM conversation_members
                 WHERE conversation_id = ? ORDER BY position ASC, rowid ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [str(row["agent_id"]) for row in rows]
    def sidebar_layout(self) -> dict:
        """The stored layout: folders by position, then items by pin/folder position."""
        conn = self._ensure()
        with self._lock:
            folders = [
                {"id": row["id"], "name": row["name"], "position": int(row["position"])}
                for row in conn.execute(
                    "SELECT * FROM sidebar_folders ORDER BY position ASC, rowid ASC"
                ).fetchall()
            ]
            items = [
                {
                    "conversation_id": row["conversation_id"],
                    "pinned": bool(row["pinned"]),
                    "pin_position": int(row["pin_position"]),
                    "folder_id": row["folder_id"],
                    "position": int(row["position"]),
                }
                for row in conn.execute(
                    """
                    SELECT * FROM sidebar_items
                     ORDER BY pinned DESC, pin_position ASC, folder_id IS NOT NULL,
                              position ASC, rowid ASC
                    """
                ).fetchall()
            ]
        return {"folders": folders, "items": items}
    def replace_sidebar(self, items: list[dict], folder_ids: list[str]) -> dict:
        """Replace the whole layout; raises :class:`~app.sidebar.SidebarInvalid`."""
        with self._transaction() as tx:
            shaped = shape_layout(
                items=items,
                folder_ids=folder_ids,
                conversation_ids=[
                    str(row["id"])
                    for row in tx.execute(
                        "SELECT id FROM conversations "
                        "WHERE kind NOT IN ('private', 'background', 'thread')"
                    )
                ],
                known_folder_ids=[
                    str(row["id"]) for row in tx.execute("SELECT id FROM sidebar_folders")
                ],
            )
            tx.execute("DELETE FROM sidebar_items")
            for row in shaped:
                tx.execute(
                    """
                    INSERT INTO sidebar_items
                        (conversation_id, pinned, pin_position, folder_id, position)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["conversation_id"],
                        int(row["pinned"]),
                        row["pin_position"],
                        row["folder_id"],
                        row["position"],
                    ),
                )
            for position, folder_id in enumerate(folder_ids):
                tx.execute(
                    "UPDATE sidebar_folders SET position = ? WHERE id = ?", (position, folder_id)
                )
        return self.sidebar_layout()
    def create_folder(self, name: str) -> dict:
        """Create one folder at the end; raises :class:`FolderNameTaken` on a duplicate."""
        with self._transaction() as tx:
            if self._folder_name_taken(tx, name):
                raise FolderNameTaken("已有同名分组。")
            position = tx.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM sidebar_folders"
            ).fetchone()["next"]
            tx.execute(
                "INSERT INTO sidebar_folders (id, name, position) VALUES (?, ?, ?)",
                (new_id("f"), name, int(position)),
            )
        return self.sidebar_layout()
    def rename_folder(self, folder_id: str, name: str) -> dict | None:
        with self._transaction() as tx:
            exists = tx.execute(
                "SELECT 1 FROM sidebar_folders WHERE id = ?", (folder_id,)
            ).fetchone()
            if exists is None:
                return None
            if self._folder_name_taken(tx, name, exclude_id=folder_id):
                raise FolderNameTaken("已有同名分组。")
            tx.execute("UPDATE sidebar_folders SET name = ? WHERE id = ?", (name, folder_id))
        return self.sidebar_layout()
    def delete_folder(self, folder_id: str) -> dict | None:
        """Delete one folder; its items fall back to the ungrouped area."""
        with self._transaction() as tx:
            cursor = tx.execute("DELETE FROM sidebar_folders WHERE id = ?", (folder_id,))
            if cursor.rowcount == 0:
                return None
        return self.sidebar_layout()
    @staticmethod
    def _folder_name_taken(
        conn: sqlite3.Connection, name: str, *, exclude_id: str | None = None
    ) -> bool:
        wanted = folder_name_key(name)
        for row in conn.execute("SELECT id, name FROM sidebar_folders").fetchall():
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if folder_name_key(str(row["name"])) == wanted:
                return True
        return False
    def _conversation_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "purpose": row["purpose"],
            "rules": row["rules"] or "",
            "member_ids": self.member_ids(row["id"]),
            "last_message": self.last_message_preview(row["id"]),
            "legacy_roles": json.loads(row["legacy_roles"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "unread": self.conversation_unread(row["id"]),
        }
