"""notify_user: only offered inside bot private chats, and its summary lands in the caller's chat."""

from __future__ import annotations

import asyncio

from app.prompts import MEMBER_PRIVATE_SYSTEM, MOOD_RULE
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    run_goal,
    start_run,
    store_of,
    system_prompt,
    tool_names,
    tool_results,
    wait_for_run,
)

MOOD_TAIL = "这个标签只写一次，正文和其他内容里不要再提到它。"


def seed_member(backend, name: str = "甲", instructions: str = "研究") -> dict:
    return store_of(backend).create_agent(name=name, instructions=instructions, kind="member")


def dm_call(call_id: str, agent_id: str, content: str) -> dict:
    return call(call_id, "direct_message", {"agent_id": agent_id, "content": content})


def summary_messages(detail: dict) -> list[dict]:
    return [
        message
        for message in detail["messages"]
        if message["relay"] and message["relay"]["direction"] == "summary"
    ]


def test_notify_user_is_not_offered_outside_private_chats(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend)
    group = store.create_conversation(
        kind="group",
        title="小组",
        purpose="一起做事",
        member_ids=[supervisor["id"], member["id"]],
    )
    upstream.script = [{"content": "主管直接回答"}, {"content": "群里回答"}]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, direct_id, "直接问一句")
            group_run = await start_run(
                client, group["id"], "请回答", mentions=[member["id"]]
            )
            assert (await wait_for_run(client, group_run["id"]))["status"] == "completed"

    asyncio.run(body())

    supervisor_tools = tool_names(upstream.request(0))
    assert "direct_message" in supervisor_tools
    assert "notify_user" not in supervisor_tools
    member_tools = tool_names(upstream.request(1))
    assert "direct_message" in member_tools
    assert "notify_user" not in member_tools


def test_notify_user_writes_a_summary_into_the_callers_own_chat(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend)
    member_chat = store.ensure_direct_conversation(member["id"])
    upstream.script = [
        tool_calls(dm_call("call_1", supervisor["id"], "这版方案定了吗？")),
        tool_calls(call("call_2", "notify_user", {"content": "方案已经确定，可以按第二版开工。"})),
        {"content": "定了，按第二版做。"},
        {"content": "好，我按第二版开工。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, member_chat["id"], "去问主管方案")
            assert run["status"] == "completed", run

            workspace = await get_workspace(client)
            private = next(c for c in workspace["conversations"] if c["kind"] == "private")
            private_detail = await get_conversation(client, private["id"])
            assert [m["content"] for m in agent_messages(private_detail)] == [
                "这版方案定了吗？",
                "定了，按第二版做。",
            ]

            direct = await get_conversation(client, store.direct_conversation()["id"])
            summaries = summary_messages(direct)
            assert len(summaries) == 1
            summary = summaries[0]
            assert summary["author_id"] == supervisor["id"]
            assert summary["author_name"] == "主管"
            assert summary["kind"] == "agent"
            assert summary["status"] == "complete"
            assert summary["content"] == "方案已经确定，可以按第二版开工。"
            assert summary["relay"] == {
                "conversation_id": private["id"],
                "direction": "summary",
                "peer_id": member["id"],
                "peer_name": "甲",
            }
            # Never a copy of what was said in the private chat.
            assert "这版方案定了吗" not in summary["content"]

            markers = [
                message
                for message in direct["messages"]
                if message["relay"] and message["relay"]["direction"] in ("in", "out")
            ]
            assert len(markers) == 2
            assert all(marker["content"] == "" for marker in markers)
            assert [marker["relay"]["direction"] for marker in markers] == ["in", "out"]

    asyncio.run(body())

    supervisor_private = upstream.request(1)
    assert "notify_user" in tool_names(supervisor_private)
    results = dict(tool_results(upstream.request(2)))
    assert results["call_2"]["ok"] is True
    assert "notify_user" not in tool_names(upstream.request(0))
    assert "notify_user" not in tool_names(upstream.request(3))


def test_a_second_notification_in_the_same_turn_is_refused(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend)
    member_chat = store.ensure_direct_conversation(member["id"])
    upstream.script = [
        tool_calls(dm_call("call_1", supervisor["id"], "帮我确认两件事")),
        tool_calls(call("call_2", "notify_user", {"content": "第一件事已经解决了。"})),
        tool_calls(call("call_3", "notify_user", {"content": "第二件事也已经解决了。"})),
        {"content": "两件事都确认过了。"},
        {"content": "收到。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, member_chat["id"], "让主管确认两件事")
            assert run["status"] == "completed", run

            workspace = await get_workspace(client)
            private = next(c for c in workspace["conversations"] if c["kind"] == "private")
            private_detail = await get_conversation(client, private["id"])
            actions = action_messages(private_detail)
            assert [entry["action"]["tool_name"] for entry in actions] == [
                "notify_user",
                "notify_user",
            ]
            assert [entry["action"]["status"] for entry in actions] == ["completed", "error"]
            assert "已经告诉过用户" in actions[1]["action"]["error"]
            assert summary_messages(private_detail) == []

            direct = await get_conversation(client, store.direct_conversation()["id"])
            summaries = summary_messages(direct)
            assert [message["content"] for message in summaries] == ["第一件事已经解决了。"]

    asyncio.run(body())

    results = dict(tool_results(upstream.request(3)))
    assert results["call_2"]["ok"] is True
    assert results["call_3"]["ok"] is False
    assert "已经告诉过用户" in results["call_3"]["error"]


def test_private_prompts_state_the_notification_rules(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend)
    member_chat = store.ensure_direct_conversation(member["id"])
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "在吗")),
        {"content": "在。"},
        {"content": "问完了。"},
        tool_calls(dm_call("call_2", supervisor["id"], "在吗")),
        {"content": "在。"},
        {"content": "好。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            assert (await run_goal(client, direct_id, "和甲说一句"))["status"] == "completed"
            assert (await run_goal(client, member_chat["id"], "和主管说一句"))["status"] == (
                "completed"
            )

    asyncio.run(body())

    member_private = system_prompt(upstream.request(1))
    supervisor_private = system_prompt(upstream.request(4))
    for prompt in (member_private, supervisor_private):
        assert "只能看到" in prompt
        assert "notify_user" in prompt
        assert "不要转发或引用" in prompt
        assert "大多数私聊不需要" in prompt
        assert prompt.rstrip().endswith(MOOD_TAIL)
        assert prompt.index("notify_user") < prompt.index("<mood>")

    # The member template itself ends with the mood rule.
    formatted = MEMBER_PRIVATE_SYSTEM.format(name="甲", instructions="研究", peer="乙")
    assert formatted.endswith(MOOD_RULE)
    assert "notify_user" in formatted
    assert "只能看到" in formatted
