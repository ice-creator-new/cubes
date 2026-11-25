"""Stop semantics: real cancellation, partial text kept, nothing faked as complete."""

from __future__ import annotations

import asyncio
import time

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    find_result_key,
    get_conversation,
    get_workspace,
    result_keys,
    run_goal,
    start_run,
    stop_run,
    store_of,
    wait_for_message,
)


def test_stop_keeps_partial_text_and_marks_it_interrupted(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "写一篇长文")
            assert run["status"] == "running"
            await wait_for_message(client, direct_id, status="streaming")

            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped", stopped
            assert stopped["error"] is None
            assert stopped["finished_at"] is not None
            assert stopped["current_step"] == "任务已停止"
            assert (await get_workspace(client))["active_runs"] == []

            messages = agent_messages(await get_conversation(client, direct_id))
            assert len(messages) == 1
            assert messages[0]["status"] == "interrupted"
            partial = messages[0]["content"]
            assert partial.startswith("[1]"), partial

            # The upstream stream really was dropped: nothing more is consumed or written.
            await asyncio.sleep(0.6)
            again = agent_messages(await get_conversation(client, direct_id))
            assert again[0]["content"] == partial
            assert again[0]["status"] == "interrupted"

            # Stopping a terminal run is idempotent and reports the same run.
            repeated = await stop_run(client, run["id"])
            assert repeated["status"] == "stopped"
            assert repeated["finished_at"] == stopped["finished_at"]

    asyncio.run(body())


def test_stop_during_a_member_consult_marks_the_action_as_errored(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    backend = make_backend()
    store = store_of(backend)
    upstream.script = [
        tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})),
        lambda requests, index: tool_calls(
            call(
                "call_2",
                "create_group",
                {
                    "title": "小组",
                    "purpose": "做事",
                    "member_ids": result_keys(requests, "agent_id"),
                },
            )
        ),
        lambda requests, index: tool_calls(
            call(
                "call_3",
                "ask_agent",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "agent_id": result_keys(requests, "agent_id")[0],
                    "task": "给结论",
                },
            )
        ),
        {"content": None, "slow": True},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "让成员帮忙")

            deadline = time.monotonic() + 15
            group = None
            while time.monotonic() < deadline:
                groups = [c for c in store.list_conversations() if c["kind"] == "group"]
                if groups:
                    group = groups[0]
                    break
                await asyncio.sleep(0.05)
            assert group is not None, "the supervisor never created the group"

            member_message = await wait_for_message(client, group["id"], status="streaming")
            assert member_message["author_name"] == "甲"

            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped", stopped

            group_detail = await get_conversation(client, group["id"])
            assert [m["status"] for m in group_detail["messages"]] == ["interrupted"]
            assert group_detail["messages"][0]["author_id"] == workspace_member_id(store)

            actions = action_messages(await get_conversation(client, direct_id))
            assert [action["action"]["status"] for action in actions] == [
                "completed",
                "completed",
                "error",
            ]
            assert actions[2]["action"]["error"] == "任务已停止。"
            assert actions[2]["status"] == "interrupted"

    asyncio.run(body())


def test_a_stopped_group_turn_leaves_one_line_and_continue_goes_back_to_it(
    make_backend, upstream
) -> None:
    backend = make_backend()
    store = store_of(backend)
    designer = store.create_agent(name="甲", instructions="查资料", kind="member")
    group = store.create_conversation(
        kind="group", title="资料组", purpose="把资料查清", member_ids=[designer["id"]]
    )
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 40
    upstream.script = [
        tool_calls(call("c1", "calculate", {"expression": "1+1"})),
        {"content": None, "slow": True},
        {"content": "接着查完了，结论是 2。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, group["id"], "@甲 查一下这个数")
            # The tool row is done and the bot's answer is still streaming when it stops.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                detail = await get_conversation(client, group["id"])
                if action_messages(detail) and any(
                    m["kind"] == "agent" and m["status"] == "streaming"
                    for m in detail["messages"]
                ):
                    break
                await asyncio.sleep(0.05)
            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped", stopped

            detail = await get_conversation(client, group["id"])
            stop_line = agent_messages(detail)[-1]
            assert stop_line["author_id"] == designer["id"]
            assert stop_line["status"] == "complete"
            assert stop_line["content"].startswith("已停下：")
            # One line, not a second wall of tool rows; the tool row itself stays.
            assert len(action_messages(detail)) == 1

            # No dispatcher call for 「继续」: it returns to the interrupted bot.
            before = upstream.dispatch_request_count
            upstream.chunk_count = 2
            continued = await run_goal(client, group["id"], "继续")
            assert continued["status"] == "completed", continued
            after = await get_conversation(client, group["id"])
            assert agent_messages(after)[-1]["author_id"] == designer["id"]
            assert agent_messages(after)[-1]["content"] == "接着查完了，结论是 2。"
            assert upstream.dispatch_request_count == before
            assert store.interrupted_agent(group["id"]) is None

    asyncio.run(body())


def test_completed_run_stop_is_a_no_op(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "完成"}]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "随便聊聊")
            assert run["status"] == "completed"
            repeated = await stop_run(client, run["id"])
            assert repeated["status"] == "completed"
            assert repeated["error"] is None
            assert upstream.request_count == 1

    asyncio.run(body())


def workspace_member_id(store) -> str:
    members = [agent for agent in store.list_agents() if agent["kind"] == "member"]
    assert len(members) == 1
    return members[0]["id"]
