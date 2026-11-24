"""Members and the supervisor share the toolkit packs: real calls, honest errors."""

from __future__ import annotations

import asyncio

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    run_goal,
    store_of,
    system_prompt,
)


def member_direct(backend) -> tuple[dict, str]:
    """Seed one member and return it with the id of its direct thread."""
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="算术与事实", kind="member")
    conversation = store.ensure_direct_conversation(member["id"])
    assert conversation is not None
    return member, conversation["id"]


def tool_texts(request: dict) -> dict[str, str]:
    """Raw tool results by call id: toolkit packs answer with plain text, not JSON."""
    return {
        message["tool_call_id"]: message["content"]
        for message in request["messages"]
        if message.get("role") == "tool"
    }


def tool_names(request: dict) -> list[str]:
    return [entry["function"]["name"] for entry in request["tools"] or []]


def test_a_member_calls_a_toolkit_tool_and_continues(make_backend, upstream) -> None:
    backend = make_backend()
    member, conversation_id = member_direct(backend)
    upstream.script = [
        tool_calls(call("call_1", "calculate", {"expression": "6*7"})),
        {"content": "结果是 42。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, conversation_id, "帮我算一下 6 乘 7")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 2
            assert run["tool_calls"] == 1

            detail = await get_conversation(client, conversation_id)
            actions = action_messages(detail)
            assert len(actions) == 1
            action = actions[0]
            assert action["author_id"] == member["id"]
            assert action["action"]["tool_name"] == "calculate"
            assert action["action"]["status"] == "completed"
            assert action["status"] == "complete"
            assert action["content"] == action["action"]["summary"]
            assert action["content"].strip()
            # The reply after the tool call is the only visible agent text.
            assert [m["content"] for m in agent_messages(detail)] == ["结果是 42。"]

        first, second = upstream.requests
        assert "calculate" in tool_names(first)
        assert "direct_message" in tool_names(first)
        # Management tools stay supervisor-only.
        assert "create_agent" not in tool_names(first)
        assert "42" in tool_texts(second)["call_1"]
        assert "甲" in system_prompt(first)

    asyncio.run(body())


def test_a_broken_tool_call_becomes_an_error_action_and_the_run_continues(
    make_backend, upstream
) -> None:
    backend = make_backend()
    member, conversation_id = member_direct(backend)
    upstream.script = [
        # Not valid JSON: the engine refuses it before any tool runs.
        tool_calls(call("call_1", "calculate", "{oops")),
        {"content": "我换一种算法，结果是 42。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, conversation_id, "算一下")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 1

            detail = await get_conversation(client, conversation_id)
            actions = action_messages(detail)
            assert len(actions) == 1
            action = actions[0]
            assert action["action"]["status"] == "error"
            assert action["action"]["error"]
            assert action["action"]["error"].endswith("。")
            assert "失败" in action["content"]
            assert action["author_id"] == member["id"]
            assert [m["content"] for m in agent_messages(detail)] == [
                "我换一种算法，结果是 42。"
            ]

        replayed = upstream.requests[1]["messages"]
        result = next(m for m in replayed if m["role"] == "tool")
        assert result["tool_call_id"] == "call_1"
        assert "ok" in result["content"] and "error" in result["content"]
        # The failed call is replayed as an assistant tool turn, then a tool result.
        assert replayed[-2]["role"] == "assistant"
        assert replayed[-2]["tool_calls"][0]["function"]["name"] == "calculate"

    asyncio.run(body())


def test_the_supervisor_calls_a_toolkit_tool(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "current_time", {})),
        {"content": "时间已经确认。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            supervisor = workspace["agents"][0]
            run = await run_goal(client, direct_id, "现在几点")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 1

            actions = action_messages(await get_conversation(client, direct_id))
            assert len(actions) == 1
            assert actions[0]["action"]["tool_name"] == "current_time"
            assert actions[0]["action"]["status"] == "completed"
            assert actions[0]["author_id"] == supervisor["id"]

        first, second = upstream.requests
        assert "current_time" in tool_names(first)
        # The supervisor keeps its management tools next to the shared packs.
        assert "create_agent" in tool_names(first)
        assert tool_texts(second)["call_1"].strip()

    asyncio.run(body())
