"""Connectors against a real MCP server: saving, offering tools, calling them, and consent."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from app.connector_presets import PRESETS, connection_for
from app.connectors import search_path
from conftest import ServerThread
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    get_conversation,
    get_workspace,
    last_user_text,
    start_run,
    store_of,
    system_prompt,
    tool_names,
    wait_for_run,
)
from mcp_demo_server import http_app

DEMO = str(Path(__file__).with_name("mcp_demo_server.py"))


def tool_text(request: dict, call_id: str) -> str:
    """The raw text one tool result carried back to the model."""
    return next(
        m["content"] for m in request["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == call_id
    )
from mcp_demo_server import http_app

DEMO = str(Path(__file__).with_name("mcp_demo_server.py"))


def local_body(name: str = "演示", **extra) -> dict:
    return {"preset": "custom-stdio", "name": name, "command": sys.executable, "args": [DEMO], **extra}


async def add(client, body: dict) -> dict:
    response = await client.post("/connectors", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def wait_for_awaiting(client, conversation_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for message in action_messages(await get_conversation(client, conversation_id)):
            if message["action"]["status"] == "awaiting":
                return message
        await asyncio.sleep(0.05)
    raise AssertionError("no awaiting action appeared")


@pytest.fixture
def demo_http():
    server = ServerThread(http_app()).start()
    try:
        yield f"{server.base_url}/mcp"
    finally:
        server.stop()


# ---------------------------------------------------------------------- saving


def test_a_local_server_is_saved_with_its_tools_and_secrets_stay_hidden(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await add(client, local_body(env={"DEMO_MARKER": "秘密值"}))
            assert created["status"] == "connected"
            assert created["slug"] == "mcp"
            assert {tool["name"]: tool["read_only"] for tool in created["tools"]} == {
                "list_issues": True,
                "create_issue": False,
                "explode": True,
            }
            assert created["env_names"] == ["DEMO_MARKER"]
            listing = (await client.get("/connectors")).json()
            assert "秘密值" not in str(listing)
            assert {preset["id"] for preset in listing["presets"]} >= {"github", "feishu", "custom-http"}

            taken = await client.post("/connectors", json=local_body())
            assert taken.status_code == 409
            assert taken.json()["detail"] == "连接器名称「演示」已被使用，请换一个。"

    asyncio.run(body())


def test_a_connection_that_fails_is_never_saved(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            missing = await client.post(
                "/connectors",
                json={"preset": "custom-stdio", "name": "坏的", "command": "definitely-not-a-command-xyz"},
            )
            assert missing.status_code == 502
            assert missing.json()["detail"] == "找不到命令「definitely-not-a-command-xyz」，请确认它已安装，或填写完整路径。"

            crashed = await client.post("/connectors", json=local_body("崩溃", args=[DEMO, "crash"]))
            assert crashed.status_code == 502
            assert crashed.json()["detail"] == "本地服务启动后退出了：缺少配置文件 demo.json。"

            bad_url = await client.post(
                "/connectors", json={"preset": "custom-http", "name": "远程", "url": "ftp://x"}
            )
            assert bad_url.status_code == 422
            assert bad_url.json()["detail"] == "请填写以 http:// 或 https:// 开头的地址。"

            no_token = await client.post("/connectors", json={"preset": "github"})
            assert no_token.status_code == 422
            assert no_token.json()["detail"] == "请填写Token。"
            assert (await client.get("/connectors")).json()["connectors"] == []

    asyncio.run(body())


def test_a_rejected_token_is_explained_and_a_good_one_connects(make_backend, demo_http) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            refused = await client.post(
                "/connectors",
                json={"preset": "custom-http", "name": "远程", "url": demo_http,
                      "headers": {"Authorization": "Bearer wrong"}},
            )
            assert refused.status_code == 502
            assert refused.json()["detail"] == (
                "服务拒绝了凭据（HTTP 401），请检查 token 是否正确、是否过期、权限是否足够。"
            )
            created = await add(
                client,
                {"preset": "custom-http", "name": "远程", "url": demo_http,
                 "headers": {"Authorization": "Bearer good-token"}},
            )
            assert created["status"] == "connected"
            assert created["header_names"] == ["Authorization"]

            # A broken edit leaves the working connection in place.
            broken = await client.patch(
                f"/connectors/{created['id']}", json={"headers": {"Authorization": "Bearer nope"}}
            )
            assert broken.status_code == 502
            after = (await client.get("/connectors")).json()["connectors"][0]
            assert after["status"] == "connected"
            assert store_of(backend).get_connector(created["id"])["config"]["headers"] == {
                "Authorization": "Bearer good-token"
            }

    asyncio.run(body())


def test_disabling_drops_the_tools_and_enabling_reconnects(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "第一轮"}, {"content": "第二轮"}]

    async def body() -> None:
        async with client_for(backend) as client:
            created = await add(client, local_body())
            off = await client.patch(f"/connectors/{created['id']}", json={"enabled": False})
            assert off.json()["status"] == "disabled"
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await wait_for_run(client, (await start_run(client, direct_id, "看看"))["id"])
            on = await client.patch(f"/connectors/{created['id']}", json={"enabled": True})
            assert on.json()["status"] == "connected"
            await wait_for_run(client, (await start_run(client, direct_id, "再看看"))["id"])

    asyncio.run(body())
    first, second = upstream.requests
    assert not any("__" in name for name in tool_names(first))
    assert "mcp__list_issues" in tool_names(second)
    assert "已连接的外部服务：演示" in last_user_text(second)


def test_enabled_connectors_come_back_after_a_restart(make_backend) -> None:
    backend = make_backend()

    async def first() -> None:
        async with client_for(backend) as client:
            await add(client, local_body())

    asyncio.run(first())
    backend.restart()

    async def second() -> None:
        async with client_for(backend) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = (await client.get("/connectors")).json()["connectors"][0]["status"]
                if status == "connected":
                    return
                await asyncio.sleep(0.1)
            raise AssertionError(f"still {status}")

    asyncio.run(second())


# ---------------------------------------------------------------------- calls


def test_a_read_tool_runs_at_once_and_its_errors_reach_the_model(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(
            call("call_1", "mcp__list_issues", {"repo": "cubes"}),
            call("call_2", "mcp__explode", {}),
        ),
        {"content": "整理好了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await add(client, local_body())
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "cubes 有哪些问题")
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            actions = [m["action"] for m in action_messages(await get_conversation(client, direct_id))]
            assert [(a["status"], a["summary"]) for a in actions] == [
                ("completed", "已调用 演示「列出问题」"),
                ("error", "调用 演示「explode」失败：演示 返回了错误：仓库不存在"),
            ]
            assert actions[0]["detail"]["text"].startswith("cubes 的问题：#1 登录失败")

    asyncio.run(body())
    assert "仓库不存在" in tool_text(upstream.requests[1], "call_2")


def test_a_write_tool_waits_for_consent(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "mcp__create_issue", {"repo": "cubes", "title": "加连接器"})),
        tool_calls(call("call_2", "mcp__list_issues", {"repo": "cubes"})),
        {"content": "已创建"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await add(client, local_body())
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "帮我建个 issue")
            awaiting = await wait_for_awaiting(client, direct_id)
            action = awaiting["action"]
            assert action["confirmation"]["confirm_label"] == "允许"
            assert '"title": "加连接器"' in action["confirmation"]["prompt"]
            decided = await client.post(f"/actions/{action['id']}/confirm", json={"approve": True})
            assert decided.status_code == 200, decided.text
            assert decided.json()["summary"] == "已调用 演示「创建问题」"
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"

    asyncio.run(body())
    # The issue really exists: the follow-up read in the same server process sees it.
    assert "#3 加连接器" in tool_text(upstream.requests[2], "call_2")


def test_a_denied_write_never_reaches_the_server(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "mcp__create_issue", {"repo": "cubes", "title": "不该出现"})),
        tool_calls(call("call_2", "mcp__list_issues", {"repo": "cubes"})),
        {"content": "好的，不建了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await add(client, local_body())
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "帮我建个 issue")
            action = (await wait_for_awaiting(client, direct_id))["action"]
            decided = await client.post(f"/actions/{action['id']}/confirm", json={"approve": False})
            assert decided.json()["summary"] == "你拒绝了调用 演示「创建问题」"
            await wait_for_run(client, run["id"])

    asyncio.run(body())
    assert tool_text(upstream.requests[1], "call_1") == '{"ok": false, "denied": true}'
    assert "不该出现" not in tool_text(upstream.requests[2], "call_2")


# ---------------------------------------------------------------------- presets


def test_read_only_presets_ask_the_vendor_for_read_only_sessions() -> None:
    github = connection_for("github", {"values": {"token": "ghp_x"}}, False)
    assert github.headers == {"Authorization": "Bearer ghp_x", "X-MCP-Readonly": "true"}
    assert "X-MCP-Readonly" not in connection_for("github", {"values": {"token": "ghp_x"}}, True).headers
    assert connection_for("linear", {"values": {"token": "k"}}, False).url.endswith("/mcp/readonly")
    assert "read_only=true" in connection_for("supabase", {"values": {"token": "s"}}, False).url
    feishu = connection_for("feishu", {"values": {"app_id": "cli_a", "app_secret": "s"}}, False)
    assert feishu.headers["X-Lark-MCP-Allowed-Tools"] == "fetch-doc,fetch-file,get-comments,list-docs,get-user"
    assert feishu.feishu == ("cli_a", "s")
    assert connection_for("sentry", {"values": {"token": "t"}}, True).headers == {
        "Authorization": "Sentry-Bearer t"
    }
    # Every non-custom preset builds a connection from its required fields alone.
    for preset in PRESETS:
        if preset.custom:
            continue
        values = {f.id: "x" for f in preset.fields if f.required}
        built = connection_for(preset.id, {"values": values}, False)
        assert built.url.startswith("https://") or built.command == "npx"


def test_local_commands_find_tools_outside_the_gui_path(monkeypatch, tmp_path) -> None:
    bin_dir = tmp_path / ".nvm" / "versions" / "node" / "v22.0.0" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert str(bin_dir) in search_path().split(":")
