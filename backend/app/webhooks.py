"""Webhook triggers: a secret URL that starts one schedule or one workflow right now.

A hook is the one way into the backend that does not carry the launch token: an outside
service (a CI job, a cron on another machine, a form) proves itself with the hook's own
secret instead. Everything else about it is deliberately small:

- the secret is generated here, shown once, and stored as a SHA-256, never in clear;
- the comparison is constant time, so a wrong secret cannot be guessed by timing;
- one call per hook per few seconds: a misfiring sender cannot start a hundred runs;
- the body is capped, so an outside caller cannot fill memory with one request.

The backend binds to loopback by default, which is why ``docs/setup.md`` explains what
exposing it entails.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request

SECRET_BYTES = 32
# One accepted call per hook per this many seconds.
COOLDOWN_SECONDS = 5.0
# A request body may not be larger than this; a hook starts a run, it never uploads files.
MAX_BODY_BYTES = 64 * 1024


def new_secret() -> str:
    """A fresh URL-safe secret: 256 random bits, safe in a header, a query or a shell."""
    return secrets.token_urlsafe(SECRET_BYTES)


def hash_secret(secret: str) -> str:
    """The only form a secret is stored in."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_matches(presented: str | None, stored_hash: str) -> bool:
    """Constant-time check of a presented secret against the stored hash."""
    if not presented:
        return False
    digest = hash_secret(presented)
    return hmac.compare_digest(digest.encode("utf-8"), stored_hash.encode("utf-8"))


def presented_secret(request: Request) -> str | None:
    """The secret in ``Authorization: Bearer <secret>``, else the ``?token=`` parameter."""
    header = request.headers.get("authorization")
    if header:
        scheme, separator, value = header.partition(" ")
        if separator and scheme.lower() == "bearer":
            token = value.strip()
            if token:
                return token
    token = request.query_params.get("token")
    return (token or "").strip() or None


def webhook_path(webhook_id: str) -> str:
    """The public path of one hook; a client joins it to the backend's own address."""
    return f"/hooks/{webhook_id}"


def webhook_payload(row: dict[str, Any]) -> dict[str, Any]:
    """One hook as the panel reads it: the URL to copy and when it last fired.

    The secret is never part of this shape; it only comes back from the call that created
    or rotated it.
    """
    return {
        "id": row["id"],
        "target_kind": row["target_kind"],
        "target_id": row["target_id"],
        "path": webhook_path(str(row["id"])),
        "created_at": row["created_at"],
        "last_called_at": row["last_called_at"],
    }


def created_payload(row: dict[str, Any], secret: str) -> dict[str, Any]:
    """The one answer that carries the secret, for the user to copy somewhere safe."""
    return {**webhook_payload(row), "secret": secret}


async def read_input(request: Request) -> str:
    """The caller's ``input``: a JSON body's ``input`` (or a JSON string), else plain text."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"请求内容太大，最多 {MAX_BODY_BYTES // 1024} KB。",
        )
    if not raw:
        return ""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="请求体不是有效的 JSON。") from exc
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            value = data.get("input", "")
            if value is None:
                return ""
            if not isinstance(value, str):
                raise HTTPException(status_code=422, detail="「input」需要是文字。")
            return value.strip()
        raise HTTPException(status_code=422, detail="请求体需要是 JSON 对象。")
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="请求体不是 UTF-8 文字。") from exc


class Throttle:
    """One accepted call per hook per interval; memory only, so a restart forgives."""

    def __init__(self, interval: float = COOLDOWN_SECONDS) -> None:
        self._interval = interval
        self._last: dict[str, float] = {}

    def take(self, key: str) -> float:
        """Reserve this call; returns how long to wait instead when one just happened."""
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return self._interval - (now - last)
        self._last[key] = now
        return 0.0

    def forget(self, key: str) -> None:
        self._last.pop(key, None)


# One process-wide limiter; a hook id is never reused, so old entries are harmless.
throttle = Throttle()
