"""Launch-token guard for the desktop backend.

The desktop shell binds the backend to a random loopback port and hands its webview a random
token over IPC, so a token in the request is the only way to prove the caller is that window.
Development keeps the token empty, which installs nothing and leaves the API as it was.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

UNAUTHORIZED_DETAIL = "缺少或错误的访问令牌。"


def _presented(header: str | None) -> str | None:
    """The token inside ``Authorization: Bearer <token>``, or ``None`` for anything else."""
    if not header:
        return None
    scheme, separator, value = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return value.strip() or None


def _matches(header: str | None, token: str) -> bool:
    presented = _presented(header)
    if presented is None:
        return False
    # Constant time: a loopback port is still reachable by any local process.
    return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))


def _verifies_itself(path: str) -> bool:
    """A path that carries its own credential, so the launch token does not apply.

    A hook URL authenticates with its own secret; a CLI tool endpoint authenticates with the
    per-turn token in its path (see ``app.cli_tools``). Management of both lives under
    ``/webhooks`` and the ordinary API, behind the launch token like everything else.
    """
    return path.startswith("/hooks/") or path.startswith("/cli-tools/")


def install_auth(app: FastAPI, token: str) -> None:
    """Require ``token`` on every request; an empty token installs no guard at all."""
    if not token:
        return

    @app.middleware("http")
    async def require_launch_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # A preflight never carries credentials, and refusing it would hide the real answer.
        if (
            request.method == "OPTIONS"
            or _verifies_itself(request.url.path)
            or _matches(request.headers.get("authorization"), token)
        ):
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": UNAUTHORIZED_DETAIL})
