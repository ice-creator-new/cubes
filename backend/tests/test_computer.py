"""The shared computer against a fake agent that dials in over a real WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    collect_events,
    get_conversation,
    get_run,
    get_workspace,
    last_user_text,
    start_run,
    system_prompt,
    tool_names,
    use_standard_budget,
    wait_for_run,
)
from test_connectors import tool_text, wait_for_awaiting
from test_images import enable_vision

from app.computer import SCREEN_LEASE_SECONDS, ComputerHub

SHOT = b"\xff\xd8\xff\xe0" + b"\x00" * 24
SHOT_LABEL = "[沙盒截图，红圈是鼠标位置]"
BROWSER_SHOT_LABEL = "[浏览器截图，红圈是鼠标位置]"


class FakeAgent:
    """Answers a screen or browser call with a JPEG, a shell call with fake output; ``reply`` may
    override one answer."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        delay: float = 0.0,
        reply: Callable[[str, dict], dict | None] | None = None,
        version: int = 4,
        place: str | None = None,
        name: str = "测试沙盒",
    ) -> None:
        self.url = url.replace("http://", "ws://") + "/computer/agent"
        self.token = token
        self.delay = delay
        self.reply = reply
        self.version = version
        self.place = place
        self.name = name
        self.calls: list[tuple[str, dict]] = []
        self.ws: Any = None
        self.close_code: int | None = None
        self._task: asyncio.Task | None = None

    async def start(self, client) -> "FakeAgent":
        self._task = asyncio.create_task(self._run())
        await wait_connected(client, True)
        return self

    def hello(self) -> dict:
        hello = {
            "type": "hello",
            "version": self.version,
            "name": self.name,
            "os": "Debian GNU/Linux 13 (trixie)",
            "screen": {"width": 1280, "height": 720},
            "view": "http://127.0.0.1:6080/vnc.html",
        }
        if self.place:
            hello["place"] = self.place
        return hello

    async def _run(self) -> None:
        hello = self.hello()
        async with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
            self.ws = ws
            try:
                await ws.send(json.dumps(hello))
                async for raw in ws:
                    message = json.loads(raw)
                    self.calls.append((message["op"], message["args"]))
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    answer = self.reply(message["op"], message["args"]) if self.reply else None
                    if answer == "hang":
                        await ws.close()
                        return
                    if answer is None and message["op"] == "shell":
                        answer = {
                            "ok": True,
                            "exit_code": 0,
                            "timed_out": False,
                            "seconds": 0.1,
                            "cwd": "/home/cubes",
                            "stdout": f"ran: {message['args']['command']}\n",
                            "stderr": "",
                        }
                    answer = answer or {
                        "ok": True,
                        "image": base64.b64encode(SHOT).decode(),
                        "mime": "image/jpeg",
                        "cursor": {"x": 10, "y": 20},
                    }
                    await ws.send(json.dumps({"type": "result", "id": message["id"], **answer}))
            except ConnectionClosed:
                pass
            self.close_code = ws.close_code

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class ConcurrentAgent(FakeAgent):
    """The real agent answers each call on its own task; this fake does the same.

    It exists to prove the backend sends a second run's call while the first one is still
    out: a serial fake would hide exactly the overlap being tested.
    """

    def __init__(self, *args, pace=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pace = pace
        self.in_flight = 0
        self.max_in_flight = 0
        self._answers: set[asyncio.Task] = set()

    async def _run(self) -> None:
        async with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"}) as ws:
            self.ws = ws
            try:
                await ws.send(json.dumps(self.hello()))
                async for raw in ws:
                    message = json.loads(raw)
                    self.calls.append((message["op"], message["args"]))
                    task = asyncio.create_task(self._answer(ws, message))
                    self._answers.add(task)
                    task.add_done_callback(self._answers.discard)
            except ConnectionClosed:
                pass
            self.close_code = ws.close_code
            for task in list(self._answers):
                task.cancel()
            await asyncio.gather(*self._answers, return_exceptions=True)

    async def _answer(self, ws, message: dict) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            wait = self.pace(message["op"]) if self.pace is not None else self.delay
            if wait:
                await asyncio.sleep(wait)
            answer = self.reply(message["op"], message["args"]) if self.reply else None
            if answer is None and message["op"] == "shell":
                answer = {
                    "ok": True,
                    "exit_code": 0,
                    "timed_out": False,
                    "seconds": 0.1,
                    "cwd": "/home/cubes",
                    "stdout": f"ran: {message['args']['command']}\n",
                    "stderr": "",
                }
            answer = answer or {
                "ok": True,
                "image": base64.b64encode(SHOT).decode(),
                "mime": "image/jpeg",
                "cursor": {"x": 10, "y": 20},
            }
            await ws.send(json.dumps({"type": "result", "id": message["id"], **answer}))
        finally:
            self.in_flight -= 1


async def wait_connected(client, connected: bool, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = (await client.get("/computer")).json()
        if state["connected"] is connected:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"computer never became connected={connected}")


async def token_of(client) -> str:
    return (await client.get("/computer")).json()["token"]


def computer(call_id: str, **arguments) -> dict:
    return call(call_id, "computer", arguments)


def shell(call_id: str, **arguments) -> dict:
    return call(call_id, "computer_shell", arguments)


def browser(call_id: str, **arguments) -> dict:
    return call(call_id, "computer_browser", arguments)


PAGE = {
    "ok": True,
    "kind": "page",
    "title": "Example Domain",
    "url": "https://example.com/",
    "tabs": [{"id": "1A2B", "title": "Example Domain", "url": "https://example.com/"}],
    "steps": [{"action": "打开", "ok": True, "result": "已打开 https://example.com"}],
    "elements": "Example Domain\n[1]<a>Learn more</a>",
    "above": 0,
    "below": 1.5,
}


def screenshot_turns(request: dict) -> list[list]:
    """The screenshot follow-up turns in one request, each as its content parts.

    A turn whose pixels were pruned comes back as ``[]``; the two tools have their own labels
    and their own budget of kept pixels.
    """
    found = []
    for message in request["messages"]:
        content = message.get("content")
        if (
            message["role"] == "user"
            and isinstance(content, list)
            and content
            and isinstance(content[0], dict)
            and content[0].get("text") in (SHOT_LABEL, BROWSER_SHOT_LABEL)
        ):
            found.append(content)
        elif message["role"] == "user" and isinstance(content, str) and content.startswith(
            "[较早的沙盒截图"
        ):
            found.append([])
    return found


# ---------------------------------------------------------------------- pairing


def test_only_a_current_sandbox_with_the_right_token_connects(make_backend, upstream) -> None:
    upstream.script = [{"content": "好的。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            stranger = FakeAgent(backend.url, "wrong")
            await stranger._run()
            assert stranger.close_code == 4401
            # Version 1 drove the Mac itself, 2 had no shell or browser, 3 drove Playwright one
            # action at a time: all refused.
            for old in (1, 2, 3):
                outdated = FakeAgent(backend.url, await token_of(client), version=old)
                await outdated._run()
                assert outdated.close_code == 4426
            assert (await client.get("/computer")).json()["connected"] is False

            direct_id = (await get_workspace(client))["direct_conversation_id"]
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            state = await wait_connected(client, True)
            assert (state["name"], state["screen"]) == ("测试沙盒", {"width": 1280, "height": 720})
            assert state["view"] == "http://127.0.0.1:6080/vnc.html"
            # Connected, but this model is not marked as seeing images: no screen tool.
            await wait_for_run(client, (await start_run(client, direct_id, "看看"))["id"])
            await enable_vision(client)
            await wait_for_run(client, (await start_run(client, direct_id, "再看看"))["id"])
            await agent.stop()
            await wait_connected(client, False)
            await wait_for_run(client, (await start_run(client, direct_id, "还在吗"))["id"])

    asyncio.run(body())
    blind, seeing, gone = upstream.requests
    assert {"computer_shell", "computer_browser"} <= set(tool_names(blind))
    assert "computer" not in tool_names(blind)
    assert {"computer", "computer_shell", "computer_browser"} <= set(tool_names(seeing))
    hint = last_user_text(seeing)
    assert "测试沙盒" in hint and "不是用户的电脑" in hint and "ctrl" in hint
    # The hint is written by goal: the shell for data and files, the browser for clicking a page,
    # the screen for desktop programs only, and a tool change after two failures.
    assert hint.index("computer_shell") < hint.index("computer_browser") < hint.index("桌面程序（")
    assert "失败两次就换工具" in hint
    # Each description says the same thing the backend enforces.
    schemas = {tool["function"]["name"]: tool["function"] for tool in seeing["tools"]}
    assert "curl" in schemas["computer_shell"]["description"]
    assert "跳过" in schemas["computer_browser"]["description"]
    assert "桌面程序" in schemas["computer"]["description"]
    assert "会被后台直接拒绝" in schemas["computer"]["description"]
    assert not {"computer", "computer_shell", "computer_browser"} & set(tool_names(gone))
    assert "测试沙盒" not in last_user_text(gone)


def test_the_token_survives_a_restart(make_backend) -> None:
    backend = make_backend()

    async def read() -> str:
        async with client_for(backend) as client:
            return await token_of(client)

    first = asyncio.run(read())
    backend.restart()
    assert asyncio.run(read()) == first


# ---------------------------------------------------------------------- consent


def test_the_first_step_asks_once_and_the_screenshots_reach_the_model(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(computer("c1", action="screenshot")),
        tool_calls(computer("c2", action="click", x=5000, y=10)),
        tool_calls(computer("c3", action="click", x=100, y=200)),
        tool_calls(computer("c4", action="type", text="你好")),
        tool_calls(computer("c5", action="key", text="Return")),
        {"content": "保存好了。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "帮我保存文件")
            awaiting = (await wait_for_awaiting(client, direct_id))["action"]
            assert awaiting["confirmation"]["confirm_label"] == "允许"
            assert "测试沙盒" in awaiting["confirmation"]["prompt"]
            assert agent.calls == []  # nothing happens on the machine before the user allows it
            decided = await client.post(f"/actions/{awaiting['id']}/confirm", json={"approve": True})
            assert decided.json()["summary"] == "看了一眼屏幕"
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"

            actions = [m for m in action_messages(await get_conversation(client, direct_id))]
            # Only the first step asked; the bad coordinate never reached the machine.
            assert [a["action"]["status"] for a in actions] == ["completed", "error", "completed", "completed", "completed"]
            assert all(a["action"]["confirmation"] is None for a in actions[1:])
            assert [len(a["attachments"]) for a in actions] == [1, 0, 1, 1, 1]
            assert agent.calls == [
                ("screenshot", {}),
                ("click", {"x": 100, "y": 200}),
                ("type", {"text": "你好"}),
                ("key", {"keys": "return"}),
            ]
            latest = (await client.get("/computer")).json()
            assert latest["latest_image_id"] == actions[-1]["attachments"][0]["id"]
            assert latest["holder"] is None  # released when the run ended
            await agent.stop()

    asyncio.run(body())
    requests = upstream.requests
    assert "超出截图范围" in tool_text(requests[2], "c2")
    assert json.loads(tool_text(requests[1], "c1"))["cursor"] == {"x": 10, "y": 20}
    # Pixels ride in a labelled turn right after the call; only the newest two keep them.
    first = screenshot_turns(requests[1])
    assert len(first) == 1 and first[0][-1]["type"] == "image_url"
    last = screenshot_turns(requests[5])
    assert [any(p.get("type") == "image_url" for p in turn) for turn in last] == [False, False, True, True]


def test_a_refusal_holds_for_the_rest_of_the_run(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(computer("c1", action="screenshot")),
        tool_calls(computer("c2", action="screenshot")),
        {"content": "好的，不碰电脑。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "看看屏幕")
            awaiting = (await wait_for_awaiting(client, direct_id))["action"]
            decided = await client.post(f"/actions/{awaiting['id']}/confirm", json={"approve": False})
            assert decided.json()["summary"] == "你拒绝了使用沙盒电脑"
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            assert agent.calls == []
            await agent.stop()

    asyncio.run(body())
    assert tool_text(upstream.requests[1], "c1") == '{"ok": false, "denied": true}'
    assert "用户拒绝" in tool_text(upstream.requests[2], "c2")


def test_the_panel_stop_ends_the_run_using_the_sandbox(make_backend, upstream) -> None:
    upstream.script = [tool_calls(computer("c1", action="wait", seconds=5)), {"content": "不该走到这里。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), delay=3.0).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "等一下")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            deadline = time.monotonic() + 5
            while not agent.calls and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert (await client.get("/computer")).json()["holder"]["run_id"] == run["id"]
            stopped = await client.post("/computer/stop", json={})
            assert stopped.status_code == 200 and stopped.json()["holder"] is None
            assert (await get_run(client, run["id"]))["status"] == "stopped"
            await agent.stop()

    asyncio.run(body())
    assert upstream.request_count == 1


# ---------------------------------------------------------------------- sharing


def test_a_second_bot_waits_until_the_first_run_is_done(make_backend, upstream) -> None:
    plans = {"任务甲": [("screenshot", {}), ("click", {"x": 1, "y": 1})], "任务乙": [("move", {"x": 2, "y": 2})]}

    def respond(requests: list[dict], index: int) -> dict:
        request = requests[index - 1]
        goal = next(m["content"] for m in request["messages"] if m["role"] == "user" and "任务" in str(m["content"]))
        plan = plans["任务甲" if "任务甲" in goal else "任务乙"]
        done = sum(1 for m in request["messages"] if m["role"] == "tool")
        if done < len(plan):
            op, args = plan[done]
            return tool_calls(computer(f"{goal[:3]}{done}", action=op, **args))
        return {"content": "做完了。"}

    upstream.script = [respond]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), delay=0.4).start(client)
            workspace = await get_workspace(client)
            created = await client.post("/agents", json={"name": "小乙", "instructions": "帮忙操作电脑"})
            assert created.status_code == 201, created.text
            member_chat = (await client.post(f"/agents/{created.json()['id']}/direct")).json()["id"]

            first = await start_run(client, workspace["direct_conversation_id"], "任务甲")
            asked = (await wait_for_awaiting(client, workspace["direct_conversation_id"]))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            second = await start_run(client, member_chat, "任务乙")
            asked = (await wait_for_awaiting(client, member_chat))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})

            waited = False
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                step = (await get_run(client, second["id"]))["current_step"]
                waited = waited or step.startswith("等「") and step.endswith("用完屏幕")
                if (await get_run(client, first["id"]))["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert waited
            assert (await wait_for_run(client, second["id"]))["status"] == "completed"
            assert [op for op, _ in agent.calls] == ["screenshot", "click", "move"]
            await agent.stop()

    asyncio.run(body())


def test_two_runs_shell_commands_run_side_by_side(make_backend, upstream) -> None:
    def respond(requests: list[dict], index: int) -> dict:
        request = requests[index - 1]
        goal = next(m["content"] for m in request["messages"] if m["role"] == "user" and "任务" in str(m["content"]))
        done = sum(1 for m in request["messages"] if m["role"] == "tool")
        if done == 0:
            return tool_calls(shell(f"{goal[:3]}1", command=f"echo {goal}", timeout=10))
        return {"content": "做完了。"}

    upstream.script = [respond]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await ConcurrentAgent(backend.url, await token_of(client), delay=0.6).start(client)
            workspace = await get_workspace(client)
            created = await client.post("/agents", json={"name": "小乙", "instructions": "帮忙跑命令"})
            assert created.status_code == 201, created.text
            member_chat = (await client.post(f"/agents/{created.json()['id']}/direct")).json()["id"]

            first = await start_run(client, workspace["direct_conversation_id"], "任务甲")
            asked = (await wait_for_awaiting(client, workspace["direct_conversation_id"]))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            second = await start_run(client, member_chat, "任务乙")
            asked = (await wait_for_awaiting(client, member_chat))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})

            assert (await wait_for_run(client, first["id"]))["status"] == "completed"
            assert (await wait_for_run(client, second["id"]))["status"] == "completed"
            # Both commands were out at the sandbox at the same time, not one after the other.
            assert agent.max_in_flight == 2, agent.max_in_flight
            assert [op for op, _args in agent.calls] == ["shell", "shell"]
            await agent.stop()

    asyncio.run(body())


def test_a_shell_does_not_wait_for_the_screen(make_backend, upstream) -> None:
    plans = {"任务甲": computer("甲1", action="screenshot"), "任务乙": shell("乙1", command="echo b", timeout=10)}

    def respond(requests: list[dict], index: int) -> dict:
        request = requests[index - 1]
        goal = next(m["content"] for m in request["messages"] if m["role"] == "user" and "任务" in str(m["content"]))
        key = "任务甲" if "任务甲" in goal else "任务乙"
        done = sum(1 for m in request["messages"] if m["role"] == "tool")
        if done == 0:
            return tool_calls(plans[key])
        return {"content": "做完了。"}

    upstream.script = [respond]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await ConcurrentAgent(
                backend.url,
                await token_of(client),
                pace=lambda op: 0.1 if op == "shell" else 3.0,
            ).start(client)
            workspace = await get_workspace(client)
            created = await client.post("/agents", json={"name": "小乙", "instructions": "帮忙跑命令"})
            assert created.status_code == 201, created.text
            member_chat = (await client.post(f"/agents/{created.json()['id']}/direct")).json()["id"]

            first = await start_run(client, workspace["direct_conversation_id"], "任务甲")
            asked = (await wait_for_awaiting(client, workspace["direct_conversation_id"]))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = (await client.get("/computer")).json()
                if state["holder"] is not None and state["holder"]["run_id"] == first["id"]:
                    break
                await asyncio.sleep(0.02)
            else:  # pragma: no cover - the screen never got held
                raise AssertionError("the screen was never held")

            second = await start_run(client, member_chat, "任务乙")
            asked = (await wait_for_awaiting(client, member_chat))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            completed = await wait_for_run(client, second["id"])
            assert completed["status"] == "completed"
            # The shell finished while the other run still held the screen: no waiting step.
            assert (await get_run(client, first["id"]))["status"] == "running"
            assert (await wait_for_run(client, first["id"]))["status"] == "completed"
            await agent.stop()

    asyncio.run(body())


def test_a_sandbox_browser_op_waits_for_the_other_runs_browser(make_backend, upstream) -> None:
    plans = {
        "任务甲": browser("甲1", actions=[{"action": "open", "url": "https://a.example"}]),
        "任务乙": browser("乙1", actions=[{"action": "open", "url": "https://b.example"}]),
    }

    def respond(requests: list[dict], index: int) -> dict:
        request = requests[index - 1]
        goal = next(m["content"] for m in request["messages"] if m["role"] == "user" and "任务" in str(m["content"]))
        key = "任务甲" if "任务甲" in goal else "任务乙"
        done = sum(1 for m in request["messages"] if m["role"] == "tool")
        if done == 0:
            return tool_calls(plans[key])
        return {"content": "做完了。"}

    upstream.script = [respond]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await ConcurrentAgent(
                backend.url,
                await token_of(client),
                reply=lambda op, _args: PAGE if op == "browser" else None,
                pace=lambda op: 3.0 if op == "browser" else 0.1,
            ).start(client)
            workspace = await get_workspace(client)
            created = await client.post("/agents", json={"name": "小乙", "instructions": "帮忙打开网页"})
            assert created.status_code == 201, created.text
            member_chat = (await client.post(f"/agents/{created.json()['id']}/direct")).json()["id"]

            first = await start_run(client, workspace["direct_conversation_id"], "任务甲")
            asked = (await wait_for_awaiting(client, workspace["direct_conversation_id"]))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if any(op == "browser" for op, _args in agent.calls):
                    break
                await asyncio.sleep(0.02)

            second = await start_run(client, member_chat, "任务乙")
            asked = (await wait_for_awaiting(client, member_chat))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            waited = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                step = (await get_run(client, second["id"]))["current_step"]
                waited = waited or "等前一条命令跑完" in step
                if (await get_run(client, first["id"]))["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert waited
            assert (await wait_for_run(client, first["id"]))["status"] == "completed"
            assert (await wait_for_run(client, second["id"]))["status"] == "completed"
            # One browser batch at a time on the sandbox's single Chromium.
            assert agent.max_in_flight == 1, agent.max_in_flight
            await agent.stop()

    asyncio.run(body())


# ---------------------------------------------------------------------- holding


def test_the_screen_hold_is_a_lease_not_an_ownership() -> None:
    hub = ComputerHub(lambda *_args: None, lambda: "token")

    async def body() -> None:
        first = {"run_id": "a", "conversation_id": "c1", "agent_id": "x", "agent_name": "甲"}
        await hub.acquire(first, stopped=lambda: False, timeout=1)
        assert hub.holder_name("b") == "甲"
        # Touching the screen keeps the lease; silence past it hands the screen to the next run.
        hub.touch_screen("a")
        assert hub.holder_name("b") == "甲"
        first["screen_at"] -= SCREEN_LEASE_SECONDS + 1
        assert hub.holder_name("b") is None
        assert hub.holder_run_id is None
        second = {"run_id": "b", "conversation_id": "c2", "agent_id": "y", "agent_name": "乙"}
        await hub.acquire(second, stopped=lambda: False, timeout=1)
        assert hub.holder_run_id == "b"
        hub.release("b")
        assert hub.holder_run_id is None
        # The panel's view of the holder expires with the lease too.
        await hub.acquire(second, stopped=lambda: False, timeout=1)
        second["screen_at"] -= SCREEN_LEASE_SECONDS + 1
        assert hub.state()["holder"] is None

    asyncio.run(body())


# ---------------------------------------------------------------------- stopping

def test_a_disconnect_mid_step_reaches_the_model_and_drops_the_tools(make_backend, upstream) -> None:
    upstream.script = [tool_calls(computer("c1", action="screenshot")), {"content": "电脑断了。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), reply=lambda *_: "hang").start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "看看屏幕")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            await agent.stop()

    asyncio.run(body())
    assert "沙盒电脑断开了连接" in tool_text(upstream.requests[1], "c1")
    assert not {"computer", "computer_shell", "computer_browser"} & set(tool_names(upstream.requests[1]))


# ---------------------------------------------------------------------- tools


def test_a_bot_that_cannot_see_still_gets_the_shell_and_the_browser(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(computer("c1", action="screenshot")),
        tool_calls(shell("c2", command="ls ~/Desktop")),
        {"content": "桌面是空的。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "看看桌面有什么")
            # The screen call is refused before anything is asked; the shell call then asks once.
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            assert asked["tool_name"] == "computer_shell"
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            assert agent.calls == [("shell", {"command": "ls ~/Desktop", "timeout": 60})]
            await agent.stop()

    asyncio.run(body())
    first = upstream.requests[0]
    assert {"computer_shell", "computer_browser"} <= set(tool_names(first))
    assert "computer" not in tool_names(first)
    assert "看不到屏幕截图" in last_user_text(first)
    assert "computer_shell" in tool_text(upstream.requests[1], "c1")
    # A command's answer is text the model reads directly, and no screenshot follows it.
    answer = tool_text(upstream.requests[2], "c2")
    assert answer.startswith("退出码 0") and "ran: ls ~/Desktop" in answer
    assert screenshot_turns(upstream.requests[2]) == []


def test_shell_browser_and_screen_share_one_consent_and_report_in_their_own_shape(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(shell("c1", command="echo hi", timeout=5)),
        tool_calls(browser("c2", action="open", url="example.com")),
        tool_calls(browser("c3", actions=[{"action": "click"}])),
        tool_calls(
            browser(
                "c4",
                actions=[
                    {"action": "input", "index": 3, "text": "mimo"},
                    {"action": "keys", "keys": "enter"},
                ],
                screenshot=True,
            )
        ),
        tool_calls(computer("c5", action="screenshot")),
        {"content": "都看过了。"},
    ]
    backend = make_backend()

    def reply(op: str, args: dict) -> dict | None:
        if op == "browser":
            page = dict(PAGE)
            if args.get("screenshot"):
                page.update(image=base64.b64encode(SHOT).decode(), mime="image/jpeg")
            return page
        return None

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), reply=reply).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "去 example.com 看看")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            assert asked["tool_name"] == "computer_shell" and "运行命令" in asked["confirmation"]["prompt"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"

            actions = action_messages(await get_conversation(client, direct_id))
            assert [(m["action"]["tool_name"], m["action"]["status"]) for m in actions] == [
                ("computer_shell", "completed"),
                ("computer_browser", "completed"),
                ("computer_browser", "error"),
                ("computer_browser", "completed"),
                ("computer", "completed"),
            ]
            assert actions[0]["action"]["summary"] == "运行「echo hi」，完成"
            # The opened row shows the command exactly as it ran, beside what it printed.
            assert actions[0]["action"]["detail"]["command"] == "echo hi"
            assert "command" not in actions[1]["action"]["detail"]
            assert actions[1]["action"]["detail"]["url"] == "https://example.com/"
            # Only the browser step that asked for one, and the screen step, carry a screenshot.
            assert [len(m["attachments"]) for m in actions] == [0, 0, 0, 1, 1]
            assert agent.calls == [
                ("shell", {"command": "echo hi", "timeout": 5}),
                # A bare single action still works, and a scheme-less address becomes https.
                ("browser", {"actions": [{"action": "open", "url": "https://example.com", "new_tab": False}]}),
                # Several actions go in one call; a lowercase key name reaches the browser in
                # the casing it requires.
                (
                    "browser",
                    {
                        "actions": [
                            {"action": "input", "index": 3, "text": "mimo", "clear": True},
                            {"action": "keys", "keys": "Enter"},
                        ],
                        "screenshot": True,
                    },
                ),
                ("screenshot", {}),
            ]
            await agent.stop()

    asyncio.run(body())
    page = tool_text(upstream.requests[2], "c2")
    assert "✓ 打开" in page and "页面：Example Domain" in page and "[1]<a>Learn more</a>" in page
    assert "下面还有 1.5 屏" in page
    assert "缺少 index" in tool_text(upstream.requests[3], "c3")
    assert len(screenshot_turns(upstream.requests[4])) == 1


# ---------------------------------------------------------------------- guards


def browser_page(*steps: dict, image: bool = False) -> dict:
    """A browser answer carrying the steps the fake sandbox really ran."""
    page = dict(PAGE)
    page["steps"] = [dict(step, result=step.get("result", "ok")) for step in steps]
    if image:
        page.update(image=base64.b64encode(SHOT).decode(), mime="image/jpeg")
    return page


def test_a_batch_runs_its_valid_prefix_and_skips_the_rest_with_the_reason(make_backend, upstream) -> None:
    """One bad step no longer throws the whole call away; the prefix runs, the rest is skipped."""
    upstream.script = [
        tool_calls(
            browser(
                "c1",
                actions=[
                    {"action": "find_text", "text": "票价"},
                    {"action": "bogus"},
                    {"action": "click", "index": 3},
                ],
            )
        ),
        tool_calls(
            browser(
                "c2",
                # A screenshot step is the top-level flag; the field drift (`key`/`text`) and
                # `up` arrive as the browser expects them.
                actions=[
                    {"action": "screenshot"},
                    {"action": "key", "text": "Return"},
                    {"action": "scroll", "up": True},
                ],
            )
        ),
        {"content": "看过了。"},
    ]
    backend = make_backend()

    def reply(op: str, args: dict) -> dict | None:
        if op != "browser":
            return None
        steps = [{"action": step["action"], "ok": True} for step in args["actions"]]
        return browser_page(*steps, image=bool(args.get("screenshot")))

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), reply=reply).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "查一下票价")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            assert [op for op, _ in agent.calls] == ["browser", "browser"]
            assert agent.calls[0] == (
                "browser",
                {"actions": [{"action": "find_text", "text": "票价"}]},
            )
            assert agent.calls[1] == (
                "browser",
                {
                    "actions": [
                        {"action": "keys", "keys": "Enter"},
                        {"action": "scroll", "down": False, "pages": 1.0},
                    ],
                    "screenshot": True,
                },
            )
            await agent.stop()

    asyncio.run(body())
    first = tool_text(upstream.requests[1], "c1")
    assert "✓ 找到文字：ok" in first
    assert "… 第 2 个动作的 action 只能是" in first
    assert "… 第 3 个动作（click）：前面一步无效，没有执行" in first
    assert "✓ 按键" in tool_text(upstream.requests[2], "c2")


def test_a_browser_answer_that_reports_a_failure_is_shown_as_a_failure(make_backend, upstream) -> None:
    """browser-use says ok with the complaint as the content; the backend classifies it."""
    upstream.script = [
        tool_calls(browser("c1", actions=[{"action": "click", "index": 2}])),
        {"content": "没点到。"},
    ]
    backend = make_backend()

    def reply(op: str, args: dict) -> dict | None:
        if op != "browser":
            return None
        return browser_page(
            {"action": "click", "ok": True, "result": "Element index 2 not available - page may have changed."},
            {"action": "search_page", "ok": True, "result": "Text '查询车次' not found"},
        )

    async def body() -> None:
        async with client_for(backend) as client:
            agent = await FakeAgent(backend.url, await token_of(client), reply=reply).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "点查看票价")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            actions = action_messages(await get_conversation(client, direct_id))
            assert actions[0]["action"]["status"] == "completed"
            assert "没成功" in actions[0]["action"]["summary"]
            assert "编号过期" in actions[0]["action"]["summary"]
            await agent.stop()

    asyncio.run(body())
    page = tool_text(upstream.requests[1], "c1")
    assert "✗ 点击：Element index 2 not available" in page and "编号过期" in page
    assert "✗ 页内搜索：Text '查询车次' not found" in page and "没找到" in page
    assert "✓" not in page


def test_the_same_step_on_an_unchanged_page_is_refused_and_then_ends_the_run(make_backend, upstream) -> None:
    """A verbatim repeat that changed nothing is a correctable error; the third one is a limit."""
    repeat = tool_calls(browser("c1", actions=[{"action": "click", "index": 3}]))
    upstream.script = [
        repeat,
        tool_calls(browser("c2", actions=[{"action": "click", "index": 3}])),
        tool_calls(browser("c3", actions=[{"action": "click", "index": 3}])),
        {"content": "做不到了。"},
    ]
    backend = make_backend()

    def reply(op: str, args: dict) -> dict | None:
        return None if op != "browser" else browser_page({"action": "click", "ok": True})

    async def body() -> None:
        async with client_for(backend) as client:
            agent = await FakeAgent(backend.url, await token_of(client), reply=reply).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "点同一个编号")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            # The refusal never reaches the sandbox, and the third try ends the run.
            assert (await wait_for_run(client, run["id"]))["status"] == "limited"
            assert len(agent.calls) == 1
            actions = action_messages(await get_conversation(client, direct_id))
            assert "和上一步完全一样" in actions[1]["action"]["error"]
            assert "重复了两次" in actions[2]["action"]["error"]
            await agent.stop()

    asyncio.run(body())
    assert "和上一步完全一样" in tool_text(upstream.requests[2], "c2")
    # The second call never reached the sandbox, and the third ended the run instead of
    # buying another model call.
    assert upstream.request_count == 3


def test_each_sandbox_tool_has_its_own_budget(make_backend, upstream) -> None:
    """The pixel tool's share cannot be spent by the browser, and vice versa."""
    upstream.script = [
        tool_calls(computer("c1", action="screenshot")),
        tool_calls(computer("c2", action="click", x=100, y=200)),
        {"content": "不该走到这里。"},
    ]
    backend = make_backend(max_computer_screen_steps=1, max_computer_browser_steps=5)
    # Only a finite preset has sandbox caps; the default one is unlimited.
    use_standard_budget(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "点一下")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "limited"
            assert [op for op, _ in agent.calls] == ["screenshot"]
            actions = action_messages(await get_conversation(client, direct_id))
            # The screen's own share is spent; the browser's is untouched, and the run ends.
            assert "沙盒屏幕操作用到了 1 步的上限" in actions[-1]["action"]["error"]
            await agent.stop()

    asyncio.run(body())


def test_driving_the_browser_switches_the_screen_tool_off(make_backend, upstream) -> None:
    """Pixel-driving the browser was forbidden by the prompt and done anyway in 8 of 15 runs."""
    upstream.script = [
        tool_calls(browser("c1", actions=[{"action": "open", "url": "example.com"}])),
        tool_calls(computer("c2", action="click", x=100, y=200)),
        tool_calls(computer("c3", action="key", keys="ctrl+l")),
        tool_calls(computer("c4", action="type", text="baidu.com")),
        # alt+tab is the way out: after it the screen tool works on the desktop program again.
        tool_calls(computer("c5", action="key", keys="alt+tab")),
        tool_calls(computer("c6", action="click", x=300, y=400)),
        {"content": "处理完了。"},
    ]
    backend = make_backend()

    def reply(op: str, args: dict) -> dict | None:
        return None if op != "browser" else browser_page({"action": "open", "ok": True})

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), reply=reply).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "去 example.com 然后点开文件管理器")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            # Only the browser, alt+tab and the click that follows it reach the machine.
            assert [op for op, _ in agent.calls] == ["browser", "key", "click"]
            await agent.stop()

    asyncio.run(body())
    refused = tool_text(upstream.requests[2], "c2")
    assert "屏幕工具不能点网页" in refused and "computer_browser" in refused
    assert "地址栏" in tool_text(upstream.requests[3], "c3")
    assert "地址栏" in tool_text(upstream.requests[4], "c4")


def test_each_op_gets_the_deadline_the_agent_needs() -> None:
    """The backend used to give every op 45 s while the agent allowed a browser batch 150 s."""
    from app import computer as rules

    assert rules.call_seconds("shell", {"command": "make", "timeout": 60}) == 65.0
    assert rules.call_seconds("shell", {"command": "make", "timeout": 600}) == 605.0
    assert rules.call_seconds("browser", {"actions": [{"action": "click"}]}) == 30.0
    assert rules.call_seconds("browser", {"actions": [{"action": "click"}] * 3}) == 90.0
    assert rules.call_seconds("browser", {"actions": [{"action": "click"}] * 5}) == 150.0
    assert rules.call_seconds("browser", {"actions": []}) == 30.0  # a screenshot-only call
    assert rules.call_seconds("click", {"x": 1, "y": 2}) == 20.0
    assert rules.call_seconds("screenshot", {}) == 20.0


def test_the_screen_keeps_two_screenshots_and_the_browser_keeps_one(make_backend, upstream) -> None:
    """Each sandbox tool has its own pixel budget in the context of a reply."""
    upstream.script = [
        tool_calls(computer("c1", action="screenshot")),
        tool_calls(computer("c2", action="click", x=100, y=200)),
        tool_calls(browser("c3", actions=[{"action": "open", "url": "example.com"}], screenshot=True)),
        tool_calls(browser("c4", actions=[{"action": "click", "index": 3}], screenshot=True)),
        {"content": "都看过了。"},
    ]
    backend = make_backend()

    def reply(op: str, args: dict) -> dict | None:
        if op != "browser":
            return None
        return browser_page({"action": "open", "ok": True}, image=bool(args.get("screenshot")))

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), reply=reply).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "看看画面")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            await agent.stop()

    asyncio.run(body())
    turns = screenshot_turns(upstream.requests[4])
    # Two screen turns (the old one keeps its pixels), two browser turns (the older one does not).
    assert [any(part.get("type") == "image_url" for part in turn) for turn in turns] == [
        True,
        True,
        False,
        True,
    ]


def test_the_screen_says_so_when_the_picture_did_not_move(make_backend, upstream) -> None:
    """Two byte-identical screenshots mean the click did nothing; the model has to be told."""
    upstream.script = [
        tool_calls(computer("c1", action="screenshot")),
        tool_calls(computer("c2", action="click", x=336, y=577)),
        {"content": "看过了。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "点一下试试")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            actions = action_messages(await get_conversation(client, direct_id))
            assert "画面和上一步一样" in actions[1]["action"]["summary"]
            await agent.stop()

    asyncio.run(body())
    result = json.loads(tool_text(upstream.requests[2], "c2"))
    assert result["changed"] is False and result["note"] == "画面和上一步一样。"


def test_the_browser_answer_is_trimmed_to_the_viewport(make_backend, upstream) -> None:
    """A 12 KB DOM on every step is replaced by the first 40 elements and a note."""
    upcoming = "\n".join(f"[{index}]<a>链接 {index}</a>" for index in range(1, 46))
    upstream.script = [
        tool_calls(browser("c1", actions=[{"action": "open", "url": "example.com"}])),
        {"content": "看到了。"},
    ]
    backend = make_backend()

    def reply(op: str, args: dict) -> dict | None:
        if op != "browser":
            return None
        page = browser_page({"action": "open", "ok": True})
        page["elements"] = "页面标题\n" + upcoming
        return page

    async def body() -> None:
        async with client_for(backend) as client:
            agent = await FakeAgent(backend.url, await token_of(client), reply=reply).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "看看 example.com")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            await agent.stop()

    asyncio.run(body())
    page = tool_text(upstream.requests[1], "c1")
    assert page.count("<a>链接") == 40
    assert "[40]<a>链接 40</a>" in page and "[41]" not in page
    assert "还有 5 个没显示" in page and "search_page" in page


def test_a_cloud_agent_does_not_replace_the_local_sandbox(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            token = await token_of(client)
            local = await FakeAgent(backend.url, token, name="本机沙盒").start(client)
            cloud = FakeAgent(backend.url, token, place="cloud", name="云端电脑")
            cloud._task = asyncio.create_task(cloud._run())
            deadline = time.monotonic() + 10
            state = {}
            while time.monotonic() < deadline:
                state = (await client.get("/computer")).json()
                if state["machines"]["local"]["connected"] and state["machines"]["cloud"]["connected"]:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(state)
            assert state["target"] == "local"
            assert state["name"] == "本机沙盒"
            switched = await client.patch("/computer/target", json={"target": "cloud"})
            assert switched.status_code == 200, switched.text
            body_json = switched.json()
            assert body_json["target"] == "cloud"
            assert body_json["name"] == "云端电脑"
            assert body_json["machines"]["local"]["connected"] is True
            refused = await client.patch("/computer/target", json={"target": "office"})
            assert refused.status_code == 422
            await local.stop()
            await cloud.stop()

    asyncio.run(body())


def test_allowing_the_computer_clears_the_pending_list(make_backend, upstream) -> None:
    """A yes resumes the card, so the sidebar must drop it before the step finishes."""
    upstream.script = [
        tool_calls(computer("c1", action="screenshot")),
        {"content": "看完了。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client), delay=1.0).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "看一眼屏幕")
            action_id = (await wait_for_awaiting(client, direct_id))["action"]["id"]
            posted = False

            def cleared(events: list[tuple[str, dict]]) -> bool:
                nonlocal posted
                if not posted and any(name == "snapshot" for name, _ in events):
                    posted = True
                    asyncio.get_running_loop().create_task(
                        client.post(f"/actions/{action_id}/confirm", json={"approve": True})
                    )
                return any(
                    name == "decisions" and all(row.get("id") != action_id for row in payload)
                    for name, payload in events
                    if isinstance(payload, list)
                )

            await collect_events(client, "/events", until=cleared, timeout=15)
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            await agent.stop()

    asyncio.run(body())


def test_the_unlimited_preset_has_no_screen_ceiling(make_backend, upstream) -> None:
    """Nine screen steps in one run: the default preset does not stop at the standard 8."""

    def rounds(requests, index):
        if index <= 9:
            return tool_calls(computer(f"c{index}", action="move", x=index * 10, y=index * 10))
        return {"content": "看完了。"}

    upstream.script = [rounds]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "挪十次鼠标")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            done = await wait_for_run(client, run["id"])
            assert done["status"] == "completed", done
            assert done["tool_calls"] == 9
            assert len(agent.calls) == 9
            notices = [
                m["content"]
                for m in (await get_conversation(client, direct_id))["messages"]
                if m["kind"] == "notice"
            ]
            assert not any("上限" in line for line in notices)
            await agent.stop()

    asyncio.run(body())


def test_the_unlimited_preset_has_no_shell_or_total_ceiling(make_backend, upstream) -> None:
    """45 shell steps: past the standard 30 shell and the standard 40 computer steps."""

    def rounds(requests, index):
        if index <= 9:
            start = (index - 1) * 5
            return tool_calls(
                *[shell(f"s{start + step}", command=f"echo {start + step}") for step in range(1, 6)]
            )
        return {"content": "跑完了。"}

    upstream.script = [rounds]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "跑四十五条命令")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            done = await wait_for_run(client, run["id"])
            assert done["status"] == "completed", done
            assert done["tool_calls"] == 45
            assert len(agent.calls) == 45
            await agent.stop()

    asyncio.run(body())


def test_the_standard_preset_stops_the_screen_at_eight_and_says_why(
    make_backend, upstream
) -> None:
    """The finite preset keeps the screen at 8; the chat gets one line naming that limit."""

    def rounds(requests, index):
        if index <= 9:
            return tool_calls(computer(f"c{index}", action="move", x=index * 10, y=index * 10))
        return {"content": "该停了。"}

    upstream.script = [rounds]
    backend = make_backend()
    use_standard_budget(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            agent = await FakeAgent(backend.url, await token_of(client)).start(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "一直挪鼠标")
            asked = (await wait_for_awaiting(client, direct_id))["action"]
            await client.post(f"/actions/{asked['id']}/confirm", json={"approve": True})
            done = await wait_for_run(client, run["id"])
            assert done["status"] == "limited", done
            assert len(agent.calls) == 8
            assert "沙盒屏幕操作用到了 8 步的上限" in str(done["error"])
            messages = (await get_conversation(client, direct_id))["messages"]
            lines = [m["content"] for m in messages if m["kind"] == "notice"]
            reasons = [line for line in lines if "沙盒屏幕操作用到了 8 步的上限" in line]
            # One line, naming the limit and how to continue; not a second step-count line.
            assert len(reasons) == 1
            assert "先停在这里" in reasons[0] and "发「继续」接着做" in reasons[0]
            assert not any("这次任务用了" in line for line in lines)
            await agent.stop()

    asyncio.run(body())


def test_a_long_command_summary_keeps_both_ends() -> None:
    from app.computer import SUMMARY_COMMAND_CHARS, parse_call

    command = "cd ~/Documents/AI-Native/cubes/projects/doc2sheet && flutter build web --release 2>&1 | tail -6"
    call = parse_call("computer_shell", {"command": command}, {"width": 1280, "height": 800}, vision=False)
    quoted = call.done.removeprefix("运行「").removesuffix("」")
    # The cut is in the middle: the start and the end both survive, and the line stays short.
    assert quoted.startswith("cd ~/Documents") and quoted.endswith("| tail -6")
    assert "…" in quoted and len(quoted) <= SUMMARY_COMMAND_CHARS
    # A short command is quoted whole.
    assert parse_call("computer_shell", {"command": "ls -la"}, {}, vision=False).done == "运行「ls -la」"
