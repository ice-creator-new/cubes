"""Agent skills: the row, the folder on disk and per-bot opt-outs.

One row mirrors a ``<name>/SKILL.md`` folder's frontmatter; the file is the source of truth,
so every edit rewrites it and the row together. Scripts run only in the sandbox computer,
and a bot-written skill starts disabled until the user enables it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from .. import skills as skills_rules
from .db import _UNSET, new_id, now_iso

class Skills:
    """The skill rows and their folders."""

    # ------------------------------------------------------------------ queries

    def list_skills(self) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute("SELECT * FROM skills ORDER BY name ASC").fetchall()
        return [self._skill_row(row) for row in rows]
    def get_skill(self, skill_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        return None if row is None else self._skill_row(row)
    def skill_by_name(self, name: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
        return None if row is None else self._skill_row(row)
    def skill_name_taken(self, name: str, *, exclude_id: str | None = None) -> bool:
        conn = self._ensure()
        with self._lock:
            row = conn.execute("SELECT 1 FROM skills WHERE name = ?", (name,)).fetchone()
        return row is not None
    def available_skills(self, agent: dict) -> list[dict]:
        """The skills this bot may use: enabled and not in its ``skills_disabled``."""
        permissions = agent.get("tool_permissions")
        disabled: set[str] = set()
        if isinstance(permissions, dict):
            raw = permissions.get("skills_disabled") or []
            disabled = {str(name) for name in raw}
        return [
            skill for skill in self.list_skills()
            if skill["enabled"] and skill["name"] not in disabled
        ]

    # ------------------------------------------------------------------ files

    def skill_folder(self, name: str) -> Path:
        return self.skills_dir / name
    def skill_body(self, skill: dict) -> str:
        """The SKILL.md body (without frontmatter), read back from disk."""
        path = self.skill_folder(skill["name"]) / skills_rules.SKILL_FILE_NAME
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise skills_rules.SkillInvalid("SKILL.md 文件已丢失。") from exc
        return skills_rules.split_frontmatter(text)[1]
    def skill_files(self, skill: dict) -> list[dict]:
        """Every file except SKILL.md, as ``{path, size}`` sorted by path."""
        folder = self.skill_folder(skill["name"])
        result: list[dict] = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            if relative == skills_rules.SKILL_FILE_NAME:
                continue
            result.append({"path": relative, "size": path.stat().st_size})
        return result

    # ------------------------------------------------------------------ writes

    def create_skill(
        self,
        *,
        name: str,
        description: str,
        body: str,
        metadata: dict[str, str] | None = None,
        source: str,
        source_url: str | None = None,
        author_id: str | None = None,
        enabled: bool,
        files: dict[str, bytes] | None = None,
        has_scripts: bool,
        file_count: int,
    ) -> dict:
        """Write the folder and its row; ``name`` must be free."""
        name = skills_rules.validate_name(name)
        description = skills_rules.validate_description(description)
        body = skills_rules.validate_body(body)
        timestamp = now_iso()
        skill_id = new_id("sk")
        folder = self.skill_folder(name)
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        markdown = skills_rules.render_skill_markdown(
            name, description, body, metadata=metadata
        )
        (folder / skills_rules.SKILL_FILE_NAME).write_text(markdown, encoding="utf-8")
        os.chmod(folder / skills_rules.SKILL_FILE_NAME, 0o600)
        for relative, data in (files or {}).items():
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            os.chmod(target, 0o600)
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO skills
                    (id, name, description, source, source_url, author_id, enabled,
                     has_scripts, file_count, use_count, last_used_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                """,
                (
                    skill_id,
                    name,
                    description,
                    source,
                    source_url,
                    author_id,
                    int(enabled),
                    int(has_scripts),
                    file_count,
                    timestamp,
                    timestamp,
                ),
            )
        created = self.get_skill(skill_id)
        assert created is not None
        return created
    def update_skill(
        self,
        skill_id: str,
        *,
        enabled: bool | object = _UNSET,
        description: str | object = _UNSET,
        body: str | object = _UNSET,
    ) -> dict | None:
        """Edit one skill, rewriting its SKILL.md; ``name`` is immutable."""
        skill = self.get_skill(skill_id)
        if skill is None:
            return None
        new_description = (
            skill["description"]
            if description is _UNSET
            else skills_rules.validate_description(str(description))
        )
        new_body = self.skill_body(skill) if body is _UNSET else skills_rules.validate_body(str(body))
        new_enabled = skill["enabled"] if enabled is _UNSET else bool(enabled)
        metadata = self._metadata_of(skill)
        markdown = skills_rules.render_skill_markdown(
            skill["name"], new_description, new_body, metadata=metadata
        )
        (self.skill_folder(skill["name"]) / skills_rules.SKILL_FILE_NAME).write_text(
            markdown, encoding="utf-8"
        )
        os.chmod(self.skill_folder(skill["name"]) / skills_rules.SKILL_FILE_NAME, 0o600)
        with self._transaction() as tx:
            cursor = tx.execute(
                """
                UPDATE skills
                   SET description = ?, enabled = ?, updated_at = ?
                 WHERE id = ?
                """,
                (new_description, int(new_enabled), now_iso(), skill_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_skill(skill_id)
    def bump_skill_use(self, skill_id: str) -> None:
        with self._transaction() as tx:
            tx.execute(
                "UPDATE skills SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                (now_iso(), skill_id),
            )
    def delete_skill(self, skill_id: str) -> bool:
        skill = self.get_skill(skill_id)
        if skill is None:
            return False
        name = skill["name"]
        with self._transaction() as tx:
            tx.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        shutil.rmtree(self.skill_folder(name), ignore_errors=True)
        self.prune_skill_from_permissions(name)
        return True

    # ------------------------------------------------------------------ opt-outs

    def prune_skill_from_permissions(self, name: str) -> None:
        """Drop ``name`` from every agent's ``tool_permissions.skills_disabled``."""
        conn = self._ensure()
        with self._lock:
            rows = conn.execute(
                "SELECT id, tool_permissions FROM agents WHERE tool_permissions IS NOT NULL"
            ).fetchall()
            changed: list[tuple[str, str]] = []
            for row in rows:
                try:
                    permissions = json.loads(row["tool_permissions"])
                except ValueError:
                    continue
                if not isinstance(permissions, dict):
                    continue
                disabled = permissions.get("skills_disabled") or []
                if name in disabled:
                    permissions["skills_disabled"] = [n for n in disabled if n != name]
                    changed.append(
                        (json.dumps(permissions, ensure_ascii=False), str(row["id"]))
                    )
        if changed:
            with self._transaction() as tx:
                for serialized, agent_id in changed:
                    tx.execute(
                        "UPDATE agents SET tool_permissions = ? WHERE id = ?",
                        (serialized, agent_id),
                    )

    # ------------------------------------------------------------------ helpers

    def _metadata_of(self, skill: dict) -> dict[str, str]:
        path = self.skill_folder(skill["name"]) / skills_rules.SKILL_FILE_NAME
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        return skills_rules.split_frontmatter(text)[0].get("metadata") or {}
    def _skill_row(self, row: sqlite3.Row) -> dict:
        author_id = row["author_id"]
        author = self.get_agent(author_id) if author_id else None
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "source": row["source"],
            "source_url": row["source_url"],
            "author_id": author_id,
            "author_name": author["name"] if author else None,
            "enabled": bool(row["enabled"]),
            "has_scripts": bool(row["has_scripts"]),
            "file_count": row["file_count"],
            "use_count": row["use_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
