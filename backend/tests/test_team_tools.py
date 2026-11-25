"""Real native tool execution: entities, ordering, member isolation and identity."""

from __future__ import annotations

import asyncio
import json

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    assistant_tool_calls,
    client_for,
    find_result_key,
    get_conversation,
    get_workspace,
    kinds,
    names_of,
    result_keys,
    run_goal,
    start_run,
    store_of,
    system_prompt,
    tool_names,
    tool_results,
    use_standard_budget,
    wait_for_run,
)



def test_supervisor_uses_native_tools_to_build_a_team_and_consult_a_member(
    make_backend, upstream
) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(
            call("call_1", "create_agent", {"name": "产品研究员", "instructions": "研究竞品与用户"}),
            call("call_2", "create_agent", {"name": "交互设计师", "instructions": "设计交互方案"}),
        ),
        lambda requests, index: tool_calls(
            call(
                "call_3",
                "create_group",
                {
                    "title": "产品验证小组",
                    "purpose": "验证产品方向",
                    "member_ids": result_keys(requests, "agent_id"),
                },
            )
        ),
        lambda requests, index: tool_calls(
            call(
                "call_4",
                "post_group_message",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "content": "请各自给出结论",
                },
            )
        ),
        lambda requests, index: tool_calls(
            call(
                "call_5",
                "ask_agent",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "agent_id": result_keys(requests, "agent_id")[0],
                    "task": "给出你的产品结论",
                },
            )
        ),
        {"content": "成员结论：先做小范围验证"},
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "帮我验证一个产品方向")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 6
            assert run["tool_calls"] == 5

            workspace = await get_workspace(client)
            assert kinds(workspace["agents"]) == ["supervisor", "member", "member"]
            assert names_of(workspace["agents"][1:]) == ["产品研究员", "交互设计师"]
            supervisor = workspace["agents"][0]
            researcher, designer = workspace["agents"][1], workspace["agents"][2]

            groups = [c for c in workspace["conversations"] if c["kind"] == "group"]
            assert len(groups) == 1
            group = groups[0]
            assert group["title"] == "产品验证小组"
            assert group["purpose"] == "验证产品方向"
            assert group["member_ids"] == [researcher["id"], designer["id"]]
            # Conversations are newest first, behind the pinned supervisor direct thread.
            assert workspace["conversations"][0]["id"] == direct_id

            direct = await get_conversation(client, direct_id)
            actions = action_messages(direct)
            assert [m["action"]["status"] for m in actions] == ["completed"] * 5
            assert [m["status"] for m in actions] == ["complete"] * 5
            assert all(m["author_id"] == supervisor["id"] for m in actions)
            assert [m["action"]["target_agent_id"] for m in actions[:2]] == [
                researcher["id"],
                designer["id"],
            ]
            assert actions[2]["action"]["target_conversation_id"] == group["id"]
            assert actions[3]["action"]["target_conversation_id"] == group["id"]
            assert actions[4]["action"]["target_agent_id"] == researcher["id"]
            assert actions[4]["action"]["target_conversation_id"] == group["id"]
            assert agent_messages(direct)[-1]["content"] == "汇总完成"

            group_detail = await get_conversation(client, group["id"])
            assert [m["content"] for m in group_detail["messages"]] == [
                "请各自给出结论",
                "成员结论：先做小范围验证",
            ]
            reply = group_detail["messages"][1]
            assert reply["kind"] == "agent"
            assert reply["author_id"] == researcher["id"]
            assert reply["author_name"] == "产品研究员"
            assert reply["avatar_seed"] == researcher["avatar_seed"]
            assert reply["status"] == "complete"

        requests = upstream.requests
        assert upstream.request_count == 6

        # Chronological replay: one assistant tool_calls turn then one tool result per call.
        replayed = requests[1]["messages"]
        turn, first, second = replayed[-3], replayed[-2], replayed[-1]
        assert [entry["id"] for entry in turn["tool_calls"]] == ["call_1", "call_2"]
        assert [entry["tool_call_id"] for entry in (first, second)] == ["call_1", "call_2"]
        assert turn["role"] == "assistant"

        # The member really ran with its own instructions and only this group's history.
        member_request = requests[4]
        assert "calculate" in tool_names(member_request)
        assert "create_agent" not in tool_names(member_request)
        member_system = system_prompt(member_request)
        assert "产品研究员" in member_system
        assert "研究竞品与用户" in member_system
        assert "产品验证小组" in member_system
        assert "验证产品方向" in member_system
        member_contents = [m["content"] for m in member_request["messages"]]
        assert any("给出你的产品结论" in content for content in member_contents)
        assert any("请各自给出结论" in content for content in member_contents)
        assert not any("帮我验证一个产品方向" in content for content in member_contents)

        # The member's real reply comes back to the supervisor as a tool result.
        results = dict(tool_results(requests[5]))
        assert results["call_5"]["ok"] is True
        assert results["call_5"]["content"] == "成员结论：先做小范围验证"
        assert results["call_5"]["conversation_id"] == group["id"]

    asyncio.run(body())


def test_set_group_members_updates_membership(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})),
        tool_calls(
            call("call_2", "create_group", {"title": "小组", "purpose": "一起做事", "member_ids": ["甲"]})
        ),
        lambda requests, index: tool_calls(
            call(
                "call_3",
                "set_group_members",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "member_ids": result_keys(requests, "agent_id"),
                },
            )
        ),
        {"content": "已调整"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "建一个小组")
            assert run["status"] == "completed", run
            workspace = await get_workspace(client)
            supervisor = workspace["agents"][0]
            member = workspace["agents"][1]
            group = next(c for c in workspace["conversations"] if c["kind"] == "group")
            assert group["member_ids"] == [member["id"]]
            actions = action_messages(await get_conversation(client, direct_id))
            assert actions[1]["action"]["target_conversation_id"] == group["id"]
            assert actions[2]["action"]["target_conversation_id"] == group["id"]
            assert all(action["action"]["status"] == "completed" for action in actions)

    asyncio.run(body())


def test_ask_agent_enforces_group_membership_and_member_kind(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="甲", instructions="研究", kind="member")
    other = store.create_agent(name="乙", instructions="设计", kind="member")
    group_a = store.create_conversation(
        kind="group", title="甲组", purpose="a", member_ids=[supervisor["id"], member["id"]]
    )
    group_b = store.create_conversation(
        kind="group", title="乙组", purpose="b", member_ids=[supervisor["id"], other["id"]]
    )
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "ask_agent",
                {"group_id": group_b["id"], "agent_id": member["id"], "task": "t"},
            ),
            call(
                "call_2",
                "ask_agent",
                {"group_id": group_a["id"], "agent_id": supervisor["id"], "task": "t"},
            ),
            call(
                "call_3",
                "set_group_members",
                {"group_id": group_a["id"], "member_ids": ["a_missing"]},
            ),
        ),
        {"content": "了解限制"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group_a["id"], "在甲组里做事")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 3
            detail = await get_conversation(client, group_a["id"])
            actions = action_messages(detail)
            assert [action["action"]["status"] for action in actions] == [
                "error",
                "error",
                "error",
            ]
            assert "不在群组" in actions[0]["action"]["error"]
            assert "不能作为被咨询的成员" in actions[1]["action"]["error"]
            assert "不存在" in actions[2]["action"]["error"]
            # No member was ever consulted, so the group only holds the goal and the answer.
            assert [m["kind"] for m in detail["messages"]] == ["user", "action", "action", "action", "agent"]
            workspace = await get_workspace(client)
            assert workspace["agents"][1]["id"] == member["id"]
            assert workspace["agents"][2]["id"] == other["id"]

    asyncio.run(body())


def test_supervisor_context_is_scoped_to_the_selected_conversation(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="甲", instructions="研究竞品", kind="member")
    group = store.create_conversation(
        kind="group", title="验证小组", purpose="验证方向", member_ids=[supervisor["id"], member["id"]]
    )
    direct = store.direct_conversation()
    assert direct is not None
    store.append_message(
        conversation_id=direct["id"],
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="私聊里独有的内容",
        status="complete",
    )
    upstream.script = [{"content": "好的"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "群里的目标")
            assert run["status"] == "completed"
            request = upstream.requests[0]
            system = system_prompt(request)
            assert "主管" in system
            assert "验证小组" in system
            assert "验证方向" in system
            assert "甲" in system
            contents = [m["content"] for m in request["messages"]]
            # The newest user turn also carries the <runtime …/> line.
            assert any(content.startswith("群里的目标") for content in contents)
            assert "私聊里独有的内容" not in "\n".join(contents)
            assert request["tools"] is not None

    asyncio.run(body())


def test_unknown_tools_and_invalid_arguments_are_real_recoverable_errors(
    make_backend, upstream
) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(
            call("call_1", "delete_everything", {"what": "all"}),
            call("call_2", "create_agent", {"name": "缺指令的成员"}),
            call("call_3", "create_agent", "{not json"),
            call("call_4", "create_agent", {"name": "多余字段", "instructions": "x", "kind": "supervisor"}),
        ),
        {"content": "已改用直接回答"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "试试工具")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 4

            workspace = await get_workspace(client)
            assert kinds(workspace["agents"]) == ["supervisor"]

            detail = await get_conversation(client, direct_id)
            actions = action_messages(detail)
            assert [action["action"]["tool_name"] for action in actions] == [
                "delete_everything",
                "create_agent",
                "create_agent",
                "create_agent",
            ]
            assert [action["action"]["status"] for action in actions] == ["error"] * 4
            assert [action["status"] for action in actions] == ["complete"] * 4
            assert "未知工具" in actions[0]["action"]["error"]
            assert "instructions" in actions[1]["action"]["error"]
            assert "JSON" in actions[2]["action"]["error"]
            assert "kind" in actions[3]["action"]["error"]
            assert agent_messages(detail)[-1]["content"] == "已改用直接回答"

            results = tool_results(upstream.requests[1])
            assert [result["ok"] for _, result in results] == [False, False, False, False]
            assert "未知工具" in results[0][1]["error"]

    asyncio.run(body())


def test_tool_arguments_sent_as_an_object_are_still_executed(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        {
            **tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})),
            "object_arguments": True,
        },
        {"content": "完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "建一个成员")
            assert run["status"] == "completed", run
            assert names_of((await get_workspace(client))["agents"][1:]) == ["甲"]
            actions = action_messages(await get_conversation(client, direct_id))
            assert [action["action"]["status"] for action in actions] == ["completed"]
            replayed = assistant_tool_calls(upstream.requests[1])
            assert json.loads(replayed[0]["function"]["arguments"]) == {
                "name": "甲",
                "instructions": "研究",
            }

    asyncio.run(body())


def test_repeated_tool_call_id_reuses_the_stored_result(make_backend, upstream) -> None:
    backend = make_backend()
    arguments = {"name": "甲", "instructions": "研究"}
    upstream.script = [
        tool_calls(call("call_1", "create_agent", arguments)),
        tool_calls(call("call_1", "create_agent", arguments)),
        {"content": "完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "创建一个成员")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 1

            workspace = await get_workspace(client)
            assert kinds(workspace["agents"]) == ["supervisor", "member"]
            member = workspace["agents"][1]

            detail = await get_conversation(client, direct_id)
            actions = action_messages(detail)
            assert len(actions) == 1
            assert actions[0]["action"]["status"] == "completed"

            store = store_of(backend)
            assert store.find_action(run["id"], "call_1")["status"] == "completed"
            assert store.find_action(run["id"], "call_never_seen") is None

            replayed = tool_results(upstream.requests[2])
            assert len(replayed) == 2
            assert replayed[0] == replayed[1]
            assert replayed[1][1]["agent_id"] == member["id"]

    asyncio.run(body())


def test_repeated_tool_call_id_with_other_arguments_never_repeats_side_effects(
    make_backend, upstream
) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})),
        tool_calls(call("call_1", "create_agent", {"name": "乙", "instructions": "设计"})),
        {"content": "完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "创建一个成员")
            assert run["status"] == "completed", run
            assert run["tool_calls"] == 1

            workspace = await get_workspace(client)
            assert names_of(workspace["agents"][1:]) == ["甲"]

            detail = await get_conversation(client, direct_id)
            actions = action_messages(detail)
            assert [action["action"]["status"] for action in actions] == ["completed", "error"]
            assert "tool_call_id" in actions[1]["action"]["error"]

            replayed = tool_results(upstream.requests[2])
            assert replayed[0][1]["ok"] is True
            assert replayed[1][1]["ok"] is False
            assert "tool_call_id" in replayed[1][1]["error"]

    asyncio.run(body())


def test_a_simple_question_is_answered_without_creating_anything(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "这是一个简单回答"}]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "1+1 等于几？")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 1
            assert run["tool_calls"] == 0
            workspace = await get_workspace(client)
            assert kinds(workspace["agents"]) == ["supervisor"]
            assert [c["kind"] for c in workspace["conversations"]] == ["direct"]
            assert assistant_tool_calls(upstream.requests[0]) == []

    asyncio.run(body())


def test_action_messages_are_scoped_to_the_originating_run_conversation(
    make_backend, upstream
) -> None:
    backend = make_backend()
    store = store_of(backend)
    group = store.create_conversation(
        kind="group", title="小组", purpose="做事", member_ids=[store.supervisor()["id"]]
    )
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "post_group_message",
                {"group_id": group["id"], "content": "先做一版原型"},
            )
        ),
        {"content": "已经安排"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "安排一件事")
            assert run["status"] == "completed"
            assert run["conversation_id"] == direct_id
            direct = await get_conversation(client, direct_id)
            actions = action_messages(direct)
            assert [action["action"]["tool_name"] for action in actions] == ["post_group_message"]
            assert actions[0]["action"]["target_conversation_id"] == group["id"]
            group_detail = await get_conversation(client, group["id"])
            assert [m["content"] for m in group_detail["messages"]] == ["先做一版原型"]
            assert not action_messages(group_detail)

    asyncio.run(body())


def test_run_completes_without_an_sse_subscription(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "群里的回答"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            run = await start_run(client, workspace["direct_conversation_id"], "直接开始")
            assert run["status"] == "running"
            # Polling the run is enough: leaving the chat page never stops the task.
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed"
            detail = await get_conversation(client, workspace["direct_conversation_id"])
            assert detail["latest_run"]["id"] == run["id"]
            assert detail["latest_run"]["model_calls"] == 1

    asyncio.run(body())


def test_a_consulted_member_running_out_of_its_turn_budget_does_not_end_the_run(
    make_backend, upstream
) -> None:
    """A nested turn's own budget ends that reply, not the run that asked for it."""
    backend = make_backend(max_turn_model_calls=2)
    use_standard_budget(backend)
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="研究", kind="member")
    group = store.create_conversation(
        kind="group", title="小组", purpose="p", member_ids=[supervisor["id"], member["id"]]
    )
    upstream.script = [
        # The supervisor consults 甲, whose own turn then keeps asking for tools.
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "查"})
        ),
        tool_calls(call("m1", "workflow_list", {})),
        tool_calls(call("m2", "workflow_list", {})),
        # Out of calls now: this one is its last, and it still asks for a tool — the refusal
        # ends that turn instead of the run.
        tool_calls(call("m3", "workflow_list", {})),
        {"content": "我先把能做的做了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "让甲查一下")
            assert run["status"] == "completed", run
            assert run["error"] is None
            detail = await get_conversation(client, group["id"])
            names = [row["action"]["tool_name"] for row in action_messages(detail)]
            assert names[0] == "ask_agent"
            assert action_messages(detail)[0]["action"]["status"] == "completed"
            # The consulted bot's own calls were cut; the supervisor's result says why.
            results = dict(tool_results(upstream.requests[-1]))
            assert "预算用完" in results["c1"].get("note", "")
            # The supervisor still had its own turn, and its answer stands.
            assert "我先把能做的做了。" in [m["content"] for m in detail["messages"] if m["kind"] == "agent"]

    asyncio.run(body())


def test_create_group_resolves_names_reuses_a_match_and_posts_one_intro(
    make_backend, upstream
) -> None:
    backend = make_backend()
    state = {"direct": 0}

    def reply(requests, index):
        request = requests[index - 1]
        joined = " ".join(str(m.get("content")) for m in request["messages"])
        if "建一个方案组" not in joined:
            # Nothing should ask the new group's members to introduce themselves.
            return {"content": "收到"}
        state["direct"] += 1
        if state["direct"] == 1:
            return tool_calls(
                call("c1", "create_agent", {"name": "甲", "instructions": "出方案"}),
                call("c2", "create_agent", {"name": "乙", "instructions": "评风险"}),
            )
        if state["direct"] == 2:
            return tool_calls(
                call(
                    "g1",
                    "create_group",
                    {
                        "title": "方案组",
                        "purpose": "把方案做出来",
                        "member_ids": ["甲", "@乙"],
                        "include_supervisor": True,
                        "first_message": "大家开工",
                    },
                )
            )
        if state["direct"] == 3:
            # Same title and members in a different order: the existing group is reused.
            return tool_calls(
                call(
                    "g2",
                    "create_group",
                    {
                        "title": "方案组",
                        "purpose": "把方案做出来",
                        "member_ids": ["乙", "甲"],
                        "include_supervisor": True,
                    },
                )
            )
        return {"content": "都建好了"}

    upstream.script = [reply]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "建一个方案组")
            assert run["status"] == "completed", run
            workspace = await get_workspace(client)
            supervisor = workspace["agents"][0]
            first, second = workspace["agents"][1], workspace["agents"][2]
            groups = [c for c in workspace["conversations"] if c["kind"] == "group"]
            assert len(groups) == 1
            group = groups[0]
            # Names and 「@名字」 resolve; the supervisor joins only because it was asked for.
            assert group["member_ids"] == [supervisor["id"], first["id"], second["id"]]

            # Only the creator's one-line opening stays: no member introduces itself.
            group_detail = await get_conversation(client, group["id"])
            assert [m["kind"] for m in group_detail["messages"]] == ["agent"]
            opening = group_detail["messages"][0]
            assert opening["author_id"] == supervisor["id"]
            assert opening["content"] == "大家开工"

            results = {
                key: value
                for request in upstream.requests
                if "建一个方案组" in " ".join(
                    str(m.get("content")) for m in request["messages"]
                )
                for key, value in tool_results(request)
            }
            assert results["g1"]["conversation_id"] == group["id"]
            assert results["g1"]["existing"] is False
            assert results["g2"]["existing"] is True
            assert results["g2"]["conversation_id"] == group["id"]

        # The direct run answered twice about the group; no member intro turn was served.
        assert upstream.request_count == 4

    asyncio.run(body())
