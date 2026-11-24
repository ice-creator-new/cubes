"""Cubes' own tools, offered to a bot that runs on the user's Claude Code or Codex CLI.

A CLI bot does its thinking inside the CLI, so Cubes hands it an MCP endpoint per turn
(``app.cli_tools``). The fake CLI below is the real client shape: it reads ``--mcp-config``
out of its argv, speaks JSON-RPC over HTTP, and answers in Claude Code's event format. The
tests run against a real uvicorn app, so the socket, the token, the action row and the store
are all the ones the desktop app uses.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import httpx
import pytest

from helpers import client_for, get_conversation, start_run, store_of, wait_for_run

from app import cli_engines
from app.agent.run_state import ActiveRun
from app.cli_tools import Hub, ToolBridge
from app.providers import ResolvedModel

# The fake speaks the CLI's stream-json output and the MCP wire protocol; nothing else.
FAKE_CLI = '''#!__PYTHON__
import json, os, sys, urllib.request

argv = sys.argv[1:]
sys.stdin.read()

def emit(event):
    print(json.dumps(event), flush=True)

if "--version" in argv:
    print("9.9.9 (Fake CLI)")
    sys.exit(0)
if "auth" in argv and "status" in argv:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth_token"}))
    sys.exit(0)

out_path = os.environ.get("FAKE_MCP_OUT", "")
call = os.environ.get("FAKE_MCP_CALL", "")
arguments = json.loads(os.environ.get("FAKE_MCP_ARGS", "{}"))
url = ""
headers = {}
if "--mcp-config" in argv:
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    server = (config.get("mcpServers") or {}).get("cubes") or {}
    url = server.get("url", "")
    headers = dict(server.get("headers") or {})

def rpc(body):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None

tools = []
result = ""
if url:
    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "fake-cli", "version": "1"}}})
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = [tool.get("name") for tool in ((listed or {}).get("result") or {}).get("tools") or []]
    if call:
        answer = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": call, "arguments": arguments}})
        content = ((answer or {}).get("result") or {}).get("content") or []
        result = "".join(part.get("text", "") for part in content)
if out_path:
    with open(out_path, "w") as handle:
        json.dump({"tools": tools, "result": result, "url": url}, handle, ensure_ascii=False)

emit({"type": "system", "subtype": "init", "session_id": "s-mcp"})
emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
    "delta": {"type": "text_delta", "text": "好的，"}}})
emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
    "delta": {"type": "text_delta", "text": "已经处理。"}}})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "好的，已经处理。"}],
    "usage": {"input_tokens": 1, "output_tokens": 1}}})
emit({"type": "result", "subtype": "success", "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 2}})
'''


@pytest.fixture
def fake_mcp_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake as ``claude`` and point both CLIs' command names at it."""
    cli_engines._STATUS_CACHE.clear()
    script = tmp_path / "claude"
    script.write_text(FAKE_CLI.replace("__PYTHON__", sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("CUBES_CLAUDE_BIN", str(script))
    monkeypatch.setenv("CUBES_CODEX_BIN", str(script))
    return script


async def choose_cli_model(client: httpx.AsyncClient) -> dict:
    """Make a Claude Code provider the workspace default; the supervisor then runs on it."""
    created = await client.post(
        "/providers",
        json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": ["默认"]},
    )
    assert created.status_code == 201, created.text
    provider = created.json()
    chosen = await client.put(
        "/providers/default", json={"provider_id": provider["id"], "model": "默认"}
    )
    assert chosen.status_code == 200, chosen.text
    return provider


async def rpc(
    client: httpx.AsyncClient, bridge: ToolBridge, method: str, params: dict | None = None,
    *, ident: int = 1,
) -> dict:
    """One JSON-RPC POST to a bridge, exactly as the CLI sends it."""
    body: dict = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        body["params"] = params
    response = await client.post(
        bridge.url,
        json=body,
        headers={"Accept": "application/json, text/event-stream"},
        timeout=60.0,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def tool_names(client: httpx.AsyncClient, bridge: ToolBridge) -> list[str]:
    answer = await rpc(client, bridge, "tools/list")
    return [tool["name"] for tool in answer["result"]["tools"]]


# ---------------------------------------------------------------- the whole loop


def test_a_claude_code_supervisor_creates_a_member_through_cubes_tools(
    make_backend, fake_mcp_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The acceptance case: the CLI turn calls Cubes' create_agent over MCP and it happens."""
    out = tmp_path / "mcp.json"
    monkeypatch.setenv("FAKE_MCP_OUT", str(out))
    monkeypatch.setenv("FAKE_MCP_CALL", "create_agent")
    monkeypatch.setenv(
        "FAKE_MCP_ARGS",
        json.dumps({"name": "测试员", "instructions": "你负责检查每次改动是否可用。"}, ensure_ascii=False),
    )
    backend = make_backend()
    store = store_of(backend)
    chat = store.ensure_direct_conversation((store.supervisor())["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            await choose_cli_model(client)
            run = await start_run(client, chat["id"], "请建一个叫「测试员」的成员")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", json.dumps(finished, ensure_ascii=False)
            assert finished["tool_calls"] == 1

            detail = await get_conversation(client, chat["id"])
            actions = [message for message in detail["messages"] if message.get("action")]
            assert len(actions) == 1, json.dumps(detail["messages"], ensure_ascii=False)
            assert actions[0]["action"]["tool_name"] == "create_agent"
            assert actions[0]["action"]["status"] == "completed"
            assert actions[0]["content"] == "创建了成员「测试员」"
            assert actions[0]["status"] == "complete"

    asyncio.run(body())

    members = [agent for agent in store.list_agents() if agent["kind"] == "member"]
    assert [agent["name"] for agent in members] == ["测试员"]
    written = json.loads(out.read_text())
    assert "create_agent" in written["tools"]
    assert json.loads(written["result"])["ok"] is True


# ------------------------------------------------------------------- tool lists


def test_the_endpoint_lists_only_the_tools_that_bot_was_offered(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    engine = backend.app.state.engine
    hub: Hub = backend.app.state.cli_tools
    supervisor = store.supervisor()
    member = store.create_agent(name="小测", instructions="你负责测试。", kind="member")
    direct = store.direct_conversation()

    async def body() -> None:
        async with client_for(backend) as client:
            await client.get("/workspace")  # the app learns its own port from a request
            assert hub.origin.startswith("http://127.0.0.1:")

            offered = engine._schemas("supervisor", direct, supervisor)
            supervisor_bridge = hub.begin(
                engine,
                ActiveRun(run_id="", conversation_id=direct["id"]),
                direct,
                supervisor,
                offered,
            )
            assert supervisor_bridge is not None
            try:
                listed = await tool_names(client, supervisor_bridge)
            finally:
                supervisor_bridge.close()
            assert listed == [schema["function"]["name"] for schema in offered]
            assert "create_agent" in listed

            member_chat = store.ensure_private_conversation(supervisor["id"], member["id"])
            offered_member = engine._schemas("member", member_chat, member)
            member_bridge = hub.begin(
                engine,
                ActiveRun(run_id="", conversation_id=member_chat["id"]),
                member_chat,
                member,
                offered_member,
            )
            assert member_bridge is not None
            try:
                listed_member = await tool_names(client, member_bridge)
            finally:
                member_bridge.close()
            assert listed_member == [schema["function"]["name"] for schema in offered_member]
            assert "create_agent" not in listed_member
            assert "notify_user" in listed_member
            # A closed turn's token is dead at once.
            assert listed_member != listed

    asyncio.run(body())


# ----------------------------------------------------------------------- cards


def test_a_card_tool_parks_on_the_users_decision(make_backend) -> None:
    """delete_agent waits for the card, exactly as it does for an API-model bot."""
    backend = make_backend()
    store = store_of(backend)
    engine = backend.app.state.engine
    hub: Hub = backend.app.state.cli_tools
    supervisor = store.supervisor()
    member = store.create_agent(name="要删的人", instructions="临时成员。", kind="member")
    direct = store.direct_conversation()
    run, _ = store.begin_run(direct["id"], "把要删的人删掉")
    active = ActiveRun(run_id=run["id"], conversation_id=direct["id"])
    bridge: ToolBridge | None = None

    async def body() -> None:
        nonlocal bridge
        async with client_for(backend) as client:
            await client.get("/workspace")
            bridge = hub.begin(
                engine, active, direct, supervisor,
                engine._schemas("supervisor", direct, supervisor),
            )
            assert bridge is not None
            task = asyncio.create_task(
                rpc(
                    client,
                    bridge,
                    "tools/call",
                    {"name": "delete_agent", "arguments": {"agent_id": member["id"], "reason": "测试"}},
                    ident=3,
                )
            )
            deadline = time.monotonic() + 15
            awaiting = None
            while time.monotonic() < deadline:
                detail = await get_conversation(client, direct["id"])
                waiting = [
                    message
                    for message in detail["messages"]
                    if message.get("action") and message["action"]["status"] == "awaiting"
                ]
                if waiting:
                    awaiting = waiting[0]
                    break
                assert not task.done(), "the call returned before the card was answered"
                await asyncio.sleep(0.05)
            assert awaiting is not None, "no confirmation card appeared"
            action_id = awaiting["action"]["id"]
            # Nothing happened yet: the member is still there.
            assert store.get_agent(member["id"]) is not None

            confirmed = await client.post(f"/actions/{action_id}/confirm", json={"approve": True})
            assert confirmed.status_code == 200, confirmed.text
            answer = await asyncio.wait_for(task, timeout=15)
            assert json.loads(answer["result"]["content"][0]["text"])["ok"] is True

    asyncio.run(body())
    assert bridge is not None
    bridge.close()
    assert store.get_agent(member["id"]) is None


# ---------------------------------------------------------------------- tokens


def test_a_wrong_or_expired_token_is_refused(make_backend) -> None:
    # The launch token guards the rest of the API; this path proves itself with its own.
    backend = make_backend(auth_token="9a3f0c7d5b1e4826a0c3f7d9e2b50814")
    store = store_of(backend)
    engine = backend.app.state.engine
    hub: Hub = backend.app.state.cli_tools
    supervisor = store.supervisor()
    direct = store.direct_conversation()

    async def body() -> None:
        headers = {"Authorization": "Bearer 9a3f0c7d5b1e4826a0c3f7d9e2b50814"}
        async with httpx.AsyncClient(base_url=backend.url, timeout=30.0, trust_env=False) as owner:
            await owner.get("/workspace", headers=headers)
            assert hub.origin
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "x", "version": "1"},
                },
            }
            accept = {"Accept": "application/json, text/event-stream"}
            # No launch token: the turn token alone is enough for the endpoint itself.
            async with client_for(backend) as cli:
                for token in ("", "not-a-token", "0" * 43):
                    response = await cli.post(
                        f"{hub.origin}/cli-tools/{token}", json=init, headers=accept
                    )
                    assert response.status_code == 401, token
                    assert "已失效" in response.text

                bridge = hub.begin(
                    engine, ActiveRun(run_id="", conversation_id=direct["id"]), direct, supervisor,
                    engine._schemas("supervisor", direct, supervisor),
                )
                assert bridge is not None
                listed = await cli.post(bridge.url, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/list"
                }, headers=accept)
                assert listed.status_code == 200, listed.text
                assert {tool["name"] for tool in listed.json()["result"]["tools"]} >= {"create_agent"}

                bridge.expires_at = 0.0  # one tick past the turn: the token ages out
                response = await cli.post(bridge.url, json=init, headers=accept)
                assert response.status_code == 401
                bridge.close()

    asyncio.run(body())


# ----------------------------------------------------------------------- argv


def test_the_cli_arguments_point_at_the_turn_endpoint() -> None:
    """Claude Code gets a JSON MCP config; Codex gets dotted ``-c`` overrides."""
    hub = Hub()
    hub.origin = "http://127.0.0.1:9"
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "create_agent",
                "description": "创建一个新的团队成员。",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    bridge = hub.begin(
        object(),
        ActiveRun(run_id="", conversation_id="c1"),
        {"id": "c1", "kind": "direct"},
        {"id": "a1", "kind": "supervisor"},
        schemas,
    )
    assert bridge is not None
    assert bridge.url == f"{hub.origin}/cli-tools/{bridge.token}"

    claude = bridge.argv("claude-code")
    config = json.loads(claude[claude.index("--mcp-config") + 1])
    server = config["mcpServers"]["cubes"]
    assert server["type"] == "http"
    assert server["url"] == bridge.url
    assert server["headers"]["Authorization"] == f"Bearer {bridge.token}"
    assert "--strict-mcp-config" in claude
    assert claude[claude.index("--allowedTools") + 1] == "mcp__cubes__create_agent"

    codex = bridge.argv("codex")
    assert codex[0] == "-c"
    assert codex[1] == f'mcp_servers.cubes.url="{bridge.url}"'
    assert codex[2] == "-c"
    assert codex[3] == (
        f'mcp_servers.cubes.http_headers={{Authorization="Bearer {bridge.token}"}}'
    )
    argv = cli_engines.argv_for("codex", _resolved(), "codex", "系统", mcp=codex)
    assert argv[-1] == "-"
    assert f'mcp_servers.cubes.url="{bridge.url}"' in argv
    bridge.close()
    assert hub.bridge_for(bridge.token) is None


def _resolved():
    return ResolvedModel(
        protocol="codex", base_url="", api_key="", model="默认", provider_id="p", provider_name="n"
    )


# ------------------------------------------------------------------------ stop


def test_stopping_the_run_ends_a_parked_call_and_the_cli_turn(
    make_backend, fake_mcp_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CLI parked on a card is stopped by the same button as any other bot."""
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="等确认的人", instructions="临时成员。", kind="member")
    out = tmp_path / "mcp.json"
    monkeypatch.setenv("FAKE_MCP_OUT", str(out))
    monkeypatch.setenv("FAKE_MCP_CALL", "delete_agent")
    monkeypatch.setenv(
        "FAKE_MCP_ARGS", json.dumps({"agent_id": member["id"], "reason": "测试停止"}, ensure_ascii=False)
    )
    chat = store.ensure_direct_conversation((store.supervisor())["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            await choose_cli_model(client)
            run = await start_run(client, chat["id"], "把等确认的人删掉")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                detail = await get_conversation(client, chat["id"])
                if any(
                    message.get("action") and message["action"]["status"] == "awaiting"
                    for message in detail["messages"]
                ):
                    break
                await asyncio.sleep(0.05)
            else:  # pragma: no cover - the card must appear
                raise AssertionError("the card never appeared")

            stopped = await client.post(f"/runs/{run['id']}/stop", json={})
            assert stopped.status_code == 200, stopped.text
            finished = await wait_for_run(client, run["id"], statuses=("stopped",), timeout=25)
            assert finished["status"] == "stopped"
            # The denied card left the member alone.
            assert store.get_agent(member["id"]) is not None

    asyncio.run(body())
    # No token outlives its turn: every bridge the app opened is gone.
    hub: Hub = backend.app.state.cli_tools
    assert hub.bridges == {}
