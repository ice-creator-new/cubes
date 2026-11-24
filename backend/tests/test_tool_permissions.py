"""Per-member tool permissions: hiding packs from one agent's requests, and PATCH.

``workspace_dir`` gives the file and code packs a folder to work in (they are always
offered), a real stdio MCP connector supplies the connector pack, and a fake sandbox
websocket supplies the computer pack. Each test hides a set of packs from one member and
asserts the other agent still sees them.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fake_upstream import call, tool_calls
from helpers import client_for, get_workspace, run_goal, store_of, tool_names
from test_computer import FakeAgent

DEMO = str(Path(__file__).with_name("mcp_demo_server.py"))

CODE_TOOLS = {"run_code"}
FILE_TOOLS = {"file_list", "file_read", "file_write"}
COMPUTER_TOOLS = {"computer_shell", "computer_browser", "computer"}
CONNECTOR_TOOLS = {"mcp__list_issues", "mcp__create_issue", "mcp__explode"}


def build_team(backend) -> dict:
    """Supervisor plus two members in one group, seeded straight into the store."""
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"], reviewer["id"]],
    )
    return {"supervisor": supervisor, "designer": designer, "reviewer": reviewer, "group": group}


async def add_connector(client) -> dict:
    response = await client.post(
        "/connectors",
        json={
            "preset": "custom-stdio",
            "name": "演示",
            "command": sys.executable,
            "args": [DEMO],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def connect_sandbox(client) -> FakeAgent:
    state = (await client.get("/computer")).json()
    agent = FakeAgent(str(client.base_url), state["token"])
    await agent.start(client)
    return agent


async def patch_member(client, agent_id: str, permissions: dict | None) -> dict:
    response = await client.patch(f"/agents/{agent_id}", json={"tool_permissions": permissions})
    assert response.status_code == 200, response.text
    return response.json()


def consult_script(team) -> list[dict]:
    """The supervisor asks 甲; 甲 replies with plain text."""
    return [
        tool_calls(
            call(
                "call_1",
                "ask_agent",
                {
                    "group_id": team["group"]["id"],
                    "agent_id": team["designer"]["id"],
                    "task": "请给出你的结论",
                },
            )
        ),
        {"content": "甲的结论"},
    ]


# ------------------------------------------------------------------------ PATCH


def test_patch_tool_permissions_set_clear_and_invalid(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    jia = team["designer"]

    async def body() -> None:
        async with client_for(backend) as client:
            # An object sets all four switches.
            patched = await patch_member(
                client, jia["id"], {"code": False, "files": False, "computer": False, "connectors": "none"}
            )
            assert patched["tool_permissions"] == {
                "code": False,
                "files": False,
                "computer": False,
                "connectors": "none",
                "skills_disabled": [],
            }

            workspace = await get_workspace(client)
            listed = next(a for a in workspace["agents"] if a["id"] == jia["id"])
            assert listed["tool_permissions"]["connectors"] == "none"

            # An explicit null clears the switches (everything allowed again).
            cleared = await patch_member(client, jia["id"], None)
            assert cleared["tool_permissions"] is None

            # A bad connectors value is a 422.
            bad = await client.patch(
                f"/agents/{jia['id']}",
                json={"tool_permissions": {"code": False, "files": False, "computer": False, "connectors": "everything"}},
            )
            assert bad.status_code == 422, bad.text

            # A missing switch is a 422.
            short = await client.patch(
                f"/agents/{jia['id']}", json={"tool_permissions": {"code": False}}
            )
            assert short.status_code == 422, short.text

            # An empty body is a 422 (at least one field required).
            empty = await client.patch(f"/agents/{jia['id']}", json={})
            assert empty.status_code == 422, empty.text

    asyncio.run(body())


# --------------------------------------------------------------- pack hiding


def test_member_permissions_hide_all_packs_from_that_agent_only(
    make_backend, upstream, tmp_path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    backend = make_backend(workspace_dir=workspace)
    team = build_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            await add_connector(client)
            sandbox = await connect_sandbox(client)
            try:
                await patch_member(
                    client,
                    team["designer"]["id"],
                    {"code": False, "files": False, "computer": False, "connectors": "none"},
                )

                upstream.script = consult_script(team)
                direct_id = (await get_workspace(client))["direct_conversation_id"]
                run = await run_goal(client, direct_id, "咨询甲")
                assert run["status"] == "completed", run

                supervisor_names = tool_names(upstream.request(0))
                member_names = tool_names(upstream.request(1))

                # The supervisor keeps every pack.
                assert CODE_TOOLS <= set(supervisor_names)
                assert FILE_TOOLS <= set(supervisor_names)
                assert {"computer_shell", "computer_browser"} <= set(supervisor_names)
                assert CONNECTOR_TOOLS <= set(supervisor_names)

                # 甲 loses all four packs but keeps the ordinary tools.
                assert not CODE_TOOLS & set(member_names)
                assert not FILE_TOOLS & set(member_names)
                assert not COMPUTER_TOOLS & set(member_names)
                assert not CONNECTOR_TOOLS & set(member_names)
                assert "calculate" in member_names
                assert "web_search" in member_names
            finally:
                await sandbox.stop()

    asyncio.run(body())


def test_connectors_read_hides_write_connectors_only(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            await add_connector(client)
            await patch_member(
                client,
                team["designer"]["id"],
                {"code": True, "files": True, "computer": True, "connectors": "read"},
            )

            upstream.script = consult_script(team)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "咨询甲")
            assert run["status"] == "completed", run

            supervisor_names = tool_names(upstream.request(0))
            member_names = tool_names(upstream.request(1))

            # The supervisor sees the write tool; 甲 sees only the read-only ones.
            assert "mcp__create_issue" in supervisor_names
            assert "mcp__list_issues" in supervisor_names
            assert "mcp__create_issue" not in member_names
            assert "mcp__list_issues" in member_names
            assert "mcp__explode" in member_names

    asyncio.run(body())
