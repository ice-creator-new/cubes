"""Always-allow rules: the tool a bot may call again without a confirmation card.

One row is one remembered 「总是允许」 click. ``agent_id`` names the bot that asked; a NULL
``agent_id`` means every bot (the card only offers the per-bot version today, but the row
shape already says what "any bot" would mean). Deleting a member keeps its rules: they name
an id, and an id that is gone simply never matches again.
"""

from __future__ import annotations

from .db import new_id, now_iso


class ApprovalRules:
    """The stored rules behind a card's 「总是允许」 button."""

    def list_approval_rules(self) -> list[dict]:
        """Every rule, newest first, each with the name of the bot it names (if any)."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                """
                SELECT r.*, a.name AS agent_name
                  FROM approval_rules r
                  LEFT JOIN agents a ON a.id = r.agent_id
                 ORDER BY r.created_at DESC, r.rowid DESC
                """
            ).fetchall()
        return [self._approval_rule_row(row) for row in rows]

    def approval_rule_for(self, tool_name: str, agent_id: str) -> dict | None:
        """The rule that covers this call: the bot's own first, then "any bot"'s."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                """
                SELECT * FROM approval_rules
                 WHERE tool_name = ? AND (agent_id = ? OR agent_id IS NULL)
                 ORDER BY agent_id IS NULL ASC, rowid ASC LIMIT 1
                """,
                (tool_name, agent_id),
            ).fetchone()
        return None if row is None else self._approval_rule_row(row)

    def add_approval_rule(self, *, tool_name: str, agent_id: str | None) -> dict:
        """Remember one rule, reusing the row that already covers the same pair."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT id FROM approval_rules WHERE tool_name = ? AND agent_id IS ?",
                (tool_name, agent_id),
            ).fetchone()
            if row is not None:
                rule_id = str(row["id"])
            else:
                rule_id = new_id("r")
                tx.execute(
                    "INSERT INTO approval_rules (id, tool_name, agent_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (rule_id, tool_name, agent_id, now_iso()),
                )
        rule = self.get_approval_rule(rule_id)
        assert rule is not None
        return rule

    def action_message(self, action_id: str) -> dict | None:
        """The message row one action is shown as, or ``None`` once it is gone."""
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM messages WHERE action_id = ? ORDER BY rowid ASC LIMIT 1",
                (action_id,),
            ).fetchone()
        return None if row is None else self._message_row(row)

    def get_approval_rule(self, rule_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM approval_rules WHERE id = ?", (rule_id,)
            ).fetchone()
        return None if row is None else self._approval_rule_row(row)

    def delete_approval_rule(self, rule_id: str) -> bool:
        """Delete one rule; ``False`` when it was already gone."""
        with self._transaction() as tx:
            cursor = tx.execute("DELETE FROM approval_rules WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _approval_rule_row(row) -> dict:
        keys = row.keys()
        return {
            "id": row["id"],
            "tool_name": row["tool_name"],
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"] if "agent_name" in keys else None,
            "created_at": row["created_at"],
        }
