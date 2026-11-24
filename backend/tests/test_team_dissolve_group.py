"""Dissolving a group: store cascades, the REST route, and the supervisor's confirming tool."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.errors import GroupDissolveRefused
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

BUSY = "这个群组正在回复，等它结束后再解散。"
NOT_A_GROUP = "只能解散群组。"
RESTART_ERROR = "服务重启导致任务中断，未完成的部分已保留。"
TIMEOUT_ERROR = "等待确认超时，群组「小组」没有解散。"
INSIDE = "不能在要解散的群组里发起解散，本次调用未执行。"


def seed_group(backend, title: str = "小组", purpose: str = "一起做事") -> dict:
    store = store_of(backend)
    member = store.create_agent(name=f"{title}成员", instructions="研究", kind="member")
    group = store.create_conversation(
        kind="group",
        title=title,
        purpose=purpose,
        member_ids=[store.supervisor()["id"], member["id"]],
    )
    return group


def dissolve_call(call_id: str, group_id: str, reason: str = "这个目标已经结束") -> dict:
    return call(call_id, "dissolve_group", {"group_id": group_id, "reason": reason})


async def wait_for_awaiting(client, conversation_id: str, *, timeout: float = 15.0) -> dict:
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


def test_dissolve_group_removes_the_group_and_only_its_own_rows(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    other = store.create_agent(name="乙", instructions="设计", kind="member")
    group = store.create_conversation(
        kind="group",
        title="小组",
        purpose="一起做事",
        member_ids=[supervisor["id"], other["id"]],
    )
    direct_id = store.direct_conversation()["id"]
    member_chat = store.ensure_direct_conversation(other["id"])
    private = store.ensure_private_conversation(supervisor["id"], other["id"])

    message = store.append_message(
        conversation_id=group["id"],
        author_id=other["id"],
        author_name="乙",
        avatar_seed=other["avatar_seed"],
        kind="agent",
        content="群里的发言",
        status="complete",
    )
    run, _ = store.begin_run(group["id"], "群里的目标")
    action, _ = store.create_action(
        run_id=run["id"],
        conversation_id=group["id"],
        tool_call_id="call_1",
        tool_name="note_create",
        arguments="{}",
        summary="正在记笔记",
        author_id=other["id"],
        author_name="乙",
        avatar_seed=other["avatar_seed"],
    )
    store.finish_action(
        action["id"], status="completed", summary="记了一条笔记", message_status="complete"
    )
    store.record_invocation(
        run_id=run["id"],
        tool_call_id="call_1",
        tool_name="note_create",
        arguments="{}",
        result="{}",
        action_id=action["id"],
    )
    store.update_run(run["id"], status="completed", finished_at="2026-10-20T00:00:00Z")

    note = store.create_note(title="结论", content="内容", author_id=other["id"])
    task = store.create_task(title="继续调研", assignee_id=other["id"])
    store.replace_sidebar(
        [
            {"conversation_id": direct_id, "pinned": True, "folder_id": None},
            {"conversation_id": group["id"], "pinned": False, "folder_id": None},
        ],
        [],
    )
    assert group["id"] in [item["conversation_id"] for item in store.sidebar_layout()["items"]]

    removed = store.dissolve_group(group["id"])

    assert removed is not None
    assert removed["id"] == group["id"]
    assert removed["title"] == "小组"
    assert removed["member_ids"] == [supervisor["id"], other["id"]]

    # The group and everything that belonged only to it are gone, rows included.
    assert store.get_conversation(group["id"]) is None
    assert store.list_messages(group["id"]) == []
    assert store.get_message(message["id"]) is None
    assert store.get_run(run["id"]) is None
    assert store.get_action(action["id"]) is None
    # tool_invocations has no foreign key: it follows its run explicitly.
    assert (
        store._ensure()
        .execute("SELECT 1 FROM tool_invocations WHERE run_id = ?", (run["id"],))
        .fetchone()
        is None
    )
    assert group["id"] not in [
        item["conversation_id"] for item in store.sidebar_layout()["items"]
    ]

    # Members, their chats, their notes and their tasks stay.
    assert store.get_agent(other["id"]) is not None
    assert store.get_conversation(direct_id) is not None
    assert store.get_conversation(member_chat["id"]) is not None
    assert store.get_conversation(private["id"]) is not None
    assert store.get_note(note["id"]) is not None
    assert store.get_task(task["id"])["assignee_id"] == other["id"]


def test_dissolve_group_keeps_an_action_that_points_at_it_from_elsewhere(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    group = seed_group(backend)
    direct_id = store.direct_conversation()["id"]
    run, _ = store.begin_run(direct_id, "在主管私聊里做的事")
    action, _ = store.create_action(
        run_id=run["id"],
        conversation_id=direct_id,
        tool_call_id="call_1",
        tool_name="create_group",
        arguments="{}",
        summary="建立了群组「小组」",
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
    )
    store.finish_action(
        action["id"],
        status="completed",
        summary="建立了群组「小组」",
        message_status="complete",
        result="{}",
        target_conversation_id=group["id"],
    )
    store.update_run(run["id"], status="completed", finished_at="2026-10-20T00:00:00Z")

    assert store.dissolve_group(group["id"]) is not None

    # A history row may dangle: the payload is untouched, the row itself survives.
    surviving = store.get_action(action["id"])
    assert surviving is not None
    assert surviving["target_conversation_id"] == group["id"]
    assert surviving["summary"] == "建立了群组「小组」"
    assert len(store.list_messages(direct_id)) == 2


def test_dissolve_group_returns_none_for_an_unknown_conversation(make_backend) -> None:
    backend = make_backend()
    assert store_of(backend).dissolve_group("c_missing") is None


def test_dissolve_group_refuses_everything_that_is_not_a_group(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="研究", kind="member")
    direct_id = store.direct_conversation()["id"]
    member_chat = store.ensure_direct_conversation(member["id"])
    private = store.ensure_private_conversation(supervisor["id"], member["id"])

    for conversation_id in (direct_id, member_chat["id"], private["id"]):
        with pytest.raises(GroupDissolveRefused) as raised:
            store.dissolve_group(conversation_id)
        assert str(raised.value) == NOT_A_GROUP

    assert store.direct_conversation() is not None
    assert store.get_conversation(member_chat["id"]) is not None
    assert store.get_conversation(private["id"]) is not None


def test_dissolve_group_refuses_while_the_group_is_running(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    group = seed_group(backend)
    run, _ = store.begin_run(group["id"], "群里的目标")

    with pytest.raises(GroupDissolveRefused) as raised:
        store.dissolve_group(group["id"])
    assert str(raised.value) == BUSY
    assert store.get_conversation(group["id"]) is not None

    store.update_run(run["id"], status="stopped", finished_at="2026-10-20T00:00:00Z")
    assert store.dissolve_group(group["id"]) is not None
    assert store.get_conversation(group["id"]) is None


def test_dissolve_group_accepts_a_migrated_read_only_group(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    group = store.create_conversation(
        kind="group",
        title="旧圆桌",
        purpose="迁移来的",
        member_ids=[supervisor["id"]],
        legacy_roles=[{"name": "旧角色", "instructions": "旧职责", "avatar_seed": "abc"}],
    )
    assert store.get_conversation(group["id"])["legacy_roles"] != []

    assert store.dissolve_group(group["id"]) is not None
    assert store.get_conversation(group["id"]) is None


# ----------------------------------------------------------------- the HTTP route


def test_dissolve_group_route_returns_204_and_publishes_workspace_and_sidebar(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    group = seed_group(backend)
    direct_id = store.direct_conversation()["id"]
    store.replace_sidebar(
        [
            {"conversation_id": direct_id, "pinned": True, "folder_id": None},
            {"conversation_id": group["id"], "pinned": False, "folder_id": None},
        ],
        [],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            async def dissolve() -> None:
                response = await client.delete(f"/conversations/{group['id']}")
                assert response.status_code == 204, response.text
                assert response.content == b""

            events = await collect_events_after(
                client,
                "/events",
                after=dissolve,
                until=lambda seen: any(name == "sidebar" for name, _ in seen),
            )
            names = [name for name, _ in events]
            assert names[0] == "snapshot"
            assert "workspace" in names
            assert "sidebar" in names
            assert names.index("workspace") < names.index("sidebar")

            workspace = payloads(events, "workspace")[-1]
            assert group["id"] not in [
                conversation["id"] for conversation in workspace["conversations"]
            ]
            sidebar = payloads(events, "sidebar")[-1]
            assert group["id"] not in [item["conversation_id"] for item in sidebar["items"]]

            current = await get_workspace(client)
            assert [c["title"] for c in current["conversations"]] == ["主管"]
            gone = await client.get(f"/conversations/{group['id']}")
            assert gone.status_code == 404

    asyncio.run(body())


def test_dissolve_group_route_reports_unknown_non_groups_and_busy(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct_id = store.direct_conversation()["id"]
    group = seed_group(backend)
    run, _ = store.begin_run(group["id"], "群里的目标")

    async def body() -> None:
        async with client_for(backend) as client:
            missing = await client.delete("/conversations/c_missing")
            assert missing.status_code == 404, missing.text
            assert missing.json()["detail"] == "群组不存在。"

            boss = await client.delete(f"/conversations/{direct_id}")
            assert boss.status_code == 409, boss.text
            assert boss.json()["detail"] == NOT_A_GROUP

            busy = await client.delete(f"/conversations/{group['id']}")
            assert busy.status_code == 409, busy.text
            assert busy.json()["detail"] == BUSY
            assert group["id"] in [
                conversation["id"]
                for conversation in (await get_workspace(client))["conversations"]
            ]

    asyncio.run(body())

    store.update_run(run["id"], status="stopped", finished_at="2026-10-20T00:00:00Z")

    async def afterwards() -> None:
        async with client_for(backend) as client:
            removed = await client.delete(f"/conversations/{group['id']}")
            assert removed.status_code == 204, removed.text
            assert (await client.get(f"/conversations/{group['id']}")).status_code == 404

    asyncio.run(afterwards())


# ------------------------------------------------------------ the supervisor tool


def test_dissolve_group_tool_waits_for_confirmation_then_dissolves(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    group = seed_group(backend)
    supervisor = store.supervisor()
    message = store.append_message(
        conversation_id=group["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content="群里的一条消息",
        status="complete",
    )
    upstream.script = [
        tool_calls(dissolve_call("call_1", group["id"])),
        {"content": "已按你的决定处理"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把这个群解散")
            awaiting = await wait_for_awaiting(client, direct_id)

            action = awaiting["action"]
            assert action["tool_name"] == "dissolve_group"
            assert action["summary"] == "解散群组「小组」"
            assert action["error"] is None
            assert awaiting["content"] == "解散群组「小组」"
            assert awaiting["status"] == "streaming"
            assert action["confirmation"] == {
                "prompt": (
                    "主管想解散群组「小组」：这个目标已经结束。"
                    "解散后群组和它的聊天记录会一起删除，成员和他们的私聊都会保留。"
                ),
                "confirm_label": "解散",
                "deny_label": "保留",
            }

            waiting_run = await get_run(client, run["id"])
            assert waiting_run["status"] == "running"
            assert waiting_run["current_step"] == "等待你确认解散「小组」"
            assert store.get_conversation(group["id"]) is not None

            confirmed = await client.post(
                f"/actions/{action['id']}/confirm", json={"approve": True}
            )
            assert confirmed.status_code == 200, confirmed.text
            decided = confirmed.json()
            assert decided["status"] == "completed"
            assert decided["summary"] == "已解散群组「小组」"
            assert decided["confirmation"] is None

            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            assert store.get_conversation(group["id"]) is None
            assert store.get_message(message["id"]) is None
            assert (await client.get(f"/conversations/{group['id']}")).status_code == 404

            detail = await get_conversation(client, direct_id)
            actions = action_messages(detail)
            assert [entry["action"]["status"] for entry in actions] == ["completed"]

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is True
    assert results["call_1"]["title"] == "小组"


def test_dissolve_group_tool_denied_keeps_the_group(make_backend, upstream) -> None:
    backend = make_backend()
    group = seed_group(backend)
    upstream.script = [
        tool_calls(dissolve_call("call_1", "小组")),
        {"content": "那就先留着"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把这个群解散")
            awaiting = await wait_for_awaiting(client, direct_id)

            denied = await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": False}
            )
            assert denied.status_code == 200, denied.text
            payload = denied.json()
            assert payload["status"] == "completed"
            assert payload["summary"] == "你保留了群组「小组」"
            assert payload["error"] is None
            assert payload["confirmation"] is None

            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            assert store_of(backend).get_conversation(group["id"]) is not None

            detail = await get_conversation(client, direct_id)
            action = action_messages(detail)[-1]
            assert action["content"] == "你保留了群组「小组」"
            assert action["status"] == "complete"

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"] == {"ok": False, "denied": True}


def test_confirming_a_dissolve_while_the_group_is_busy_fails_honestly(
    make_backend, upstream
) -> None:
    backend = make_backend()
    store = store_of(backend)
    group = seed_group(backend)
    upstream.script = [
        tool_calls(dissolve_call("call_1", group["id"])),
        {"content": "那就等它忙完再说"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把这个群解散")
            awaiting = await wait_for_awaiting(client, direct_id)

            # While the user is deciding, work starts inside the group.
            store.begin_run(group["id"], "同时在忙的事")

            confirmed = await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": True}
            )
            assert confirmed.status_code == 200, confirmed.text
            payload = confirmed.json()
            assert payload["status"] == "error"
            assert payload["error"] == BUSY
            assert payload["confirmation"] is None

            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            assert store.get_conversation(group["id"]) is not None

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is False
    assert results["call_1"]["error"] == BUSY


def test_dissolve_group_tool_times_out_without_dissolving(make_backend, upstream) -> None:
    backend = make_backend(confirm_timeout_seconds=0.3)
    group = seed_group(backend)
    upstream.script = [
        tool_calls(dissolve_call("call_1", group["id"])),
        {"content": "没有确认，先不动"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把这个群解散")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            detail = await get_conversation(client, direct_id)
            action = action_messages(detail)[-1]
            assert action["action"]["status"] == "error"
            assert action["action"]["error"] == TIMEOUT_ERROR
            assert action["content"] == TIMEOUT_ERROR
            assert action["action"]["confirmation"] is None
            assert store_of(backend).get_conversation(group["id"]) is not None

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is False
    assert results["call_1"]["error"] == TIMEOUT_ERROR


def test_stopping_a_run_while_awaiting_a_dissolve_keeps_the_group(
    make_backend, upstream
) -> None:
    backend = make_backend()
    group = seed_group(backend)
    upstream.script = [tool_calls(dissolve_call("call_1", group["id"]))]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "把这个群解散")
            await wait_for_awaiting(client, direct_id)

            started = time.monotonic()
            stopped = await stop_run(client, run["id"])
            elapsed = time.monotonic() - started
            assert stopped["status"] == "stopped", stopped
            assert elapsed < 5.0

            detail = await get_conversation(client, direct_id)
            action = action_messages(detail)[-1]
            assert action["action"]["status"] == "completed"
            assert action["action"]["summary"] == "你保留了群组「小组」"
            assert action["action"]["confirmation"] is None
            assert action["status"] == "complete"
            assert store_of(backend).get_conversation(group["id"]) is not None

    asyncio.run(body())


def test_dissolving_the_group_the_supervisor_is_speaking_in_is_refused(
    make_backend, upstream
) -> None:
    backend = make_backend()
    group = seed_group(backend)
    upstream.script = [
        tool_calls(dissolve_call("call_1", group["id"])),
        {"content": "这个群不能在这里解散"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "解散这个群")
            assert run["status"] == "completed", run

            detail = await get_conversation(client, group["id"])
            action = action_messages(detail)[-1]
            assert action["action"]["tool_name"] == "dissolve_group"
            assert action["action"]["status"] == "error"
            assert action["action"]["error"] == INSIDE
            assert action["action"]["confirmation"] is None
            assert store_of(backend).get_conversation(group["id"]) is not None

    asyncio.run(body())

    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is False
    assert results["call_1"]["error"] == INSIDE


def test_a_member_calling_dissolve_group_is_refused(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    group = seed_group(backend)
    member_id = next(
        agent_id for agent_id in group["member_ids"] if agent_id != store.supervisor()["id"]
    )
    member_chat = store.ensure_direct_conversation(member_id)
    upstream.script = [
        tool_calls(dissolve_call("call_1", group["id"], "想解散")),
        {"content": "我做不到这件事"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, member_chat["id"], "解散小组")
            assert run["status"] == "completed", run

            detail = await get_conversation(client, member_chat["id"])
            action = action_messages(detail)[-1]
            assert action["action"]["tool_name"] == "dissolve_group"
            assert action["action"]["status"] == "error"
            assert "未知工具" in action["action"]["error"]
            assert action["action"]["confirmation"] is None
            assert store.get_conversation(group["id"]) is not None

    asyncio.run(body())

    assert "dissolve_group" not in tool_names(upstream.requests[0])
    results = dict(tool_results(upstream.requests[1]))
    assert results["call_1"]["ok"] is False
    assert "未知工具" in results["call_1"]["error"]


def test_the_supervisor_is_offered_dissolve_group(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "好的"}]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "随便聊聊")
            assert run["status"] == "completed", run

    asyncio.run(body())

    names = tool_names(upstream.requests[0])
    assert "dissolve_group" in names
    assert "delete_agent" in names


def test_a_restart_fails_an_action_left_awaiting_a_dissolve(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    group = seed_group(backend)
    direct_id = store.direct_conversation()["id"]
    run, _ = store.begin_run(direct_id, "把这个群解散")
    action, _ = store.create_action(
        run_id=run["id"],
        conversation_id=direct_id,
        tool_call_id="call_1",
        tool_name="dissolve_group",
        arguments="{}",
        summary="解散群组「小组」",
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        status="awaiting",
        confirmation={
            "prompt": "主管想解散群组「小组」：这个目标已经结束。",
            "confirm_label": "解散",
            "deny_label": "保留",
        },
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

            assert (await get_run(client, run["id"]))["status"] == "stopped"

    asyncio.run(body())
    assert store_of(backend).get_conversation(group["id"]) is not None
