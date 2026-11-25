"""One bot's piece of work in one conversation, its steps, and the turns it caused.

An activity is one agent's turn inside a run: from the moment it starts answering until it
writes its final answer, stops or fails. Its steps are the messages that turn wrote — every
tool call (an ``action`` row) and every narration line that led into one — so nothing here
counts what the database already knows: a step list and its count are derived at read time
and can never drift from the transcript.

In a group the chat shows one card per running activity and only the final answer; the steps
live in the activity's own detail page, which is what :meth:`Activities.activity_detail`
assembles: this activity's section first, then one section per child activity (a bot it
consulted, a task thread its ``start_task`` opened), in start order.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

from .db import _UNSET, new_id, now_iso

RUNNING = "running"
# The turn is parked on something only the user can answer: a consent card or a question.
AWAITING = "awaiting"
COMPLETED = "completed"
STOPPED = "stopped"
ERROR = "error"
LIMITED = "limited"
UNFINISHED = (RUNNING, AWAITING)

# A title is a short verb phrase, the way a to-do list names one: 16 汉字 fit one card row.
TITLE_CHARS = 16
# One line under the title, so the list can be skimmed without opening anything.
SUMMARY_CHARS = 40
# The card's quiet line: the newest prose of the running turn.
LATEST_CHARS = 120
# A step's subtitle is an excerpt, not the tool's whole answer.
EXCERPT_CHARS = 80

# The tools whose action is a hand-off to another turn/run, drawn as that section's entry.
HANDOFF_TOOLS = ("direct_message", "ask_agent", "start_task", "group_assign")
ASK_TOOL = "ask_user"
NOTE_KINDS = ("agent",)


def _stamp() -> str:
    """When one activity began, to the microsecond.

    The work log pages on ``started_at`` alone, and two turns of the same bot can begin
    inside the same second (parallel opinions, a quick follow-up): a whole-second stamp
    would put them out of order and drop one at a page boundary.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def clip(text: str, limit: int) -> str:
    """One flat line, cut to ``limit`` characters with a trailing ellipsis when it is cut."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"


def activity_title(text: str) -> str:
    """The opening of a request, squeezed to a card title (never empty for real text)."""
    return clip(text, TITLE_CHARS)


def first_sentence(text: str, limit: int = SUMMARY_CHARS) -> str:
    """The first sentence of a reply, cut to ``limit`` characters."""
    flat = " ".join(str(text).split())
    for mark in ("。", "！", "？", "!", "?", "\n"):
        head, found, _tail = flat.partition(mark)
        if found and head.strip():
            return clip(head, limit)
    return clip(flat, limit)


def _excerpt(raw: Any) -> str:
    """A tool's own words for its step: an error, or a short line out of its JSON result."""
    if not raw:
        return ""
    text = str(raw).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return clip(text, EXCERPT_CHARS)
    if isinstance(value, dict):
        for key in ("message", "content", "summary", "text", "error", "reason"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return clip(found, EXCERPT_CHARS)
        if value.get("denied") is True:
            return "你拒绝了这一步"
        return ""
    if isinstance(value, list):
        return ""
    return clip(text, EXCERPT_CHARS)


def _row(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


class Activities:
    """Activities, their live fields, and the sections one detail page shows."""

    # ------------------------------------------------------------------ writing

    def create_activity(
        self,
        *,
        conversation_id: str,
        agent_id: str,
        agent_name: str,
        request: str = "",
        run_id: str | None = None,
        task_id: str | None = None,
        parent_id: str | None = None,
        request_message_id: str | None = None,
    ) -> dict:
        """Open one turn; its title starts as the request's own opening words."""
        activity_id = new_id("ac")
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO activities
                    (id, conversation_id, agent_id, agent_name, run_id, task_id, parent_id,
                     title, summary, status, current_step, latest_text,
                     request_message_id, answer_message_id, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', '', ?, NULL, ?)
                """,
                (
                    activity_id,
                    conversation_id,
                    agent_id,
                    agent_name,
                    run_id,
                    task_id,
                    parent_id,
                    activity_title(request) or "新的一轮",
                    RUNNING,
                    request_message_id,
                    _stamp(),
                ),
            )
        activity = self.get_activity(activity_id)
        assert activity is not None
        return activity

    def get_activity(self, activity_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        return None if row is None else _row(row)

    def update_activity(
        self,
        activity_id: str,
        *,
        status: str | object = _UNSET,
        current_step: str | object = _UNSET,
        latest_text: str | object = _UNSET,
        title: str | object = _UNSET,
        summary: str | object = _UNSET,
        answer_message_id: str | None | object = _UNSET,
        finished_at: str | None | object = _UNSET,
        parent_id: str | None | object = _UNSET,
        task_id: str | None | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("current_step", current_step),
            ("latest_text", _clip_latest(latest_text)),
            ("title", title),
            ("summary", summary),
            ("answer_message_id", answer_message_id),
            ("finished_at", finished_at),
            ("parent_id", parent_id),
            ("task_id", task_id),
        ):
            if value is _UNSET:
                continue
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return self.get_activity(activity_id)
        params.append(activity_id)
        conn = self._ensure()
        with self._lock:
            cursor = conn.execute(
                f"UPDATE activities SET {', '.join(sets)} WHERE id = ?", params
            )
            if cursor.rowcount == 0:
                return None
        return self.get_activity(activity_id)

    def finish_activity(
        self,
        activity_id: str,
        *,
        status: str,
        summary: str = "",
        answer_message_id: str | None = None,
    ) -> dict | None:
        """One turn ended: it keeps its summary and answer, and no longer names a live step."""
        with self._transaction() as tx:
            cursor = tx.execute(
                """
                UPDATE activities
                   SET status = ?, summary = ?, answer_message_id = ?, current_step = '',
                       finished_at = ?
                 WHERE id = ?
                """,
                (
                    status,
                    clip(summary, SUMMARY_CHARS),
                    answer_message_id,
                    now_iso(),
                    activity_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_activity(activity_id)

    # ------------------------------------------------------------------ reading

    def activity_of_message(self, message_id: str) -> dict | None:
        """The activity whose turn wrote one message, if any."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT a.* FROM activities a JOIN messages m ON m.activity_id = a.id
                 WHERE m.id = ? LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        return None if row is None else _row(row)

    def activities_of_run(self, run_id: str, *, unfinished: bool = False) -> list[dict]:
        """Every activity of one run, oldest first (only the still-open ones when asked)."""
        sql = "SELECT * FROM activities WHERE run_id = ?"
        params: list[Any] = [run_id]
        if unfinished:
            sql += " AND status IN (?, ?)"
            params.extend(UNFINISHED)
        sql += " ORDER BY started_at, rowid"
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(sql, params).fetchall()
        return [_row(row) for row in rows]

    def activity_children(self, activity_id: str) -> list[dict]:
        """The activities this one caused, in the order their turns began."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM activities WHERE parent_id = ? ORDER BY started_at, rowid",
                (activity_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def list_activities(
        self, agent_id: str, *, limit: int = 50, before: str | None = None
    ) -> tuple[list[dict], bool]:
        """One bot's activities across every conversation, newest first."""
        sql = "SELECT * FROM activities WHERE agent_id = ?"
        params: list[Any] = [agent_id]
        if before:
            sql += " AND started_at < ?"
            params.append(str(before))
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(limit + 1)
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(sql, params).fetchall()
        kept = rows[:limit]
        return [_row(row) for row in kept], len(rows) > limit

    def activity_answer(self, activity_id: str) -> dict | None:
        """The turn's final answer bubble, if it wrote one (a stop or a narration is not one)."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT * FROM messages
                 WHERE activity_id = ? AND kind = 'agent' AND status = 'complete'
                   AND COALESCE(leads_to_tools, 0) = 0 AND TRIM(content) != ''
                 ORDER BY rowid DESC LIMIT 1
                """,
                (activity_id,),
            ).fetchone()
        return None if row is None else self._message_row(row)

    def activity_steps(self, activity_id: str) -> int:
        """Tool calls plus the narration lines leading into them, counted from the rows."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total FROM messages
                 WHERE activity_id = ?
                   AND (kind = 'action' OR (kind = 'agent' AND leads_to_tools = 1))
                """,
                (activity_id,),
            ).fetchone()
        return int(row["total"])

    def activity_awaiting(self, activity_id: str) -> bool:
        """Whether one of this turn's cards is still waiting for the user."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT 1 FROM actions a JOIN messages m ON m.id = a.message_id
                 WHERE m.activity_id = ? AND a.status = 'awaiting' LIMIT 1
                """,
                (activity_id,),
            ).fetchone()
        return row is not None

    def activity_tool(self, activity_id: str) -> str:
        """The tool of the turn's newest call, so a card draws the icon its transcript row does."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT a.tool_name FROM messages m JOIN actions a ON a.id = m.action_id
                 WHERE m.activity_id = ? AND m.kind = 'action'
                 ORDER BY m.seq DESC LIMIT 1
                """,
                (activity_id,),
            ).fetchone()
        return "" if row is None else str(row["tool_name"] or "")

    # ------------------------------------------------------------------ board

    def replace_activity_todos(self, activity_id: str, items: Sequence[dict]) -> list[dict]:
        """Replace an activity's whole to-do list, in the order given."""
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute("DELETE FROM activity_todos WHERE activity_id = ?", (activity_id,))
            for index, item in enumerate(items):
                tx.execute(
                    "INSERT INTO activity_todos (activity_id, idx, text, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        activity_id,
                        index,
                        str(item["text"]),
                        str(item["status"]),
                        timestamp,
                    ),
                )
        return self.list_activity_todos(activity_id)

    def list_activity_todos(self, activity_id: str) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT text, status FROM activity_todos WHERE activity_id = ? ORDER BY idx",
                (activity_id,),
            ).fetchall()
        return [{"text": row["text"], "status": row["status"]} for row in rows]

    def add_activity_note(
        self,
        activity_id: str,
        *,
        kind: str,
        text: str,
        source_message_id: str | None = None,
    ) -> dict:
        note_id = new_id("an")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                "INSERT INTO activity_notes "
                "(id, activity_id, kind, text, source_message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (note_id, activity_id, kind, text, source_message_id, timestamp),
            )
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM activity_notes WHERE id = ?", (note_id,)
            ).fetchone()
        return _row(row) if row is not None else {}

    def list_activity_notes(self, activity_id: str) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT kind, text, source_message_id, created_at FROM activity_notes "
                "WHERE activity_id = ? ORDER BY created_at, rowid",
                (activity_id,),
            ).fetchall()
        return [
            {
                "kind": row["kind"],
                "text": row["text"],
                "source_message_id": row["source_message_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def live_activities(self, conversation_id: str) -> list[dict]:
        """Every turn still open in one conversation, oldest first (work and reply lanes)."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM activities WHERE conversation_id = ? "
                "AND status IN ('running', 'awaiting') ORDER BY started_at",
                (conversation_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def last_work_activity(self, conversation_id: str, agent_id: str) -> dict | None:
        """This bot's newest work-lane turn here, finished or not: what it last worked on."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT a.* FROM activities a
                  LEFT JOIN runs r ON r.id = a.run_id
                 WHERE a.conversation_id = ? AND a.agent_id = ?
                   AND COALESCE(r.lane, 'work') != 'reply'
                 ORDER BY a.started_at DESC, a.rowid DESC LIMIT 1
                """,
                (conversation_id, agent_id),
            ).fetchone()
        return None if row is None else _row(row)

    def live_work_activities(self, conversation_id: str) -> list[dict]:
        """The open turns actually doing the job: ``reply`` lanes are not a board of work."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT a.* FROM activities a
                  LEFT JOIN runs r ON r.id = a.run_id
                 WHERE a.conversation_id = ?
                   AND a.status IN ('running', 'awaiting')
                   AND COALESCE(r.lane, 'work') != 'reply'
                 ORDER BY a.started_at
                """,
                (conversation_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def activity_recent_steps(self, activity_id: str, limit: int = 3) -> list[str]:
        """The summaries of an activity's newest tool calls, newest last."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT a.summary FROM actions a
                  JOIN messages m ON m.action_id = a.id
                 WHERE m.activity_id = ?
                 ORDER BY a.rowid DESC LIMIT ?
                """,
                (activity_id, max(1, int(limit))),
            ).fetchall()
        return [str(row["summary"] or "").strip() for row in reversed(rows) if row["summary"]]

    def activity_view(self, activity: dict) -> dict:
        """One activity as the wire wants it: the row plus its derived step count and tool."""
        view = dict(activity)
        activity_id = str(activity["id"])
        view["steps"] = self.activity_steps(activity_id)
        view["current_tool"] = self.activity_tool(activity_id)
        view["todos"] = self.list_activity_todos(activity_id)
        view["todo_total"] = len(view["todos"])
        view["todo_done"] = sum(1 for item in view["todos"] if item["status"] == "done")
        run = self.get_run(str(activity["run_id"])) if activity.get("run_id") else None
        view["lane"] = str((run or {}).get("lane") or "work")
        return view

    # ------------------------------------------------------------------ detail

    def activity_detail(self, activity_id: str) -> dict | None:
        """One activity with its conversation title, its answer and every section of steps."""
        activity = self.get_activity(activity_id)
        if activity is None:
            return None
        conversation = self.get_conversation(str(activity["conversation_id"]))
        answer = (
            self.get_message(str(activity["answer_message_id"]))
            if activity.get("answer_message_id")
            else None
        )
        children = self.activity_children(activity_id)
        return {
            "activity": self.activity_view(activity),
            "conversation_title": conversation["title"] if conversation is not None else "",
            "answer": answer,
            "notes": self.list_activity_notes(activity_id),
            "sections": [self._section(activity), *(self._section(child) for child in children)],
        }

    def _section(self, activity: dict) -> dict:
        """One section of a detail page: a bot turn's rows and steps, oldest first."""
        activity_id = str(activity["id"])
        children = self.activity_children(activity_id)
        return {
            "id": activity_id,
            "label": activity["agent_name"],
            "agent_id": activity["agent_id"],
            "activity_id": activity_id,
            "messages": self.activity_messages(activity_id),
            "steps": self._steps(activity, children),
        }

    def _steps(self, activity: dict, children: Sequence[dict]) -> list[dict]:
        activity_id = str(activity["id"])
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT m.id, m.kind, m.content, m.created_at, m.leads_to_tools,
                       a.id AS action_id, a.tool_name, a.status AS action_status,
                       a.summary, a.error, a.result, a.target_agent_id
                  FROM messages m LEFT JOIN actions a ON a.id = m.action_id
                 WHERE m.activity_id = ?
                 ORDER BY m.rowid ASC
                """,
                (activity_id,),
            ).fetchall()
        by_agent: dict[str, str] = {}
        by_request: dict[str, str] = {}
        by_own_task: str | None = None
        for child in children:
            if child.get("agent_id") and str(child["agent_id"]) not in by_agent:
                by_agent[str(child["agent_id"])] = str(child["id"])
            if child.get("request_message_id"):
                by_request[str(child["request_message_id"])] = str(child["id"])
            if (
                by_own_task is None
                and child.get("task_id")
                and str(child["agent_id"]) == str(activity["agent_id"])
            ):
                # ``start_task`` names no target agent: the thread it opened is the same
                # bot's own child activity, and that is what the step leads to.
                by_own_task = str(child["id"])
        steps: list[dict] = []
        for row in rows:
            step = self._step(row, by_agent, by_request, by_own_task)
            if step is not None:
                steps.append(step)
        return steps

    @staticmethod
    def _step(
        row: sqlite3.Row,
        by_agent: dict[str, str],
        by_request: dict[str, str],
        by_own_task: str | None,
    ) -> dict | None:
        """Map one message of a turn to the step the detail page draws (or skip it)."""
        message_id = str(row["id"])
        created_at = row["created_at"]
        if row["action_id"]:
            tool = str(row["tool_name"] or "")
            status = str(row["action_status"] or "completed")
            if '"denied": true' in str(row["result"] or "") or str(
                row["summary"] or ""
            ).startswith("你拒绝"):
                # The user said no: the step reads as refused, not as a broken tool.
                status = "denied"
            handoff = tool in HANDOFF_TOOLS
            return {
                "id": f"{row['action_id']}",
                "kind": "ask" if tool == ASK_TOOL else ("handoff" if handoff else "tool"),
                "title": clip(str(row["summary"] or ""), EXCERPT_CHARS) or tool,
                "subtitle": _excerpt(row["error"]) or _excerpt(row["result"]),
                "status": status,
                "message_id": message_id,
                "action_id": str(row["action_id"]),
                "target_activity_id": (
                    (by_agent.get(str(row["target_agent_id"])) if row["target_agent_id"] else None)
                    or by_own_task
                    if handoff
                    else None
                ),
                "created_at": created_at,
            }
        if row["kind"] in NOTE_KINDS:
            if row["leads_to_tools"] == 1:
                return {
                    "id": message_id,
                    "kind": "note",
                    "title": first_sentence(str(row["content"] or "")),
                    "subtitle": "",
                    "status": "completed",
                    "message_id": message_id,
                    "action_id": None,
                    "target_activity_id": None,
                    "created_at": created_at,
                }
            target = by_request.get(message_id)
            if target is not None:
                return {
                    "id": f"{message_id}-handoff",
                    "kind": "handoff",
                    "title": first_sentence(str(row["content"] or "")),
                    "subtitle": "",
                    "status": "completed",
                    "message_id": message_id,
                    "action_id": None,
                    "target_activity_id": target,
                    "created_at": created_at,
                }
        return None


def _clip_latest(value: Any) -> Any:
    """The live text of a card, cut to its quiet-line length (other values pass through)."""
    if isinstance(value, str):
        return clip(value, LATEST_CHARS)
    return value
