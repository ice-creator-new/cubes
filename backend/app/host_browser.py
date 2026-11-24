"""Cubes' own Google Chrome on this Mac, driven over the DevTools protocol.

The browser a bot uses on the host is not the user's everyday Chrome: it is a second Chrome
with its own profile under Cubes' data directory, so the user can watch it, log in to a site
for the bot, and none of the user's own cookies are involved. It stays up between calls and
is launched again if it was closed.

Every run gets its own window in that Chrome, with its own DevTools connection: two runs'
pages, tabs and element numbers never mix, and a run's window closes when the run ends. The
profile is shared, so a login the user did once works for everybody.

Every call sends browser actions by page element number: the answer carries a numbered list of
the interactive elements in the viewport (``data-cubes-ref`` tags the element, so a later
action resolves exactly the element the model saw), plus the page, its title and its tabs.
Screenshots capture the page itself, never the desktop.
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

MAX_WIDTH = 1280
MAX_ELEMENTS_CHARS = 6_000
MAX_TEXT_CHARS = 12_000
MAX_JS_RESULT_CHARS = 4_000
MAX_JS_CODE_CHARS = 8_000
MAX_CONSOLE_ENTRIES = 50
LOAD_TIMEOUT_SECONDS = 20.0
LAUNCH_TIMEOUT_SECONDS = 30.0

# Chrome first, then the two drop-in builds that speak the same DevTools protocol.
_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

# Actions that cannot change the page, so the next index in the same call still means the same
# element. Everything else ends the batch: the later steps would aim at stale numbers.
_CHAIN_SAFE = frozenset(
    {"input", "select", "scroll", "find_text", "search_page", "wait", "read_text", "run_js", "console"}
)

# Walked once per answer. It tags every visible interactive element in the viewport with a
# ``data-cubes-ref`` number, which is what a later click/input resolves against exactly.
_SNAPSHOT_JS = r"""
(() => {
  const SEL = 'a,button,input,select,textarea,[role=button],[role=link],[role=checkbox],'
    + '[role=tab],[role=menuitem],[contenteditable=""],[contenteditable="true"],[onclick]';
  document.querySelectorAll('[data-cubes-ref]').forEach((el) => el.removeAttribute('data-cubes-ref'));
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const clean = (value, max) => String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, max);
  const quoted = (value, max) => {
    const text = clean(value, max);
    return text ? ' ' + JSON.stringify(text) : '';
  };
  const lines = [];
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    const tag = (el.tagName || '').toLowerCase();
    if (!tag) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    if (el.disabled) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') continue;
    const box = el.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) continue;
    if (box.bottom <= 0 || box.right <= 0 || box.top >= vh || box.left >= vw) continue;
    n += 1;
    el.setAttribute('data-cubes-ref', String(n));
    let attrs = '';
    if (type) attrs += ' type=' + JSON.stringify(type);
    const name = el.getAttribute('name');
    if (name) attrs += ' name=' + JSON.stringify(clean(name, 60));
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) attrs += ' placeholder=' + JSON.stringify(clean(placeholder, 60));
    const aria = el.getAttribute('aria-label');
    if (aria) attrs += ' aria-label=' + JSON.stringify(clean(aria, 60));
    const role = el.getAttribute('role');
    if (role) attrs += ' role=' + JSON.stringify(clean(role, 30));
    if (tag === 'a') {
      const href = el.getAttribute('href');
      if (href) attrs += ' href=' + JSON.stringify(clean(href, 160));
    }
    if (el.isContentEditable) attrs += ' contenteditable=true';
    let text = '';
    if (tag === 'input') {
      text = type === 'password' ? '' : clean(el.value, 120);
      if (!text) text = clean(placeholder, 120);
    } else if (tag === 'textarea') {
      text = clean(el.value, 120) || clean(placeholder, 120);
    } else if (tag === 'select') {
      const option = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      text = clean(option ? option.text : '', 120);
    } else {
      text = clean(el.innerText || el.textContent || aria || el.getAttribute('title') || '', 160);
    }
    lines.push('[' + n + ']<' + tag + attrs + '>' + text + '</' + tag + '>');
  }
  const doc = document.documentElement;
  const top = window.scrollY || (doc ? doc.scrollTop : 0) || 0;
  const left = window.scrollX || (doc ? doc.scrollLeft : 0) || 0;
  const height = Math.max(doc ? doc.scrollHeight : 0, document.body ? document.body.scrollHeight : 0);
  const width = Math.max(doc ? doc.scrollWidth : 0, document.body ? document.body.scrollWidth : 0);
  const round = (value) => Math.max(0, Math.round(value * 10) / 10);
  return {
    lines: lines.join('\n'),
    count: n,
    above: vh ? round(top / vh) : 0,
    below: vh ? round((height - top - vh) / vh) : 0,
    title: document.title || '',
    url: location.href || '',
  };
})()
"""

# The element a click or input aims at, scrolled into view; a missing tag is a stale number.
_RESOLVE_JS = r"""
((image) => {
  const el = document.querySelector('[data-cubes-ref="' + image + '"]');
  if (!el) return {stale: true};
  try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
  const box = el.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  const x = Math.min(Math.max(box.left + box.width / 2, 0), Math.max(vw - 1, 0));
  const y = Math.min(Math.max(box.top + box.height / 2, 0), Math.max(vh - 1, 0));
  const label = (el.tagName || '').toLowerCase() + ' '
    + String(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '')
        .replace(/\s+/g, ' ').trim().slice(0, 60);
  return {stale: false, x: x, y: y, label: label.trim()};
})(IMAGE)
"""

_FOCUS_JS = r"""
((args) => {
  const el = document.querySelector('[data-cubes-ref="' + args.image + '"]');
  if (!el) return {stale: true};
  try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
  el.focus();
  if (args.clear) {
    if (typeof el.select === 'function') {
      el.select();
    } else {
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = window.getSelection();
      if (selection) { selection.removeAllRanges(); selection.addRange(range); }
    }
  } else if (typeof el.setSelectionRange === 'function' && typeof el.value === 'string') {
    el.setSelectionRange(el.value.length, el.value.length);
  }
  return {stale: false};
})(ARGS)
"""

_SELECT_JS = r"""
((args) => {
  const el = document.querySelector('[data-cubes-ref="' + args.image + '"]');
  if (!el) return {stale: true};
  if (String(el.tagName).toLowerCase() !== 'select') return {error: '这个编号不是下拉框。'};
  const wanted = String(args.text);
  let chosen = null;
  for (const option of el.options) {
    if (option.value === wanted || String(option.text).trim() === wanted) { chosen = option; break; }
  }
  if (!chosen) {
    const options = Array.from(el.options).slice(0, 10).map((option) => String(option.text).trim());
    return {error: '没有这个选项；可选：' + options.join('、')};
  }
  el.focus();
  el.value = chosen.value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, text: String(chosen.text).trim()};
})(ARGS)
"""

_FIND_JS = r"""
((text) => {
  const needle = String(text);
  const walker = document.createTreeWalker(
    document.body || document.documentElement, NodeFilter.SHOW_TEXT
  );
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.nodeValue || node.nodeValue.indexOf(needle) === -1) continue;
    const el = node.parentElement;
    if (el) { try { el.scrollIntoView({block: 'center'}); } catch (e) {} }
    return {found: true, text: node.nodeValue.replace(/\s+/g, ' ').trim().slice(0, 120)};
  }
  return {found: false, text: 'Text ' + JSON.stringify(needle) + ' not found'};
})(TEXT)
"""

_SEARCH_JS = r"""
((pattern) => {
  const body = document.body ? (document.body.innerText || '') : '';
  let re;
  try {
    re = new RegExp(pattern, 'gi');
  } catch (e) {
    re = new RegExp(String(pattern).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  }
  const matches = body.match(re) || [];
  if (!matches.length) return {found: false, text: 'No matches found'};
  const at = body.search(re);
  const snippet = body.slice(Math.max(0, at - 40), at + 120).replace(/\s+/g, ' ').trim();
  return {found: true, text: '找到 ' + matches.length + ' 处：…' + snippet + '…'};
})(PATTERN)
"""


class BrowserStopped(Exception):
    """The run was stopped while a browser step was waiting."""


class BrowserError(Exception):
    """A sentence for the model about why the browser could not do this step."""


def default_profile_dir() -> Path:
    """``browser-profile/`` beside the database, so each Cubes install keeps its own logins.

    The desktop build's database lives under ``~/Library/Application Support/com.ice.cubes.desktop``;
    a dev backend keeps it under ``backend/data``. ``CUBES_BROWSER_PROFILE`` overrides both,
    which is how the opt-in integration test points at a throwaway profile.
    """
    override = os.environ.get("CUBES_BROWSER_PROFILE")
    if override:
        return Path(override).expanduser()
    try:
        from .config import load_settings

        database = load_settings().database_path
    except Exception:  # a broken .env must not take the whole tool down
        database = Path.home() / "Library" / "Application Support" / "com.ice.cubes.desktop" / "cubes.sqlite3"
    return database.expanduser().parent / "browser-profile"


def _clip(text: str, limit: int) -> str:
    """Head and tail around a note: a script's result and a page's text end meaningfully."""
    if len(text) <= limit:
        return text
    half = (limit - 40) // 2
    return f"{text[:half]}\n…（中间省略 {len(text) - 2 * half} 个字符）…\n{text[-half:]}"


def _js_string(value: Any) -> str:
    """A Python value as a JavaScript literal; the payload is data, never code."""
    return json.dumps(value, ensure_ascii=False)


class _Window:
    """One run's Chrome window: its own page session, its own tabs, console and load count."""

    def __init__(self, endpoint: Callable[[Callable[[], bool]], tuple[int, str]]) -> None:
        # ``endpoint`` asks the process manager for the browser's debug port and opens it if
        # it is not running yet. A window never owns the Chrome process: the manager does.
        self._endpoint = endpoint
        self._lock = threading.RLock()
        self._ws: Any = None
        self._target_id: str | None = None
        self._session_id: str | None = None
        # Every window this run owns; normally one, and a second only when a new tab had to
        # open outside it. Targets are filtered by these ids, so runs never see each other.
        self._windows: set[int] = set()
        self._message_id = 0
        self._console: deque[dict[str, str]] = deque(maxlen=MAX_CONSOLE_ENTRIES)
        # Bumped by Page.loadEventFired, so a navigation can wait for its own load even when
        # the previous document already said "complete".
        self._loads = 0

    # ------------------------------------------------------------------ public

    def call(
        self,
        actions: list[dict[str, Any]],
        *,
        screenshot: bool = False,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run up to five actions and answer with the page as the model needs it.

        A failure is reported inside ``steps`` (the backend classifies it); only a dead browser
        or a stopped run ends the whole call without a page.
        """
        stop = should_stop or (lambda: False)
        with self._lock:
            steps: list[dict[str, Any]] = []
            try:
                self._ensure(stop)
                batch = actions[:5]
                for position, step in enumerate(batch):
                    if stop():
                        raise BrowserStopped
                    name = str(step.get("action") or "")
                    ok, text = self._act(name, step, stop)
                    steps.append({"action": name, "ok": ok, "result": text})
                    if not ok:
                        break
                    remaining = len(batch) - position - 1
                    if remaining and name not in _CHAIN_SAFE:
                        steps.append(
                            {
                                "action": "…",
                                "ok": True,
                                "result": f"页面可能变了，后面 {remaining} 个动作没有执行。",
                            }
                        )
                        break
                state = self._state(stop)
                reply: dict[str, Any] = {
                    "ok": True,
                    "kind": "page",
                    "title": state["title"],
                    "url": state["url"],
                    "tabs": state["tabs"],
                    "steps": steps,
                    "elements": state["elements"],
                    "snapshot": _clip(state["elements"], MAX_ELEMENTS_CHARS),
                    "above": state["above"],
                    "below": state["below"],
                }
                if screenshot:
                    image = self._screenshot(stop)
                    if image:
                        reply["image"] = image
                        reply["mime"] = "image/jpeg"
                return reply
            except BrowserStopped:
                return {"ok": False, "error": "已停止。"}
            except BrowserError:
                raise
            except Exception as exc:  # noqa: BLE001 - the model needs a sentence
                self._drop()
                first = (str(exc).strip().splitlines() or [exc.__class__.__name__])[0]
                raise BrowserError(f"这台 Mac 的浏览器出错了：{first[:200]}。") from exc

    def close(self) -> None:
        """Close this run's window and its connection; the Chrome process stays up."""
        with self._lock:
            if self._ws is not None:
                try:
                    for page in self._pages():
                        target = str(page.get("targetId") or "")
                        if target:
                            self._send("Target.closeTarget", {"targetId": target}, timeout=5)
                except (BrowserError, BrowserStopped):
                    pass
            self._drop()
            self._windows = set()

    # ------------------------------------------------------------------ launch

    def _ensure(self, stop: Callable[[], bool]) -> None:
        if self._ws is not None and self._target_id is not None:
            return
        port, path = self._endpoint(stop)
        if not self._open(port, path):
            raise BrowserError("这台 Mac 的浏览器没有连上。")

    def _open(self, port: int, path: str) -> bool:
        try:
            ws = connect(
                f"ws://127.0.0.1:{port}{path}",
                max_size=None,
                open_timeout=10,
                close_timeout=2,
                ping_interval=None,
                proxy=None,
                # The connection outlives one call, so it is opened directly rather than
                # through ``with connect(...)``; ``legacy`` is what websockets calls that.
                legacy=True,
            )
        except (OSError, TimeoutError, WebSocketException):
            return False
        self._ws = ws
        try:
            self._attach_page()
        except BrowserError:
            self._drop()
            return False
        return True

    def _drop(self) -> None:
        ws, self._ws = self._ws, None
        self._session_id = None
        self._target_id = None
        # The window ids may still be real (a dropped socket, a live Chrome): ``_pages``
        # checks each id against the browser, so a stale one simply finds nothing.
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - closing a broken socket is best effort
                pass

    # ------------------------------------------------------------------ CDP

    def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20.0,
        stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        ws = self._ws
        if ws is None:
            raise BrowserError("浏览器没有连上。")
        self._message_id += 1
        message_id = self._message_id
        payload: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        try:
            ws.send(json.dumps(payload))
        except (ConnectionClosed, OSError) as exc:
            self._drop()
            raise BrowserError("浏览器连接断开了，下一步会重新打开。") from exc
        deadline = time.monotonic() + timeout
        while True:
            if stop is not None and stop():
                raise BrowserStopped
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError("浏览器没有在预期时间内回应。")
            try:
                raw = ws.recv(timeout=min(0.5, remaining))
            except TimeoutError:
                continue
            except (ConnectionClosed, OSError) as exc:
                self._drop()
                raise BrowserError("浏览器连接断开了，下一步会重新打开。") from exc
            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if message.get("id") == message_id:
                error = message.get("error")
                if error:
                    text = str(error.get("message") or "浏览器拒绝了这一步") if isinstance(error, dict) else str(error)
                    raise BrowserError(text.strip()[:300])
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            self._note_event(message)

    def _note_event(self, message: dict[str, Any]) -> None:
        session = message.get("sessionId")
        if session is not None and session != self._session_id:
            return
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return
        if method == "Page.loadEventFired":
            self._loads += 1
            return
        if method == "Runtime.consoleAPICalled":
            level = str(params.get("type") or "")
            if level not in ("error", "warning"):
                return
            parts: list[str] = []
            for arg in params.get("args") or []:
                if not isinstance(arg, dict):
                    continue
                if "value" in arg:
                    parts.append(str(arg["value"]))
                elif arg.get("description"):
                    parts.append(str(arg["description"]))
                elif arg.get("type"):
                    parts.append(str(arg["type"]))
            self._console.append({"level": level, "text": " ".join(parts)[:500], "url": ""})
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            level = str(entry.get("level") or "")
            if level not in ("error", "warning"):
                return
            self._console.append(
                {
                    "level": level,
                    "text": str(entry.get("text") or "")[:500],
                    "url": str(entry.get("url") or "")[:160],
                }
            )
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = details.get("text") or ""
            exception = details.get("exception") or {}
            if exception.get("description"):
                text = str(exception["description"])
            self._console.append({"level": "error", "text": str(text)[:500], "url": str(details.get("url") or "")[:160]})

    def _attach_page(self) -> None:
        """This run's own window: a Chrome window is what keeps two runs' tabs apart."""
        pages = self._pages()
        if pages:
            active = next(
                (page for page in pages if page.get("targetId") == self._target_id), pages[0]
            )
            self._attach(str(active.get("targetId") or ""))
            return
        self._open_window()

    def _new_page(self) -> str:
        """A new tab for this run, always inside one of this run's own windows."""
        if self._target_id:
            # Chrome opens a new tab in its last-used window: focus ours first.
            try:
                self._send("Target.activateTarget", {"targetId": self._target_id}, timeout=5)
            except BrowserError:
                pass
        created = self._send("Target.createTarget", {"url": "about:blank"}, timeout=10)
        target = str(created.get("targetId") or "")
        if target and not self._in_window(target):
            # Another run's window took it: close it and give this run a window of its own.
            try:
                self._send("Target.closeTarget", {"targetId": target}, timeout=5)
            except BrowserError:
                pass
            created = self._send(
                "Target.createTarget", {"url": "about:blank", "newWindow": True}, timeout=10
            )
            target = str(created.get("targetId") or "")
            if target:
                window = self._send("Browser.getWindowForTarget", {"targetId": target}, timeout=10)
                self._windows.add(int(window.get("windowId") or 0))
        return target

    def _open_window(self) -> None:
        """Create a window of this run's own and attach its first page."""
        created = self._send(
            "Target.createTarget", {"url": "about:blank", "newWindow": True}, timeout=10
        )
        target = str(created.get("targetId") or "")
        if not target:
            raise BrowserError("浏览器没有可用的标签页。")
        window = self._send("Browser.getWindowForTarget", {"targetId": target}, timeout=10)
        self._windows = {int(window.get("windowId") or 0)}
        self._attach(target)

    def _attach(self, target_id: str) -> None:
        # Drop the previous tab's session: a long session that switches tabs often would
        # otherwise keep every old one enabled and report its console events.
        previous = self._session_id
        if previous:
            try:
                self._send("Target.detachFromTarget", {"sessionId": previous}, timeout=5)
            except BrowserError:
                pass
        self._session_id = None
        attached = self._send("Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=10)
        session_id = attached.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise BrowserError("连不上浏览器的标签页。")
        self._target_id = target_id
        self._session_id = session_id
        for method in ("Page.enable", "Runtime.enable", "Log.enable"):
            try:
                self._send(method, session_id=session_id, timeout=10)
            except BrowserError:
                # A tab that vanished between listing and attaching is reported by the action.
                continue

    def _eval(
        self,
        expression: str,
        *,
        stop: Callable[[], bool] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        result = self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            session_id=self._session_id,
            timeout=timeout + 2,
            stop=stop,
        )
        if result.get("exceptionDetails"):
            details = result.get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            text = exception.get("description") or details.get("text") or "脚本出错了。"
            raise BrowserError(str(text).splitlines()[0][:300])
        value = result.get("result") or {}
        return value.get("value")

    # ------------------------------------------------------------------ actions

    def _act(
        self, name: str, step: dict[str, Any], stop: Callable[[], bool]
    ) -> tuple[bool, str]:
        if name == "open":
            return self._open_url(str(step.get("url") or ""), bool(step.get("new_tab")), stop)
        if name == "back":
            return self._back(stop)
        if name == "click":
            return self._click(int(step.get("index") or 0), stop)
        if name == "input":
            return self._input(int(step.get("index") or 0), str(step.get("text") or ""), bool(step.get("clear", True)), stop)
        if name == "select":
            return self._select(int(step.get("index") or 0), str(step.get("text") or ""), stop)
        if name == "keys":
            return self._keys(str(step.get("keys") or ""), stop)
        if name == "scroll":
            return self._scroll(bool(step.get("down", True)), float(step.get("pages") or 1), stop)
        if name == "find_text":
            return self._find_text(str(step.get("text") or ""), stop)
        if name == "search_page":
            return self._search_page(str(step.get("pattern") or ""), stop)
        if name == "switch_tab":
            return self._switch_tab(str(step.get("tab_id") or ""), stop)
        if name == "close_tab":
            return self._close_tab(str(step.get("tab_id") or ""), stop)
        if name == "wait":
            self._sleep(float(step.get("seconds") or 2), stop)
            return True, "等完了"
        if name == "read_text":
            return self._read_text(stop)
        if name == "run_js":
            return self._run_js(str(step.get("code") or ""), stop)
        if name == "console":
            return self._console_entries()
        return False, f"不支持的浏览器操作「{name}」。"

    def _open_url(self, url: str, new_tab: bool, stop: Callable[[], bool]) -> tuple[bool, str]:
        if not url:
            return False, "没有网址。"
        if new_tab:
            target = self._new_page()
            if not target:
                return False, "没能打开新标签页。"
            self._attach(target)
        before = self._loads
        previous = self._current_url(stop)
        result = self._send("Page.navigate", {"url": url}, session_id=self._session_id, stop=stop)
        error = str(result.get("errorText") or "")
        if error:
            return False, f"Navigation failed: {error}"
        self._wait_navigation(previous, before, stop)
        return True, f"已打开 {url}"

    def _back(self, stop: Callable[[], bool]) -> tuple[bool, str]:
        history = self._send("Page.getNavigationHistory", session_id=self._session_id, stop=stop)
        entries = history.get("entries") or []
        index = int(history.get("currentIndex") or 0)
        if index <= 0 or index - 1 >= len(entries):
            return False, "已经是第一页了。"
        entry = entries[index - 1]
        before = self._loads
        previous = self._current_url(stop)
        self._send(
            "Page.navigateToHistoryEntry",
            {"entryId": entry.get("id")},
            session_id=self._session_id,
            stop=stop,
        )
        self._wait_navigation(previous, before, stop)
        return True, f"已返回 {entry.get('url') or '上一页'}"

    def _click(self, index: int, stop: Callable[[], bool]) -> tuple[bool, str]:
        found = self._eval(_RESOLVE_JS.replace("IMAGE", _js_string(index)), stop=stop)
        if not isinstance(found, dict) or found.get("stale"):
            return False, "编号过期，请重新看页面"
        x, y = float(found.get("x") or 0), float(found.get("y") or 0)
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params: dict[str, Any] = {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
            if kind != "mouseMoved":
                params["buttons"] = 1
            self._send("Input.dispatchMouseEvent", params, session_id=self._session_id, stop=stop)
        self._after_change(stop)
        label = str(found.get("label") or "").strip()
        return True, f"已点击 [{index}]" + (f" {label}" if label else "")

    def _input(
        self, index: int, text: str, clear: bool, stop: Callable[[], bool]
    ) -> tuple[bool, str]:
        focused = self._eval(
            _FOCUS_JS.replace("ARGS", json.dumps({"image": index, "clear": clear})), stop=stop
        )
        if not isinstance(focused, dict) or focused.get("stale"):
            return False, "编号过期，请重新看页面"
        if text:
            self._send(
                "Input.insertText",
                {"text": text},
                session_id=self._session_id,
                stop=stop,
            )
        shown = text if len(text) <= 40 else text[:39] + "…"
        if not text:
            return True, "已清空" if clear else "没有输入文字"
        return True, f"已输入「{shown}」"

    def _select(self, index: int, text: str, stop: Callable[[], bool]) -> tuple[bool, str]:
        args = json.dumps({"image": index, "text": text})
        result = self._eval(_SELECT_JS.replace("ARGS", args), stop=stop)
        if not isinstance(result, dict):
            return False, "选择失败了。"
        if result.get("stale"):
            return False, "编号过期，请重新看页面"
        if result.get("error"):
            return False, str(result["error"])
        return True, f"已选择「{result.get('text') or text}」"

    def _keys(self, keys: str, stop: Callable[[], bool]) -> tuple[bool, str]:
        if not keys.strip():
            return False, "没有要按的键。"
        self._dispatch_keys(keys, stop)
        self._after_change(stop)
        return True, f"已按 {keys}"

    def _scroll(self, down: bool, pages: float, stop: Callable[[], bool]) -> tuple[bool, str]:
        step = max(0.1, min(pages, 10.0)) * (1 if down else -1)
        expression = (
            "window.scrollBy({top: (window.innerHeight || 800) * "
            + repr(float(step))
            + ", left: 0, behavior: 'instant'}); "
            "Math.round(window.scrollY || document.documentElement.scrollTop || 0);"
        )
        top = self._eval(expression, stop=stop)
        self._sleep(0.2, stop)
        return True, f"已向{'下' if down else '上'}滚动 {abs(pages)} 屏，当前位置 {top} 像素"

    def _find_text(self, text: str, stop: Callable[[], bool]) -> tuple[bool, str]:
        result = self._eval(_FIND_JS.replace("TEXT", _js_string(text)), stop=stop)
        if not isinstance(result, dict):
            return False, "没找到。"
        if not result.get("found"):
            return False, str(result.get("text") or f"Text {json.dumps(text)} not found")
        self._sleep(0.2, stop)
        return True, f"已滚到「{result.get('text') or text}」"

    def _search_page(self, pattern: str, stop: Callable[[], bool]) -> tuple[bool, str]:
        result = self._eval(_SEARCH_JS.replace("PATTERN", _js_string(pattern)), stop=stop)
        if not isinstance(result, dict):
            return False, "No matches found"
        return bool(result.get("found")), str(result.get("text") or "")

    def _switch_tab(self, tab_id: str, stop: Callable[[], bool]) -> tuple[bool, str]:
        pages = self._pages()
        target = next((page for page in pages if str(page.get("targetId") or "").endswith(tab_id)), None)
        if target is None:
            return False, "没有这个标签页。"
        self._attach(str(target.get("targetId")))
        self._wait_ready(stop, timeout=5)
        return True, f"已切到 {target.get('title') or target.get('url') or tab_id}"

    def _close_tab(self, tab_id: str, stop: Callable[[], bool]) -> tuple[bool, str]:
        pages = self._pages()
        target = next((page for page in pages if str(page.get("targetId") or "").endswith(tab_id)), None)
        if target is None:
            return False, "没有这个标签页。"
        target_id = str(target.get("targetId"))
        closing_active = target_id == self._target_id
        self._send("Target.closeTarget", {"targetId": target_id}, stop=stop)
        if closing_active:
            remaining = [page for page in pages if page.get("targetId") != target_id]
            if remaining:
                self._attach(str(remaining[-1].get("targetId")))
            else:
                self._open_window()
        return True, f"已关闭标签页 {tab_id}"

    def _read_text(self, stop: Callable[[], bool]) -> tuple[bool, str]:
        text = self._eval(
            "(document.body ? (document.body.innerText || '') : '').trim()", stop=stop
        )
        body = str(text or "")
        if not body:
            return True, "（页面没有可见文字）"
        return True, _clip(body, MAX_TEXT_CHARS)

    def _run_js(self, code: str, stop: Callable[[], bool]) -> tuple[bool, str]:
        if not code.strip():
            return False, "没有要执行的脚本。"
        if len(code) > MAX_JS_CODE_CHARS:
            return False, f"脚本不能超过 {MAX_JS_CODE_CHARS} 个字符。"
        try:
            value = self._eval(code, stop=stop, timeout=10)
        except BrowserError as exc:
            return False, str(exc)
        try:
            shown = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            shown = str(value)
        return True, _clip(shown if shown is not None else "undefined", MAX_JS_RESULT_CHARS)

    def _console_entries(self) -> tuple[bool, str]:
        if not self._console:
            return True, "最近的错误和警告：没有。"
        lines = []
        for entry in self._console:
            where = f"（{entry['url']}）" if entry.get("url") else ""
            lines.append(f"[{entry['level']}] {entry['text']}{where}")
        return True, _clip("最近的错误和警告：\n" + "\n".join(lines), MAX_JS_RESULT_CHARS)

    # ------------------------------------------------------------------ keys

    def _dispatch_keys(self, keys: str, stop: Callable[[], bool]) -> None:
        parts = [part.strip() for part in keys.split("+") if part.strip()]
        if not parts:
            raise BrowserError("没有要按的键。")
        modifiers = 0
        bits = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
        names: list[str] = []
        for part in parts:
            canonical = part[:1].upper() + part[1:] if len(part) > 1 else part
            if canonical in bits:
                modifiers |= bits[canonical]
            else:
                names.append(part)
        if not names:
            names = ["Control"]
        for name in names:
            key, code, code_point, text = _key_spec(name, modifiers)
            down: dict[str, Any] = {
                # A printable character is typed; a named key must reach the browser's own
                # default action (Enter submits, Tab moves focus), so it carries no text.
                "type": "keyDown" if text else "rawKeyDown",
                "key": key,
                "code": code,
                "modifiers": modifiers,
                "windowsVirtualKeyCode": code_point,
                "nativeVirtualKeyCode": code_point,
            }
            if text and not modifiers & (1 | 2 | 4):
                down["text"] = text
                down["unmodifiedText"] = text
            self._send("Input.dispatchKeyEvent", down, session_id=self._session_id, stop=stop)
            self._send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": key,
                    "code": code,
                    "modifiers": modifiers,
                    "windowsVirtualKeyCode": code_point,
                    "nativeVirtualKeyCode": code_point,
                },
                session_id=self._session_id,
                stop=stop,
            )

    # ------------------------------------------------------------------ state

    def _state(self, stop: Callable[[], bool]) -> dict[str, Any]:
        data = self._eval(_SNAPSHOT_JS, stop=stop, timeout=10)
        if not isinstance(data, dict):
            data = {}
        title = str(data.get("title") or "")
        url = str(data.get("url") or "")
        if not title and not url:
            # An error page or a tab without a document: ask the target itself.
            for page in self._pages():
                if page.get("targetId") == self._target_id:
                    title = str(page.get("title") or "")
                    url = str(page.get("url") or "")
        return {
            "title": title,
            "url": url,
            "tabs": self._tabs(),
            "elements": str(data.get("lines") or ""),
            "above": int(round(float(data.get("above") or 0))),
            "below": int(round(float(data.get("below") or 0))),
        }

    def _pages(self) -> list[dict[str, Any]]:
        """The page targets in this run's windows; another run's tabs never show up."""
        infos = self._send("Target.getTargets", timeout=10).get("targetInfos") or []
        pages: list[dict[str, Any]] = []
        for item in infos:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            target = str(item.get("targetId") or "")
            if target and self._in_window(target):
                pages.append(dict(item))
        return pages

    def _in_window(self, target_id: str) -> bool:
        if not self._windows:
            return False
        try:
            window = self._send("Browser.getWindowForTarget", {"targetId": target_id}, timeout=5)
        except BrowserError:
            return False
        return int(window.get("windowId") or 0) in self._windows

    def _tabs(self) -> list[dict[str, str]]:
        return [
            {
                "id": str(page.get("targetId") or "")[-4:],
                "title": str(page.get("title") or ""),
                "url": str(page.get("url") or ""),
            }
            for page in self._pages()
        ]

    # ------------------------------------------------------------------ waits

    def _current_url(self, stop: Callable[[], bool]) -> str:
        try:
            return str(self._eval("location.href", stop=stop, timeout=5) or "")
        except BrowserError:
            return ""

    def _wait_navigation(
        self, previous: str, before_loads: int, stop: Callable[[], bool]
    ) -> None:
        """Wait for the navigation's own load, not the previous document's readyState.

        ``Page.loadEventFired`` is the signal; when a page never fires it (a download, a frame
        document) a changed address plus ``readyState == complete`` is accepted after a grace.
        """
        deadline = time.monotonic() + LOAD_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if stop():
                raise BrowserStopped
            try:
                current = str(self._eval("location.href", stop=stop, timeout=5) or "")
                ready = self._eval("document.readyState", stop=stop, timeout=5)
            except BrowserError:
                self._sleep(0.2, stop)
                continue
            if self._loads > before_loads or (ready == "complete" and current != previous):
                self._wait_ready(stop, timeout=5)
                return
            self._sleep(0.2, stop)

    def _wait_ready(self, stop: Callable[[], bool], *, timeout: float = LOAD_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop():
                raise BrowserStopped
            try:
                ready = self._eval("document.readyState", stop=stop, timeout=5)
            except BrowserError:
                # A navigation destroys the execution context mid-wait; the next poll runs in
                # the new document.
                self._sleep(0.2, stop)
                continue
            if ready == "complete":
                return
            self._sleep(0.2, stop)

    def _after_change(self, stop: Callable[[], bool]) -> None:
        """Let a click or key settle; wait for the load only if it started a navigation."""
        self._sleep(0.25, stop)
        try:
            ready = self._eval("document.readyState", stop=stop, timeout=5)
        except BrowserError:
            # Most likely the click navigated and this document's context is already gone.
            self._wait_ready(stop, timeout=10)
            return
        if ready != "complete":
            self._wait_ready(stop, timeout=10)

    @staticmethod
    def _sleep(seconds: float, stop: Callable[[], bool]) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if stop():
                raise BrowserStopped
            time.sleep(min(0.1, remaining))

    # ------------------------------------------------------------------ screenshot

    def _screenshot(self, stop: Callable[[], bool]) -> str:
        result = self._send(
            "Page.captureScreenshot",
            {"format": "jpeg", "quality": 70, "fromSurface": True, "captureBeyondViewport": False},
            session_id=self._session_id,
            timeout=20,
            stop=stop,
        )
        encoded = result.get("data")
        if not isinstance(encoded, str) or not encoded:
            return ""
        return _downscale_jpeg(encoded)


def _active_port(profile: Path) -> tuple[int | None, str | None]:
    """The debug port a Chrome with this profile wrote, if it is still there."""
    try:
        lines = (profile / "DevToolsActivePort").read_text().splitlines()
    except OSError:
        return None, None
    if len(lines) < 2:
        return None, None
    try:
        return int(lines[0].strip()), lines[1].strip()
    except ValueError:
        return None, None


def _listening(port: int) -> bool:
    """Whether something still answers on a port a previous Chrome wrote down."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _chrome_binary() -> str:
    override = os.environ.get("CUBES_CHROME")
    candidates = (override,) if override else _BROWSERS
    for path in candidates:
        if path and Path(path).is_file():
            return str(path)
    raise BrowserError(
        "这台 Mac 没找到 Google Chrome。请安装 Chrome、Chromium 或 Edge，"
        "或者把 Chrome 的路径写进 CUBES_CHROME。"
    )


class _Chrome:
    """The one Chrome process behind every run's window.

    Every run's window is its own CDP connection, so two runs' batches proceed side by side;
    only the process and the profile are shared, which is what keeps the user's logins.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def endpoint(self, profile: Path) -> Callable[[Callable[[], bool]], tuple[int, str]]:
        def ensure(stop: Callable[[], bool]) -> tuple[int, str]:
            return self._ensure(profile, stop)

        return ensure

    def _ensure(self, profile: Path, stop: Callable[[], bool]) -> tuple[int, str]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None
            # A Chrome an earlier backend run left up with the same profile is reused rather
            # than fought over: the profile lock would refuse a second one.
            port, path = _active_port(profile)
            if port is None or path is None or not _listening(port):
                self._launch(profile, stop)
                port, path = _active_port(profile)
            if port is None or path is None:
                raise BrowserError("浏览器没有在 30 秒内启动起来。")
            return port, path

    def _launch(self, profile: Path, stop: Callable[[], bool]) -> None:
        binary = _chrome_binary()
        (profile / "DevToolsActivePort").unlink(missing_ok=True)
        args = [
            binary,
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-features=Translate,TranslateUI",
            "--window-size=1440,900",
        ]
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._proc = proc
        deadline = time.monotonic() + LAUNCH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if stop():
                self.stop()
                raise BrowserStopped
            if proc.poll() is not None:
                self._proc = None
                raise BrowserError("浏览器启动后立刻退出了；可能这个 Chrome 配置目录被别的窗口占用了。")
            port, path = _active_port(profile)
            if port is not None and path is not None:
                return
            time.sleep(0.1)
        self.stop()
        raise BrowserError("浏览器没有在 30 秒内启动起来。")

    def stop(self) -> None:
        """Backend shutdown only: the user's own Chrome is never touched."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass


class HostBrowser:
    """The Chrome this backend started, with one window per run."""

    def __init__(self, profile_dir: Path | str | None = None) -> None:
        self._profile = Path(profile_dir).expanduser() if profile_dir is not None else None
        self._lock = threading.RLock()
        self._chrome = _Chrome()
        self._runs: dict[str, _Window] = {}

    def call(
        self,
        actions: list[dict[str, Any]],
        *,
        screenshot: bool = False,
        should_stop: Callable[[], bool] | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Run up to five actions in ``run_id``'s own window."""
        return self._window(run_id).call(
            actions, screenshot=screenshot, should_stop=should_stop
        )

    def has_window(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs

    def release(self, run_id: str) -> None:
        """The run ended: close its window; every other run's window stays."""
        with self._lock:
            window = self._runs.pop(run_id, None)
        if window is not None:
            window.close()

    def close(self) -> None:
        """Backend shutdown: every run's window and the Chrome Cubes started go away."""
        with self._lock:
            windows, self._runs = list(self._runs.values()), {}
        for window in windows:
            window.close()
        self._chrome.stop()

    def _window(self, run_id: str) -> _Window:
        with self._lock:
            window = self._runs.get(run_id)
            if window is not None:
                return window
            profile = self._profile or default_profile_dir()
            self._profile = profile
            profile.mkdir(parents=True, exist_ok=True)
            window = _Window(self._chrome.endpoint(profile))
            self._runs[run_id] = window
            return window


# Named keys as CDP wants them: (key, code, virtual key code, text).
_NAMED_KEYS = {
    "Enter": ("Enter", "Enter", 13, ""),
    "Tab": ("Tab", "Tab", 9, ""),
    "Escape": ("Escape", "Escape", 27, ""),
    "Backspace": ("Backspace", "Backspace", 8, ""),
    "Delete": ("Delete", "Delete", 46, ""),
    "Space": (" ", "Space", 32, " "),
    "Home": ("Home", "Home", 36, ""),
    "End": ("End", "End", 35, ""),
    "PageUp": ("PageUp", "PageUp", 33, ""),
    "PageDown": ("PageDown", "PageDown", 34, ""),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, ""),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, ""),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, ""),
    "Insert": ("Insert", "Insert", 45, ""),
}
_ALIASES = {"return": "Enter", "esc": "Escape", "del": "Delete", "spacebar": "Space"}


def _key_spec(name: str, modifiers: int) -> tuple[str, str, int, str]:
    """``key``/``code``/virtual key code/text for one key name or single character."""
    named = _NAMED_KEYS.get(name) or _NAMED_KEYS.get(_ALIASES.get(name.lower(), ""))
    if named is not None:
        return named
    if len(name) == 1:
        char = name
        if char.isalpha():
            upper = char.upper()
            return char, f"Key{upper}", ord(upper), char
        if char.isdigit():
            return char, f"Digit{char}", ord(char), char
        return char, "", 0, char
    match = re.fullmatch(r"F(\d{1,2})", name, re.IGNORECASE)
    if match:
        number = int(match.group(1))
        if 1 <= number <= 12:
            return f"F{number}", f"F{number}", 111 + number, ""
    raise BrowserError(f"不认识的按键「{name}」。")


def _downscale_jpeg(encoded: str) -> str:
    """A page screenshot at most 1280 pixels wide; Retina captures are twice that."""
    try:
        data = base64.b64decode(encoded, validate=False)
    except (ValueError, TypeError):
        return ""
    handle, path = tempfile.mkstemp(suffix=".jpg")
    os.close(handle)
    try:
        Path(path).write_bytes(data)
        size = subprocess.run(
            ["sips", "-g", "pixelWidth", path], capture_output=True, text=True, timeout=20
        )
        match = re.search(r"pixelWidth:\s*(\d+)", size.stdout or "")
        width = int(match.group(1)) if match else 0
        if width > MAX_WIDTH:
            subprocess.run(
                ["sips", "--resampleWidth", str(MAX_WIDTH), path],
                capture_output=True,
                text=True,
                timeout=20,
            )
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")
    finally:
        Path(path).unlink(missing_ok=True)
