"""Model providers and the workspace's default model."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..providers import ProviderInvalid, ProviderNameTaken, models_json
from .db import (
    DEFAULT_MODEL_KEY,
    DEFAULT_PROVIDER_ID_KEY,
    FALLBACK_MODEL_KEY,
    FALLBACK_PROVIDER_ID_KEY,
    _UNSET,
    _json_object,
    new_id,
    now_iso,
)

class Providers:
    """Providers and the default model."""

    @classmethod
    def _default_row(cls, conn: sqlite3.Connection) -> dict[str, str] | None:
        """The stored default pair, only when both halves are present."""
        provider_id = cls._meta(conn, DEFAULT_PROVIDER_ID_KEY)
        model = cls._meta(conn, DEFAULT_MODEL_KEY)
        if not provider_id or not model:
            return None
        return {"provider_id": provider_id, "model": model}
    @classmethod
    def _write_default(
        cls, conn: sqlite3.Connection, provider_id: str, model: str
    ) -> None:
        cls._meta_set(conn, DEFAULT_PROVIDER_ID_KEY, provider_id)
        cls._meta_set(conn, DEFAULT_MODEL_KEY, model)
    @classmethod
    def _clear_default(cls, conn: sqlite3.Connection) -> None:
        cls._meta_delete(conn, DEFAULT_PROVIDER_ID_KEY)
        cls._meta_delete(conn, DEFAULT_MODEL_KEY)
    def default_model(self) -> dict[str, str] | None:
        """``{"provider_id", "model"}`` for the workspace default, or ``None``."""
        conn = self._ensure()
        with self._lock:
            return self._default_row(conn)
    def set_default_model(self, provider_id: str, model: str) -> dict[str, str]:
        """Point the workspace default at one listed model; anything else is refused."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
            if row is None:
                raise ProviderInvalid("选择的模型服务不存在，可能刚被删除。")
            if model not in json.loads(row["models"] or "[]"):
                raise ProviderInvalid("选择的模型不在这个模型服务的列表里。")
            self._write_default(tx, provider_id, model)
        return {"provider_id": provider_id, "model": model}
    @classmethod
    def _fallback_row(cls, conn: sqlite3.Connection) -> dict[str, str] | None:
        """The stored backup pair, only when both halves are present."""
        provider_id = cls._meta(conn, FALLBACK_PROVIDER_ID_KEY)
        model = cls._meta(conn, FALLBACK_MODEL_KEY)
        if not provider_id or not model:
            return None
        return {"provider_id": provider_id, "model": model}
    def fallback_model(self) -> dict[str, str] | None:
        """``{"provider_id", "model"}`` for the optional backup model, or ``None``."""
        conn = self._ensure()
        with self._lock:
            return self._fallback_row(conn)
    def set_fallback_model(self, provider_id: str, model: str) -> dict[str, str]:
        """Point the backup at one listed model; the same checks as the default."""
        with self._transaction() as tx:
            row = tx.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
            if row is None:
                raise ProviderInvalid("选择的模型服务不存在，可能刚被删除。")
            if model not in json.loads(row["models"] or "[]"):
                raise ProviderInvalid("选择的模型不在这个模型服务的列表里。")
            self._meta_set(tx, FALLBACK_PROVIDER_ID_KEY, provider_id)
            self._meta_set(tx, FALLBACK_MODEL_KEY, model)
        return {"provider_id": provider_id, "model": model}
    def clear_fallback_model(self) -> None:
        """Turn the backup model off; a failed call then ends the turn as before."""
        with self._transaction() as tx:
            self._meta_delete(tx, FALLBACK_PROVIDER_ID_KEY)
            self._meta_delete(tx, FALLBACK_MODEL_KEY)
    @staticmethod
    def _provider_name_taken(
        conn: sqlite3.Connection, name: str, *, exclude_id: str | None = None
    ) -> bool:
        wanted = name.strip().casefold()
        for row in conn.execute("SELECT id, name FROM providers").fetchall():
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if str(row["name"]).strip().casefold() == wanted:
                return True
        return False
    def list_providers(self) -> list[dict]:
        conn = self._ensure()
        with self._lock:
            rows = conn.execute("SELECT * FROM providers ORDER BY rowid ASC").fetchall()
        return [self._provider_row(row) for row in rows]
    def get_provider(self, provider_id: str) -> dict | None:
        conn = self._ensure()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
        return None if row is None else self._provider_row(row)
    def create_provider(
        self,
        *,
        name: str,
        protocol: str,
        base_url: str,
        api_key: str,
        models: list[str],
        model_caps: dict[str, dict] | None = None,
        model_overrides: dict[str, dict] | None = None,
    ) -> dict:
        """Insert one provider; the very first provider also becomes the default."""
        provider_id = new_id("p")
        timestamp = now_iso()
        with self._transaction() as tx:
            if self._provider_name_taken(tx, name):
                raise ProviderNameTaken(f"模型服务名称「{name.strip()}」已被使用，请换一个。")
            tx.execute(
                """
                INSERT INTO providers
                    (id, name, protocol, base_url, api_key, models, model_caps,
                     model_overrides, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_id,
                    name,
                    protocol,
                    base_url,
                    api_key,
                    models_json(models),
                    json.dumps(model_caps or {}, ensure_ascii=False),
                    json.dumps(model_overrides or {}, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            if self._default_row(tx) is None and models:
                self._write_default(tx, provider_id, models[0])
        provider = self.get_provider(provider_id)
        assert provider is not None
        return provider
    def update_provider(
        self,
        provider_id: str,
        *,
        name: str | object = _UNSET,
        protocol: str | object = _UNSET,
        base_url: str | object = _UNSET,
        api_key: str | object = _UNSET,
        models: list[str] | object = _UNSET,
        model_caps: dict[str, dict] | object = _UNSET,
        model_overrides: dict[str, dict] | object = _UNSET,
    ) -> dict | None:
        """Patch one provider; a default model it no longer lists moves to its first one."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT * FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
            if row is None:
                return None
            sets: list[str] = []
            params: list[Any] = []
            if name is not _UNSET:
                if self._provider_name_taken(tx, str(name), exclude_id=provider_id):
                    raise ProviderNameTaken(f"模型服务名称「{str(name).strip()}」已被使用，请换一个。")
                sets.append("name = ?")
                params.append(name)
            for column, value in (("protocol", protocol), ("base_url", base_url)):
                if value is not _UNSET:
                    sets.append(f"{column} = ?")
                    params.append(value)
            if api_key is not _UNSET:
                sets.append("api_key = ?")
                params.append(api_key)
            if models is not _UNSET:
                sets.append("models = ?")
                params.append(models_json(models))  # type: ignore[arg-type]
            for column, value in (
                ("model_caps", model_caps),
                ("model_overrides", model_overrides),
            ):
                if value is not _UNSET:
                    sets.append(f"{column} = ?")
                    params.append(json.dumps(dict(value), ensure_ascii=False))  # type: ignore[arg-type]
            sets.append("updated_at = ?")
            params.append(now_iso())
            params.append(provider_id)
            tx.execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", params)
            default = self._default_row(tx)
            if default is not None and default["provider_id"] == provider_id:
                listed = json.loads(
                    tx.execute(
                        "SELECT models FROM providers WHERE id = ?", (provider_id,)
                    ).fetchone()["models"]
                    or "[]"
                )
                if default["model"] not in listed and listed:
                    self._write_default(tx, provider_id, listed[0])
        return self.get_provider(provider_id)
    def delete_provider(self, provider_id: str) -> dict | None:
        """Remove one provider and, if it was the default, pick a usable default instead."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT * FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
            if row is None:
                return None
            tx.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            default = self._default_row(tx)
            if default is not None and default["provider_id"] == provider_id:
                remaining = tx.execute(
                    "SELECT * FROM providers ORDER BY rowid ASC LIMIT 1"
                ).fetchone()
                listed = json.loads(remaining["models"] or "[]") if remaining else []
                if remaining is not None and listed:
                    self._write_default(tx, remaining["id"], listed[0])
                else:
                    self._clear_default(tx)
            fallback = self._fallback_row(tx)
            if fallback is not None and fallback["provider_id"] == provider_id:
                self._meta_delete(tx, FALLBACK_PROVIDER_ID_KEY)
                self._meta_delete(tx, FALLBACK_MODEL_KEY)
            return self._provider_row(row)
    @staticmethod
    def _provider_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "protocol": row["protocol"],
            "base_url": row["base_url"],
            "api_key": row["api_key"],
            "models": json.loads(row["models"] or "[]"),
            # What the provider's own /models answer said, and the user's own answers.
            "model_caps": _json_object(row["model_caps"]),
            "model_overrides": _json_object(row["model_overrides"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
