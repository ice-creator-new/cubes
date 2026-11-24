"""A group message's routing: named members, the last speaker, the supervisor (real HTTP).

An invisible dispatcher (``app.dispatch``) decides who answers; its default, used by every
test that never scripts it, keeps the old order: ``@名字`` first, then whoever last spoke
here. A progress question is a structured card, not a supervisor reply.
"""

from __future__ import annotations

import asyncio

from helpers import agent_messages, client_for, get_conversation, run_goal, store_of


def build_team(backend) -> dict:
    store = store_of(backend)
    supervisor = store.supervisor()
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"], reviewer["id"]],
    )
    return {"supervisor": supervisor, "designer": designer, "reviewer": reviewer, "group": group}


def texts(request: dict) -> list[str]:
    return [str(message.get("content")) for message in request["messages"]]


def test_named_members_answer_in_order_with_no_closing(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "风险在包体积"}, {"content": "同意乙，另外热重载很快"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@乙 @甲 对 flutter 有啥看法？")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 2
            detail = await get_conversation(client, team["group"]["id"])
            trace = (await client.get(f"/runs/{run['id']}/trace")).json()

        # The trace reads as a conversation: the user's message went to each named member.
        asks = [edge["to"] for edge in trace["edges"] if edge["kind"] == "ask"]
        assert asks == [team["reviewer"]["id"], team["designer"]["id"]]
        assert [edge for edge in trace["edges"] if edge["kind"] == "consult"] == []
        # No notice, no closing word: the named members speak in the order they were named.
        assert [message["kind"] for message in detail["messages"]] == ["user", "agent", "agent"]
        replies = agent_messages(detail)
        assert [reply["author_id"] for reply in replies] == [
            team["reviewer"]["id"],
            team["designer"]["id"],
        ]
        goal = detail["messages"][0]
        assert all(reply["reply_to_id"] == goal["id"] for reply in replies)

        first, second = upstream.requests
        # A quick question is chat, not a task: no task instruction is handed over.
        assert "主管指派给你的任务" not in "".join(texts(first))
        # The second speaker reads the first one's reply.
        assert any("[乙] 风险在包体积" in text for text in texts(second))
        assert all("已经在群里发过言了" not in texts(request)[-1] for request in upstream.requests)

    asyncio.run(body())


def test_a_mention_in_a_named_turn_answers_before_the_queue_ends(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "方案如下，@乙 你看下风险"}, {"content": "风险可控"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 出个方案")
            assert run["status"] == "completed", run
            assert run["hops"] == 1
            detail = await get_conversation(client, team["group"]["id"])

        replies = agent_messages(detail)
        assert [reply["author_id"] for reply in replies] == [
            team["designer"]["id"],
            team["reviewer"]["id"],
        ]
        # The @-ed member answers the message that named it.
        assert replies[1]["reply_to_id"] == replies[0]["id"]

    asyncio.run(body())


def test_an_un_at_message_goes_to_the_last_speaker(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    store_of(backend).append_message(
        conversation_id=team["group"]["id"],
        author_id=team["designer"]["id"],
        author_name="甲",
        avatar_seed=None,
        kind="agent",
        content="方案 A 写好了。",
        status="complete",
    )
    upstream.script = [{"content": "补了一版"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "再补一版")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        assert agent_messages(detail)[-1]["author_id"] == team["designer"]["id"]
        # It is new work for that member, not a resume: no 「接着做」 notice.
        assert all("接着做" not in str(message["content"]) for message in detail["messages"])

    asyncio.run(body())


def test_a_group_where_nobody_spoke_hands_an_un_at_message_to_the_supervisor(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "我在。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "你好")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        assert agent_messages(detail)[-1]["author_id"] == team["supervisor"]["id"]

    asyncio.run(body())
