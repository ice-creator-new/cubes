"""Bots decide for themselves to bring others in: a private message to one, or a group question.

``ask_group`` posts the caller's question in a group it belongs to; every other member answers
there in turn, the supervisor closes, and the answers come back to the caller as its tool result
(real HTTP).
"""

from __future__ import annotations

import asyncio
import json

from fake_upstream import call, tool_calls
from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    run_goal,
    start_run,
    store_of,
    system_prompt,
    tool_names,
    tool_results,
    wait_for_run,
)


def build_team(backend) -> dict:
    store = store_of(backend)
    supervisor = store.supervisor()
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    outsider = store.create_agent(name="丙", instructions="写文档", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"], reviewer["id"]],
    )
    other = store.create_conversation(
        kind="group", title="文档组", purpose="写文档", member_ids=[supervisor["id"], outsider["id"]]
    )
    direct = store.ensure_direct_conversation(designer["id"])
    return {
        "supervisor": supervisor,
        "designer": designer,
        "reviewer": reviewer,
        "group": group,
        "other": other,
        "direct": direct,
    }


def test_a_member_in_a_private_chat_takes_an_open_question_to_its_group(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [
        tool_calls(call("q1", "ask_group", {"group_id": "方案小组", "question": "这个方案风险大吗？"})),
        {"content": "主要风险在工期"},
        {"content": "工期要留余量"},
        {"content": "我问了群里：乙觉得风险主要在工期，主管建议留余量。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["direct"]["id"], "你觉得这个方案能行吗")
            assert run["status"] == "completed", run
            chat = await get_conversation(client, team["direct"]["id"])
            group = await get_conversation(client, team["group"]["id"])

        # The caller's chat keeps one row that links to the group, then the caller's own answer.
        rows = [m for m in chat["messages"] if m["kind"] != "user"]
        assert [m["kind"] for m in rows] == ["action", "agent"]
        assert rows[0]["action"]["tool_name"] == "ask_group"
        assert rows[0]["action"]["summary"] == "在「方案小组」里问了大家"
        assert rows[0]["action"]["target_conversation_id"] == team["group"]["id"]
        assert rows[1]["author_id"] == team["designer"]["id"]

        # In the group: the question under the caller's name, then 乙 and the supervisor answer it.
        question, *answers = agent_messages(group)
        assert question["author_id"] == team["designer"]["id"]
        assert question["content"] == "这个方案风险大吗？"
        assert [a["author_id"] for a in answers] == [team["reviewer"]["id"], team["supervisor"]["id"]]
        assert all(a["reply_to_id"] == question["id"] for a in answers)

        first, reviewer, closing, last = upstream.requests
        assert "ask_group" in tool_names(first)
        # The member knows when to ask whom, and which groups it may ask.
        system = system_prompt(first)
        assert "找同事帮忙" in system and "ask_group 带到相关的群里" in system
        assert "方案小组" in system and "文档组" not in system
        assert "「甲」在群里说：这个方案风险大吗？" in reviewer["messages"][-1]["content"]
        assert "「乙」已经在群里发过言了" in closing["messages"][-1]["content"]
        # The answers come back to the asker as its tool result.
        result = dict(tool_results(last))["q1"]
        assert [a["from"] for a in result["answers"]] == ["乙", "主管"]
        assert result["answers"][0]["content"] == "主要风险在工期"

    asyncio.run(body())


def test_ask_group_only_reaches_groups_the_caller_is_in_and_not_from_inside_one(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [
        tool_calls(
            call("q1", "ask_group", {"group_id": "文档组", "question": "文档写到哪了"}),
            call("q2", "ask_group", {"group_id": "没有这个群", "question": "在吗"}),
        ),
        {"content": "我自己回答吧"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["direct"]["id"], "问问进度")
            assert run["status"] == "completed", run
            group = await get_conversation(client, team["other"]["id"])

        results = dict(tool_results(upstream.requests[1]))
        assert "你不在群组「文档组」里" in json.dumps(results["q1"], ensure_ascii=False)
        assert "找不到群组「没有这个群」" in json.dumps(results["q2"], ensure_ascii=False)
        assert agent_messages(group) == []

    asyncio.run(body())


def test_inside_a_group_bots_ask_with_at_and_are_not_offered_ask_group(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "我来"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 出方案")
            assert run["status"] == "completed", run

        request = upstream.requests[0]
        assert "ask_group" not in tool_names(request)
        assert "direct_message" in tool_names(request)
        assert "在群里优先 @名字 当场问" in system_prompt(request)

    asyncio.run(body())


def test_ask_group_waits_for_no_one_when_the_group_is_busy(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.chunk_delay = 0.05
    upstream.chunk_count = 40

    def answer(requests, index):
        request = requests[index - 1]
        if "你正在「方案小组」群组中工作" in system_prompt(request):
            return {"slow": True}  # 乙 keeps the group busy for about two seconds
        if any(message.get("role") == "tool" for message in request["messages"]):
            return {"content": "群里在忙，我直接回答"}
        return tool_calls(call("q1", "ask_group", {"group_id": team["group"]["id"], "question": "在吗"}))

    upstream.script = [answer]

    async def body() -> None:
        async with client_for(backend) as client:
            busy = await start_run(client, team["group"]["id"], "@乙 慢慢说")
            await asyncio.sleep(0.3)
            run = await run_goal(client, team["direct"]["id"], "问问群里")
            assert run["status"] == "completed", run
            await wait_for_run(client, busy["id"])
            group = await get_conversation(client, team["group"]["id"])

        asker = next(r for r in upstream.requests if any(m.get("role") == "tool" for m in r["messages"]))
        result = dict(tool_results(asker))["q1"]
        assert "正有一场对话在进行" in json.dumps(result, ensure_ascii=False)
        # Nothing was posted into the busy group.
        assert [m["author_id"] for m in agent_messages(group)] == [team["reviewer"]["id"]]

    asyncio.run(body())
