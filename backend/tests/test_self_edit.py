"""update_self and update_agent: the same validation as PATCH /agents, and live workspace changes."""

from __future__ import annotations

import asyncio

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    start_run,
    store_of,
    tool_results,
    wait_for_run,
)


def seed_member(backend, name: str = "甲", instructions: str = "出方案") -> dict:
    return store_of(backend).create_agent(name=name, instructions=instructions, kind="member")


def test_a_member_renames_itself_and_the_workspace_follows(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "update_self", {"name": "设计师"})),
        {"content": "以后叫我设计师。"},
    ]
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend)
    chat = store.ensure_direct_conversation(member["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, chat["id"], "给你换个名字")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            workspace = await get_workspace(client)
            renamed = next(agent for agent in workspace["agents"] if agent["id"] == member["id"])
            assert renamed["name"] == "设计师"

            detail = await get_conversation(client, chat["id"])
            assert detail["conversation"]["title"] == "设计师"
            rows = action_messages(detail)
            assert [row["action"]["summary"] for row in rows] == ["把名字改成了「设计师」"]
            assert agent_messages(detail)[-1]["content"] == "以后叫我设计师。"

            result = dict(tool_results(upstream.requests[1]))["call_1"]
            assert result["ok"] is True
            assert result["name"] == "设计师"

    asyncio.run(body())


def test_a_member_changes_its_own_appearance(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "update_self",
                {"shape": "star", "color": "jade", "eye": "#102030"},
            )
        ),
        {"content": "新样子怎么样？"},
    ]
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend)
    chat = store.ensure_direct_conversation(member["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, chat["id"], "换个样子")
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            workspace = await get_workspace(client)
            changed = next(agent for agent in workspace["agents"] if agent["id"] == member["id"])
            assert changed["appearance"] == {"shape": "star", "color": "jade", "eye": "#102030"}
            rows = action_messages(await get_conversation(client, chat["id"]))
            assert rows[0]["action"]["summary"] == "换上了新的样子"

    asyncio.run(body())


def test_a_duplicate_name_is_refused_like_patch_does(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "update_self", {"name": "乙"})),
        {"content": "这个名字已经被占了。"},
    ]
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, name="甲")
    seed_member(backend, name="乙")
    chat = store.ensure_direct_conversation(member["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, chat["id"], "改个名字")
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            result = dict(tool_results(upstream.requests[1]))["call_1"]
            assert result["ok"] is False
            assert "已被使用" in result["error"]
            assert store.get_agent(member["id"])["name"] == "甲"

    asyncio.run(body())


def test_a_member_cannot_change_another_bot(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "update_agent", {"agent_id": "supervisor", "name": "新主管"})),
        {"content": "我改不了别人。"},
    ]
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend)
    chat = store.ensure_direct_conversation(member["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, chat["id"], "把主管改名")
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            result = dict(tool_results(upstream.requests[1]))["call_1"]
            assert result["ok"] is False
            assert "update_agent" in result["error"]
            assert store.get_agent(supervisor["id"])["name"] == supervisor["name"]

    asyncio.run(body())


def test_the_supervisor_renames_a_member_through_update_agent(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "update_agent", {"agent_id": "研究员", "name": "分析师"})),
        {"content": "已经改名了。"},
    ]
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, name="研究员")
    store.ensure_direct_conversation(member["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把研究员改名")
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            rows = action_messages(await get_conversation(client, direct_id))
            assert rows[0]["action"]["summary"] == "把「研究员」改名为「分析师」"
            assert store.get_agent(member["id"])["name"] == "分析师"

    asyncio.run(body())
