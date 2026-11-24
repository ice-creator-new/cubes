"""The host's own Chrome, its new browser actions, screen zoom and the prompts that pick them.

The round trip at the bottom launches a real Chrome window on this Mac; it is opt-in because a
headless or locked-down machine cannot run it: ``CUBES_TEST_CHROME=1 pytest -q
backend/tests/test_host_browser.py``.
"""

from __future__ import annotations

import base64
import os
import re
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.computer import (
    BROWSER_ACTIONS,
    BROWSER_SCHEMA,
    SCREEN_SCHEMA,
    SandboxCall,
    parse_call,
    prompt_hint,
)
from app.models import ActionDetailOut
from app.toolkit import ToolInputError
from app.tools.computer import _browser_outcome

SCREEN = {"width": 1280, "height": 720}


# ------------------------------------------------------------------ prompts


def test_the_host_hint_sends_pages_to_its_browser_and_images_to_view_image() -> None:
    hint = prompt_hint("这台 Mac", "macOS 26", SCREEN, vision=True, host=True)
    assert "computer_browser" in hint and "read_text" in hint and "run_js" in hint
    assert "view_image" in hint
    assert "Chrome" in hint and "Cubes 自己的 Chrome" in hint
    # The old limitation is gone: the host browser lists numbered elements now.
    assert "没有元素编号" not in hint
    assert "DevTools" in hint and "Preview" in hint
    # The browser is the tool for a page, before the pixel screen tool.
    assert hint.index("网页（打开") < hint.index("看屏幕")


def test_view_image_is_only_offered_to_a_bot_that_sees() -> None:
    blind = prompt_hint("这台 Mac", "macOS 26", SCREEN, vision=False, host=True)
    assert "view_image" not in blind
    seeing = prompt_hint("Cubes 沙盒", "Debian", SCREEN, vision=True)
    assert "view_image" in seeing and "computer_browser" in seeing
    assert "不支持的浏览器" not in seeing


# ------------------------------------------------------------------ schema


def test_the_browser_schema_advertises_reading_and_debugging() -> None:
    assert {"read_text", "run_js", "console"} <= set(BROWSER_ACTIONS)
    action = BROWSER_SCHEMA["function"]["parameters"]["properties"]["actions"]["items"]["properties"]["action"]
    assert {"read_text", "run_js", "console"} <= set(action["enum"])
    item = BROWSER_SCHEMA["function"]["parameters"]["properties"]["actions"]["items"]["properties"]
    assert "code" in item


def test_run_js_read_text_and_console_parse() -> None:
    call = parse_call(
        "computer_browser",
        {
            "actions": [
                {"action": "read_text"},
                {"action": "run_js", "code": "document.title"},
                {"action": "console"},
            ]
        },
        SCREEN,
        vision=False,
    )
    assert call.args == {
        "actions": [
            {"action": "read_text"},
            {"action": "run_js", "code": "document.title"},
            {"action": "console"},
        ]
    }
    assert "执行脚本" in call.done
    # A weak model puts the script in ``text`` or ``script``; both mean code.
    drifted = parse_call(
        "computer_browser", {"actions": [{"action": "run_js", "text": "1+1"}]}, SCREEN, vision=False
    )
    assert drifted.args["actions"][0]["code"] == "1+1"


def test_run_js_without_code_is_reported_with_its_reason() -> None:
    with pytest.raises(ToolInputError) as caught:
        parse_call("computer_browser", {"actions": [{"action": "run_js"}]}, SCREEN, vision=False)
    assert "需要 code" in str(caught.value)


# ------------------------------------------------------------------ zoom


def test_screen_zoom_is_a_region_and_its_schema() -> None:
    action = SCREEN_SCHEMA["function"]["parameters"]["properties"]["action"]
    assert "zoom" in action["enum"]
    props = SCREEN_SCHEMA["function"]["parameters"]["properties"]
    assert {"width", "height"} <= set(props)
    call = parse_call(
        "computer",
        {"action": "zoom", "x": 100, "y": 80, "width": 320, "height": 200},
        SCREEN,
        vision=True,
    )
    assert call.op == "zoom"
    assert call.args == {"x": 100, "y": 80, "width": 320, "height": 200}
    assert "放大" in call.done


def test_a_zoom_region_outside_the_screenshot_is_refused() -> None:
    with pytest.raises(ToolInputError):
        parse_call(
            "computer", {"action": "zoom", "x": 1200, "y": 700, "width": 200, "height": 100}, SCREEN, vision=True
        )
    with pytest.raises(ToolInputError):
        parse_call(
            "computer", {"action": "zoom", "x": 10, "y": 10, "width": 4, "height": 4}, SCREEN, vision=True
        )


# ------------------------------------------------------------------ detail


def test_a_browser_answer_keeps_its_snapshot_in_the_row_detail() -> None:
    snapshot = '[1]<a href="https://example.com/next">下一页</a>\n[2]<button>提交</button>'
    reply = {
        "ok": True,
        "kind": "page",
        "title": "Example Domain",
        "url": "https://example.com/",
        "tabs": [],
        "steps": [],
        "elements": snapshot,
        "snapshot": snapshot,
        "above": 0,
        "below": 0,
    }
    call = SandboxCall("browser", {"actions": [{"action": "open", "url": "https://example.com/"}]}, "打开 example.com")
    outcome = _browser_outcome(call, reply, [])
    assert outcome.detail is not None
    assert outcome.detail["kind"] == "page"
    assert outcome.detail["snapshot"] == snapshot
    wire = ActionDetailOut(**outcome.detail).model_dump(mode="json")
    assert wire["snapshot"] == snapshot
    # Only a page carries it; every other kind keeps its older wire shape.
    assert "snapshot" not in ActionDetailOut(kind="text", text="输出").model_dump(mode="json")
    # The contract caps the stored snapshot at 6000 characters.
    reply["snapshot"] = "x" * 7_000
    assert len(_browser_outcome(call, reply, []).detail["snapshot"]) == 6_000


# ------------------------------------------------------------------ real Chrome

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Cubes 浏览器测试</title>
<script>
console.warn("加载时的警告");
console.error("加载时的错误");
</script></head>
<body>
<h1>Cubes 浏览器测试</h1>
<a href="/next">下一页</a>
<form id="f" onsubmit="event.preventDefault();
  const name = document.querySelector('input[name=name]').value;
  const city = document.querySelector('select[name=city]');
  document.getElementById('out').textContent = '你好 ' + name + '（' + city.options[city.selectedIndex].text + '）';">
  <label>姓名 <input name="name" placeholder="你的名字"></label>
  <label>城市 <select name="city">
    <option value="sh">上海</option>
    <option value="bj">北京</option>
  </select></label>
  <button type="submit">提交</button>
</form>
<div id="out"></div>
</body></html>"""


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _index_of(snapshot: str, marker: str) -> int:
    match = re.search(r"\[(\d+)\][^\n]*" + re.escape(marker), snapshot)
    assert match is not None, f"{marker} not in:\n{snapshot}"
    return int(match.group(1))


def test_a_real_chrome_round_trip(tmp_path) -> None:
    if os.environ.get("CUBES_TEST_CHROME") != "1":
        pytest.skip("set CUBES_TEST_CHROME=1 to launch a real Chrome window")
    from app.host_browser import HostBrowser

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_PageHandler))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    browser = HostBrowser(profile_dir=tmp_path / "browser-profile")
    try:
        opened = browser.call([{"action": "open", "url": url}], should_stop=lambda: False)
        assert opened["ok"] is True, opened
        assert opened["title"] == "Cubes 浏览器测试"
        assert opened["url"] == url
        snapshot = opened["elements"]
        assert 'placeholder="你的名字"' in snapshot
        assert "提交" in snapshot
        assert "[1]<" in snapshot
        name_index = _index_of(snapshot, 'placeholder="你的名字"')
        city_index = _index_of(snapshot, "上海")
        button_index = _index_of(snapshot, "提交")

        typed = browser.call(
            [{"action": "input", "index": name_index, "text": "张三", "clear": True}], should_stop=lambda: False
        )
        assert typed["steps"][0]["ok"] is True, typed["steps"]

        chosen = browser.call(
            [{"action": "select", "index": city_index, "text": "北京"}], should_stop=lambda: False
        )
        assert chosen["steps"][0]["ok"] is True, chosen["steps"]

        clicked = browser.call([{"action": "click", "index": button_index}], should_stop=lambda: False)
        assert clicked["steps"][0]["ok"] is True, clicked["steps"]

        text = browser.call([{"action": "read_text"}], should_stop=lambda: False)
        assert "你好 张三（北京）" in text["steps"][0]["result"], text["steps"]

        evaluated = browser.call(
            [{"action": "run_js", "code": "document.querySelector('h1').textContent"}], should_stop=lambda: False
        )
        assert evaluated["steps"][0]["result"] == '"Cubes 浏览器测试"'

        # Console messages emitted while the evaluate above was in flight are kept.
        browser.call(
            [
                {
                    "action": "run_js",
                    "code": "console.warn('运行时警告'); console.error('运行时错误'); 'ok'",
                }
            ],
            should_stop=lambda: False,
        )
        console = browser.call([{"action": "console"}], should_stop=lambda: False)
        assert "运行时警告" in console["steps"][0]["result"]
        assert "运行时错误" in console["steps"][0]["result"]

        shot = browser.call([{"action": "read_text"}], screenshot=True, should_stop=lambda: False)
        data = base64.b64decode(shot["image"])
        assert data[:2] == b"\xff\xd8"  # JPEG
        assert shot["mime"] == "image/jpeg"

        stale = browser.call([{"action": "click", "index": 9_999}], should_stop=lambda: False)
        assert stale["steps"][0]["ok"] is False
        assert "编号过期" in stale["steps"][0]["result"]

        # Tabs are listed by their last four characters; switching re-attaches to that page.
        first_tab = opened["tabs"][0]["id"]
        extra = browser.call(
            [{"action": "open", "url": url + "next", "new_tab": True}], should_stop=lambda: False
        )
        assert len(extra["tabs"]) == 2, extra["tabs"]
        assert extra["url"] == url + "next"
        extra_tab = next(tab["id"] for tab in extra["tabs"] if tab["url"] == url + "next")
        back = browser.call([{"action": "switch_tab", "tab_id": first_tab}], should_stop=lambda: False)
        assert back["steps"][0]["ok"] is True, back["steps"]
        assert back["url"] == url
        closed = browser.call([{"action": "close_tab", "tab_id": extra_tab}], should_stop=lambda: False)
        assert closed["steps"][0]["ok"] is True, closed["steps"]
        assert len(closed["tabs"]) == 1, closed["tabs"]
    finally:
        browser.close()
        server.shutdown()
        server.server_close()


def test_two_runs_get_separate_windows_and_never_see_each_others_tabs(tmp_path) -> None:
    if os.environ.get("CUBES_TEST_CHROME") != "1":
        pytest.skip("set CUBES_TEST_CHROME=1 to launch a real Chrome window")
    from app.host_browser import HostBrowser

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_PageHandler))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    browser = HostBrowser(profile_dir=tmp_path / "browser-profile")
    try:
        first = browser.call(
            [{"action": "open", "url": url + "?run=a"}],
            should_stop=lambda: False,
            run_id="a",
        )
        second = browser.call(
            [{"action": "open", "url": url + "?run=b"}],
            should_stop=lambda: False,
            run_id="b",
        )
        assert first["url"].endswith("?run=a"), first["url"]
        assert second["url"].endswith("?run=b"), second["url"]
        # Each run lists its own window's tabs only.
        assert [tab["url"] for tab in first["tabs"]] == [url + "?run=a"]
        assert [tab["url"] for tab in second["tabs"]] == [url + "?run=b"]
        first_tab = first["tabs"][0]["id"]
        assert first_tab != second["tabs"][0]["id"]
        crossed = browser.call(
            [{"action": "switch_tab", "tab_id": first_tab}],
            should_stop=lambda: False,
            run_id="b",
        )
        assert crossed["steps"][0]["ok"] is False, crossed["steps"]
        assert "没有这个标签页" in crossed["steps"][0]["result"]
        # b's page is still its own after a's window is closed.
        browser.release("a")
        assert browser.has_window("a") is False and browser.has_window("b") is True
        text = browser.call([{"action": "read_text"}], should_stop=lambda: False, run_id="b")
        assert text["ok"] is True and "Cubes 浏览器测试" in text["steps"][0]["result"]
        # A released run asks for a window again on its next call.
        again = browser.call(
            [{"action": "open", "url": url + "?run=a2"}],
            should_stop=lambda: False,
            run_id="a",
        )
        assert again["url"].endswith("?run=a2"), again["url"]
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
