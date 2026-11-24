"""Connectors: how one remote tool service is reached and what it exposes."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import _UNSET, new_id, now_iso

class Connectors:
    """Connectors."""

    def list_connectors(self) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute("SELECT * FROM connectors ORDER BY rowid ASC").fetchall()
        return [self._connector_row(row) for row in rows]
    def get_connector(self, connector_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM connectors WHERE id = ?", (connector_id,)
            ).fetchone()
        return None if row is None else self._connector_row(row)
    def connector_name_taken(self, name: str, *, exclude_id: str | None = None) -> bool:
        wanted = name.strip().casefold()
        return any(
            row["name"].strip().casefold() == wanted
            for row in self.list_connectors()
            if row["id"] != exclude_id
        )
    def create_connector(
        self,
        *,
        preset: str,
        name: str,
        slug: str,
        config: dict,
        allow_writes: bool,
        tools: list[dict],
    ) -> dict:
        connector_id = new_id("c")
        timestamp = now_iso()
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO connectors
                    (id, preset, name, slug, config, allow_writes, enabled, tools, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    connector_id,
                    preset,
                    name,
                    slug,
                    json.dumps(config, ensure_ascii=False),
                    int(allow_writes),
                    json.dumps(tools, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
        created = self.get_connector(connector_id)
        assert created is not None
        return created
    def update_connector(
        self,
        connector_id: str,
        *,
        name: str | object = _UNSET,
        config: dict | object = _UNSET,
        allow_writes: bool | object = _UNSET,
        enabled: bool | object = _UNSET,
        tools: list[dict] | object = _UNSET,
    ) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not _UNSET:
            sets.append("name = ?")
            params.append(name)
        if config is not _UNSET:
            sets.append("config = ?")
            params.append(json.dumps(config, ensure_ascii=False))
        if allow_writes is not _UNSET:
            sets.append("allow_writes = ?")
            params.append(int(bool(allow_writes)))
        if enabled is not _UNSET:
            sets.append("enabled = ?")
            params.append(int(bool(enabled)))
        if tools is not _UNSET:
            sets.append("tools = ?")
            params.append(json.dumps(tools, ensure_ascii=False))
        sets.append("updated_at = ?")
        params.append(now_iso())
        with self._transaction() as tx:
            updated = tx.execute(
                f"UPDATE connectors SET {', '.join(sets)} WHERE id = ?",
                (*params, connector_id),
            ).rowcount
        return self.get_connector(connector_id) if updated else None
    def delete_connector(self, connector_id: str) -> bool:
        with self._transaction() as tx:
            return bool(
                tx.execute("DELETE FROM connectors WHERE id = ?", (connector_id,)).rowcount
            )
    @staticmethod
    def _connector_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "preset": row["preset"],
            "name": row["name"],
            "slug": row["slug"],
            "config": json.loads(row["config"] or "{}"),
            "allow_writes": bool(row["allow_writes"]),
            "enabled": bool(row["enabled"]),
            "tools": json.loads(row["tools"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
