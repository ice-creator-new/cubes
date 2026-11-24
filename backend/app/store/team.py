"""Team settings, the digest schedule, the run trace and the per-member profile.

Team settings live in ``meta`` like the memory switches, so a change applies from the next
read without a restart. The digest backing is a hidden ``system`` schedule; this module owns
that row's lifecycle. It also answers the run trace and the member profile by reading the
messages, actions, runs, tasks, memories and the per-call usage ledger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import schedules, usage as usage_rules
from ..constants import USER_AUTHOR_NAME
from .. import step_budget as step_budget_rules
from .db import _UNSET, _json_object, now_iso

TEAM_SETTINGS_KEY = "team_settings"
PERMISSION_SETTINGS_KEY = "permission_settings"

SEND_MODES = ("steer", "queue", "interrupt")

DEFAULT_TEAM_SETTINGS: dict[str, dict[str, Any]] = {
    # How a send meets a reply already running here, for every bot without its own override.
    "send_mode": "steer",
    # How many model and tool calls one turn and one run may spend; see ``app.step_budget``.
    "step_budget": step_budget_rules.DEFAULT_PRESET,
    # A group's supervisor coordinates by default; this lets it do hands-on work there too.
    "supervisor_works_in_groups": False,
    # How many hidden task threads may execute at once; the rest wait as ``queued``.
    "max_parallel_tasks": 3,
    # The model a group's invisible dispatcher calls: ``{provider_id, model}``, or NULL for
    # automatic (the backup model when one is configured, else the workspace default).
    "dispatch_model": None,
    "review": {"enabled": False, "max_rounds": 2},
    "digest": {
        "enabled": False,
        "frequency": "daily",
        "time": "09:00",
        "weekday": 0,
        "schedule_id": None,
    },
}

DIGEST_TITLES = {"daily": "团队晨报", "weekly": "团队周报"}
DIGEST_PERIOD_WORDS = {"daily": "本日", "weekly": "本周"}


def digest_prompt(frequency: str) -> str:
    """The one instruction the digest schedule hands its owner."""
    word = DIGEST_PERIOD_WORDS.get(frequency, "本日")
    period = "day" if frequency == "daily" else "week"
    return (
        f"生成团队摘要：先调用 team_report（period=\"{period}\"）获取{word}数据，"
        "然后写一段简短的中文摘要，分成四部分：做了什么、谁花了多少、卡住了什么、建议下一步。"
        "只使用 team_report 这一个工具，不要调用其他工具。"
    )


def _digest_cron(digest: dict[str, Any]) -> str:
    """The 5-field cron for one digest rule, from its ``HH:MM`` time and weekday."""
    hour, minute = digest["time"].split(":")
    if digest.get("frequency") == "weekly":
        weekday = int(digest.get("weekday", 0))
        # The workspace counts 0=Monday..6=Sunday; cron counts 0=Sunday.
        dow = 0 if weekday == 6 else weekday + 1
        return f"{int(minute)} {int(hour)} * * {dow}"
    return f"{int(minute)} {int(hour)} * * *"


def _goal_abstract(text: str, limit: int = 80) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


class Team:
    """Team settings, digest, run trace and the member profile."""

    # ------------------------------------------------------------------ settings

    def team_settings(self) -> dict[str, Any]:
        """The workspace's team switches, with the digest's derived ``schedule_id``."""
        stored = self._stored_team_settings()
        digest = dict(stored["digest"])
        schedule = self.digest_schedule()
        digest["schedule_id"] = schedule["id"] if schedule is not None else None
        return {
            "send_mode": stored["send_mode"],
            "step_budget": stored["step_budget"],
            "supervisor_works_in_groups": stored["supervisor_works_in_groups"],
            "max_parallel_tasks": stored["max_parallel_tasks"],
            "dispatch_model": dict(stored["dispatch_model"]) if stored["dispatch_model"] else None,
            "review": dict(stored["review"]),
            "digest": digest,
        }

    def step_budget(self) -> str:
        """The workspace's step preset alone: a per-call budget check must stay one small read."""
        return str(self._stored_team_settings()["step_budget"])

    def _stored_team_settings(self) -> dict[str, Any]:
        conn = self._ensure()
        with self._lock:
            raw = self._meta(conn, TEAM_SETTINGS_KEY)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
        review = {**DEFAULT_TEAM_SETTINGS["review"], **(data.get("review") or {})}
        digest = {**DEFAULT_TEAM_SETTINGS["digest"], **(data.get("digest") or {})}
        # ``schedule_id`` is derived, never persisted.
        digest.pop("schedule_id", None)
        send_mode = data.get("send_mode")
        if send_mode not in SEND_MODES:
            send_mode = DEFAULT_TEAM_SETTINGS["send_mode"]
        step_budget = data.get("step_budget")
        if step_budget not in step_budget_rules.PRESETS:
            step_budget = DEFAULT_TEAM_SETTINGS["step_budget"]
        works_in_groups = data.get("supervisor_works_in_groups")
        if not isinstance(works_in_groups, bool):
            works_in_groups = DEFAULT_TEAM_SETTINGS["supervisor_works_in_groups"]
        max_parallel = data.get("max_parallel_tasks")
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or not 1 <= max_parallel <= 8:
            max_parallel = DEFAULT_TEAM_SETTINGS["max_parallel_tasks"]
        dispatch = data.get("dispatch_model")
        if not (
            isinstance(dispatch, dict)
            and str(dispatch.get("provider_id") or "").strip()
            and str(dispatch.get("model") or "").strip()
        ):
            dispatch = None
        return {
            "send_mode": send_mode,
            "step_budget": step_budget,
            "supervisor_works_in_groups": works_in_groups,
            "max_parallel_tasks": max_parallel,
            "dispatch_model": dispatch,
            "review": review,
            "digest": digest,
        }

    def set_team_settings(
        self,
        *,
        send_mode: str | None = None,
        step_budget: str | None = None,
        supervisor_works_in_groups: bool | None = None,
        max_parallel_tasks: int | None = None,
        dispatch_model: dict[str, str] | None | object = _UNSET,
        review: dict[str, Any] | None = None,
        digest: dict[str, Any] | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Merge partial settings, keep the digest schedule in step, and return the state."""
        current = self._stored_team_settings()
        if send_mode is not None:
            current["send_mode"] = send_mode
        if step_budget is not None:
            current["step_budget"] = step_budget
        if supervisor_works_in_groups is not None:
            current["supervisor_works_in_groups"] = supervisor_works_in_groups
        if max_parallel_tasks is not None:
            current["max_parallel_tasks"] = max(1, min(8, int(max_parallel_tasks)))
        if dispatch_model is not _UNSET:
            # None clears the pick: the dispatcher goes back to automatic.
            current["dispatch_model"] = (
                {
                    "provider_id": str((dispatch_model or {}).get("provider_id") or "").strip(),
                    "model": str((dispatch_model or {}).get("model") or "").strip(),
                }
                if isinstance(dispatch_model, dict)
                else None
            )
        if review:
            current["review"].update(review)
        if digest:
            current["digest"].update(digest)
        if digest is not None:
            self._sync_digest_schedule(current["digest"], timezone)
        with self._transaction() as tx:
            self._meta_set(tx, TEAM_SETTINGS_KEY, json.dumps(current, ensure_ascii=False))
        return self.team_settings()

    # ------------------------------------------------------------------ permissions

    def permission_settings(self) -> dict[str, Any]:
        """The workspace's approval mode and the folders the bots may use besides the working one."""
        from ..permissions import DEFAULT_MODE, MODES

        conn = self._ensure()
        with self._lock:
            raw = self._meta(conn, PERMISSION_SETTINGS_KEY)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
        mode = data.get("mode") if data.get("mode") in MODES else DEFAULT_MODE
        folders = [str(item) for item in data.get("folders") or [] if isinstance(item, str)]
        return {"mode": mode, "folders": folders}

    def set_permission_settings(
        self, *, mode: str | None = None, folders: list[str] | None = None
    ) -> dict[str, Any]:
        current = self.permission_settings()
        if mode is not None:
            current["mode"] = mode
        if folders is not None:
            current["folders"] = list(folders)
        with self._transaction() as tx:
            self._meta_set(tx, PERMISSION_SETTINGS_KEY, json.dumps(current, ensure_ascii=False))
        return self.permission_settings()

    def _sync_digest_schedule(self, digest: dict[str, Any], timezone_name: str | None) -> None:
        """Create, update or disable the hidden system schedule backing the digest."""
        schedule = self.digest_schedule()
        zone = timezone_name or "Asia/Shanghai"
        if not digest["enabled"]:
            if schedule is not None and schedule["enabled"]:
                self.update_schedule(schedule["id"], enabled=False)
            return
        title = DIGEST_TITLES.get(digest["frequency"], DIGEST_TITLES["daily"])
        cron = _digest_cron(digest)
        try:
            rule = schedules.normalize_rule(
                kind="cron",
                at=None,
                every_minutes=None,
                cron=cron,
                timezone_name=zone,
                now=datetime.now(timezone.utc),
            )
        except schedules.ScheduleError:
            # An invalid stored rule is the route's job to reject; keep the current row.
            if schedule is None:
                return
            rule = None
        supervisor = self.supervisor()
        direct = self.direct_conversation()
        prompt = digest_prompt(digest["frequency"])
        if schedule is None:
            if rule is None or direct is None:  # pragma: no cover - bootstrap makes both
                return
            self.create_schedule(
                title=title,
                prompt=prompt,
                conversation_id=direct["id"],
                conversation_title=direct["title"],
                kind="cron",
                at=None,
                every_minutes=None,
                cron=cron,
                timezone_name=zone,
                use_computer=False,
                next_run_at=rule["next_run_at"],
                created_by=USER_AUTHOR_NAME,
                owner_agent_id=supervisor["id"] if supervisor is not None else None,
                enabled=True,
                system=True,
            )
            return
        updates: dict[str, Any] = {
            "title": title,
            "prompt": prompt,
            "cron": cron,
            "timezone": zone,
            "enabled": True,
        }
        if rule is not None:
            updates["next_run_at"] = rule["next_run_at"]
        self.update_schedule(schedule["id"], **updates)

    def sync_digest_schedule(self, *, timezone: str | None = None) -> None:
        """Reconcile the digest schedule row with the stored digest settings (startup)."""
        self._sync_digest_schedule(self._stored_team_settings()["digest"], timezone)

    # ------------------------------------------------------------- participants

    def record_run_participant(self, run_id: str, agent_id: str) -> None:
        """Remember that ``agent_id`` replied in ``run_id``, once per run/agent."""
        conn = self._ensure()
        with self._lock:
            conn.execute(
                "INSERT OR IGNORE INTO run_participants (run_id, agent_id, first_at) "
                "VALUES (?, ?, ?)",
                (run_id, agent_id, now_iso()),
            )

    # --------------------------------------------------------------- run trace

    def run_trace(self, run_id: str) -> dict[str, Any] | None:
        """The graph and message list of one run, or ``None`` when unknown."""
        run = self.get_run(run_id)
        if run is None:
            return None
        messages = self._run_messages(run)
        actions = self._run_trace_actions(run_id)
        nodes: dict[str, dict[str, Any]] = {"user": {"id": "user", "name": "你", "kind": "user"}}
        order: list[str] = ["user"]

        def ensure_node(agent_id: str) -> None:
            if agent_id in nodes:
                return
            agent = self.get_agent(agent_id)
            nodes[agent_id] = {
                "id": agent_id,
                "name": agent["name"] if agent is not None else agent_id,
                "kind": agent["kind"] if agent is not None else "member",
            }
            order.append(agent_id)

        edges: list[dict[str, Any]] = []
        reply_authors = {
            (m["reply_to_id"], m["author_id"])
            for m in messages
            if m.get("kind") == "agent" and m.get("reply_to_id") and m.get("author_id")
        }
        trigger = next((m for m in messages if m.get("kind") == "user"), None)
        # Who the user handed the run to: the agent the trigger named, else whoever wrote first in
        # the run's own conversation — a reply or a tool row. A consulted member's answer can land
        # there before the responder's own reply, so replies alone would pick the wrong bot.
        mentions = list(trigger.get("mentions") or []) if trigger else []
        named = next((item for item in mentions if item != "user"), None)
        first_author = named or next(
            (
                m["author_id"]
                for m in messages
                if m.get("kind") in ("agent", "action")
                and m.get("author_id")
                and m.get("conversation_id") == run["conversation_id"]
            ),
            None,
        )
        if trigger is not None and first_author is not None:
            # Everyone who answered the user's message itself was handed it: the bot it went to
            # first, then any member @所有人 let speak (and the supervisor's closing).
            direct = [first_author] + [
                str(m["author_id"])
                for m in messages
                if m.get("kind") == "agent"
                and m.get("reply_to_id") == trigger["id"]
                and m.get("author_id")
                and m.get("conversation_id") == run["conversation_id"]
            ]
            for author_id in dict.fromkeys(direct):
                ensure_node(author_id)
                edges.append(
                    {
                        "from": "user",
                        "to": author_id,
                        "kind": "ask",
                        "at": trigger["created_at"],
                        "conversation_id": run["conversation_id"],
                        "message_id": trigger["id"],
                        "label": "发起任务",
                        "brief": None,
                    }
                )
        for message in messages:
            if message.get("kind") != "agent" or not message.get("author_id"):
                continue
            for target in message.get("mentions") or []:
                if target in ("user", message["author_id"]):
                    continue
                if (message["id"], target) not in reply_authors:
                    continue
                ensure_node(message["author_id"])
                ensure_node(target)
                edges.append(
                    {
                        "from": message["author_id"],
                        "to": target,
                        "kind": "mention",
                        "at": message["created_at"],
                        "conversation_id": message["conversation_id"],
                        "message_id": message["id"],
                        "label": "转交",
                        "brief": None,
                    }
                )
        for action in actions:
            author = action.get("author_id")
            target = action.get("target_agent_id")
            if not author or not target:
                continue
            arguments = _json_object(action.get("arguments"))
            kind = None
            label = ""
            # What was handed over, in full: the task a consult gave, the private message sent,
            # the review's feedback. The label stays a short pill on the graph.
            brief = ""
            if action["tool_name"] == "ask_agent":
                kind, label = "consult", "咨询"
                brief = str(arguments.get("task") or "")
            elif action["tool_name"] == "direct_message":
                kind, label = "dm", "私信"
                brief = str(arguments.get("content") or "")
            elif action["tool_name"] == "review":
                result = _json_object(action.get("result"))
                if result.get("kind") != "revise":
                    continue
                brief = str(result.get("feedback") or "").strip()
                feedback = " ".join(brief.split())
                kind, label = "revise", feedback[:24] if feedback else "打回"
            if kind is None:
                continue
            ensure_node(author)
            ensure_node(target)
            edges.append(
                {
                    "from": author,
                    "to": target,
                    "kind": kind,
                    "at": action["created_at"],
                    "conversation_id": action["conversation_id"],
                    "message_id": action["message_id"],
                    "label": label,
                    "brief": brief.strip() or None,
                }
            )
        for message in messages:
            if message.get("author_id"):
                ensure_node(message["author_id"])
        edges.sort(key=lambda edge: (edge["at"], edge["kind"]))
        return {
            "run": run,
            "conversation_id": run["conversation_id"],
            "nodes": [nodes[agent_id] for agent_id in order],
            "edges": edges,
            "messages": messages,
        }

    def _run_messages(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM messages WHERE run_id = ? ORDER BY created_at ASC, rowid ASC",
                (run["id"],),
            ).fetchall()
        messages = [self._message_row(row) for row in rows]
        if messages:
            return messages
        # Runs from before the migration have no ``run_id`` on messages: fall back to the
        # run's conversation within its own time window.
        start = run["created_at"]
        end = run["finished_at"] or now_iso()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND created_at >= ? "
                "AND created_at <= ? ORDER BY created_at ASC, rowid ASC",
                (run["conversation_id"], start, end),
            ).fetchall()
        return [self._message_row(row) for row in rows]

    def _run_trace_actions(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT a.tool_name, a.target_agent_id, a.message_id, a.conversation_id, "
                "a.created_at, a.arguments, a.result, m.author_id AS author_id "
                "FROM actions a LEFT JOIN messages m ON m.id = a.message_id "
                "WHERE a.run_id = ? AND a.status = 'completed' "
                "ORDER BY a.created_at ASC, a.rowid ASC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- profile

    def agent_profile(
        self, agent_id: str, *, period: str, now: datetime, zone: Any
    ) -> dict[str, Any] | None:
        """One member's stats: replies/runs/tasks all-time, usage for ``period``."""
        agent = self.get_agent(agent_id)
        if agent is None:
            return None
        since = usage_rules.period_start(period, now, zone)
        conn = self._ensure()
        with self._lock:
            replies = int(
                conn.execute(
                    "SELECT COUNT(*) FROM messages m "
                    "JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE m.author_id = ? AND m.kind = 'agent' AND m.status = 'complete' "
                    "AND c.kind != 'background'",
                    (agent_id,),
                ).fetchone()[0]
            )
            runs = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT run_id) FROM ("
                    "SELECT run_id FROM run_participants WHERE agent_id = ? "
                    "UNION SELECT run_id FROM model_usage WHERE agent_id = ? AND run_id IS NOT NULL)",
                    (agent_id, agent_id),
                ).fetchone()[0]
            )
            status_rows = conn.execute(
                "SELECT r.status, COUNT(*) AS n FROM runs r WHERE r.id IN ("
                "SELECT run_id FROM run_participants WHERE agent_id = ? "
                "UNION SELECT run_id FROM model_usage WHERE agent_id = ? AND run_id IS NOT NULL) "
                "GROUP BY r.status",
                (agent_id, agent_id),
            ).fetchall()
            statuses = {str(row["status"]): int(row["n"]) for row in status_rows}
            runs_completed = statuses.get("completed", 0)
            runs_failed = (
                statuses.get("error", 0)
                + statuses.get("limited", 0)
                + statuses.get("stopped", 0)
            )
            tasks_done = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status = 'done'",
                    (agent_id,),
                ).fetchone()[0]
            )
            tasks_open = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status IN ('todo', 'doing')",
                    (agent_id,),
                ).fetchone()[0]
            )
            passed, revised = self._review_counts(conn, agent_id)
            top_tools = self._top_tools(conn, agent_id)
            memories = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE scope = 'agent' AND agent_id = ? "
                    "AND status = 'active'",
                    (agent_id,),
                ).fetchone()[0]
            )
            recent_runs = self._recent_runs(conn, agent_id)
        usage_entry = self._agent_usage(agent, since, now, zone)
        return {
            "agent_id": agent_id,
            "period": period,
            "replies": replies,
            "runs": runs,
            "runs_completed": runs_completed,
            "runs_failed": runs_failed,
            "tasks_done": tasks_done,
            "tasks_open": tasks_open,
            "reviews": {"passed": passed, "revised": revised},
            "top_tools": top_tools,
            "usage": {
                "total_tokens": usage_entry["total_tokens"],
                "model_calls": usage_entry["model_calls"],
            },
            "memories": memories,
            "recent_runs": recent_runs,
        }

    def _review_counts(self, conn: sqlite3.Connection, agent_id: str) -> tuple[int, int]:
        passed = 0
        revised = 0
        for row in conn.execute(
            "SELECT result FROM actions WHERE tool_name = 'review' AND target_agent_id = ? "
            "AND status = 'completed'",
            (agent_id,),
        ).fetchall():
            data = _json_object(row["result"])
            if data.get("kind") == "pass":
                passed += 1
            elif data.get("kind") == "revise":
                revised += 1
        return passed, revised

    def _top_tools(self, conn: sqlite3.Connection, agent_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT a.tool_name, COUNT(*) AS n FROM actions a "
            "JOIN messages m ON m.id = a.message_id "
            "WHERE m.author_id = ? AND a.tool_name NOT IN ('token_budget', 'review') "
            "GROUP BY a.tool_name ORDER BY n DESC, a.tool_name ASC LIMIT 5",
            (agent_id,),
        ).fetchall()
        return [{"tool_name": str(row["tool_name"]), "count": int(row["n"])} for row in rows]

    def _recent_runs(self, conn: sqlite3.Connection, agent_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT r.id AS run_id, r.conversation_id, r.status, r.goal, r.created_at, "
            "c.title AS conversation_title "
            "FROM runs r LEFT JOIN conversations c ON c.id = r.conversation_id "
            "WHERE r.id IN (SELECT run_id FROM run_participants WHERE agent_id = ? "
            "UNION SELECT run_id FROM model_usage WHERE agent_id = ? AND run_id IS NOT NULL) "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 5",
            (agent_id, agent_id),
        ).fetchall()
        return [
            {
                "run_id": str(row["run_id"]),
                "conversation_id": str(row["conversation_id"]),
                "conversation_title": str(row["conversation_title"] or ""),
                "status": str(row["status"]),
                "goal": _goal_abstract(str(row["goal"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    # --------------------------------------------------------------- team report

    def team_report(self, *, period: str, now: datetime, zone: Any) -> dict[str, Any]:
        """Facts JSON for the digest: per-agent activity, open tasks and failed runs."""
        since_key = usage_rules.utc_key(usage_rules.period_start(period, now, zone))
        conn = self._ensure()
        with self._lock:
            entries = []
            for agent in self.list_agents():
                replies = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM messages m "
                        "JOIN conversations c ON c.id = m.conversation_id "
                        "WHERE m.author_id = ? AND m.kind = 'agent' AND m.status = 'complete' "
                        "AND c.kind != 'background' AND m.created_at >= ?",
                        (agent["id"], since_key),
                    ).fetchone()[0]
                )
                runs = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT rp.run_id) FROM run_participants rp "
                        "JOIN runs r ON r.id = rp.run_id WHERE rp.agent_id = ? AND r.created_at >= ?",
                        (agent["id"], since_key),
                    ).fetchone()[0]
                )
                runs_failed = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT rp.run_id) FROM run_participants rp "
                        "JOIN runs r ON r.id = rp.run_id WHERE rp.agent_id = ? "
                        "AND r.status IN ('error', 'limited', 'stopped') AND r.created_at >= ?",
                        (agent["id"], since_key),
                    ).fetchone()[0]
                )
                passed, revised = self._review_counts_since(conn, agent["id"], since_key)
                total_tokens, model_calls = self._period_tokens(conn, agent["id"], since_key)
                done, open_, blocked = self._task_counts(conn, agent["id"])
                entries.append(
                    {
                        "agent_id": agent["id"],
                        "name": agent["name"],
                        "kind": agent["kind"],
                        "replies": replies,
                        "runs": runs,
                        "runs_failed": runs_failed,
                        "review_passed": passed,
                        "review_revised": revised,
                        "total_tokens": total_tokens,
                        "model_calls": model_calls,
                        "tasks_done": done,
                        "tasks_open": open_,
                        "tasks_blocked": blocked,
                    }
                )
            open_tasks = self._open_tasks(conn)
            failed_runs = self._failed_runs(conn, since_key)
        return {
            "period": period,
            "agents": entries,
            "open_tasks": open_tasks,
            "failed_runs": failed_runs,
        }

    def _review_counts_since(
        self, conn: sqlite3.Connection, agent_id: str, since_key: str
    ) -> tuple[int, int]:
        passed = 0
        revised = 0
        for row in conn.execute(
            "SELECT result FROM actions WHERE tool_name = 'review' AND target_agent_id = ? "
            "AND status = 'completed' AND created_at >= ?",
            (agent_id, since_key),
        ).fetchall():
            data = _json_object(row["result"])
            if data.get("kind") == "pass":
                passed += 1
            elif data.get("kind") == "revise":
                revised += 1
        return passed, revised

    def _period_tokens(
        self, conn: sqlite3.Connection, agent_id: str, since_key: str
    ) -> tuple[int, int]:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS total, "
            "COUNT(*) AS n FROM model_usage WHERE agent_id = ? AND created_at >= ?",
            (agent_id, since_key),
        ).fetchone()
        return int(row["total"]), int(row["n"])

    def _task_counts(self, conn: sqlite3.Connection, agent_id: str) -> tuple[int, int, int]:
        done = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status = 'done'",
                (agent_id,),
            ).fetchone()[0]
        )
        open_ = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status IN ('todo', 'doing')",
                (agent_id,),
            ).fetchone()[0]
        )
        blocked = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status = 'blocked'",
                (agent_id,),
            ).fetchone()[0]
        )
        return done, open_, blocked

    def _open_tasks(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT t.title, t.status, t.assignee_id, a.name AS assignee "
            "FROM tasks t LEFT JOIN agents a ON a.id = t.assignee_id "
            "WHERE t.status IN ('todo', 'doing', 'blocked') "
            "ORDER BY t.created_at ASC, t.rowid ASC LIMIT 10"
        ).fetchall()
        return [
            {
                "title": str(row["title"]),
                "status": str(row["status"]),
                "assignee_id": row["assignee_id"],
                "assignee": str(row["assignee"] or ""),
            }
            for row in rows
        ]

    def _failed_runs(self, conn: sqlite3.Connection, since_key: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, goal, error, created_at FROM runs "
            "WHERE status IN ('error', 'limited', 'stopped') AND created_at >= ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 5",
            (since_key,),
        ).fetchall()
        return [
            {
                "run_id": str(row["id"]),
                "goal": _goal_abstract(str(row["goal"])),
                "error": str(row["error"] or ""),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]
