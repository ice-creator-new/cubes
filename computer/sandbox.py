"""Cubes 沙盒：在 Apple container 里开一台 Linux 小桌面，机器人只操作它，碰不到这台 Mac。

The sandbox runs ``cubes_computer.py`` inside the VM. The VM cannot reach a backend that only
listens on the Mac's 127.0.0.1, so this launcher relays exactly one path, ``/computer/agent``,
from the container network's gateway to the backend. Nothing else of the API is reachable from
inside the sandbox.

    uv run --python 3.12 --with-requirements computer/sandbox-requirements.txt \\
        computer/sandbox.py --server http://127.0.0.1:8000 --token <配对令牌>

The live view (noVNC) is published on the Mac's 127.0.0.1 only. Ctrl+C stops and removes the
sandbox; files inside it do not survive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil
import socket
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.http11 import Request, Response

HERE = Path(__file__).resolve().parent
IMAGE = "cubes-sandbox:3"
NAME_PREFIX = "cubes-sandbox-"
AGENT_PATH = "/computer/agent"
VIEW_PORT = 6080
MAX_FRAME = 8 * 1024 * 1024


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["container", *args], capture_output=True, text=True, check=check)


def _require_container() -> None:
    if shutil.which("container") is None:
        sys.exit("没有找到 Apple container：请先安装 https://github.com/apple/container 。")
    if _run("system", "status", check=False).returncode != 0:
        print("正在启动 Apple container 服务…", flush=True)
        subprocess.run(["container", "system", "start", "--enable-kernel-install"], check=True)


def _image_exists(image: str) -> bool:
    return _run("image", "inspect", image, check=False).returncode == 0


def _build(image: str) -> None:
    print(f"正在构建沙盒镜像 {image}，第一次要几分钟…", flush=True)
    subprocess.run(
        ["container", "build", "-t", image, "-f", str(HERE / "Containerfile"), str(HERE)], check=True
    )
    # The builder VM holds 2 GB while it runs; nothing needs it once the image exists.
    _run("stop", "buildkit", check=False)


def _gateway() -> str:
    inspected = json.loads(_run("network", "inspect", "default").stdout)
    return str(inspected[0]["status"]["ipv4Gateway"])


def _free_port(host: str) -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _backend_ws(server: str) -> str:
    parts = urlsplit(server)
    if parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
        sys.exit("--server 需要后端地址，例如 http://127.0.0.1:8000 。")
    scheme = "wss" if parts.scheme in ("https", "wss") else "ws"
    return f"{scheme}://{parts.netloc}{AGENT_PATH}"


async def _pump(source, target) -> None:
    async for message in source:
        await target.send(message)


async def relay(gateway: str, port: int, backend: str) -> None:
    """Forward only ``/computer/agent`` from the sandbox network to the backend."""

    def only_agent(_: ServerConnection, request: Request) -> Response | None:
        if request.path != AGENT_PATH:
            return Response(404, "Not Found", Headers(), b"")
        return None

    async def handle(inside: ServerConnection) -> None:
        headers = {"Authorization": inside.request.headers.get("Authorization", "")}
        try:
            async with connect(backend, additional_headers=headers, max_size=MAX_FRAME, proxy=None) as outside:
                tasks = [
                    asyncio.create_task(_pump(inside, outside)),
                    asyncio.create_task(_pump(outside, inside)),
                ]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
                code = outside.close_code or 1000
                await inside.close(code=code if code >= 4000 else 1000)
        except (OSError, InvalidStatus):
            await inside.close(code=1011)
        except ConnectionClosed:
            pass

    async with serve(handle, gateway, port, process_request=only_agent, max_size=MAX_FRAME):
        await asyncio.Future()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cubes 沙盒：Apple container 里的 Linux 桌面")
    parser.add_argument("--server", required=True, help="Cubes 后端地址，例如 http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="「电脑」面板里的配对令牌")
    parser.add_argument("--image", default=IMAGE, help="沙盒镜像名")
    parser.add_argument("--rebuild", action="store_true", help="重新构建镜像")
    parser.add_argument("--view-port", type=int, default=VIEW_PORT, help="实时画面在本机的端口")
    parser.add_argument("--place", default="local", choices=("local", "cloud"), help="这台沙盒登记为本机还是云端")
    args = parser.parse_args(argv)

    backend = _backend_ws(args.server)
    _require_container()
    if args.rebuild or not _image_exists(args.image):
        _build(args.image)

    gateway = _gateway()
    relay_port = _free_port("127.0.0.1")
    view = f"http://127.0.0.1:{args.view_port}/vnc.html?autoconnect=1&resize=scale"
    # A fresh name each run: a second launcher never removes the first one's sandbox.
    name = NAME_PREFIX + secrets.token_hex(3)
    started = _run(
        "run", "-d", "--rm", "--init", "--name", name,
        "-c", "2", "-m", "2G",
        "-p", f"127.0.0.1:{args.view_port}:6080",
        "-e", f"CUBES_TOKEN={args.token}",
        "-e", f"CUBES_SERVER=ws://{gateway}:{relay_port}",
        "-e", f"CUBES_VIEW={view}",
        "-e", f"CUBES_PLACE={args.place}",
        args.image,
        check=False,
    )
    if started.returncode != 0:
        sys.exit(f"沙盒没有启动：{started.stderr.strip() or started.stdout.strip()}")

    print(f"沙盒 {name} 已启动。实时画面：{view}", flush=True)
    print("按 Ctrl+C 关闭并删除沙盒。", flush=True)
    # Closing the terminal or a kill must also remove the VM, not only Ctrl+C: the sandbox holds
    # 2 GB while it runs.
    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        asyncio.run(relay(gateway, relay_port, backend))
    except KeyboardInterrupt:
        pass
    finally:
        _run("stop", name, check=False)
        _run("rm", "-f", name, check=False)
        print("沙盒已关闭。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
