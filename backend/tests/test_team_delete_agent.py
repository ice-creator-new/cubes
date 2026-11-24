"""Deleting a member: store cascades, the REST route, and the supervisor's confirming tool."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.errors import AgentDeleteRefused
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    payloads,
    run_goal,
    start_run,
    stop_run,
    store_of,
    tool_names,
    tool_results,
    wait_for_run,
)

BUSY = "这个成员正在回复，等它结束后再删除。"
RESTART_ERROR = "服务重启导致任务中断，未完成的部分已保留。"
TIMEOUT_ERROR = "等待确认超时，成员「甲」没有删除。"


def seed_member(backend, name: str = "甲", instructions: str = "研究") -> dict:
    return store_of(backend).create_agent(name=name, instructions=instructions, kind="member")


def delete_call(call_id: str, agent_id: str, reason: str = "它已经完成任务") -> dict:
    return call(call_id, "delete_agent", {"agent_id": agent_id, "reason": reason})


async def wait_for_awaiting(
    client, conversation_id: str, *, timeout: float = 15.0
) -> dict:
    """The first action message the engine put into the awaiting status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await get_conversation(client, conversation_id)
        for message in action_messages(detail):
            if message["action"]["status"] == "awaiting":
                return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no awaiting action appeared in {conversation_id}")


async def collect_events_after(client, url: str, *, after, until, timeout: float = 20.0):
    """Read SSE frames, call ``after`` once the snapshot arrived, stop when ``until`` holds."""
    events: list[tuple[str, dict]] = []

    async def read() -> None:
        async with client.stream("GET", url) as response:
            assert response.status_code == 200, response.text
            event: str | None = None
            data: list[str] = []
            fired = False
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data.append(line[len("data:") :].strip())
                elif line.strip() == "":
                    if event is not None:
                        events.append((event, json.loads("".join(data))))
                    event, data = None, []
                    if not fired and events:
                        fired = True
                        await after()
                    if until(events):
                        return
                else:
                    events.append(("comment", {"line": line}))

    try:
        await asyncio.wait_for(read(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"timed out waiting for events: {[name for name, _ in events]}"
        ) from None
    return events


# --------------------------------------------------------------------- the store


def test_delete_agent_drops_its_conversations_membership_and_task_assignment(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend)
    other = store.create_agent(name="乙", instructions="设计", kind="member")
    direct_id = store.direct_conversation()["id"]

    member_chat = store.ensure_direct_conversation(member["id"])
    private = store.ensure_private_conversation(supervisor["id"], member["id"])
    group = store.create_conversation(
        kind="group",
        title="小组",
        purpose="一起做事",
        member_ids=[supervisor["id"], member["id"], other["id"]],
    )
    store.append_message(
        conversation_id=member_chat["id"],
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="你的私聊里的话",
        status="complete",
    )
    store.append_message(
        conversation_id=private["id"],
        author_id=supervisor["id"],
        author_name="主管",
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content="私下说说",
        status="complete",
    )
    group_message = store.append_message(
        conversation_id=group["id"],
        author_id=member["id"],
        author_name="甲",
        avatar_seed=member["avatar_seed"],
        kind="agent",
        content="我在群里的历史发言",
        status="complete",
    )
    task = store.create_task(title="写一份报告", assignee_id=member["id"])
    kept_task = store.create_task(title="别人的活", assignee_id=other["id"])
    store.replace_sidebar(
        [
            {"conversation_id": direct_id, "pinned": True, "folder_id": None},
            {"conversation_id": member_chat["id"], "pinned": False, "folder_id": None},
        ],
        [],
    )
    assert member_chat["id"] in [
        item["conversation_id"] for item in store.sidebar_layout()["items"]
    ]

    deleted = store.delete_agent(member["id"])

    assert deleted is not None
    assert deleted["id"] == member["id"]
    assert deleted["name"] == "甲"
    assert store.get_agent(member["id"]) is None

    # The direct chat and every private chat it belonged to are gone, rows included.
    assert store.get_conversation(member_chat["id"]) is None
    assert store.get_conversation(private["id"]) is None
    assert store.list_messages(member_chat["id"]) == []
    assert store.list_messages(private["id"]) == []
    assert store.get_conversation(direct_id) is not None

    # Groups keep their history and lose only the member row.
    kept_group = store.get_conversation(group["id"])
    assert kept_group is not None
    assert kept_group["member_ids"] == [supervisor["id"], other["id"]]
    assert store.list_messages(group["id"]) == [group_message]
    assert store.list_messages(group["id"])[0]["author_name"] == "甲"
    assert store.list_messages(group["id"])[0]["author_id"] == member["id"]

    # Tasks lose the assignee instead of the task.
    assert store.get_task(task["id"])["assignee_id"] is None
    assert store.get_task(kept_task["id"])["assignee_id"] == other["id"]

    ids = [item["conversation_id"] for item in store.sidebar_layout()["items"]]
    assert member_chat["id"] not in ids
    assert direct_id in ids
    assert [c["kind"] for c in store.list_conversations()] == ["direct", "group"]


def test_delete_agent_refuses_the_supervisor(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None

    with pytest.raises(AgentDeleteRefused) as raised:
        store.delete_agent(supervisor["id"])
    assert str(raised.value) == "「主管」不能删除。"
    assert store.get_agent(supervisor["id"]) is not None
    assert store.direct_conversation() is not None


def test_delete_agent_returns_none_for_an_unknown_member(make_backend) -> None:
    backend = make_backend()
    assert store_of(backend).delete_agent("a_missing") is None


def test_delete_agent_refuses_while_a_run_involves_it(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend)
    direct_id = store.direct_conversation()["id"]
    member_chat = store.ensure_direct_conversation(member["id"])
    run, _ = store.begin_run(direct_id, "目标")

    store.update_run(run["id"], current_agent_id=member["id"])
    with pytest.raises(AgentDeleteRefused) as raised:
        store.delete_agent(member["id"])
    assert str(raised.value) == BUSY

    store.update_run(run["id"], current_agent_id=None, pending_agent_ids=[member["id"]])
    with pytest.raises(AgentDeleteRefused) as raised:
        store.delete_agent(member["id"])
    assert str(raised.value) == BUSY

    # A run inside the member's own conversation blocks it even without its id on the run.
    store.update_run(run["id"], pending_agent_ids=[])
    chat_run, _ = store.begin_run(member_chat["id"], "私聊里的目标")
    with pytest.raises(AgentDeleteRefused) as raised:
        store.delete_agent(member["id"])
    assert str(raised.value) == BUSY

    store.update_run(chat_run["id"], status="stopped", finished_at="2026-10-20T00:00:00Z")
    store.update_run(run["id"], status="stopped", finished_at="2026-10-20T00:00:00Z")
    assert store.delete_agent(member["id"])["id"] == member["id"]
    assert store.get_agent(member["id"]) is None


# ----------------------------------------------------------------- the HTTP route


def test_delete_agent_route_returns_204_and_publishes_workspace_and_sidebar(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend)
    member_chat = store.ensure_direct_conversation(member["id"])
    direct_id = store.direct_conversation()["id"]
    store.replace_sidebar(
        [
            {"conversation_id": direct_id, "pinned": True, "folder_id": None},
            {"conversation_id": member_chat["id"], "pinned": False, "folder_id": None},
        ],
        [],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            async def delete_member() -> None:
                response = await client.delete(f"/agents/{member['id']}")
                assert response.status_code == 204, response.text
                assert response.content == b""

            events = await collect_events_after(
                client,
                "/events",
                after=delete_member,
                until=lambda seen: any(name == "sidebar" for name, _ in seen),
            )
            names = [name for name, _ in events]
            assert names[0] == "snapshot"
            assert "workspace" in names
            assert "sidebar" in names
            assert names.index("workspace") < names.index("sidebar")

            workspace = payloads(events, "workspace")[-1]
            assert member["id"] not in [agent["id"] for agent in workspace["agents"]]
            assert member_chat["id"] not in [
                conversation["id"] for conversation in workspace["conversations"]
            ]
            sidebar = payloads(events, "sidebar")[-1]
            assert member_chat["id"] not in [
                item["conversation_id"] for item in sidebar["items"]
            ]

            current = await get_workspace(client)
            assert [agent["name"] for agent in current["agents"]] == ["主管"]
            gone = await client.get(f"/conversations/{member_chat['id']}")
            assert gone.status_code == 404

    asyncio.run(body())


def test_delete_agent_route_reports_unknown_supervisor_and_busy(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend)
    direct_id = store.direct_conversation()["id"]
    run, _ = store.begin_run(direct_id, "目标")
    store.update_run(run["id"], current_agent_id=member["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            missing = await client.delete("/agents/a_missing")
            assert missing.status_code == 404, missing.text
            assert missing.json()["detail"] == "成员不存在。"

            boss = await client.delete(f"/agents/{supervisor['id']}")
            assert boss.status_code == 409, boss.text
            assert boss.json()["detail"] == "「主管」不能删除。"

            busy = await client.delete(f"/agents/{member['id']}")
            assert busy.status_code == 409, busy.text
            assert busy.json()["detail"] == BUSY
            assert member["id"] in [agent["id"] for agent in (await get_workspace(client))["agents"]]

    asyncio.run(body())

    store.update_run(run["id"], status="stopped", finished_at="2026-10-20T00:00:00Z")

    async def afterwards() -> None:
        async with client_for(backend) as client:
            removed = await client.delete(f"/agents/{member['id']}")
            assert removed.status_code == 204, removed.text
            assert (await client.post(f"/agents/{member['id']}/direct")).status_code == 404

    asyncio.run(afterwards())


# ------------------------------------------------------------ the supervisor tool


def test_delete_agent_tool_waits_for_confirmation_then_deletes(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend)
    member_chat = store.ensure_direct_conversation(member["id"])
    private = store.ensure_private_conversation(store.supervisor()["id"], member["id"])
    group = store.create_conversation(
        kind="group",
        title="小组",
        purpose="做事",
        member_ids=[store.supervisor()["id"], member["id"]],
    )
    store.append_message(
        conversation_id=group["id"],
        author_id=member["id"],
        author_name="甲",
        avatar_seed=member["avatar_seed"],
        kind="agent",
        content="群里的历史发言",
        status="complete",
    )
    upstream.script = [
        tool_calls(delete_call("call_1", member["id"])),
        {"content": "已按你的决定处理"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把甲删掉")
            awaiting = await wait_for_awaiting(client, direct_id)

            action = awaiting["action"]
            assert action["tool_name"] == "delete_agent"
            assert action["summary"] == "删除成员「甲」"
            assert action["error"] is None
            assert awaiting["content"] == "删除成员「甲」"
            assert awaiting["status"] == "streaming"
            assert action["confirmation"] == {
                "prompt": (
                    "主管想删除成员「甲」：它已经完成任务。"
                    "删除后，它和你的私聊、它参与的机器人私聊会一起删除，群组里的历史发言会保留。"
                ),
                "confirm_label": "删除",
                "deny_label": "保留",
            }

            waiting_run = await get_run(client, run["id"])
            assert waiting_run["status"] == "running"
            assert waiting_run["current_step"] == "等待你确认删除「甲」"
            # Nothing happened before the user decided.
            assert store.get_agent(member["id"]) is not None

            confirmed = await client.post(
                f"/actions/{action['id']}/confirm", json={"approve": True}
            )
            assert confirmed.status_code == 200, confirmed.text
            decided = confirmed.json()
            assert decided["status"] == "completed"
            assert decided["summary"] == "已删除成员「甲」"
            assert decided["confirmation"] is None

            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            again = await client.post(
                f"/actions/{action['id']}/confirm", json={"approve": True}
            )
            assert again.status_code == 409, again.text
            assert again.json()["detail"] == "这个操作已经处理过了。"

            unknown = await client.post("/actions/x_missing/confirm", json={"approve": True})
            assert unknown.status_code == 404, unknown.text
            assert unknown.json()["detail"] == "操作不存在。"

            assert store.get_agent(member["id"]) is None
            assert store.get_conversation(member_chat["id"]) is None
            assert store.get_conversation(private["id"]) is None
            assert store.get_conversation(group["id"]) is not None
            assert [m["content"] for m in store.list_messages(group["id"])] == [
                "群里的历史发言"
            ]

            detail = await get_conversation(client, direct_id)
            actions = action_messages(detail)
            assert [entry["action"]["status"] for entry in actions] == ["completed"]

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is True
    assert results["call_1"]["name"] == "甲"


def test_delete_agent_tool_denied_keeps_the_member(make_backend, upstream) -> None:
    backend = make_backend()
    member = seed_member(backend)
    upstream.script = [
        tool_calls(delete_call("call_1", member["id"], "它没用了")),
        {"content": "那就先留着"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把甲删掉")
            awaiting = await wait_for_awaiting(client, direct_id)

            denied = await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": False}
            )
            assert denied.status_code == 200, denied.text
            payload = denied.json()
            assert payload["status"] == "completed"
            assert payload["summary"] == "你保留了成员「甲」"
            assert payload["error"] is None
            assert payload["confirmation"] is None

            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            assert store_of(backend).get_agent(member["id"]) is not None

            detail = await get_conversation(client, direct_id)
            action = action_messages(detail)[-1]
            assert action["content"] == "你保留了成员「甲」"
            assert action["status"] == "complete"

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"] == {"ok": False, "denied": True}


def test_confirming_a_delete_while_the_member_is_busy_fails_honestly(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend)
    member_chat = store.ensure_direct_conversation(member["id"])
    upstream.script = [
        tool_calls(delete_call("call_1", member["id"])),
        {"content": "那就等它忙完再说"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把甲删掉")
            awaiting = await wait_for_awaiting(client, direct_id)

            # While the user is deciding, work starts in the member's own chat.
            store.begin_run(member_chat["id"], "同时在忙的事")

            confirmed = await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": True}
            )
            assert confirmed.status_code == 200, confirmed.text
            payload = confirmed.json()
            assert payload["status"] == "error"
            assert payload["error"] == BUSY
            assert payload["confirmation"] is None

            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            assert store.get_agent(member["id"]) is not None

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is False
    assert results["call_1"]["error"] == BUSY


def test_delete_agent_tool_times_out_without_deleting(make_backend, upstream) -> None:
    backend = make_backend(confirm_timeout_seconds=0.3)
    member = seed_member(backend)
    upstream.script = [
        tool_calls(delete_call("call_1", member["id"])),
        {"content": "没有确认，先不动"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把甲删掉")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            detail = await get_conversation(client, direct_id)
            action = action_messages(detail)[-1]
            assert action["action"]["status"] == "error"
            assert action["action"]["error"] == TIMEOUT_ERROR
            assert action["content"] == TIMEOUT_ERROR
            assert action["action"]["confirmation"] is None
            assert store_of(backend).get_agent(member["id"]) is not None

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is False
    assert results["call_1"]["error"] == TIMEOUT_ERROR


def test_stopping_a_run_while_awaiting_treats_it_as_denied(make_backend, upstream) -> None:
    backend = make_backend()
    member = seed_member(backend)
    upstream.script = [tool_calls(delete_call("call_1", member["id"]))]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把甲删掉")
            await wait_for_awaiting(client, direct_id)

            started = time.monotonic()
            stopped = await stop_run(client, run["id"])
            elapsed = time.monotonic() - started
            assert stopped["status"] == "stopped", stopped
            assert elapsed < 5.0

            detail = await get_conversation(client, direct_id)
            action = action_messages(detail)[-1]
            assert action["action"]["status"] == "completed"
            assert action["action"]["summary"] == "你保留了成员「甲」"
            assert action["action"]["confirmation"] is None
            assert action["status"] == "complete"
            assert store_of(backend).get_agent(member["id"]) is not None

    asyncio.run(body())


def test_a_restart_fails_an_action_left_awaiting(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct_id = store.direct_conversation()["id"]
    run, _ = store.begin_run(direct_id, "把甲删掉")
    action, _ = store.create_action(
        run_id=run["id"],
        conversation_id=direct_id,
        tool_call_id="call_1",
        tool_name="delete_agent",
        arguments="{}",
        summary="删除成员「甲」",
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        status="awaiting",
        confirmation={"prompt": "主管想删除成员「甲」：它已经完成任务。", "confirm_label": "删除", "deny_label": "保留"},
    )

    backend.restart()

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await get_conversation(client, direct_id)
            awaiting = [m for m in action_messages(detail) if m["action"]["id"] == action["id"]]
            assert len(awaiting) == 1
            message = awaiting[0]
            assert message["action"]["status"] == "error"
            assert message["action"]["error"] == RESTART_ERROR
            assert message["action"]["confirmation"] is None
            assert message["status"] == "interrupted"

            refused = await client.post(
                f"/actions/{action['id']}/confirm", json={"approve": True}
            )
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"] == "这个操作已经处理过了。"

            run_now = await get_run(client, run["id"])
            assert run_now["status"] == "stopped"

    asyncio.run(body())


def test_a_member_calling_delete_agent_is_refused(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "甲", "研究")
    other = store.create_agent(name="乙", instructions="设计", kind="member")
    member_chat = store.ensure_direct_conversation(member["id"])
    upstream.script = [
        tool_calls(delete_call("call_1", other["id"], "想删掉乙")),
        {"content": "我做不到这件事"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, member_chat["id"], "删掉乙")
            assert run["status"] == "completed", run

            detail = await get_conversation(client, member_chat["id"])
            action = action_messages(detail)[-1]
            assert action["action"]["tool_name"] == "delete_agent"
            assert action["action"]["status"] == "error"
            assert "未知工具" in action["action"]["error"]
            assert action["action"]["confirmation"] is None
            assert store.get_agent(other["id"]) is not None

    asyncio.run(body())

    assert "delete_agent" not in tool_names(upstream.requests[0])
    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is False
    assert "未知工具" in results["call_1"]["error"]
