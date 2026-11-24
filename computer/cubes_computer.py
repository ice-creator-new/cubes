"""Cubes 沙盒里的连接程序：让团队里的机器人用终端、浏览器自动化和屏幕操作这台 Linux 虚拟机。

Runs inside the sandbox VM (``computer/Containerfile``) on its X display and dials out to the
Cubes backend over a WebSocket. The backend sends one call at a time; three kinds answer it:

- ``shell``: one bash command in the user's home, its output and exit code, no screenshot.
- ``browser``: browser-use drives the Chromium window on the screen over CDP, by page elements
  instead of pixels. One call may carry several actions; it stops early when the page changes.
  Each answer carries the page's title, address, tabs and its indexed elements in browser-use's
  own format, plus how much of the page lies above and below.
- everything else: the screen itself. Input goes through ``xdotool``; every answer carries a
  fresh screenshot downscaled to at most ``MAX_WIDTH`` pixels wide, and coordinates are pixels of
  that screenshot.

Started by the image's entrypoint with ``CUBES_SERVER`` and ``CUBES_TOKEN``; ``sandbox.py`` on
the Mac starts the VM and passes both.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import mss
from PIL import Image, ImageDraw
from browser_use import BrowserSession, Tools
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

PROTOCOL_VERSION = 4
MAX_WIDTH = 1280
JPEG_QUALITY = 70
# Let the screen catch up with a click or keystroke before it is photographed.
SETTLE_SECONDS = 0.6
CURSOR_RADIUS = 10
XDOTOOL_SECONDS = 60
RECONNECT_SECONDS = (1, 2, 5, 10)
CLOSE_UNAUTHORIZED = 4401
CLOSE_REPLACED = 4409
CLOSE_OUTDATED = 4426

HOME = Path.home()
# Output beyond this is cut from the middle: the start says what ran, the end how it ended.
MAX_OUTPUT_CHARS = 12_000
MAX_SHELL_SECONDS = 600
# The Chromium on the screen, started by the entrypoint with this debugging port.
CDP_URL = "http://127.0.0.1:9222"
CDP_WAIT_SECONDS = 20
BROWSER_SECONDS = 30
MAX_PAGE_CHARS = 12_000
# Actions a browser call may carry, and the browser-use action each one becomes.
BROWSER_ACTIONS = {
    "open": "navigate",
    "back": "go_back",
    "click": "click",
    "input": "input",
    "select": "select_dropdown",
    "keys": "send_keys",
    "scroll": "scroll",
    "find_text": "find_text",
    "search_page": "search_page",
    "switch_tab": "switch",
    "close_tab": "close",
    "wait": "wait",
}
# These leave the page where it was, so the next action in the batch can still find its index.
SAFE_TO_CHAIN = {"input", "select", "scroll", "find_text", "search_page", "wait"}
MAX_BATCH = 5
# Read/debug actions the host's own Chrome speaks; browser-use has no equivalent here.
HOST_ONLY_ACTIONS = {"read_text", "run_js", "console"}

# Models often write Mac or web key names; xdotool wants X keysyms.
KEY_NAMES = {
    "cmd": "ctrl",
    "command": "ctrl",
    "meta": "super",
    "win": "super",
    "control": "ctrl",
    "option": "alt",
    "opt": "alt",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "spacebar": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "page_up": "Prior",
    "pagedown": "Next",
    "page_down": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "plus": "plus",
    "minus": "minus",
}
MODIFIERS = {"ctrl", "alt", "shift", "super"}
BUTTONS = {"click": "1", "double_click": "1", "right_click": "3"}
SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}


class ActionError(Exception):
    """A Chinese sentence for the model about why this step did not happen."""


def xdotool(*args: str) -> str:
    try:
        done = subprocess.run(
            ["xdotool", *args], capture_output=True, text=True, timeout=XDOTOOL_SECONDS, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise ActionError(f"这一步没有完成：{(exc.stderr or '').strip() or 'xdotool 出错'}。") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActionError("这一步没有完成：输入工具没有响应。") from exc
    return done.stdout


@dataclass(frozen=True)
class Screen:
    """The X display in pixels, and the screenshot size the model works in."""

    width: int
    height: int
    shot_width: int
    shot_height: int

    @classmethod
    def detect(cls) -> "Screen":
        width, height = (int(part) for part in xdotool("getdisplaygeometry").split())
        shot_width = min(MAX_WIDTH, width)
        shot_height = round(height * shot_width / width)
        return cls(width, height, shot_width, shot_height)

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        sx = round((x + 0.5) * self.width / self.shot_width)
        sy = round((y + 0.5) * self.height / self.shot_height)
        return min(max(sx, 0), self.width - 1), min(max(sy, 0), self.height - 1)

    def to_shot(self, x: int, y: int) -> tuple[int, int]:
        return round(x * self.shot_width / self.width), round(y * self.shot_height / self.height)


def system_name() -> str:
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        return "Linux"
    return release.get("PRETTY_NAME") or release.get("NAME") or "Linux"


# ---------------------------------------------------------------------- screen


def pointer() -> tuple[int, int]:
    fields = dict(
        line.split("=", 1) for line in xdotool("getmouselocation", "--shell").splitlines() if "=" in line
    )
    return int(fields["X"]), int(fields["Y"])


def screenshot(screen: Screen) -> tuple[str, dict]:
    """The display as base64 JPEG at the model's size, with the pointer marked."""
    with mss.MSS() as grabber:
        shot = grabber.grab(grabber.monitors[1])
    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if image.size != (screen.shot_width, screen.shot_height):
        image = image.resize((screen.shot_width, screen.shot_height), Image.Resampling.LANCZOS)
    cx, cy = screen.to_shot(*pointer())
    ImageDraw.Draw(image).ellipse(
        (cx - CURSOR_RADIUS, cy - CURSOR_RADIUS, cx + CURSOR_RADIUS, cy + CURSOR_RADIUS),
        outline=(255, 40, 40),
        width=3,
    )
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii"), {"x": cx, "y": cy}


# ---------------------------------------------------------------------- input


def keysyms(raw: str) -> str:
    """``cmd+shift+t`` as the xdotool chord ``ctrl+shift+t``."""
    names = [part.strip() for part in raw.split("+") if part.strip()]
    if not names:
        raise ActionError(f"不认识的按键「{raw}」。")
    chord = []
    for name in names:
        lowered = name.lower()
        mapped = KEY_NAMES.get(lowered, lowered if lowered in MODIFIERS else name)
        if len(mapped) == 1 and mapped.isalpha():
            mapped = mapped.lower()
        chord.append(mapped)
    return "+".join(chord)


def move(screen: Screen, args: dict) -> None:
    x, y = screen.to_screen(int(args["x"]), int(args["y"]))
    xdotool("mousemove", "--sync", str(x), str(y))


def perform(screen: Screen, op: str, args: dict) -> None:
    if op == "screenshot":
        return
    if op == "wait":
        time.sleep(float(args.get("seconds", 2)))
        return
    if op == "move":
        move(screen, args)
        return
    if op in BUTTONS:
        move(screen, args)
        repeat = ["--repeat", "2", "--delay", "80"] if op == "double_click" else []
        xdotool("click", *repeat, BUTTONS[op])
        return
    if op == "drag":
        move(screen, args)
        to_x, to_y = screen.to_screen(int(args["to_x"]), int(args["to_y"]))
        xdotool("mousedown", "1")
        xdotool("mousemove", "--sync", str(to_x), str(to_y))
        xdotool("mouseup", "1")
        return
    if op == "scroll":
        if "x" in args and "y" in args:
            move(screen, args)
        direction = str(args.get("direction", "down"))
        if direction not in SCROLL_BUTTONS:
            raise ActionError(f"不认识的滚动方向「{direction}」。")
        xdotool("click", "--repeat", str(int(args.get("amount", 3))), "--delay", "40", SCROLL_BUTTONS[direction])
        return
    if op == "type":
        xdotool("type", "--delay", "12", "--", str(args.get("text", "")))
        return
    if op == "key":
        xdotool("key", "--clearmodifiers", keysyms(str(args.get("keys", ""))))
        return
    raise ActionError(f"不支持的操作「{op}」。")


def zoom(screen: Screen, args: dict) -> dict:
    """One region of the screenshot, cropped from the real pixels and enlarged for small controls."""
    try:
        x, y = int(args.get("x", 0)), int(args.get("y", 0))
        width, height = int(args.get("width", 0)), int(args.get("height", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "zoom 的 x、y、width、height 必须是整数。"}
    if width < 1 or height < 1 or x < 0 or y < 0 or x + width > screen.shot_width or y + height > screen.shot_height:
        return {
            "ok": False,
            "error": f"放大区域超出了截图范围（{screen.shot_width}×{screen.shot_height}）。",
        }
    try:
        with mss.MSS() as grabber:
            shot = grabber.grab(grabber.monitors[1])
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except (mss.ScreenShotError, OSError):
        return {"ok": False, "error": "截屏失败：虚拟屏幕没有响应。"}
    scale_x = screen.width / screen.shot_width
    scale_y = screen.height / screen.shot_height
    left = min(max(round(x * scale_x), 0), screen.width - 1)
    top = min(max(round(y * scale_y), 0), screen.height - 1)
    crop_w = max(1, min(round(width * scale_x), screen.width - left))
    crop_h = max(1, min(round(height * scale_y), screen.height - top))
    crop = image.crop((left, top, left + crop_w, top + crop_h))
    # At most 4x: a 100 px control becomes readable, a 20 px one does not become noise.
    target_w = min(MAX_WIDTH, max(crop.width, crop.width * 4))
    if crop.width != target_w:
        crop = crop.resize((target_w, max(1, round(crop.height * target_w / crop.width))), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    crop.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return {
        "ok": True,
        "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "mime": "image/jpeg",
        "screen": {"width": screen.shot_width, "height": screen.shot_height},
        "zoom": {"x": x, "y": y, "width": width, "height": height},
    }


def run_call(screen: Screen, op: str, args: dict) -> dict:
    """Perform, settle, photograph: the whole answer to one call."""
    if op == "zoom":
        return zoom(screen, args)
    try:
        perform(screen, op, args)
    except ActionError as exc:
        return {"ok": False, "error": str(exc)}
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"参数不对：{exc}。"}
    if op not in ("screenshot", "wait"):
        time.sleep(SETTLE_SECONDS)
    try:
        image, cursor = screenshot(screen)
    except (mss.ScreenShotError, ActionError):
        return {"ok": False, "error": "截屏失败：虚拟屏幕没有响应。"}
    return {
        "ok": True,
        "image": image,
        "mime": "image/jpeg",
        "cursor": cursor,
        "screen": {"width": screen.shot_width, "height": screen.shot_height},
    }


# ---------------------------------------------------------------------- shell


def clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = (limit - 40) // 2
    return f"{text[:half]}\n…（中间省略 {len(text) - 2 * half} 个字符）…\n{text[-half:]}"


def run_shell(args: dict) -> dict:
    """One bash command in its own process group, so a timeout takes its children with it."""
    command = str(args.get("command", ""))
    timeout = min(max(int(args.get("timeout", 60)), 1), MAX_SHELL_SECONDS)
    cwd = Path(str(args.get("cwd") or HOME)).expanduser()
    if not cwd.is_dir():
        return {"ok": False, "error": f"目录「{cwd}」不存在。"}
    started = time.monotonic()
    # Output goes to files, not pipes: a command that floods stdout can never block on a full pipe.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", errors="replace")
        stderr = err.read().decode("utf-8", errors="replace")
    return {
        "ok": True,
        "exit_code": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 1),
        "cwd": str(cwd),
        "stdout": clip_middle(stdout, MAX_OUTPUT_CHARS),
        "stderr": clip_middle(stderr, MAX_OUTPUT_CHARS // 2),
    }


# ---------------------------------------------------------------------- browser


def cdp_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def ensure_chromium() -> None:
    """The browser the user sees, with its debugging port; started again if someone closed it."""
    if cdp_ready():
        return
    subprocess.Popen(
        ["cubes-browser"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + CDP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if cdp_ready():
            return
        time.sleep(0.25)
    raise ActionError("浏览器没有启动起来。")


class BrowserDriver:
    """browser-use attached to the Chromium window everyone sees on the sandbox screen.

    It never runs browser-use's own agent or model: Cubes' bot decides, this only executes the
    actions it sent and reports the page the way browser-use shows it to its own models.
    """

    def __init__(self) -> None:
        self._session: BrowserSession | None = None
        self._tools = Tools()
        self._model = self._tools.registry.create_action_model()

    async def _connected(self) -> BrowserSession:
        if self._session is not None:
            return self._session
        await asyncio.to_thread(ensure_chromium)
        session = BrowserSession(cdp_url=CDP_URL, is_local=False)
        await session.start()
        self._session = session
        return session

    async def run(self, args: dict) -> dict:
        try:
            return await asyncio.wait_for(self._run(args), timeout=BROWSER_SECONDS * MAX_BATCH)
        except asyncio.TimeoutError:
            await self._drop()
            return {"ok": False, "error": "浏览器在规定时间内没有完成这一步。"}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # a dropped CDP link: reconnect on the next call
            await self._drop()
            first = (str(exc).strip().splitlines() or [exc.__class__.__name__])[0]
            return {"ok": False, "error": f"浏览器连接出错：{first[:200]}。下一步会重新连接。"}

    async def _drop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.stop()

    async def _run(self, args: dict) -> dict:
        session = await self._connected()
        batch = (args.get("actions") or [])[:MAX_BATCH]
        done: list[dict] = []
        url_before = await session.get_current_page_url() if batch else None
        for index, step in enumerate(batch):
            name = str(step.get("action", ""))
            if name in HOST_ONLY_ACTIONS:
                done.append(
                    {
                        "action": name,
                        "ok": False,
                        "result": (
                            "沙盒浏览器不支持这一步：读正文用 search_page，"
                            "调试页面用 computer_shell 的 curl；read_text、run_js、console "
                            "只有本机 Chrome 支持。"
                        ),
                    }
                )
                break
            if name not in BROWSER_ACTIONS:
                done.append({"action": name, "ok": False, "result": f"不支持的浏览器操作「{name}」。"})
                break
            params = {key: value for key, value in step.items() if key != "action"}
            result = await self._tools.act(
                self._model(**{BROWSER_ACTIONS[name]: params}),
                browser_session=session,
                action_timeout=BROWSER_SECONDS,
            )
            ok = not result.error
            text = result.error or result.extracted_content or ""
            done.append({"action": name, "ok": ok, "result": clip_middle(str(text), 3_000)})
            if not ok:
                break
            # Anything but a safe action may have changed the page: the rest would aim at stale
            # indexes, so the model sees the new state first.
            remaining = len(batch) - index - 1
            if remaining and (name not in SAFE_TO_CHAIN or await session.get_current_page_url() != url_before):
                done.append({"action": "…", "ok": True, "result": f"页面可能变了，后面 {remaining} 个动作没有执行。"})
                break
        state = await session.get_browser_state_summary(include_screenshot=False)
        page = state.page_info
        height = page.viewport_height if page else 0
        return {
            "ok": True,
            "kind": "page",
            "title": state.title,
            "url": state.url,
            "tabs": [{"id": tab.target_id[-4:], "title": tab.title, "url": tab.url} for tab in state.tabs],
            "steps": done,
            "elements": clip_middle(state.dom_state.llm_representation(), MAX_PAGE_CHARS),
            "above": round(page.pixels_above / height, 1) if height else 0,
            "below": round(page.pixels_below / height, 1) if height else 0,
        }


# ---------------------------------------------------------------------- connection


async def session(
    ws: ClientConnection, name: str, view: str | None, browser: BrowserDriver, place: str
) -> None:
    screen = Screen.detect()
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "version": PROTOCOL_VERSION,
                "name": name,
                "os": system_name(),
                "screen": {"width": screen.shot_width, "height": screen.shot_height},
                "view": view,
                "place": place,
            }
        )
    )
    print(f"已连接 Cubes：截图 {screen.shot_width}×{screen.shot_height}。", flush=True)
    # Commands are independent processes, so two runs' shells run side by side. The screen
    # and the one Chromium are single, so those take turns behind ``desk``.
    desk = asyncio.Lock()
    sending = asyncio.Lock()
    running: set[asyncio.Task] = set()

    async def answer(message: dict) -> None:
        op, args = str(message.get("op")), message.get("args") or {}
        try:
            if op == "shell":
                reply = await asyncio.to_thread(run_shell, args)
            elif op == "browser":
                async with desk:
                    reply = await browser.run(args)
                    if reply.get("ok") and args.get("screenshot"):
                        image, cursor = await asyncio.to_thread(screenshot, screen)
                        reply.update(image=image, mime="image/jpeg", cursor=cursor)
            else:
                async with desk:
                    reply = await asyncio.to_thread(run_call, screen, op, args)
        except Exception as exc:  # noqa: BLE001 - the backend needs a sentence, not a drop
            reply = {"ok": False, "error": str(exc).strip()[:300] or exc.__class__.__name__}
        async with sending:
            await ws.send(json.dumps({"type": "result", "id": message.get("id"), **reply}))

    try:
        async for raw in ws:
            message = json.loads(raw)
            if message.get("type") != "call":
                continue
            task = asyncio.create_task(answer(message))
            running.add(task)
            task.add_done_callback(running.discard)
    finally:
        # A dropped connection takes its in-flight work with it; nobody will read the answers.
        for task in list(running):
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


async def main_async(server: str, token: str, name: str, view: str | None, place: str) -> int:
    url = server.rstrip("/") + "/computer/agent"
    delays = iter(RECONNECT_SECONDS)
    browser = BrowserDriver()
    while True:
        try:
            async with connect(
                url,
                additional_headers={"Authorization": f"Bearer {token}"},
                max_size=8 * 1024 * 1024,
                proxy=None,
            ) as ws:
                delays = iter(RECONNECT_SECONDS)
                await session(ws, name, view, browser, place)
        except InvalidStatus as exc:
            print(f"Cubes 拒绝了连接（HTTP {exc.response.status_code}）。", file=sys.stderr, flush=True)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            if code == CLOSE_UNAUTHORIZED:
                print("配对令牌不对：请从 Cubes 的「电脑」面板复制最新的命令。", file=sys.stderr, flush=True)
                return 1
            if code == CLOSE_REPLACED:
                print("另一台沙盒接管了连接，本程序退出。", flush=True)
                return 0
            if code == CLOSE_OUTDATED:
                print("Cubes 不接受这个版本的连接程序：请用新的 sandbox.py 重新构建镜像。", file=sys.stderr, flush=True)
                return 1
            print("和 Cubes 的连接断开了，正在重连…", flush=True)
        except OSError:
            print("连不上 Cubes，正在重试…", file=sys.stderr, flush=True)
        await asyncio.sleep(next(delays, RECONNECT_SECONDS[-1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cubes 沙盒连接程序")
    parser.add_argument("--server", default=os.environ.get("CUBES_SERVER"), help="Cubes 后端地址；默认读 CUBES_SERVER")
    parser.add_argument("--token", default=os.environ.get("CUBES_TOKEN"), help="配对令牌；默认读 CUBES_TOKEN")
    parser.add_argument("--name", default="Cubes 沙盒", help="显示给机器人的名字")
    parser.add_argument("--view", default=os.environ.get("CUBES_VIEW"), help="实时画面的地址；默认读 CUBES_VIEW")
    parser.add_argument(
        "--place",
        default=os.environ.get("CUBES_PLACE", "local"),
        choices=("local", "cloud"),
        help="local 是这台 Mac 上的沙盒，cloud 是云端电脑",
    )
    args = parser.parse_args(argv)
    if not args.server or not args.token:
        parser.error("需要 CUBES_SERVER 和 CUBES_TOKEN。")
    try:
        return asyncio.run(main_async(args.server, args.token, args.name, args.view, args.place))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
