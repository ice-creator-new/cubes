"""Entry point for the backend the desktop shell starts as its child.

The shell starts this process on a random loopback port with a random token and hands both to
its webview; nothing else gets the token, so nothing else can use the API.

Two shapes:

- The release bundle passes ``--data-dir``: the database and the avatar cache live under the
  app's own data directory, and nothing reads ``backend/.env``, so the app starts as an empty
  team and the user adds model providers in settings.
- ``npm run desktop`` runs this file from source and passes ``--env-file backend/.env``: settings
  come from that file exactly as for ``uvicorn app.main:app`` (its ``DATABASE_PATH``, search
  keys, workspace directory), so the dev window shows the dev team. It also passes the dev
  server's origin with ``--allow-origin``.

Both pass ``--parent-pid``: when the shell dies without saying goodbye (a crash, or a force
quit), this process notices and exits instead of lingering.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import socket
import sys
import threading
from pathlib import Path

import uvicorn

from app.config import Settings, load_settings
from app.engine import SupervisorEngine
from app.main import create_app

HOST = "127.0.0.1"
# The origin the packaged app serves its page from (web/electron/main.cjs).
APP_ORIGINS = ("app://cubes",)
# The shell sends SIGTERM and kills the process two seconds later; leave room to exit first.
GRACEFUL_SHUTDOWN_SECONDS = 2
PARENT_POLL_SECONDS = 1.0


class Server(uvicorn.Server):
    """uvicorn that ends the open event streams the moment it starts shutting down.

    uvicorn waits for every open response before it runs the app's shutdown, and the
    webview's ``/events`` stream never ends by itself: without this, each quit sat out the
    whole graceful timeout and then cancelled the stream anyway.
    """

    def __init__(self, config: uvicorn.Config, engine: SupervisorEngine) -> None:
        super().__init__(config)
        self._engine = engine

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._engine.close_streams()
        await super().shutdown(sockets)


def _port(text: str) -> int:
    number = int(text)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间。")
    return number


def _pid(text: str) -> int:
    number = int(text)
    if number <= 1:
        raise argparse.ArgumentTypeError("父进程号必须大于 1。")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cubes 桌面端后端")
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--data-dir", type=Path, help="桌面版的数据目录")
    parser.add_argument("--env-file", type=Path, help="开发模式：按这个 .env 读取设置")
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="除桌面窗口外再放行的来源，可以重复",
    )
    parser.add_argument("--parent-pid", type=_pid, help="这个进程没了就退出")
    args = parser.parse_args(argv)
    if (args.data_dir is None) == (args.env_file is None):
        parser.error("--data-dir 和 --env-file 必须且只能给一个。")
    return args


def build_settings(args: argparse.Namespace) -> Settings:
    """Settings for one desktop backend; the launch token always guards it."""
    origins = tuple(dict.fromkeys((*APP_ORIGINS, *args.allow_origin)))
    if args.env_file is not None:
        # Development: the same settings `uvicorn app.main:app` would read.
        base = load_settings(args.env_file)
        return dataclasses.replace(base, auth_token=args.token, cors_origins=origins)
    data_dir = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)  # The database holds plaintext provider keys.
    database_path = data_dir / "data" / "cubes.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_path.parent.chmod(0o700)
    return Settings(database_path=database_path, auth_token=args.token, cors_origins=origins)


def watch_parent(server: uvicorn.Server, parent_pid: int) -> threading.Thread:
    """Ask ``server`` to exit once ``parent_pid`` is gone.

    An orphan is re-parented, so ``getppid`` stops matching; a parent that still exists but is
    no longer ours (a recycled pid) is caught by the same check.
    """

    def run() -> None:
        while not server.should_exit:
            if os.getppid() != parent_pid:
                print("桌面窗口已经退出，后端随之关闭。", file=sys.stderr, flush=True)
                server.should_exit = True
                return
            threading.Event().wait(PARENT_POLL_SECONDS)

    thread = threading.Thread(target=run, name="parent-watch", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = create_app(build_settings(args))
    server = Server(
        uvicorn.Config(
            app,
            host=HOST,
            port=args.port,
            log_level="info",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        ),
        app.state.engine,
    )
    if args.parent_pid is not None:
        watch_parent(server, args.parent_pid)
    try:
        server.run()
    except OSError as error:  # A taken port: the shell picks another one and tries again.
        print(f"后端启动失败：{error}", file=sys.stderr)
        return 1
    # uvicorn swallows a failed bind and only stops; the shell needs a non-zero code to retry.
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
