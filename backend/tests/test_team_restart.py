"""Restart behaviour: interrupted runs are failed truthfully and never silently resumed."""

from __future__ import annotations

import asyncio
import sqlite3

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    names_of,
    run_goal,
    start_run,
    store_of,
)


def test_entities_and_text_survive_a_restart(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})),
        {"content": "第一次完成"},
    ]
    state: dict = {}

    async def before() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "创建一个成员")
            assert run["status"] == "completed"
            state["workspace"] = await get_workspace(client)
            state["detail"] = await get_conversation(client, direct_id)
            state["run"] = run

    asyncio.run(before())
    requests_before = upstream.request_count
    backend.restart()

    async def after() -> None:
        async with client_for(backend) as client:
            assert await get_workspace(client) == state["workspace"]
            assert await get_conversation(client, state["detail"]["conversation"]["id"]) == (
                state["detail"]
            )
            assert await get_run(client, state["run"]["id"]) == state["run"]

    asyncio.run(after())
    assert upstream.request_count == requests_before


def test_graceful_shutdown_stops_the_active_run_without_resuming(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    backend = make_backend()
    state: dict = {}

    async def before() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "写一篇长文")
            state["run_id"] = run["id"]
            state["direct_id"] = direct_id

    asyncio.run(before())
    backend.restart()

    async def after() -> None:
        async with client_for(backend) as client:
            run = await get_run(client, state["run_id"])
            assert run["status"] == "stopped", run
            detail = await get_conversation(client, state["direct_id"])
            assert detail["conversation"]["id"] == state["direct_id"]
            for message in agent_messages(detail):
                assert message["status"] == "interrupted"
            assert (await get_workspace(client))["active_runs"] == []

    asyncio.run(after())


def test_a_run_left_running_by_a_crash_is_failed_on_the_next_start(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})),
        {"content": "完成"},
    ]
    state: dict = {}

    async def before() -> None:
        async with client_for(backend) as client:
            state["direct_id"] = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, state["direct_id"], "创建一个成员")
            assert run["status"] == "completed"
            state["run_id"] = run["id"]
            detail = await get_conversation(client, state["direct_id"])
            state["message_id"] = agent_messages(detail)[-1]["id"]
            state["supervisor_id"] = (await get_workspace(client))["supervisor_id"]

    asyncio.run(before())
    requests_before = upstream.request_count
    backend.stop()

    # Simulate a process that died mid-run: the persisted rows are active again.
    path = backend.settings.database_path
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "UPDATE runs SET status = 'running', finished_at = NULL WHERE id = ?",
            (state["run_id"],),
        )
        connection.execute(
            "UPDATE messages SET status = 'streaming' WHERE id = ?", (state["message_id"],)
        )
        connection.execute(
            """
            INSERT INTO actions
                (id, run_id, conversation_id, tool_call_id, tool_name, arguments, status, summary,
                 result, error, target_agent_id, target_conversation_id, message_id, created_at, updated_at)
            VALUES ('x_crashed', ?, ?, 'call_crashed', 'create_agent', '{}', 'running', '正在创建成员',
                    NULL, NULL, NULL, NULL, 'm_crashed', '2026-10-16T00:00:00Z', '2026-10-16T00:00:00Z')
            """,
            (state["run_id"], state["direct_id"]),
        )
        connection.execute(
            """
            INSERT INTO messages
                (id, conversation_id, author_id, author_name, avatar_seed, kind, content, status,
                 action_id, seq, created_at)
            VALUES ('m_crashed', ?, ?, '主管', 'seed-crashed', 'action', '正在创建成员', 'streaming',
                    'x_crashed', 99, '2026-10-16T00:00:00Z')
            """,
            (state["direct_id"], state["supervisor_id"]),
        )
        connection.commit()
    finally:
        connection.close()

    backend.start()

    async def after() -> None:
        async with client_for(backend) as client:
            run = await get_run(client, state["run_id"])
            assert run["status"] == "stopped", run
            assert run["error"] == "服务重启导致任务中断，未完成的部分已保留。"
            assert run["current_step"] == "任务已停止"
            assert run["finished_at"] is not None
            assert (await get_workspace(client))["active_runs"] == []

            detail = await get_conversation(client, state["direct_id"])
            statuses = {m["id"]: m["status"] for m in detail["messages"]}
            assert statuses[state["message_id"]] == "interrupted"
            assert statuses["m_crashed"] == "interrupted"

            crashed = [m for m in action_messages(detail) if m["action"]["id"] == "x_crashed"]
            assert len(crashed) == 1
            assert crashed[0]["action"]["status"] == "error"
            assert crashed[0]["action"]["error"] == "服务重启导致任务中断，未完成的部分已保留。"
            assert names_of((await get_workspace(client))["agents"][1:]) == ["甲"]

    asyncio.run(after())
    # Nothing resumed on its own: no further model call was made.
    assert upstream.request_count == requests_before


def test_workspace_snapshot_keeps_avatar_seeds_and_member_order(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    store.create_agent(name="甲", instructions="研究", kind="member")
    store.create_agent(name="乙", instructions="设计", kind="member")

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert [agent["name"] for agent in workspace["agents"]] == ["主管", "甲", "乙"]
            seeds = [agent["avatar_seed"] for agent in workspace["agents"]]
            assert all(len(seed) == 32 for seed in seeds)
            assert len(set(seeds)) == 3

    asyncio.run(body())


def test_a_crash_fails_every_running_conversation(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    group = store.create_conversation(
        kind="group", title="并行群", purpose="并行做事", member_ids=[supervisor["id"]]
    )
    state: dict = {}

    async def before() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "第一个长任务")
            second = await start_run(client, group["id"], "第二个长任务")
            state["runs"] = [first["id"], second["id"]]
            state["conversations"] = [direct_id, group["id"]]

    asyncio.run(before())
    backend.stop()

    # A process that died mid-run leaves both rows active.
    connection = sqlite3.connect(str(backend.settings.database_path))
    try:
        connection.execute("UPDATE runs SET status = 'running', finished_at = NULL")
        connection.execute("UPDATE messages SET status = 'streaming' WHERE kind = 'agent'")
        connection.commit()
    finally:
        connection.close()

    backend.start()

    async def after() -> None:
        async with client_for(backend) as client:
            for run_id in state["runs"]:
                run = await get_run(client, run_id)
                assert run["status"] == "stopped", run
                assert run["error"] == "服务重启导致任务中断，未完成的部分已保留。"
                assert run["pending_agent_ids"] == []
            for conversation_id in state["conversations"]:
                detail = await get_conversation(client, conversation_id)
                for message in agent_messages(detail):
                    assert message["status"] == "interrupted"
            assert (await get_workspace(client))["active_runs"] == []

    asyncio.run(after())
