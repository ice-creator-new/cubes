"""Per-run budget boundaries: stop before the excess work, end as ``limited``."""

from __future__ import annotations

import asyncio

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    names_of,
    run_goal,
    store_of,
    tool_names,
    use_standard_budget,
)


def test_model_call_budget_ends_the_run_as_limited(make_backend, upstream) -> None:
    backend = make_backend(max_model_calls=2)
    use_standard_budget(backend)
    upstream.script = [
        {
            "content": "先说明一下计划",
            "tool_calls": [
                call("call_1", "create_agent", {"name": "成员1", "instructions": "做事"})
            ],
        },
        lambda requests, index: tool_calls(
            call(f"call_{index}", "create_agent", {"name": f"成员{index}", "instructions": "做事"})
        ),
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "做一个大项目")
            assert run["status"] == "limited", run
            assert run["model_calls"] == 2
            assert run["finished_at"] is not None
            assert upstream.request_count == 2

            workspace = await get_workspace(client)
            assert names_of(workspace["agents"][1:]) == ["成员1", "成员2"]
            assert workspace["active_runs"] == []

            detail = await get_conversation(client, direct_id)
            actions = action_messages(detail)
            assert [action["action"]["status"] for action in actions] == [
                "completed",
                "completed",
            ]
            # Text produced before the budget stop is kept, never rolled back.
            assert agent_messages(detail)[0]["content"] == "先说明一下计划"
            assert agent_messages(detail)[0]["status"] == "complete"

    asyncio.run(body())


def test_tool_call_budget_ends_the_run_as_limited(make_backend, upstream) -> None:
    backend = make_backend(max_tool_calls=2)
    use_standard_budget(backend)
    upstream.script = [
        tool_calls(
            call("call_1", "create_agent", {"name": "甲", "instructions": "做事"}),
            call("call_2", "create_agent", {"name": "乙", "instructions": "做事"}),
            call("call_3", "create_agent", {"name": "丙", "instructions": "做事"}),
        )
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "建三个人")
            assert run["status"] == "limited", run
            assert run["tool_calls"] == 2
            assert upstream.request_count == 1

            workspace = await get_workspace(client)
            assert names_of(workspace["agents"][1:]) == ["甲", "乙"]

            actions = action_messages(await get_conversation(client, direct_id))
            assert [action["action"]["status"] for action in actions] == ["completed", "completed", "error"]
            assert "工具执行上限" in actions[2]["action"]["error"]
            assert actions[2]["action"]["tool_name"] == "create_agent"

    asyncio.run(body())


def test_new_agent_budget_ends_the_run_as_limited(make_backend, upstream) -> None:
    backend = make_backend(max_new_agents=1)
    upstream.script = [
        tool_calls(
            call("call_1", "create_agent", {"name": "甲", "instructions": "做事"}),
            call("call_2", "create_agent", {"name": "乙", "instructions": "做事"}),
        )
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "建两个人")
            assert run["status"] == "limited", run
            workspace = await get_workspace(client)
            assert names_of(workspace["agents"][1:]) == ["甲"]
            actions = action_messages(await get_conversation(client, direct_id))
            assert [action["action"]["status"] for action in actions] == ["completed", "error"]
            assert "成员创建上限" in actions[1]["action"]["error"]

    asyncio.run(body())


def test_new_group_budget_ends_the_run_as_limited(make_backend, upstream) -> None:
    backend = make_backend(max_new_groups=1)
    upstream.script = [
        tool_calls(call("call_0", "create_agent", {"name": "甲", "instructions": "做事"})),
        tool_calls(
            call("call_1", "create_group", {"title": "甲组", "purpose": "a", "member_ids": ["甲"]}),
            call("call_2", "create_group", {"title": "乙组", "purpose": "b", "member_ids": ["甲"]}),
        ),
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "建两个群")
            assert run["status"] == "limited", run
            workspace = await get_workspace(client)
            groups = [c for c in workspace["conversations"] if c["kind"] == "group"]
            assert [group["title"] for group in groups] == ["甲组"]
            actions = action_messages(await get_conversation(client, direct_id))
            assert [action["action"]["status"] for action in actions] == [
                "completed",
                "completed",
                "error",
            ]

    asyncio.run(body())


def test_budgets_are_counted_per_run_and_entities_are_reused(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "做事"})),
        {"content": "第一次完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await run_goal(client, direct_id, "建一个成员")
            assert first["status"] == "completed"
            assert first["model_calls"] == 2
            assert first["tool_calls"] == 1

            second = await run_goal(client, direct_id, "再回答一个问题")
            assert second["status"] == "completed"
            assert second["model_calls"] == 1
            assert second["tool_calls"] == 0
            workspace = await get_workspace(client)
            assert names_of(workspace["agents"][1:]) == ["甲"]
            # The second run starts from the stored context, not from scratch.
            contents = [m["content"] for m in upstream.requests[2]["messages"]]
            assert "第一次完成" in contents

    asyncio.run(body())


def calc(index: int) -> dict:
    return call(f"calc_{index}", "calculate", {"expression": f"{index} + 1"})


def test_a_turn_out_of_tools_answers_with_what_it_has(make_backend, upstream) -> None:
    """The refused calls say why; the bot's next call is its last, and its answer is kept."""
    backend = make_backend(max_turn_tool_calls=2)
    use_standard_budget(backend)
    upstream.script = [
        tool_calls(calc(1), calc(2)),
        tool_calls(calc(3), calc(4)),
        {"content": "前两步算完了，第三步留给下一轮。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "算四步")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 2

            detail = await get_conversation(client, direct_id)
            statuses = [action["action"]["status"] for action in action_messages(detail)]
            assert statuses == ["completed", "completed", "error", "error"]
            assert "这一轮的工具调用上限（2）" in action_messages(detail)[2]["action"]["error"]
            assert agent_messages(detail)[-1]["content"] == "前两步算完了，第三步留给下一轮。"

        # Both refused calls tell the model to answer now.
        last = upstream.requests[2]
        refused = [m["content"] for m in last["messages"] if m.get("role") == "tool"][-2:]
        assert all("不能再调用工具" in text for text in refused)

    asyncio.run(body())


def test_a_turn_that_asks_for_tools_after_its_last_call_ends_that_turn_only(
    make_backend, upstream
) -> None:
    """Its budget is the bot's own: the reply stops, the task it was part of does not."""
    backend = make_backend(max_turn_tool_calls=1)
    use_standard_budget(backend)
    upstream.script = [tool_calls(calc(1)), tool_calls(calc(2)), tool_calls(calc(3))]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "一直算")
            assert run["status"] == "completed", run
            assert run["error"] is None
            # One call went through, the wrap offered a last answer, and the tool asked for in
            # that answer was refused: three requests, and the third one ends the turn.
            assert upstream.request_count == 3
            detail = await get_conversation(client, direct_id)
            last = action_messages(detail)[-1]["action"]
            assert last["status"] == "error"
            assert "调用预算用完了" in (last["error"] or "")

    asyncio.run(body())


def test_each_member_in_a_relay_has_its_own_turn_budget(make_backend, upstream) -> None:
    """The member who speaks second is not starved by the tools the first one used."""
    backend = make_backend(max_turn_tool_calls=2)
    use_standard_budget(backend)
    store = store_of(backend)
    supervisor = store.supervisor()
    first = store.create_agent(name="甲", instructions="调研", kind="member")
    second = store.create_agent(name="乙", instructions="评估", kind="member")
    group = store.create_conversation(
        kind="group",
        title="小组",
        purpose="评估",
        member_ids=[supervisor["id"], first["id"], second["id"]],
    )
    upstream.script = [
        tool_calls(calc(1), calc(2)),
        {"content": "查完了 @乙 你来评估"},
        tool_calls(calc(3), calc(4)),
        {"content": "评估完了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@甲 先调研")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 4

            detail = await get_conversation(client, group["id"])
            assert all(a["action"]["status"] == "completed" for a in action_messages(detail))
            assert [m["author_name"] for m in agent_messages(detail)] == ["甲", "乙"]

        assert "calculate" in tool_names(upstream.requests[2])

    asyncio.run(body())


def test_the_default_preset_is_unlimited_and_a_turn_can_exceed_the_old_32_ceiling(
    make_backend, upstream
) -> None:
    """No step ceiling by default: 40 tool calls in one turn, then the model answers."""
    backend = make_backend()

    def rounds(requests, index):
        if index <= 8:
            start = (index - 1) * 5
            return tool_calls(*[calc(start + step) for step in range(1, 6)])
        return {"content": "40 步算完了。"}

    upstream.script = [rounds]

    async def body() -> None:
        async with client_for(backend) as client:
            settings = (await client.get("/settings/team")).json()
            assert settings["step_budget"] == "unlimited"
            assert settings["step_budgets"]["unlimited"]["unlimited"] is True
            assert settings["step_budgets"]["unlimited"]["turn_tool_calls"] == 0
            # The sandbox caps follow the preset too: none at all, or the standard shape scaled.
            assert settings["step_budgets"]["unlimited"]["computer_steps"] == 0
            assert settings["step_budgets"]["unlimited"]["computer_screen_steps"] == 0
            assert settings["step_budgets"]["standard"]["computer_steps"] == 40
            assert settings["step_budgets"]["standard"]["computer_screen_steps"] == 8
            assert settings["step_budgets"]["long"]["computer_steps"] == 120
            assert settings["step_budgets"]["long"]["computer_screen_steps"] == 24
            assert settings["step_budgets"]["marathon"]["computer_steps"] == 300
            assert settings["step_budgets"]["marathon"]["computer_shell_steps"] == 225

            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "算 40 步")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 40
            assert run["wrapped"] is False
            actions = action_messages(await get_conversation(client, direct_id))
            assert all(action["action"]["status"] == "completed" for action in actions)
            # No budget notice when nothing was cut.
            notices = [
                m["content"]
                for m in (await get_conversation(client, direct_id))["messages"]
                if m["kind"] == "notice"
            ]
            assert not any("先停在这里" in line for line in notices)

    asyncio.run(body())


def test_standard_preset_restores_the_32_tool_ceiling_with_a_continue_notice(
    make_backend, upstream
) -> None:
    """Standard is today's behaviour: 32 tools, the wrap note, and a 继续 notice."""
    backend = make_backend()
    use_standard_budget(backend)

    def rounds(requests, index):
        if index <= 8:
            start = (index - 1) * 5
            return tool_calls(*[calc(start + step) for step in range(1, 6)])
        return {"content": "先算到这里。"}

    upstream.script = [rounds]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "一直算")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 32
            assert run["wrapped"] is True

            detail = await get_conversation(client, direct_id)
            refused = [a for a in action_messages(detail) if a["action"]["status"] == "error"]
            assert refused and "这一轮的工具调用上限（32）" in refused[0]["action"]["error"]
            notices = [m["content"] for m in detail["messages"] if m["kind"] == "notice"]
            assert any(
                "这一轮用了 32 步" in line and "发「继续」接着做" in line for line in notices
            )

        # The wrap call told the model it may not call tools any more.
        last = upstream.requests[-1]
        refused_results = [
            m.get("content")
            for m in last["messages"]
            if m.get("role") == "tool" and "不能再调用工具" in str(m.get("content"))
        ]
        assert len(refused_results) >= 3

    asyncio.run(body())


def test_continue_after_a_limited_run_in_a_direct_chat_carries_the_resume_cue(
    make_backend, upstream
) -> None:
    """「继续」 after a limited run tells the bot to pick up where it stopped."""
    backend = make_backend(max_tool_calls=1)
    use_standard_budget(backend)
    upstream.script = [
        tool_calls(
            call("call_1", "create_agent", {"name": "甲", "instructions": "做事"}),
            call("call_2", "create_agent", {"name": "乙", "instructions": "做事"}),
        ),
        {"content": "接着做完了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await run_goal(client, direct_id, "建两个人")
            assert first["status"] == "limited", first

            detail = await get_conversation(client, direct_id)
            notices = [m["content"] for m in detail["messages"] if m["kind"] == "notice"]
            assert any("发「继续」接着做" in line for line in notices)

            second = await run_goal(client, direct_id, "继续")
            assert second["status"] == "completed", second

        # The second run's request carries the cue at the end of the user turn.
        contents = [
            message.get("content")
            for message in upstream.requests[-1]["messages"]
            if message.get("role") == "user"
        ]
        assert any(
            isinstance(text, str) and "接着上一次没做完的工作" in text for text in contents
        )

    asyncio.run(body())
