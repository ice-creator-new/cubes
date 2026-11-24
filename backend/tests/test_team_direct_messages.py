"""Visible bot-to-bot private chats: creation, reuse, relays, depth, stop and budgets."""

from __future__ import annotations

import asyncio
import time

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    run_goal,
    start_run,
    stop_run,
    store_of,
    system_prompt,
    use_standard_budget,
    tool_results,
    wait_for_message,
)


def private_conversations(workspace: dict) -> list[dict]:
    return [c for c in workspace["conversations"] if c["kind"] == "private"]


def seed_member(backend, name: str, instructions: str = "做研究") -> dict:
    return store_of(backend).create_agent(name=name, instructions=instructions, kind="member")


def dm_call(call_id: str, agent_id: str, content: str) -> dict:
    return call(call_id, "direct_message", {"agent_id": agent_id, "content": content})


def test_supervisor_direct_messages_a_member(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲", "天气与出行")
    supervisor_id = store.supervisor()["id"]
    member_direct_id = store.ensure_direct_conversation(member["id"])["id"]
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "帮我确认今天的天气")),
        {"content": "今天晴，适合出门。"},
        {"content": "成员说今天晴。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            run = await run_goal(client, direct_id, "让甲查一下天气")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 3
            assert run["tool_calls"] == 1

            workspace = await get_workspace(client)
            privates = private_conversations(workspace)
            assert len(privates) == 1
            private = privates[0]
            assert private["title"] == "主管 ⇄ 甲"
            assert len(private["member_ids"]) == 2
            assert set(private["member_ids"]) == {supervisor_id, member["id"]}
            assert private["last_message"] == "今天晴，适合出门。"

            # The private history is exactly the two bot messages, nobody else's.
            detail = await get_conversation(client, private["id"])
            assert [m["kind"] for m in detail["messages"]] == ["agent", "agent"]
            first, second = detail["messages"]
            assert first["author_id"] == supervisor_id
            assert first["content"] == "帮我确认今天的天气"
            assert first["status"] == "complete"
            assert second["author_id"] == member["id"]
            assert second["content"] == "今天晴，适合出门。"
            assert second["status"] == "complete"
            # The opening message names the user question it serves, from the caller's chat.
            caller = await get_conversation(client, direct_id)
            question = next(m for m in caller["messages"] if m["kind"] == "user")
            assert first["origin"] == {
                "message_id": question["id"],
                "conversation_id": direct_id,
                "author_name": question["author_name"],
                "content": "让甲查一下天气",
            }
            assert second["origin"] is None

            # Relay markers land in the target's own direct chat with the user, but they
            # carry no text at all: only the source and the fact a message was sent.
            member_chat = await get_conversation(client, member_direct_id)
            relays = [m for m in member_chat["messages"] if m["relay"]]
            assert len(relays) == 2
            incoming, outgoing = relays
            assert incoming["author_id"] == supervisor_id
            assert incoming["author_name"] == "主管"
            assert incoming["content"] == ""
            assert incoming["relay"] == {
                "conversation_id": private["id"],
                "direction": "in",
                "peer_id": supervisor_id,
                "peer_name": "主管",
            }
            assert outgoing["author_id"] == member["id"]
            assert outgoing["author_name"] == "甲"
            assert outgoing["content"] == ""
            assert outgoing["relay"] == {
                "conversation_id": private["id"],
                "direction": "out",
                "peer_id": supervisor_id,
                "peer_name": "主管",
            }

            # The caller's conversation shows one ordinary action row that points at both.
            caller = await get_conversation(client, direct_id)
            actions = action_messages(caller)
            assert len(actions) == 1
            action = actions[0]
            assert action["action"]["tool_name"] == "direct_message"
            assert action["action"]["status"] == "completed"
            assert action["action"]["target_agent_id"] == member["id"]
            assert action["action"]["target_conversation_id"] == private["id"]
            assert action["content"] == "私信 甲"
            assert agent_messages(caller)[-1]["content"] == "成员说今天晴。"

        member_request = upstream.requests[1]
        assert "今天晴" in str(tool_results(upstream.requests[2]))
        results = dict(tool_results(upstream.requests[2]))
        assert results["call_1"]["content"] == "今天晴，适合出门。"
        assert results["call_1"]["conversation_id"] == private["id"]
        # The target turn replays only the private history and knows its peer.
        private_prompt = system_prompt(member_request)
        assert "甲" in private_prompt and "主管" in private_prompt
        assert any(
            "帮我确认今天的天气" in (message.get("content") or "")
            for message in member_request["messages"]
        )

    asyncio.run(body())


def test_a_member_can_be_messaged_by_name_and_sees_the_team_roster(make_backend, upstream) -> None:
    """Real models address colleagues by name; members also need ids to start a DM."""
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "代码开发助手")
    other = seed_member(backend, "文档助手")
    upstream.script = [
        tool_calls(dm_call("call_1", " 代码开发助手 ", "Rust 和 Go 哪个更适合网关？")),
        {"content": "看延迟要求。"},
        {"content": "它说看延迟要求。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "问问代码开发助手")
            assert run["status"] == "completed", run
            privates = private_conversations(await get_workspace(client))
            assert [p["title"] for p in privates] == ["主管 ⇄ 代码开发助手"]
            actions = action_messages(await get_conversation(client, direct_id))
            assert actions[0]["action"]["status"] == "completed"
            assert actions[0]["action"]["target_agent_id"] == member["id"]

    asyncio.run(body())
    member_prompt = system_prompt(upstream.requests[1])
    assert f"文档助手（id {other['id']}）" in member_prompt
    assert f"代码开发助手（id {member['id']}）" not in member_prompt
    assert member_prompt.rstrip().endswith("这个标签只写一次，正文和其他内容里不要再提到它。")


def test_the_same_private_conversation_is_reused(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲")
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "第一件事")),
        {"content": "收到第一件事。"},
        tool_calls(dm_call("call_2", member["id"], "第二件事")),
        {"content": "收到第二件事。"},
        {"content": "都办好了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            run = await run_goal(client, direct_id, "让我和甲来回确认")
            assert run["status"] == "completed", run

            workspace = await get_workspace(client)
            privates = private_conversations(workspace)
            assert len(privates) == 1
            detail = await get_conversation(client, privates[0]["id"])
            assert [m["content"] for m in detail["messages"]] == [
                "第一件事",
                "收到第一件事。",
                "第二件事",
                "收到第二件事。",
            ]
            actions = action_messages(await get_conversation(client, direct_id))
            assert [a["action"]["target_conversation_id"] for a in actions] == [
                privates[0]["id"],
                privates[0]["id"],
            ]

        assert len(private_conversations({"conversations": store.list_conversations()})) == 1

    asyncio.run(body())


def test_nesting_deeper_than_two_levels_is_refused(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    first = seed_member(backend, "甲")
    second = seed_member(backend, "乙")
    supervisor_id = store.supervisor()["id"]
    upstream.script = [
        tool_calls(dm_call("call_1", first["id"], "请找乙确认")),
        tool_calls(dm_call("call_2", second["id"], "乙，请回答")),
        # Depth two: this call must be refused without reaching the supervisor.
        tool_calls(dm_call("call_3", supervisor_id, "主管，插一句话")),
        {"content": "我的回答。"},
        {"content": "乙说：我的回答。"},
        {"content": "汇总完成。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            run = await run_goal(client, direct_id, "让甲联系乙")
            assert run["status"] == "completed", run

            workspace = await get_workspace(client)
            privates = private_conversations(workspace)
            assert len(privates) == 2
            by_members = {
                frozenset(conversation["member_ids"]): conversation for conversation in privates
            }
            outer = by_members[frozenset({supervisor_id, first["id"]})]
            inner = by_members[frozenset({first["id"], second["id"]})]

            inner_detail = await get_conversation(client, inner["id"])
            refused = [m for m in inner_detail["messages"] if m["kind"] == "action"]
            assert len(refused) == 1
            assert refused[0]["action"]["tool_name"] == "direct_message"
            assert refused[0]["action"]["status"] == "error"
            assert "上限" in refused[0]["action"]["error"]
            assert refused[0]["status"] == "complete"
            # The refused call never opened a private chat with the supervisor.
            assert frozenset({second["id"], supervisor_id}) not in by_members

            outer_detail = await get_conversation(client, outer["id"])
            assert [m["content"] for m in agent_messages(outer_detail)] == [
                "请找乙确认",
                "乙说：我的回答。",
            ]

    asyncio.run(body())


def test_a_bot_cannot_message_itself(make_backend, upstream) -> None:
    backend = make_backend()
    supervisor_id = store_of(backend).supervisor()["id"]
    upstream.script = [
        tool_calls(dm_call("call_1", supervisor_id, "自言自语")),
        {"content": "还是自己处理吧。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "试一下私信自己")
            assert run["status"] == "completed", run

            actions = action_messages(await get_conversation(client, direct_id))
            assert len(actions) == 1
            assert actions[0]["action"]["status"] == "error"
            assert "自己" in actions[0]["action"]["error"]
            assert actions[0]["action"]["target_conversation_id"] is None

            workspace = await get_workspace(client)
            assert private_conversations(workspace) == []

    asyncio.run(body())


def test_a_private_conversation_refuses_user_runs(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲")
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "聊一句")),
        {"content": "聊完了。"},
        {"content": "好了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, direct_id, "和甲聊一句")
            workspace = await get_workspace(client)
            private = private_conversations(workspace)[0]

            response = await client.post(
                f"/conversations/{private['id']}/runs", json={"content": "我也说一句"}
            )
            assert response.status_code == 403, response.text
            assert response.json()["detail"] == "私聊只能旁观，不能发言。"
            detail = await get_conversation(client, private["id"])
            assert [m["kind"] for m in detail["messages"]] == ["agent", "agent"]
            assert store.active_run(private["id"]) is None

    asyncio.run(body())


def test_private_conversations_stay_out_of_the_sidebar_layout(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲")
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "聊一句")),
        {"content": "聊完了。"},
        {"content": "好了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, direct_id, "和甲聊一句")
            workspace = await get_workspace(client)
            private = private_conversations(workspace)[0]
            assert private["id"] in [c["id"] for c in workspace["conversations"]]

            rejected = await client.put(
                "/sidebar",
                json={
                    "items": [
                        {"conversation_id": private["id"], "pinned": True, "folder_id": None}
                    ],
                    "folder_ids": [],
                },
            )
            assert rejected.status_code == 422, rejected.text
            assert private["id"] in rejected.json()["detail"]

            accepted = await client.put(
                "/sidebar",
                json={
                    "items": [
                        {"conversation_id": direct_id, "pinned": True, "folder_id": None}
                    ],
                    "folder_ids": [],
                },
            )
            assert accepted.status_code == 200, accepted.text
            assert [item["conversation_id"] for item in accepted.json()["items"]] == [direct_id]

    asyncio.run(body())


def test_stop_during_a_nested_private_turn_marks_it_interrupted(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲")
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "说点长的")),
        {"content": None, "slow": True},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "和甲聊长一点")

            deadline = time.monotonic() + 15
            private = None
            while time.monotonic() < deadline:
                privates = [
                    conversation
                    for conversation in store.list_conversations()
                    if conversation["kind"] == "private"
                ]
                if privates:
                    private = privates[0]
                    break
                await asyncio.sleep(0.05)
            assert private is not None, "the private conversation never opened"
            await wait_for_message(client, private["id"], status="streaming")

            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped", stopped

            detail = await get_conversation(client, private["id"])
            assert [m["status"] for m in detail["messages"]] == ["complete", "interrupted"]

            actions = action_messages(await get_conversation(client, direct_id))
            assert actions[-1]["action"]["tool_name"] == "direct_message"
            assert actions[-1]["action"]["status"] == "error"
            assert actions[-1]["action"]["error"] == "任务已停止。"
            assert actions[-1]["status"] == "interrupted"

    asyncio.run(body())


def test_a_running_private_message_already_points_at_its_private_chat(make_backend, upstream) -> None:
    """The caller's 「已发消息给」 marker must be clickable while the other bot still answers."""
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲")
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "说点长的")),
        {"content": None, "slow": True},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "和甲聊长一点")
            deadline = time.monotonic() + 15
            private = None
            while time.monotonic() < deadline and private is None:
                private = next(
                    (c for c in store.list_conversations() if c["kind"] == "private"), None
                )
                await asyncio.sleep(0.05)
            assert private is not None, "the private conversation never opened"
            await wait_for_message(client, private["id"], status="streaming")

            action = action_messages(await get_conversation(client, direct_id))[-1]["action"]
            assert action["tool_name"] == "direct_message"
            assert action["status"] == "running"
            assert action["target_agent_id"] == member["id"]
            assert action["target_conversation_id"] == private["id"]

            await stop_run(client, run["id"])

    asyncio.run(body())


def test_nested_model_calls_count_against_the_same_run_budget(make_backend, upstream) -> None:
    backend = make_backend(max_model_calls=2)
    use_standard_budget(backend)
    member = seed_member(backend, "甲")
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "说一句")),
        {"content": "我说完了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "和甲说一句")
            assert run["status"] == "limited", run
            assert run["model_calls"] == 2
            assert run["error"] and "模型调用上限" in run["error"]

            actions = action_messages(await get_conversation(client, direct_id))
            assert actions[-1]["action"]["tool_name"] == "direct_message"
            assert actions[-1]["action"]["status"] == "completed"

            private = private_conversations(await get_workspace(client))[0]
            detail = await get_conversation(client, private["id"])
            assert detail["messages"][-1]["content"] == "我说完了。"
            assert detail["messages"][-1]["status"] == "complete"

    asyncio.run(body())


def test_a_restart_keeps_private_chats_to_their_two_bots(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲")
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "聊一句")),
        {"content": "聊完了。"},
        {"content": "好了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, direct_id, "和甲聊一句")

        backend.restart()

        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            privates = private_conversations(workspace)
            assert len(privates) == 1
            assert privates[0]["member_ids"] == [store.supervisor()["id"], member["id"]]
            detail = await get_conversation(client, privates[0]["id"])
            assert [m["content"] for m in detail["messages"]] == ["聊一句", "聊完了。"]

    asyncio.run(body())


def test_relay_markers_are_empty_and_never_become_the_preview(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲")
    member_direct = store.ensure_direct_conversation(member["id"])
    store.append_message(
        conversation_id=member_direct["id"],
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="先打个招呼",
        status="complete",
    )
    upstream.script = [
        tool_calls(dm_call("call_1", member["id"], "帮我确认今天的天气")),
        {"content": "今天晴，适合出门。"},
        {"content": "成员说今天晴。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "让甲查一下天气")
            assert run["status"] == "completed", run
            private = private_conversations(await get_workspace(client))[0]

            member_chat = await get_conversation(client, member_direct["id"])
            relays = [m for m in member_chat["messages"] if m["relay"]]
            assert [m["content"] for m in relays] == ["", ""]
            assert [m["relay"]["direction"] for m in relays] == ["in", "out"]
            # An empty marker never replaces the real preview above it.
            assert member_chat["conversation"]["last_message"] == "先打个招呼"

            previews = {
                conversation["id"]: conversation["last_message"]
                for conversation in (await get_workspace(client))["conversations"]
            }
            assert previews[member_direct["id"]] == "先打个招呼"
            assert previews[private["id"]] == "今天晴，适合出门。"

    asyncio.run(body())
