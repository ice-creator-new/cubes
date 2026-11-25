"""Drive the computer the backend is running on.

``local`` is this machine, not the Apple container. On a Mac the backend takes the screenshot,
moves the pointer and types; a shell command runs in the user's own shell. Nothing has to be
started first. Screen recording and accessibility are macOS permission dialogs, not a command
the user pastes.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .host_browser import HostBrowser

MAX_WIDTH = 1280
MAX_OUTPUT_CHARS = 12_000
MAX_SHELL_SECONDS = 600
SETTLE_SECONDS = 0.35

# macOS virtual key codes. Letters go through keystroke instead.
_KEY_CODES = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "spacebar": 49,
    "delete": 51,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "forwarddelete": 117,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "page_up": 116,
    "pagedown": 121,
    "page_down": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}
_MODIFIERS = {
    "cmd": "command down",
    "command": "command down",
    "meta": "command down",
    "ctrl": "control down",
    "control": "control down",
    "alt": "option down",
    "option": "option down",
    "opt": "option down",
    "shift": "shift down",
}


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _Rect(ctypes.Structure):
    _fields_ = [("origin", _Point), ("size", _Size)]


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = (MAX_OUTPUT_CHARS - 40) // 2
    return f"{text[:half]}\n…（中间省略 {len(text) - 2 * half} 个字符）…\n{text[-half:]}"


def _osa(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
    )


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class HostComputer:
    """Shell, and on a Mac the screen, of the machine this process runs on."""

    def __init__(self, default_cwd: Callable[[], str | Path] | None = None) -> None:
        # A shell call with no cwd starts in the workspace's working folder, not the user's
        # home: scratch files and experiments belong there, never in the home directory.
        self._default_cwd = default_cwd
        self._lock = threading.Lock()
        # One run's commands never kill or overwrite another's: each call registers its own
        # process under its run id, and a cancel names that run.
        self._procs: dict[str, set[subprocess.Popen]] = {}
        # A stop belongs to the run that asked for it; two runs' browser waits and commands
        # must not end each other.
        self._stops: dict[str, threading.Event] = {}
        self._cg: ctypes.CDLL | None = None
        # Cubes' own Chrome for the host browser tool; it starts on first use, and holds one
        # window per run.
        self._chrome = HostBrowser()

    def _stop_for(self, run_id: str) -> threading.Event:
        with self._lock:
            event = self._stops.get(run_id)
            if event is None:
                event = self._stops[run_id] = threading.Event()
            return event

    def arm(self, run_id: str = "") -> None:
        self._stop_for(run_id).clear()

    def info(self) -> dict:
        name = "这台 Mac" if sys.platform == "darwin" else "这台电脑"
        system = self._system()
        screen = self._shot_size()
        return {
            "place": "local",
            "host": True,
            "name": name,
            "os": system,
            "screen": {"width": screen[0], "height": screen[1]},
            "view": None,
            "skills_dir": str(Path.home() / "Cubes" / "skills"),
        }

    def cancel(self, run_id: str = "") -> None:
        """Stop ``run_id``'s browser waits and commands; other runs keep running."""
        self._stop_for(run_id).set()
        with self._lock:
            procs = list(self._procs.get(run_id, ()))
        for proc in procs:
            if proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    def close(self) -> None:
        """Backend shutdown: every run's Chrome window goes, the user's own does not."""
        with self._lock:
            events = list(self._stops.values())
            procs = [proc for group in self._procs.values() for proc in group]
        for event in events:
            event.set()
        for proc in procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
        self._chrome.close()

    def call(self, op: str, args: dict, *, run_id: str = "") -> dict:
        try:
            if op == "shell":
                return self._shell(args, run_id)
            if op == "browser":
                return self._browser(args, run_id)
            return self._screen(op, args)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "这一步超时了。"}
        except Exception as exc:  # noqa: BLE001 - the model needs a sentence, not a traceback
            text = str(exc).strip() or exc.__class__.__name__
            return {"ok": False, "error": text}

    def has_browser(self, run_id: str) -> bool:
        return self._chrome.has_window(run_id)

    def release_browser(self, run_id: str) -> None:
        """The run ended: close the Chrome window it was using."""
        self._chrome.release(run_id)

    # ------------------------------------------------------------------ shell

    def _shell(self, args: dict, run_id: str = "") -> dict:
        command = str(args.get("command") or "")
        if not command.strip():
            return {"ok": False, "error": "没有要运行的命令。"}
        timeout = min(max(int(args.get("timeout") or 60), 1), MAX_SHELL_SECONDS)
        raw_cwd = args.get("cwd")
        if not raw_cwd and self._default_cwd is not None:
            try:
                raw_cwd = self._default_cwd()
            except OSError:
                raw_cwd = None
        cwd = Path(str(raw_cwd or Path.home())).expanduser()
        if not cwd.is_dir():
            return {"ok": False, "error": f"目录「{cwd}」不存在。"}
        stop = self._stop_for(run_id)
        if stop.is_set():
            return {"ok": False, "error": "已停止。"}
        started = time.monotonic()
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                ["bash", "-lc", command],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            with self._lock:
                self._procs.setdefault(run_id, set()).add(proc)
            if stop.is_set():
                self.cancel(run_id)
                proc.wait(timeout=2)
                with self._lock:
                    group = self._procs.get(run_id)
                    if group is not None:
                        group.discard(proc)
                return {"ok": False, "error": "已停止。"}
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            finally:
                with self._lock:
                    group = self._procs.get(run_id)
                    if group is not None:
                        group.discard(proc)
            out.seek(0)
            err.seek(0)
            stdout = out.read().decode("utf-8", errors="replace")
            stderr = err.read().decode("utf-8", errors="replace")
        return {
            "ok": True,
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 1),
            "cwd": str(cwd),
            "stdout": _clip(stdout),
            "stderr": _clip(stderr),
        }

    # ------------------------------------------------------------------ browser

    def _browser(self, args: dict, run_id: str = "") -> dict:
        """Cubes' own Chrome, by element numbers; each run gets its own window."""
        actions = args.get("actions") or []
        if not isinstance(actions, list):
            actions = []
        return self._chrome.call(
            [step for step in actions[:5] if isinstance(step, dict)],
            screenshot=args.get("screenshot") is True,
            should_stop=self._stop_for(run_id).is_set,
            run_id=run_id,
        )

    # ------------------------------------------------------------------ screen

    def _screen(self, op: str, args: dict) -> dict:
        if sys.platform != "darwin":
            return {"ok": False, "error": "这台电脑不能操作屏幕，只能用终端。"}
        # A zoom is not an action on the screen: it only looks closer at one region.
        if op == "zoom":
            return self._zoom(args)
        try:
            self._perform(op, args)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if op not in ("screenshot", "wait"):
            time.sleep(SETTLE_SECONDS)
        ok, image, cursor = self._photograph()
        if not ok:
            return {"ok": False, "error": image}
        width, height = self._shot_size()
        return {
            "ok": True,
            "image": image,
            "mime": "image/jpeg",
            "cursor": cursor,
            "screen": {"width": width, "height": height},
        }

    def _perform(self, op: str, args: dict) -> None:
        if op == "screenshot":
            return
        if op == "wait":
            time.sleep(min(max(float(args.get("seconds") or 2), 0), 60))
            return
        if op in ("move", "click", "double_click", "right_click", "drag"):
            self._require_accessibility()
            x, y = self._points(args)
            if op == "move":
                self._mouse(5, x, y)
                return
            if op == "drag":
                to_x, to_y = self._points(args, "to_x", "to_y")
                self._mouse(1, x, y)
                self._mouse(6, to_x, to_y)
                self._mouse(2, to_x, to_y)
                return
            down, up, button = (3, 4, 1) if op == "right_click" else (1, 2, 0)
            repeats = 2 if op == "double_click" else 1
            for _ in range(repeats):
                self._mouse(down, x, y, button)
                self._mouse(up, x, y, button)
                time.sleep(0.08)
            return
        if op == "scroll":
            self._require_accessibility()
            if "x" in args and "y" in args:
                x, y = self._points(args)
                self._mouse(5, x, y)
            direction = str(args.get("direction") or "down")
            amount = int(args.get("amount") or 3)
            dx, dy = {"up": (0, amount), "down": (0, -amount), "left": (-amount, 0), "right": (amount, 0)}.get(
                direction, (None, None)
            )
            if dx is None:
                raise ValueError(f"不认识的滚动方向「{direction}」。")
            self._scroll(dx, dy)
            return
        if op == "type":
            self._require_accessibility()
            self._type(str(args.get("text") or ""))
            return
        if op == "key":
            self._require_accessibility()
            self._key(str(args.get("keys") or ""))
            return
        raise ValueError(f"不支持的操作「{op}」。")

    def _zoom(self, args: dict) -> dict:
        """One region of the screen, enlarged, in screenshot coordinates.

        The model sees a 1280-wide screenshot but the controls on it may be a few pixels;
        cropping the real pixels around the target and blowing them up is what makes a small
        checkbox or icon clickable by hand.
        """
        shot_w, shot_h = self._shot_size()
        x, y = int(args.get("x") or 0), int(args.get("y") or 0)
        width, height = int(args.get("width") or 0), int(args.get("height") or 0)
        if width < 1 or height < 1 or x < 0 or y < 0 or x + width > shot_w or y + height > shot_h:
            return {"ok": False, "error": f"放大区域超出了截图范围（{shot_w}×{shot_h}）。"}
        try:
            pixels_w, pixels_h, scale = self._display()
        except (OSError, AttributeError):
            return {"ok": False, "error": "读不到屏幕尺寸。"}
        left = round(x * pixels_w / shot_w)
        top = round(y * pixels_h / shot_h)
        crop_w = max(1, round(width * pixels_w / shot_w))
        crop_h = max(1, round(height * pixels_h / shot_h))
        points = (round(left / scale), round(top / scale), round(crop_w / scale), round(crop_h / scale))
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        try:
            shot = subprocess.run(
                ["screencapture", "-x", "-R", ",".join(str(part) for part in points), path],
                capture_output=True,
                text=True,
                timeout=20,
            )
            file = Path(path)
            if shot.returncode != 0 or not file.is_file() or file.stat().st_size < 200:
                return {
                    "ok": False,
                    "error": "截屏失败。请在「系统设置 → 隐私与安全性 → 屏幕录制」里允许 Cubes。",
                }
            # Enlarge small controls, but never beyond 1280 wide (4x is enough to read a 320 px
            # crop without turning a 20 px one into a metre of JPEG noise).
            target = min(MAX_WIDTH, max(crop_w, crop_w * 4))
            subprocess.run(
                ["sips", "--resampleWidth", str(target), "-s", "format", "jpeg", path],
                capture_output=True,
                text=True,
                timeout=20,
            )
            encoded = base64.b64encode(file.read_bytes()).decode("ascii")
        finally:
            Path(path).unlink(missing_ok=True)
        return {
            "ok": True,
            "image": encoded,
            "mime": "image/jpeg",
            "screen": {"width": shot_w, "height": shot_h},
            "zoom": {"x": x, "y": y, "width": width, "height": height},
        }

    def _photograph(self) -> tuple[bool, str, dict]:
        handle, path = tempfile.mkstemp(suffix=".jpg")
        os.close(handle)
        try:
            shot = subprocess.run(
                ["screencapture", "-x", "-t", "jpg", path],
                capture_output=True,
                text=True,
                timeout=20,
            )
            file = Path(path)
            if shot.returncode != 0 or not file.is_file() or file.stat().st_size < 800:
                return False, "截屏失败。请在「系统设置 → 隐私与安全性 → 屏幕录制」里允许 Cubes。", {}
            width, height = self._shot_size()
            subprocess.run(
                ["sips", "-z", str(height), str(width), path],
                capture_output=True,
                timeout=20,
            )
            encoded = base64.b64encode(file.read_bytes()).decode("ascii")
            return True, encoded, {"x": 0, "y": 0}
        finally:
            Path(path).unlink(missing_ok=True)

    def _points(self, args: dict, x_field: str = "x", y_field: str = "y") -> tuple[float, float]:
        pixels_w, pixels_h, scale = self._display()
        shot_w, shot_h = self._shot_size()
        px = int(args[x_field]) * pixels_w / shot_w
        py = int(args[y_field]) * pixels_h / shot_h
        return px / scale, py / scale

    def _mouse(self, kind: int, x: float, y: float, button: int = 0) -> None:
        cg = self._core()
        event = cg.CGEventCreateMouseEvent(None, kind, _Point(x, y), button)
        if not event:
            raise ValueError("没法移动鼠标。")
        cg.CGEventPost(0, event)
        cg.CFRelease(event)

    def _scroll(self, dx: int, dy: int) -> None:
        cg = self._core()
        cg.CGEventCreateScrollWheelEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_int32,
        ]
        event = cg.CGEventCreateScrollWheelEvent(None, 1, 2, dy, dx)
        if not event:
            raise ValueError("没法滚动。")
        cg.CGEventPost(0, event)
        cg.CFRelease(event)

    def _type(self, text: str) -> None:
        for chunk in text.split("\n"):
            if chunk:
                result = _osa(f'tell application "System Events" to keystroke {_quote(chunk)}')
                if result.returncode != 0:
                    raise ValueError(self._access_error(result.stderr))
            if chunk != text.split("\n")[-1]:
                self._key("return")

    def _key(self, raw: str) -> None:
        parts = [part.strip() for part in raw.split("+") if part.strip()]
        if not parts:
            raise ValueError("没有要按的键。")
        mods = []
        key = parts[-1]
        for part in parts:
            name = _MODIFIERS.get(part.lower())
            if name is not None and part is not parts[-1]:
                mods.append(name)
            elif part is not parts[-1] and part.lower() not in _MODIFIERS:
                key = part
        using = " using {" + ", ".join(mods) + "}" if mods else ""
        code = _KEY_CODES.get(key.lower())
        if code is None and len(key) == 1:
            script = f'tell application "System Events" to keystroke {_quote(key)}{using}'
        elif code is None:
            raise ValueError(f"不认识的按键「{raw}」。")
        else:
            script = f'tell application "System Events" to key code {code}{using}'
        result = _osa(script)
        if result.returncode != 0:
            raise ValueError(self._access_error(result.stderr))

    def _require_accessibility(self) -> None:
        result = _osa('tell application "System Events" to get unix id of first process whose frontmost is true')
        if result.returncode != 0:
            raise ValueError(self._access_error(result.stderr))

    @staticmethod
    def _access_error(stderr: str) -> str:
        text = (stderr or "").strip()
        if "不允许" in text or "not allowed" in text.lower() or "1002" in text:
            return "这台 Mac 还没允许 Cubes 控制键鼠。请在「系统设置 → 隐私与安全性 → 辅助功能」里打开。"
        return text or "没法操作这台 Mac 的键鼠。"

    def _shot_size(self) -> tuple[int, int]:
        try:
            pixels_w, pixels_h, _scale = self._display()
        except (OSError, AttributeError):
            return MAX_WIDTH, 800
        if pixels_w <= MAX_WIDTH:
            return pixels_w, pixels_h
        return MAX_WIDTH, max(1, round(pixels_h * MAX_WIDTH / pixels_w))

    def _display(self) -> tuple[int, int, float]:
        cg = self._core()
        display = cg.CGMainDisplayID()
        pixels_w = int(cg.CGDisplayPixelsWide(display))
        pixels_h = int(cg.CGDisplayPixelsHigh(display))
        bounds = cg.CGDisplayBounds(display)
        points_w = bounds.size.width or pixels_w
        scale = pixels_w / points_w if points_w else 1.0
        if pixels_w <= 0 or pixels_h <= 0:
            raise OSError("读不到屏幕尺寸")
        return pixels_w, pixels_h, scale

    def _core(self) -> ctypes.CDLL:
        if self._cg is not None:
            return self._cg
        if sys.platform != "darwin":
            raise OSError("不是 macOS")
        library = ctypes.util.find_library("CoreGraphics")
        if not library:
            raise OSError("没有 CoreGraphics")
        cg = ctypes.cdll.LoadLibrary(library)
        foundation = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
        cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGDisplayBounds.restype = _Rect
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _Point, ctypes.c_uint32]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        foundation.CFRelease.argtypes = [ctypes.c_void_p]
        cg.CFRelease = foundation.CFRelease
        self._cg = cg
        return cg

    @staticmethod
    def _system() -> str:
        if sys.platform == "darwin":
            version = platform.mac_ver()[0]
            return f"macOS {version}" if version else "macOS"
        return platform.system() or "电脑"


def host_permissions() -> dict:
    """Whether this Mac has let the backend see the screen and move the pointer.

    ``needed`` is false off a Mac: there is nothing to authorize. Neither check prompts.
    """
    if sys.platform != "darwin":
        return {"needed": False, "screen": True, "accessibility": True}
    return {"needed": True, "screen": _screen_allowed(prompt=False), "accessibility": _accessibility_allowed(prompt=False)}


def request_host_permissions() -> dict:
    """Ask macOS for both, then open the System Settings pane that is still closed."""
    if sys.platform == "darwin":
        _screen_allowed(prompt=True)
        _accessibility_allowed(prompt=True)
    state = host_permissions()
    if state["needed"] and not state["screen"]:
        _open_privacy("Privacy_ScreenCapture")
    elif state["needed"] and not state["accessibility"]:
        _open_privacy("Privacy_Accessibility")
    return state


def _screen_allowed(*, prompt: bool) -> bool:
    try:
        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        fn = cg.CGRequestScreenCaptureAccess if prompt else cg.CGPreflightScreenCaptureAccess
        fn.restype = ctypes.c_bool
        return bool(fn())
    except (AttributeError, OSError):
        return False


def _accessibility_allowed(*, prompt: bool) -> bool:
    try:
        services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        if not prompt:
            services.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(services.AXIsProcessTrusted())
        services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        key = ctypes.c_void_p.in_dll(services, "kAXTrustedCheckOptionPrompt")
        true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        keys = (ctypes.c_void_p * 1)(key)
        values = (ctypes.c_void_p * 1)(true)
        options = cf.CFDictionaryCreate(
            None,
            keys,
            values,
            1,
            ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"),
            ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks"),
        )
        try:
            return bool(services.AXIsProcessTrustedWithOptions(options))
        finally:
            if options:
                cf.CFRelease.argtypes = [ctypes.c_void_p]
                cf.CFRelease(options)
    except (AttributeError, OSError, ValueError):
        return False


def _open_privacy(anchor: str) -> None:
    subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?{anchor}"],
        check=False,
        timeout=10,
    )
