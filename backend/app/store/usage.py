"""Per-call token usage: one row per model call, and the ``GET /usage`` roll-up.

Every model call an agent makes — a reply, a context-compaction summary, a memory
extraction — writes one row here, attributed to that agent. The run keeps its own running
totals separately (``runs``), so this ledger exists to answer "how much has *this* bot used
in *this* period".
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .. import usage as usage_rules
from .db import new_id, now_iso


def _bucket_index(local: datetime, buckets: list[datetime], until: datetime) -> int | None:
    """The index of the bucket holding ``local``, or ``None`` if it falls outside the range."""
    for index, start in enumerate(buckets):
        end = buckets[index + 1] if index + 1 < len(buckets) else until
        if start <= local < end:
            return index
    return None


class Usage:
    """The per-call ledger and the per-agent roll-up."""

    def record_usage(
        self,
        *,
        agent_id: str,
        conversation_id: str | None,
        run_id: str | None,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> None:
        conn = self._ensure()
        with self._lock:
            conn.execute(
                """
                INSERT INTO model_usage
                    (id, run_id, agent_id, conversation_id, purpose, input_tokens,
                     output_tokens, cached_tokens, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("u"),
                    run_id,
                    agent_id,
                    conversation_id,
                    purpose,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, cached_tokens),
                    now_iso(),
                ),
            )

    def budget_used(self, agent_id: str, period: str, now: datetime, zone: ZoneInfo) -> int:
        """Total tokens (input + output) one agent spent in its own budget period."""
        since_key = usage_rules.utc_key(usage_rules.period_start(period, now, zone))
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                  FROM model_usage
                 WHERE agent_id = ? AND created_at >= ?
                """,
                (agent_id, since_key),
            ).fetchone()
        return int(row[0])

    def usage_report(self, *, period: str, now: datetime, zone: ZoneInfo) -> dict:
        """The ``GET /usage`` payload: one entry per agent, supervisor first."""
        since, until = usage_rules.period_bounds(period, now, zone)
        since_key = usage_rules.utc_key(since)
        until_key = usage_rules.utc_key(until)
        entries = [self._agent_usage(agent, since, now, zone) for agent in self.list_agents()]
        conn = self._ensure()
        with self._lock:
            totals = self._period_totals(conn, since_key, until_key)
            by_purpose = self._by_purpose(conn, since_key, until_key)
            previous_total = self._previous_total(
                conn,
                usage_rules.utc_key(usage_rules.previous_period_start(period, now, zone)),
                since_key,
            )
            series = self._series(conn, period, now, zone, since, until)
        return {
            "period": period,
            "since": since.isoformat(timespec="seconds"),
            "agents": entries,
            "until": until.isoformat(timespec="seconds"),
            "totals": totals,
            "previous_total": previous_total,
            "by_purpose": by_purpose,
            "series": series,
        }

    def _period_totals(self, conn, since_key: str, until_key: str) -> dict:
        """Tokens and calls across *all* rows in the period — including deleted agents."""
        row = conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                   COUNT(*) AS model_calls
              FROM model_usage
             WHERE created_at >= ? AND created_at < ?
            """,
            (since_key, until_key),
        ).fetchone()
        return {
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cached_tokens": int(row["cached_tokens"]),
            "total_tokens": int(row["input_tokens"]) + int(row["output_tokens"]),
            "model_calls": int(row["model_calls"]),
        }

    def _by_purpose(self, conn, since_key: str, until_key: str) -> dict:
        """``total_tokens`` per stored ``purpose``; the three known keys are always present."""
        result: dict[str, int] = {key: 0 for key in usage_rules.PURPOSES}
        rows = conn.execute(
            """
            SELECT purpose,
                   COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS total
              FROM model_usage
             WHERE created_at >= ? AND created_at < ?
             GROUP BY purpose
            """,
            (since_key, until_key),
        ).fetchall()
        for row in rows:
            result[row["purpose"]] = int(row["total"])
        return result

    def _previous_total(self, conn, start_key: str, end_key: str) -> int:
        """Total tokens of the same-length period immediately before this one."""
        row = conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS total
              FROM model_usage
             WHERE created_at >= ? AND created_at < ?
            """,
            (start_key, end_key),
        ).fetchone()
        return int(row["total"])

    def _series(
        self,
        conn,
        period: str,
        now: datetime,
        zone: ZoneInfo,
        since: datetime,
        until: datetime,
    ) -> list[dict]:
        """Gap-free buckets covering the period, each with its own ``total_tokens`` and
        per-agent subtotals (only non-zero agents listed)."""
        buckets = usage_rules.bucket_starts(period, now, zone)
        series = [
            {"start": bucket.isoformat(timespec="seconds"), "total_tokens": 0, "by_agent": {}}
            for bucket in buckets
        ]
        rows = conn.execute(
            """
            SELECT created_at, agent_id, input_tokens, output_tokens
              FROM model_usage
             WHERE created_at >= ? AND created_at < ?
            """,
            (usage_rules.utc_key(since), usage_rules.utc_key(until)),
        ).fetchall()
        for row in rows:
            local = datetime.fromisoformat(row["created_at"]).astimezone(zone)
            index = _bucket_index(local, buckets, until)
            if index is None:  # pragma: no cover - rows are within [since, until)
                continue
            total = int(row["input_tokens"]) + int(row["output_tokens"])
            bucket = series[index]
            bucket["total_tokens"] += total
            agent_id = row["agent_id"]
            bucket["by_agent"][agent_id] = bucket["by_agent"].get(agent_id, 0) + total
        return series

    def _agent_usage(
        self, agent: dict, since: datetime, now: datetime, zone: ZoneInfo
    ) -> dict:
        since_key = usage_rules.utc_key(since)
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                       COUNT(*) AS model_calls
                  FROM model_usage
                 WHERE agent_id = ? AND created_at >= ?
                """,
                (agent["id"], since_key),
            ).fetchone()
            budget = agent.get("token_budget")
            budget_used = None
            if isinstance(budget, dict) and budget.get("period") in usage_rules.PERIODS:
                budget_key = usage_rules.utc_key(
                    usage_rules.period_start(budget["period"], now, zone)
                )
                used = conn.execute(
                    """
                    SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                      FROM model_usage
                     WHERE agent_id = ? AND created_at >= ?
                    """,
                    (agent["id"], budget_key),
                ).fetchone()
                budget_used = int(used[0])
            last = conn.execute(
                """
                SELECT m.created_at, m.mood
                  FROM messages m
                  JOIN conversations c ON c.id = m.conversation_id
                 WHERE m.author_id = ? AND m.kind = 'agent' AND m.status = 'complete'
                   AND c.kind != 'background'
                 ORDER BY m.created_at DESC, m.rowid DESC
                 LIMIT 1
                """,
                (agent["id"],),
            ).fetchone()
        return {
            "agent_id": agent["id"],
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cached_tokens": int(row["cached_tokens"]),
            "total_tokens": int(row["input_tokens"]) + int(row["output_tokens"]),
            "model_calls": int(row["model_calls"]),
            "budget_used": budget_used,
            "last_reply_at": last["created_at"] if last is not None else None,
            "last_mood": last["mood"] if last is not None else None,
        }
