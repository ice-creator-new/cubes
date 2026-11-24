"""Workflows and their runs: the graph, the walk through it, and each node's own row.

A definition is a row whose ``steps`` column holds its nodes and whose ``edges`` column holds
the connections (a branch node's edges carry the choice they belong to). A run copies both, so
editing a plan never rewrites a walk already in progress. Node state is keyed by node id: the
panel may reorder or insert nodes, and a walk in progress keeps answering about the same node.
Both survive a restart — the engine re-derives everything it needs from these rows.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from .. import workflows as rules
from .db import _UNSET, new_id, now_iso


class Workflows:
    """The definitions and the runs that walk them."""

    # ------------------------------------------------------------------ definitions

    def list_workflows(self) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM workflows ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [self._workflow_row(row) for row in rows]

    def get_workflow(self, workflow_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        return None if row is None else self._workflow_row(row)

    def workflow_by_name(self, name: str, *, owner_agent_id: str | None = None) -> dict | None:
        """A workflow by name (trimmed, case-insensitive), optionally only one bot's."""
        wanted = " ".join(str(name).split()).casefold()
        for workflow in self.list_workflows():
            if workflow["name"].casefold() != wanted:
                continue
            if owner_agent_id is not None and workflow["owner_agent_id"] != owner_agent_id:
                continue
            return workflow
        return None

    def workflows_of(self, agent_id: str) -> list[dict]:
        return [
            workflow for workflow in self.list_workflows()
            if workflow["owner_agent_id"] == agent_id
        ]

    def count_workflows_of(self, agent_id: str) -> int:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT COUNT(*) FROM workflows WHERE owner_agent_id = ?", (agent_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def create_workflow(
        self,
        *,
        name: str,
        description: str,
        owner_agent_id: str,
        created_by: str,
        nodes: Sequence[dict],
        edges: Sequence[dict] | None = None,
        source: str = "agent",
        enabled: bool = True,
    ) -> dict:
        """Insert one definition; validate the graph before it lands."""
        if edges is None:
            edges = rules.edges_from_chain(nodes)
        workflow_id = new_id("w")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO workflows
                    (id, name, description, owner_agent_id, created_by, steps, edges, version,
                     enabled, source, run_count, last_run_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, NULL, ?, ?)
                """,
                (
                    workflow_id,
                    name,
                    description,
                    owner_agent_id,
                    created_by,
                    json.dumps(list(nodes), ensure_ascii=False),
                    json.dumps(list(edges), ensure_ascii=False),
                    1 if enabled else 0,
                    source,
                    timestamp,
                    timestamp,
                ),
            )
        workflow = self.get_workflow(workflow_id)
        assert workflow is not None
        return workflow

    def update_workflow(
        self,
        workflow_id: str,
        *,
        name: Any = _UNSET,
        description: Any = _UNSET,
        nodes: Any = _UNSET,
        edges: Any = _UNSET,
        enabled: Any = _UNSET,
        owner_agent_id: Any = _UNSET,
    ) -> dict | None:
        """Apply one edit; changing the graph or the name is a new version of the plan."""
        sets: list[str] = []
        values: list[Any] = []
        bump = False
        if name is not _UNSET:
            sets.append("name = ?")
            values.append(str(name))
            bump = True
        if description is not _UNSET:
            sets.append("description = ?")
            values.append(str(description))
        if nodes is not _UNSET:
            sets.append("steps = ?")
            values.append(json.dumps(list(nodes), ensure_ascii=False))
            bump = True
        if edges is not _UNSET:
            sets.append("edges = ?")
            values.append(json.dumps(list(edges), ensure_ascii=False))
            bump = True
        if enabled is not _UNSET:
            sets.append("enabled = ?")
            values.append(1 if enabled else 0)
        if owner_agent_id is not _UNSET:
            sets.append("owner_agent_id = ?")
            values.append(str(owner_agent_id))
            bump = True
        if not sets:
            return self.get_workflow(workflow_id)
        if bump:
            sets.append("version = version + 1")
        sets.append("updated_at = ?")
        values.append(now_iso())
        values.append(workflow_id)
        conn = self._ensure()
        with self._lock:
            conn.execute(f"UPDATE workflows SET {', '.join(sets)} WHERE id = ?", values)
        return self.get_workflow(workflow_id)

    def delete_workflow(self, workflow_id: str) -> bool:
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        return cursor.rowcount > 0

    def bump_workflow_run_count(self, workflow_id: str) -> None:
        conn = self._ensure()
        with self._lock:
            conn.execute(
                "UPDATE workflows SET run_count = run_count + 1, last_run_at = ? WHERE id = ?",
                (now_iso(), workflow_id),
            )

    # ------------------------------------------------------------------ runs

    def begin_workflow_run(
        self,
        workflow: dict,
        *,
        conversation_id: str,
        input: str,
        started_by: str,
    ) -> dict:
        """One walk: a run row, its graph, and one state row per node."""
        workflow_run_id = new_id("wr")
        timestamp = now_iso()
        states = rules.pending_states(workflow["steps"])
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO workflow_runs
                    (id, workflow_id, workflow_version, conversation_id, run_id, status,
                     cursor, input, summary, steps, edges, started_by, created_at, updated_at,
                     finished_at)
                VALUES (?, ?, ?, ?, NULL, ?, 0, ?, '', ?, ?, ?, ?, ?, NULL)
                """,
                (
                    workflow_run_id,
                    workflow["id"],
                    workflow["version"],
                    conversation_id,
                    rules.STATUS_QUEUED,
                    input,
                    json.dumps(list(workflow["steps"]), ensure_ascii=False),
                    json.dumps(list(workflow["edges"]), ensure_ascii=False),
                    started_by,
                    timestamp,
                    timestamp,
                ),
            )
            for state in states:
                tx.execute(
                    """
                    INSERT INTO workflow_steps
                        (workflow_run_id, node_id, step_index, title, kind, status, attempts,
                         approved, output, choice, message_id, started_at, finished_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, '', NULL, NULL, NULL, NULL)
                    """,
                    (
                        workflow_run_id,
                        state["node_id"],
                        state["step_index"],
                        state["title"],
                        state["kind"],
                        state["status"],
                    ),
                )
        run = self.get_workflow_run(workflow_run_id)
        assert run is not None
        return run

    def get_workflow_run(self, workflow_run_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (workflow_run_id,)
            ).fetchone()
        return None if row is None else self._workflow_run_row(row)

    def workflow_run_by_engine_run(self, run_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return None if row is None else self._workflow_run_row(row)

    def workflow_run_by_conversation(self, conversation_id: str) -> dict | None:
        """The newest walk this conversation holds, live or finished.

        A walk gets a page of its own, so that page asks about exactly one walk; a finished
        one still belongs there: its flow is what the page is for.
        """
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE conversation_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._workflow_run_row(row)

    def live_workflow_run_by_conversation(self, conversation_id: str) -> dict | None:
        """The walk this chat is serving, if one is still owed an action.

        A walk lives in its owner's direct chat, next to everything else said there, so a
        finished one is history and only a live one belongs to the chat right now.
        """
        marks = ",".join("?" for _ in rules.LIVE_STATUSES)
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                f"SELECT * FROM workflow_runs WHERE conversation_id = ? "
                f"AND status IN ({marks}) ORDER BY rowid DESC LIMIT 1",
                (conversation_id, *rules.LIVE_STATUSES),
            ).fetchone()
        return None if row is None else self._workflow_run_row(row)

    def queued_workflow_run(self, conversation_id: str) -> dict | None:
        """The oldest walk waiting for this chat's run slot to free up."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE conversation_id = ? AND status = ? "
                "AND run_id IS NULL ORDER BY rowid ASC LIMIT 1",
                (conversation_id, rules.STATUS_QUEUED),
            ).fetchone()
        return None if row is None else self._workflow_run_row(row)

    def live_workflow_runs(self) -> list[dict]:
        """Every walk still owed an action: what a restart must not silently lose."""
        marks = ",".join("?" for _ in rules.LIVE_STATUSES)
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                f"SELECT * FROM workflow_runs WHERE status IN ({marks}) ORDER BY rowid ASC",
                rules.LIVE_STATUSES,
            ).fetchall()
        return [self._workflow_run_row(row) for row in rows]

    def list_workflow_runs(self, workflow_id: str, *, limit: int = 20) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? "
                "ORDER BY rowid DESC LIMIT ?",
                (workflow_id, limit),
            ).fetchall()
        return [self._workflow_run_row(row) for row in rows]

    def update_workflow_run(self, workflow_run_id: str, **changes: Any) -> dict | None:
        """Set any of ``status``, ``cursor``, ``run_id``, ``summary``, ``finished_at``."""
        allowed = {"status", "cursor", "run_id", "summary", "finished_at"}
        sets: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unknown workflow run column: {key}")
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return self.get_workflow_run(workflow_run_id)
        sets.append("updated_at = ?")
        values.append(now_iso())
        values.append(workflow_run_id)
        conn = self._ensure()
        with self._lock:
            conn.execute(f"UPDATE workflow_runs SET {', '.join(sets)} WHERE id = ?", values)
        return self.get_workflow_run(workflow_run_id)

    # ------------------------------------------------------------------ nodes

    def workflow_step_states(self, workflow_run_id: str) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_run_id = ? ORDER BY step_index ASC",
                (workflow_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_workflow_step(
        self, workflow_run_id: str, node_id: str, **changes: Any
    ) -> dict | None:
        """Set any of a node's own columns; ``node_id`` is the stable key."""
        allowed = {
            "status",
            "attempts",
            "approved",
            "output",
            "choice",
            "message_id",
            "started_at",
            "finished_at",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unknown workflow step column: {key}")
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return None
        values.extend([workflow_run_id, node_id])
        conn = self._ensure()
        with self._lock:
            conn.execute(
                f"UPDATE workflow_steps SET {', '.join(sets)} "
                "WHERE workflow_run_id = ? AND node_id = ?",
                values,
            )
            row = conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_run_id = ? AND node_id = ?",
                (workflow_run_id, node_id),
            ).fetchone()
        return None if row is None else dict(row)

    def step_state(self, workflow_run_id: str, node_id: str) -> dict | None:
        for state in self.workflow_step_states(workflow_run_id):
            if state["node_id"] == node_id:
                return state
        return None

    def take_workflow_loop(
        self, workflow_run_id: str, loop_key: str, node_ids: Sequence[str]
    ) -> dict | None:
        """Send a walk round a loop once: count the round and put its body back to pending.

        One transaction, so a crash never leaves a round half rewound. Each rewound node keeps
        what it produced in its ``history`` (the chat still shows those messages, labelled with
        their round), and its ``rounds`` goes up by one. ``attempts`` and ``approved`` reset: a
        new round of a ``confirm`` node is asked for again, and a retry budget is per round.
        """
        wanted = set(node_ids)
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT loops FROM workflow_runs WHERE id = ?", (workflow_run_id,)
            ).fetchone()
            if row is None:
                return None
            loops = _json_dict(row["loops"])
            loops[loop_key] = int(loops.get(loop_key, 0)) + 1
            tx.execute(
                "UPDATE workflow_runs SET loops = ?, updated_at = ? WHERE id = ?",
                (json.dumps(loops, ensure_ascii=False), now_iso(), workflow_run_id),
            )
            for state in tx.execute(
                "SELECT * FROM workflow_steps WHERE workflow_run_id = ?", (workflow_run_id,)
            ).fetchall():
                if state["node_id"] not in wanted:
                    continue
                history = _json_list(state["history"])
                # A node still pending this round did not run: it keeps its round number and
                # leaves no history, so ``rounds`` always equals the rounds it really took.
                if state["status"] == rules.STEP_PENDING:
                    continue
                history.append(
                    {
                        "round": int(state["rounds"]),
                        "status": state["status"],
                        "output": state["output"],
                        "choice": state["choice"],
                        "message_id": state["message_id"],
                        "started_at": state["started_at"],
                        "finished_at": state["finished_at"],
                    }
                )
                tx.execute(
                    """
                    UPDATE workflow_steps
                    SET status = ?, attempts = 0, approved = 0, output = '', choice = NULL,
                        message_id = NULL, started_at = NULL, finished_at = NULL,
                        rounds = rounds + 1, history = ?
                    WHERE workflow_run_id = ? AND node_id = ?
                    """,
                    (
                        rules.STEP_PENDING,
                        json.dumps(history[-rules.MAX_LOOPS :], ensure_ascii=False),
                        workflow_run_id,
                        state["node_id"],
                    ),
                )
        return self.get_workflow_run(workflow_run_id)

    def park_workflow_run(self, workflow_run_id: str) -> dict | None:
        """Put one walk down as paused so it continues from the same step later.

        A node that was running goes back to ``pending``: its work, and its tool calls, did not
        survive whatever stopped the run. A node parked on a question stays as it is.
        """
        conn = self._ensure()
        with self._lock:
            conn.execute(
                "UPDATE workflow_steps SET status = ? WHERE workflow_run_id = ? AND status = ?",
                (rules.STEP_PENDING, workflow_run_id, rules.STEP_RUNNING),
            )
            conn.execute(
                "UPDATE workflow_runs SET status = ?, run_id = NULL, updated_at = ? WHERE id = ?",
                (rules.STATUS_PAUSED, now_iso(), workflow_run_id),
            )
        return self.get_workflow_run(workflow_run_id)

    def pause_live_workflow_runs(self) -> int:
        """A restart: a walk that was mid-node becomes paused, and continues from there.

        A walk parked on a question is left alone: nothing was in flight, and its question is
        still answerable exactly as before the restart.
        """
        marks = ",".join("?" for _ in rules.MID_STATUSES)
        conn = self._ensure()
        with self._lock:
            live = [
                str(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM workflow_runs WHERE status IN ({marks})", rules.MID_STATUSES
                ).fetchall()
            ]
            for workflow_run_id in live:
                self.park_workflow_run(workflow_run_id)
        return len(live)

    # ------------------------------------------------------------------ shaping

    @staticmethod
    def _workflow_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "owner_agent_id": row["owner_agent_id"],
            "created_by": row["created_by"],
            "steps": _json_list(row["steps"]),
            "edges": _json_list(row["edges"]),
            "version": int(row["version"]),
            "enabled": bool(row["enabled"]),
            "source": row["source"],
            "run_count": int(row["run_count"]),
            "last_run_at": row["last_run_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _workflow_run_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "workflow_version": int(row["workflow_version"]),
            "conversation_id": row["conversation_id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "cursor": int(row["cursor"]),
            "input": row["input"],
            "summary": row["summary"],
            "steps": _json_list(row["steps"]),
            "edges": _json_list(row["edges"]),
            "started_by": row["started_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "loops": _json_dict(row["loops"]),
        }


def _json_list(raw: Any) -> list[dict]:
    """A stored JSON list of objects; anything else reads as empty rather than raising."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _json_dict(raw: Any) -> dict[str, int]:
    """A stored ``{edge_key: count}``; anything malformed reads as no rounds taken."""
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): int(count) for key, count in value.items() if isinstance(count, int)}
