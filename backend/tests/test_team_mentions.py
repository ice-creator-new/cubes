"""Routing, @-handoff, hop limits and per-conversation parallelism (real HTTP)."""

from __future__ import annotations

import asyncio
import time

from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    run_goal,
    start_run,
    stop_run,
    store_of,
    system_prompt,
    tool_names,
    wait_for_message,
    wait_for_run,
)


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
    return {
        "supervisor": supervisor,
        "designer": designer,
        "reviewer": reviewer,
        "group": group,
    }


def test_a_mentioned_member_replies_instead_of_the_supervisor(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "我先出一版方案"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 请出方案")
            assert run["status"] == "completed", run
            assert run["hops"] == 0
            assert run["current_agent_id"] is None
            assert run["pending_agent_ids"] == []
            assert run["model_calls"] == 1

            detail = await get_conversation(client, team["group"]["id"])
            # A quick answer stays in the group: no task thread, no card.
            assert [message["kind"] for message in detail["messages"]] == ["user", "agent"]
            goal, reply = detail["messages"]
            assert goal["mentions"] == [team["designer"]["id"]]
            assert reply["author_id"] == team["designer"]["id"]
            assert reply["author_name"] == "甲"
            assert reply["content"] == "我先出一版方案"
            assert reply["status"] == "complete"
            assert reply["reply_to_id"] == goal["id"]
            assert reply["mentions"] == []

        assert upstream.request_count == 1
        request = upstream.requests[0]
        # A member receives the shared packs and private chat, never management tools.
        assert "calculate" in tool_names(request)
        assert "direct_message" in tool_names(request)
        assert "create_agent" not in tool_names(request)
        system = system_prompt(request)
        assert "甲" in system
        assert "出方案" in system
        assert "乙" in system  # the group roster is visible to every member
        # Scratch work goes under the working folder, and @ is only a real request.
        assert "草稿/" in system
        assert "不要为了让人批准" in system
        assert "不要替用户决定" in system
        # The group turn sees the user's own words, mentions included.
        assert "群组里做一件活" not in system
        contents = [message["content"] for message in request["messages"]]
        assert any("@甲 请出方案" in content for content in contents)

    asyncio.run(body())


def test_a_group_message_without_mentions_goes_to_the_supervisor(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "我来安排"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "大家看下方向")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])
            reply = agent_messages(detail)[-1]
            assert reply["author_id"] == team["supervisor"]["id"]
            assert reply["content"] == "我来安排"
            assert detail["messages"][0]["mentions"] == []

        assert upstream.request_count == 1
        assert upstream.requests[0]["tools"] is not None

    asyncio.run(body())


def test_member_handoff_replies_to_the_message_that_named_it(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [
        {"content": "方案在这里，@乙 请评估风险"},
        {"content": "风险可控"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 先出方案")
            assert run["status"] == "completed", run
            assert run["hops"] == 1
            assert run["model_calls"] == 2
            detail = await get_conversation(client, team["group"]["id"])
            # Each member answers the group directly; a plain answer opens no thread.
            assert [m["kind"] for m in detail["messages"]] == [
                "user",
                "agent",
                "agent",
            ]
            goal = detail["messages"][0]
            designer, reviewer = agent_messages(detail)
            assert designer["author_id"] == team["designer"]["id"]
            assert designer["mentions"] == [team["reviewer"]["id"]]
            assert reviewer["author_id"] == team["reviewer"]["id"]
            assert reviewer["reply_to_id"] == designer["id"]
            assert reviewer["content"] == "风险可控"

        assert upstream.request_count == 2

    asyncio.run(body())


def test_supervisors_final_answer_can_hand_the_turn_to_a_member(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [
        {"content": "@乙 请先评估风险"},
        {"content": "风险很低"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "做个决定")
            assert run["status"] == "completed", run
            assert run["hops"] == 1
            detail = await get_conversation(client, team["group"]["id"])
            replies = agent_messages(detail)
            supervisor_reply = next(
                m for m in replies if m["author_id"] == team["supervisor"]["id"]
            )
            member_reply = next(m for m in replies if m["author_id"] == team["reviewer"]["id"])
            assert supervisor_reply["mentions"] == [team["reviewer"]["id"]]
            assert member_reply["reply_to_id"] == supervisor_reply["id"]

    asyncio.run(body())


def test_explicit_mentions_beat_the_message_body(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "我来说"}] * 2

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(
                client,
                team["group"]["id"],
                "请两位配合",
                mentions=[team["reviewer"]["id"], team["designer"]["id"]],
            )
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            detail = await get_conversation(client, team["group"]["id"])
            # One result per named member, in the order they were named.
            authors = [message["author_id"] for message in agent_messages(detail)]
            assert authors == [team["reviewer"]["id"], team["designer"]["id"]]
            assert detail["messages"][0]["mentions"] == [
                team["reviewer"]["id"],
                team["designer"]["id"],
            ]

    asyncio.run(body())


def test_relay_stops_after_two_hops_and_the_supervisor_concludes(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [
        {"content": "方案先这样，@乙 看下风险"},
        {"content": "风险可控，@甲 你补个细节"},
        {"content": "细节补好了，@乙 你确认下"},
        {"content": "结论：方案已定；还需要你决定：什么时候上线？"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 开始接力")
            assert run["status"] == "completed", run
            assert run["hops"] == 2
            assert run["pending_agent_ids"] == []
            assert run["current_agent_id"] is None
            # One turn per hop, then the supervisor's closing turn: never an endless relay.
            assert run["model_calls"] == 4

            detail = await get_conversation(client, team["group"]["id"])
            replies = agent_messages(detail)
            assert len(replies) == 4
            assert all(reply["status"] == "complete" for reply in replies)
            assert [reply["author_id"] for reply in replies] == [
                team["designer"]["id"],
                team["reviewer"]["id"],
                team["designer"]["id"],
                team["supervisor"]["id"],
            ]
            # No system notice line: the closing answer is the end of the run.
            assert all(message["kind"] != "notice" for message in detail["messages"])
            assert replies[-1]["content"].startswith("结论：")

        # The third hand-off was refused; the supervisor got the closing instruction instead.
        assert upstream.request_count == 4
        closing = upstream.requests[-1]["messages"][-1]["content"]
        assert "不要再 @ 任何同事" in closing
        assert "还需要用户决定什么" in closing

    asyncio.run(body())


def test_unknown_names_are_ignored(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "好的"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@丙 请接手")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])
            assert detail["messages"][0]["mentions"] == []
            assert agent_messages(detail)[0]["author_id"] == team["supervisor"]["id"]
            assert (await get_workspace(client))["agents"][-1]["name"] == "乙"

    asyncio.run(body())


def test_mentioning_a_non_member_is_rejected(make_backend) -> None:
    backend = make_backend()
    team = build_team(backend)
    store = store_of(backend)
    outsider = store.create_agent(name="丙", instructions="旁观", kind="member")

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.post(
                f"/conversations/{team['group']['id']}/runs",
                json={"content": "请 outsider 看看", "mentions": [outsider["id"]]},
            )
            assert response.status_code == 422, response.text
            assert "不在这个会话里" in response.json()["detail"]
            unknown = await client.post(
                f"/conversations/{team['group']['id']}/runs",
                json={"content": "请人看看", "mentions": ["a_missing"]},
            )
            assert unknown.status_code == 422
            detail = await get_conversation(client, team["group"]["id"])
            assert detail["messages"] == []

    asyncio.run(body())


def test_member_direct_chat_is_answered_by_that_member(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="只谈设计细节", kind="member")
    upstream.script = [{"content": "设计上我建议先做一版原型"}]

    async def body() -> None:
        async with client_for(backend) as client:
            chat = (await client.post(f"/agents/{member['id']}/direct")).json()
            run = await run_goal(client, chat["id"], "帮我看看设计")
            assert run["status"] == "completed", run
            assert run["conversation_id"] == chat["id"]

            detail = await get_conversation(client, chat["id"])
            assert [message["kind"] for message in detail["messages"]] == ["user", "agent"]
            reply = detail["messages"][1]
            assert reply["author_id"] == member["id"]
            assert reply["author_name"] == "甲"
            assert reply["reply_to_id"] == detail["messages"][0]["id"]

            # A private chat never runs the supervisor's tool loop.
            assert (await get_workspace(client))["direct_conversation_id"] != chat["id"]

        assert upstream.request_count == 1
        request = upstream.requests[0]
        assert "direct_message" in tool_names(request)
        assert "create_agent" not in tool_names(request)
        system = system_prompt(request)
        assert "只谈设计细节" in system
        assert "私聊" in system

    asyncio.run(body())


def test_stop_during_a_handoff_interrupts_the_stream_and_clears_the_queue(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 40
    upstream.script = [{"content": "@乙 请接手评估"}, {"content": None, "slow": True}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, team["group"]["id"], "@甲 先出方案")
            # 甲 answers in the group, @-hands to 乙, whose reply starts streaming.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                detail = await get_conversation(client, team["group"]["id"])
                streaming = [
                    m
                    for m in detail["messages"]
                    if m["kind"] == "agent" and m["status"] == "streaming"
                ]
                if streaming and streaming[0]["author_name"] == "乙":
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("乙 never started streaming")

            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped", stopped
            assert stopped["pending_agent_ids"] == []
            assert stopped["current_agent_id"] is None
            assert (await get_workspace(client))["active_runs"] == []

            detail = await get_conversation(client, team["group"]["id"])
            replies = agent_messages(detail)
            assert [(m["author_name"], m["status"]) for m in replies] == [
                ("甲", "complete"),
                ("乙", "interrupted"),
            ]
            # A plain group reply makes no task row: there is nothing to resume.
            tasks = (await client.get(f"/conversations/{team['group']['id']}/tasks")).json()["tasks"]
            assert tasks == []

            # Stopping leaves nothing to resume on its own: no extra reply appears.
            await asyncio.sleep(0.4)
            assert [m["status"] for m in agent_messages(detail)] == ["complete", "interrupted"]

    asyncio.run(body())


def test_two_conversations_run_in_parallel(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    team = build_team(backend)
    supervisor = team["supervisor"]
    other = store.create_conversation(
        kind="group",
        title="另一个群",
        purpose="并行工作",
        member_ids=[supervisor["id"]],
    )
    upstream.chunk_delay = 0.08
    upstream.chunk_count = 12

    async def body() -> None:
        async with client_for(backend) as client:
            first = await start_run(client, team["group"]["id"], "@甲 慢慢做")
            second = await start_run(client, other["id"], "另一个任务慢慢做")

            deadline = time.monotonic() + 10
            running: list[dict] = []
            while time.monotonic() < deadline:
                running = (await get_workspace(client))["active_runs"]
                if len(running) == 2:
                    break
                await asyncio.sleep(0.05)
            assert {run["id"] for run in running} == {first["id"], second["id"]}

            # The second conversation is not blocked by the first one.
            assert (await get_run(client, second["id"]))["status"] == "running"

            done_first, done_second = await asyncio.gather(
                wait_for_run(client, first["id"]), wait_for_run(client, second["id"])
            )
            assert done_first["status"] == "completed", done_first
            assert done_second["status"] == "completed", done_second
            assert (await get_workspace(client))["active_runs"] == []

            first_detail = await get_conversation(client, team["group"]["id"])
            second_detail = await get_conversation(client, other["id"])
            assert agent_messages(first_detail)[0]["author_id"] == team["designer"]["id"]
            assert agent_messages(second_detail)[0]["author_id"] == supervisor["id"]

    asyncio.run(body())


def test_a_member_mention_inside_a_member_reply_does_not_queue_itself(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "@甲 自己再想想，不用麻烦别人"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 开始")
            assert run["status"] == "completed", run
            assert run["hops"] == 0
            detail = await get_conversation(client, team["group"]["id"])
            assert [message["kind"] for message in detail["messages"]] == ["user", "agent"]
            assert agent_messages(detail)[0]["mentions"] == [team["designer"]["id"]]

        assert upstream.request_count == 1

    asyncio.run(body())
