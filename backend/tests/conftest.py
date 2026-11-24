"""Test fixtures: real uvicorn servers for both the app and an isolated upstream."""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

# The app drives this Mac directly unless a test says not to. The suite talks to a fake agent.
os.environ["CUBES_HOST_COMPUTER"] = "0"

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from fake_upstream import FakeUpstream  # noqa: E402


class ServerThread:
    """Run a FastAPI app under uvicorn on an ephemeral loopback port."""

    def __init__(self, app: object) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self.port = self._socket.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "ServerThread":
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._server.started:
                return self
            if not self._thread.is_alive():
                break
            time.sleep(0.02)
        raise RuntimeError("server failed to start")

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None
        with contextlib.suppress(Exception):
            self._socket.close()


class Backend:
    """The app under test, restartable against the same SQLite file."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.app: Any = None
        self._server: ServerThread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "backend not started"
        return self._server.base_url

    def start(self) -> "Backend":
        self.app = create_app(self.settings)
        self._server = ServerThread(self.app).start()
        return self

    def restart(self) -> "Backend":
        """Stop the ASGI app (lifespan shutdown), then start a fresh one on the same DB."""
        self.stop()
        return self.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None


@pytest.fixture
def upstream() -> FakeUpstream:
    fake = FakeUpstream()
    server = ServerThread(fake.app).start()
    fake.base_url = f"{server.base_url}/v1"  # type: ignore[attr-defined]
    # Anthropic base URLs carry no ``/v1``: the adapter appends ``/v1/messages`` itself.
    fake.root_url = server.base_url  # type: ignore[attr-defined]
    try:
        yield fake
    finally:
        server.stop()


@pytest.fixture
def make_backend(tmp_path: Path, upstream: FakeUpstream):
    """Factory returning a started app wired to the isolated upstream and a temp DB."""
    started: list[Backend] = []

    def factory(**overrides: object) -> Backend:
        defaults: dict[str, object] = {
            "base_url": upstream.base_url,  # type: ignore[attr-defined]
            "api_key": "test-key",
            "model": "test-model",
            "database_path": tmp_path / f"cubes-{len(started)}.sqlite3",
            "request_timeout": 15.0,
            "stop_timeout": 5.0,
        }
        defaults.update(overrides)
        backend = Backend(Settings(**defaults)).start()  # type: ignore[arg-type]
        started.append(backend)
        return backend

    try:
        yield factory
    finally:
        for backend in started:
            backend.stop()
