"""SSE contract: snapshot first, real progress events, ids on every payload, never ends."""

from __future__ import annotations

import asyncio

from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    collect_events,
    event_names,
    find_result_key,
    first_payload,
    get_workspace,
    payloads,
    result_keys,
    settled,
    start_run,
    stop_run,
    wait_for_message,
    store_of,
    wait_for_run,
)


def test_idle_events_send_the_snapshot_then_stay_open(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events = await collect_events(
                client, f"/events?conversation_id={direct_id}", duration=0.6
            )
            assert event_names(events) == ["snapshot"]
            snapshot = first_payload(events, "snapshot")
            assert snapshot["workspace"] == workspace
            assert snapshot["conversation"]["conversation"]["id"] == direct_id
            assert snapshot["conversation"]["messages"] == []
            assert snapshot["conversation"]["latest_run"] is None

            unselected = await collect_events(client, "/events", duration=0.6)
            assert event_names(unselected) == ["snapshot"]
            assert first_payload(unselected, "snapshot")["conversation"] is None

    asyncio.run(body())


def test_idle_events_keep_the_connection_open_with_heartbeats(make_backend, monkeypatch) -> None:
    import app.routes.events as events_module

    monkeypatch.setattr(events_module, "HEARTBEAT_SECONDS", 0.15)
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            events = await collect_events(client, "/events", duration=0.7)
            names = event_names(events)
            assert names[0] == "snapshot"
            assert "done" not in names
            # An idle stream reports liveness instead of ending.
            assert names.count("comment") >= 2
            assert all(payload["line"].startswith(":") for payload in payloads(events, "comment"))

    asyncio.run(body())


def test_events_forward_workspace_run_message_and_delta_events(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.chunk_delay = 0.25
    upstream.script = [
        {
            "content": "先说明一下我的计划",
            "tool_calls": [
                call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})
            ],
        },
        {"content": None, "slow": True},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            run = await start_run(client, direct_id, "做一个计划")
            await wait_for_message(client, direct_id, status="streaming")

            events = await collect_events(
                client, f"/events?conversation_id={direct_id}", until=settled
            )
            names = event_names(events)
            assert names[0] == "snapshot"
            assert "done" not in names
            assert "workspace" in names
            assert "run" in names
            assert "delta" in names
            assert "message_start" in names
            assert "message_end" in names

            # A workspace event after the member was really created.
            created = payloads(events, "workspace")[-1]
            assert [agent["name"] for agent in created["agents"]] == ["主管", "甲"]

            # Run events carry real counters and end with a terminal status.
            runs = payloads(events, "run")
            assert all("conversation_id" in payload for payload in runs)
            assert runs[-1]["status"] == "completed"
            assert runs[-1]["model_calls"] == 2
            assert runs[-1]["current_agent_id"] is None
            assert runs[-1]["pending_agent_ids"] == []
            assert runs[-1]["hops"] == 0

            # Action messages stream their pending state and close with the real status.
            action_starts = [
                payload for payload in payloads(events, "message_start") if payload["kind"] == "action"
            ]
            assert [payload["action"]["tool_name"] for payload in action_starts] == ["create_agent"]
            assert action_starts[0]["status"] == "streaming"
            action_ends = [
                payload for payload in payloads(events, "message_end") if payload["kind"] == "action"
            ]
            assert action_ends[0]["action"]["status"] == "completed"
            assert action_ends[0]["action"]["target_agent_id"] == created["agents"][1]["id"]

            # Deltas name the conversation and the message they extend.
            deltas = payloads(events, "delta")
            assert all(delta["conversation_id"] == direct_id for delta in deltas)
            assert all(delta["text"] for delta in deltas)
            assert all("message_id" in delta for delta in deltas)
            known_ids = {
                payload["id"]
                for name in ("message_start", "message_end")
                for payload in payloads(events, name)
            }
            assert {delta["message_id"] for delta in deltas} <= known_ids

            final = [
                payload
                for payload in payloads(events, "message_end")
                if payload["kind"] == "agent" and payload["status"] == "complete"
            ]
            assert final
            assert final[-1]["content"].startswith("[")
            assert final[-1]["author_name"] == "主管"
            assert final[-1]["reply_to_id"] is not None

            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed"

    asyncio.run(body())


def test_a_new_private_chat_is_announced_before_its_messages(make_backend, upstream) -> None:
    """An open window must learn about a private chat a run creates, or it cannot open it."""
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="做研究", kind="member")
    upstream.chunk_delay = 0.05
    upstream.script = [
        tool_calls(call("call_1", "direct_message", {"agent_id": member["id"], "content": "在吗"})),
        {"content": "在。"},
        {"content": "甲说在。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert not [c for c in workspace["conversations"] if c["kind"] == "private"]
            reader = asyncio.create_task(collect_events(client, "/events", until=settled))
            await asyncio.sleep(0.2)
            await start_run(client, workspace["direct_conversation_id"], "问问甲")
            events = await reader

            # The finished tool row names the private chat the run opened.
            private_id = next(
                payload["action"]["target_conversation_id"]
                for name, payload in events
                if name == "message_end" and payload["kind"] == "action"
                and payload["action"]["tool_name"] == "direct_message"
                and payload["action"]["target_conversation_id"]
            )
            first_message = next(
                index for index, (name, payload) in enumerate(events)
                if name == "message_start" and payload.get("conversation_id") == private_id
            )
            announced = [
                index for index, (name, payload) in enumerate(events)
                if name == "workspace" and any(c["id"] == private_id for c in payload["conversations"])
            ]
            assert announced and announced[0] < first_message

    asyncio.run(body())


def test_events_for_other_conversations_still_carry_their_ids(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.chunk_delay = 0.2
    upstream.script = [
        {
            "content": "先建一个小组",
            "tool_calls": [
                call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})
            ],
        },
        lambda requests, index: tool_calls(
            call(
                "call_2",
                "create_group",
                {
                    "title": "验证小组",
                    "purpose": "验证方向",
                    "member_ids": result_keys(requests, "agent_id"),
                },
            )
        ),
        lambda requests, index: tool_calls(
            call(
                "call_3",
                "post_group_message",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "content": "请在群里开始工作",
                },
            )
        ),
        {"content": "已经安排"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            run = await start_run(client, direct_id, "安排一次协作")
            await wait_for_message(client, direct_id, status="streaming")

            events = await collect_events(client, f"/events?conversation_id={direct_id}", until=settled)
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed"
            workspace = await get_workspace(client)
            group = next(c for c in workspace["conversations"] if c["kind"] == "group")

            group_starts = [
                payload
                for payload in payloads(events, "message_start")
                if payload["conversation_id"] == group["id"]
            ]
            assert group_starts
            assert [payload["content"] for payload in group_starts] == ["请在群里开始工作"]
            group_ends = [
                payload
                for payload in payloads(events, "message_end")
                if payload["conversation_id"] == group["id"]
            ]
            assert group_ends
            assert all(payload["status"] == "complete" for payload in group_ends)

    asyncio.run(body())


def test_events_from_a_stopped_run_report_the_terminal_state(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")

            reader = asyncio.create_task(
                collect_events(client, f"/events?conversation_id={direct_id}", until=settled)
            )
            await asyncio.sleep(0.3)
            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped"
            events = await asyncio.wait_for(reader, timeout=15)

            names = event_names(events)
            assert names[0] == "snapshot"
            assert "done" not in names
            terminal = payloads(events, "run")[-1]
            assert terminal["status"] == "stopped"
            assert terminal["current_agent_id"] is None
            assert terminal["pending_agent_ids"] == []
            ends = payloads(events, "message_end")
            assert ends
            assert any(payload["status"] == "interrupted" for payload in ends)
            detail = (await client.get(f"/conversations/{direct_id}")).json()
            assert detail["latest_run"]["status"] == "stopped"
            assert detail["latest_run"]["error"] is None

    asyncio.run(body())


def test_one_subscription_receives_runs_from_every_conversation(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    backend = make_backend()
    store = backend.app.state.store
    supervisor = store.supervisor()
    group = store.create_conversation(
        kind="group", title="旁观群", purpose="什么都不做", member_ids=[supervisor["id"]]
    )

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]

            def saw_other_run(events: list[tuple[str, dict]]) -> bool:
                return any(
                    name == "delta" and payload.get("conversation_id") == direct_id
                    for name, payload in events
                )

            reader = asyncio.create_task(
                collect_events(
                    client, f"/events?conversation_id={group['id']}", until=saw_other_run
                )
            )
            await asyncio.sleep(0.4)
            run = await start_run(client, direct_id, "写一篇长文")
            events = await asyncio.wait_for(reader, timeout=20)

            assert event_names(events)[0] == "snapshot"
            assert first_payload(events, "snapshot")["conversation"]["conversation"]["id"] == (
                group["id"]
            )
            # Live events of a run in another conversation are still delivered.
            runs = [payload for payload in payloads(events, "run") if payload["id"] == run["id"]]
            assert runs
            assert all(payload["conversation_id"] == direct_id for payload in runs)
            assert any(
                payload["conversation_id"] == direct_id for payload in payloads(events, "delta")
            )
            await stop_run(client, run["id"])

    asyncio.run(body())
