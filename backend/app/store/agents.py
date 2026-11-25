"""Agents: the workspace roster, its names and the supervisor lookup."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from ..appearance import appearance_json, parse_appearance
from ..errors import AGENT_BUSY, AgentDeleteRefused, AgentNameTaken
from ..providers import model_ref_json, model_status
from .db import _UNSET, new_id, now_iso

class Agents:
    """The roster and its names."""

    @staticmethod
    def _name_key(name: str) -> str:
        """Unique comparison key: trimmed and case-insensitive across the workspace."""
        return name.strip().casefold()
    def _name_taken(
        self, conn: sqlite3.Connection, name: str, *, exclude_id: str | None = None
    ) -> bool:
        wanted = self._name_key(name)
        for row in conn.execute("SELECT id, name FROM agents").fetchall():
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if self._name_key(str(row["name"])) == wanted:
                return True
        return False
    def create_agent(
        self,
        *,
        name: str,
        instructions: str,
        kind: str = "member",
        avatar_seed: str | None = None,
    ) -> dict:
        conn = self._ensure()
        timestamp = now_iso()
        agent_id = new_id("a")
        seed = avatar_seed or uuid.uuid4().hex
        with self._lock:
            if self._name_taken(conn, name):
                raise AgentNameTaken(f"成员名称「{name.strip()}」已被使用，请换一个名字。")
            conn.execute(
                """
                INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, name, instructions, kind, seed, timestamp, timestamp),
            )
        agent = self.get_agent(agent_id)
        assert agent is not None
        return agent
    def get_agent(self, agent_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return None if row is None else self._agent_row(row)
    def list_agents(self) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM agents
                 ORDER BY CASE kind WHEN 'supervisor' THEN 0 ELSE 1 END, rowid ASC
                """
            ).fetchall()
        return [self._agent_row(row) for row in rows]
    def supervisor(self) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM agents WHERE kind = 'supervisor' ORDER BY rowid ASC LIMIT 1"
            ).fetchone()
        return None if row is None else self._agent_row(row)
    def update_agent(
        self,
        agent_id: str,
        *,
        name: str | object = _UNSET,
        instructions: str | object = _UNSET,
        appearance: dict | None | object = _UNSET,
        model_ref: dict | None | object = _UNSET,
        token_budget: dict | None | object = _UNSET,
        tool_permissions: dict | None | object = _UNSET,
        send_mode: str | None | object = _UNSET,
        permission_mode: str | None | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not _UNSET:
            sets.append("name = ?")
            params.append(name)
        if instructions is not _UNSET:
            sets.append("instructions = ?")
            params.append(instructions)
        if appearance is not _UNSET:
            sets.append("appearance = ?")
            params.append(appearance_json(appearance))  # type: ignore[arg-type]
        if model_ref is not _UNSET:
            sets.append("model_ref = ?")
            params.append(None if model_ref is None else model_ref_json(model_ref))
        if token_budget is not _UNSET:
            sets.append("token_budget = ?")
            params.append(
                None
                if token_budget is None
                else json.dumps(token_budget, ensure_ascii=False)
            )
        if tool_permissions is not _UNSET:
            sets.append("tool_permissions = ?")
            params.append(
                None
                if tool_permissions is None
                else json.dumps(tool_permissions, ensure_ascii=False)
            )
        if send_mode is not _UNSET:
            sets.append("send_mode = ?")
            params.append(send_mode)
        if permission_mode is not _UNSET:
            sets.append("permission_mode = ?")
            params.append(permission_mode)
        if not sets:
            return self.get_agent(agent_id)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(agent_id)
        conn = self._ensure()
        with self._lock:
            if name is not _UNSET and self._name_taken(conn, str(name), exclude_id=agent_id):
                raise AgentNameTaken(f"成员名称「{str(name).strip()}」已被使用，请换一个名字。")
            cursor = conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                return None
        return self.get_agent(agent_id)
    def delete_agent(self, agent_id: str) -> dict | None:
        """Delete one member and everything private to it; return the removed row.

        The supervisor is never deletable. A member is refused while any running run is
        using it: speaking, queued, or running inside its own direct or private chat.
        Groups lose only the membership row; their history keeps the author snapshot. The
        task threads it worked in are its own, like its chats: they go with it.
        """
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None:
                return None
            if row["kind"] == "supervisor":
                raise AgentDeleteRefused(f"「{row['name']}」不能删除。")
            conversations = [
                str(found["id"])
                for found in tx.execute(
                    """
                    SELECT c.id FROM conversations c
                      JOIN conversation_members m ON m.conversation_id = c.id
                     WHERE m.agent_id = ?
                       AND c.kind IN ('direct', 'private', 'background', 'thread')
                    """,
                    (agent_id,),
                ).fetchall()
            ]
            self._refuse_while_busy(tx, agent_id, conversations)
            for conversation_id in conversations:
                # Messages, runs, actions and sidebar rows follow the conversation.
                self._detach_memories(tx, conversation_id)
                tx.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            tx.execute("DELETE FROM conversation_members WHERE agent_id = ?", (agent_id,))
            # The schedule stays; its results then come from the supervisor.
            tx.execute(
                "UPDATE scheduled_tasks SET owner_agent_id = NULL, workspace_conversation_id = NULL "
                "WHERE owner_agent_id = ?",
                (agent_id,),
            )
            tx.execute("UPDATE tasks SET assignee_id = NULL WHERE assignee_id = ?", (agent_id,))
            tx.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            return self._agent_row(row)
    @staticmethod
    def _refuse_while_busy(
        conn: sqlite3.Connection, agent_id: str, conversation_ids: list[str]
    ) -> None:
        """Refuse a delete while any running run is speaking as, or with, this member."""
        for run in conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall():
            if run["conversation_id"] in conversation_ids:
                raise AgentDeleteRefused(AGENT_BUSY)
            if run["current_agent_id"] == agent_id:
                raise AgentDeleteRefused(AGENT_BUSY)
            if agent_id in json.loads(run["pending_agent_ids"] or "[]"):
                raise AgentDeleteRefused(AGENT_BUSY)
    def _agent_row(self, row: sqlite3.Row) -> dict:
        agent = {
            "id": row["id"],
            "name": row["name"],
            "instructions": row["instructions"],
            "kind": row["kind"],
            "avatar_seed": row["avatar_seed"],
            "appearance": parse_appearance(row["appearance"]),
            "model_ref": (
                json.loads(row["model_ref"]) if row["model_ref"] else None
            ),
            "token_budget": (
                json.loads(row["token_budget"]) if row["token_budget"] else None
            ),
            "tool_permissions": (
                json.loads(row["tool_permissions"]) if row["tool_permissions"] else None
            ),
            "send_mode": (
                "parallel" if row["send_mode"] == "steer" else (row["send_mode"] or None)
            ),
            "permission_mode": row["permission_mode"] or None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        agent["model_status"] = model_status(self, agent)
        return agent
