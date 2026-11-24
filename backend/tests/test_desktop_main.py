"""The desktop entry point: which settings each shape gets, and the orphan watchdog."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import desktop_main

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_release_shape_keeps_everything_under_the_data_dir(tmp_path: Path) -> None:
    args = desktop_main.parse_args(["--port", "8123", "--token", "t", "--data-dir", str(tmp_path / "app")])
    settings = desktop_main.build_settings(args)
    assert settings.database_path == (tmp_path / "app" / "data" / "cubes.sqlite3").resolve()
    assert settings.auth_token == "t"
    assert settings.cors_origins == desktop_main.APP_ORIGINS
    # The release app starts empty: nothing from a `.env` reaches it.
    assert settings.api_key == "" and settings.model == ""
    assert stat.S_IMODE((tmp_path / "app").stat().st_mode) == 0o700


def test_dev_shape_reads_the_env_file_and_admits_the_dev_origin(tmp_path: Path, monkeypatch) -> None:
    for name in ("DATABASE_PATH", "MODEL_NAME", "MODEL_API_KEY", "MODEL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        f"DATABASE_PATH={tmp_path / 'dev.sqlite3'}\nMODEL_NAME=dev-model\nMODEL_API_KEY=dev-key\n",
        encoding="utf-8",
    )
    args = desktop_main.parse_args(
        [
            "--port", "8123", "--token", "t",
            "--env-file", str(env),
            "--allow-origin", "http://127.0.0.1:1420",
        ]
    )
    settings = desktop_main.build_settings(args)
    assert settings.database_path == tmp_path / "dev.sqlite3"
    assert settings.model == "dev-model"
    assert settings.auth_token == "t"
    assert settings.cors_origins == (*desktop_main.APP_ORIGINS, "http://127.0.0.1:1420")


@pytest.mark.parametrize(
    "argv",
    [
        ["--port", "1", "--token", "t"],
        ["--port", "1", "--token", "t", "--data-dir", "/tmp/a", "--env-file", "/tmp/.env"],
    ],
)
def test_exactly_one_settings_source_is_required(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        desktop_main.parse_args(argv)


def test_the_backend_exits_when_its_parent_dies(tmp_path: Path) -> None:
    """A shell that crashes, or a force quit, must not leave the backend behind."""
    port_probe = __import__("socket").socket()
    port_probe.bind(("127.0.0.1", 0))
    port = port_probe.getsockname()[1]
    port_probe.close()
    # A throwaway parent that starts the backend, prints its pid, then dies without a word.
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, subprocess, sys;"
                "child = subprocess.Popen([sys.executable, 'desktop_main.py', '--port', sys.argv[1],"
                " '--token', 't', '--data-dir', sys.argv[2], '--parent-pid', str(os.getpid())],"
                " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
                "print(child.pid, flush=True);"
                "import time; time.sleep(1.5); os._exit(0)"
            ),
            str(port),
            str(tmp_path / "app"),
        ],
        cwd=BACKEND_DIR,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    parent.wait(timeout=10)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(child_pid, 9)
    pytest.fail("the backend outlived its parent")


def test_quit_does_not_wait_out_the_open_event_stream(tmp_path: Path) -> None:
    """The webview always holds `/events` open; SIGTERM must end it, not sit out the timeout."""
    sock = __import__("socket")
    port_probe = sock.socket()
    port_probe.bind(("127.0.0.1", 0))
    port = port_probe.getsockname()[1]
    port_probe.close()
    backend = subprocess.Popen(
        [sys.executable, "desktop_main.py", "--port", str(port), "--token", "t", "--data-dir", str(tmp_path / "app")],
        cwd=BACKEND_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                stream = sock.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("the backend never listened")
                time.sleep(0.1)
        stream.sendall(f"GET /events HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer t\r\n\r\n".encode())
        stream.settimeout(10)
        assert stream.recv(64).startswith(b"HTTP/1.1 200")
        started = time.monotonic()
        backend.send_signal(15)
        backend.wait(timeout=10)
        # The graceful timeout is 2s: a stream that is never ended costs all of it.
        assert time.monotonic() - started < desktop_main.GRACEFUL_SHUTDOWN_SECONDS
        stream.close()
    finally:
        if backend.poll() is None:
            backend.kill()
            backend.wait()
