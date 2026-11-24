"""Approval modes and folders, over real HTTP: the card each mode skips or shows.

Every test runs against a temporary working folder, so nothing outside ``tmp_path`` is read
or written. The upstream supplies the tool calls; the assertions check the stored actions,
the files on disk and the result the model was handed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from app import approvals, schedules
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    get_conversation,
    get_workspace,
    last_user_text,
    run_goal,
    start_run,
    store_of,
    system_prompt,
    tool_results,
    wait_for_run,
)

# ------------------------------------------------------------------------ helpers


async def wait_for_awaiting(
    client, conversation_id: str, *, tool_name: str | None = None, timeout: float = 15.0
) -> dict:
    deadline = time.monotonic() + timeout
    detail = await get_conversation(client, conversation_id)
    while time.monotonic() < deadline:
        for message in action_messages(detail):
            action = message["action"]
            if action["status"] != "awaiting":
                continue
            if tool_name is None or action["tool_name"] == tool_name:
                return message
        await asyncio.sleep(0.05)
        detail = await get_conversation(client, conversation_id)
    shown = [
        (m["action"]["tool_name"], m["action"]["status"]) for m in action_messages(detail)
    ]
    raise AssertionError(f"no awaiting {tool_name or 'action'} in {conversation_id}: {shown}")


async def awaiting_actions(client, conversation_id: str) -> list[dict]:
    detail = await get_conversation(client, conversation_id)
    return [m["action"] for m in action_messages(detail) if m["action"]["status"] == "awaiting"]


async def set_mode(client, mode: str) -> dict:
    response = await client.patch("/settings/permissions", json={"mode": mode})
    assert response.status_code == 200, response.text
    return response.json()


def write_call(call_id: str, path: str, content: str = "正文") -> dict:
    return call(call_id, "file_write", {"path": path, "content": content})


def write_result(requests: list[dict]) -> dict:
    """The last tool result the model was handed (the write's outcome)."""
    results = tool_results(requests[-1])
    assert results, "the model saw no tool result"
    return results[-1][1]


def tool_texts(request: dict) -> list[str]:
    """Every tool result exactly as the model read it (a file write answers in plain text)."""
    return [
        str(message.get("content") or "")
        for message in request["messages"]
        if message.get("role") == "tool"
    ]


# --------------------------------------------------------------------------- ask


def test_ask_approving_a_write_creates_the_file(make_backend, upstream, tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = make_backend(workspace_dir=workspace)
    upstream.script = [
        tool_calls(write_call("call_1", "notes/方案.md", "第一行")),
        {"content": "写好了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct, "把方案写到 notes/方案.md")
            awaiting = await wait_for_awaiting(client, direct, tool_name="file_write")
            assert awaiting["action"]["confirmation"]["rule"] == "files:write_inside"
            assert not (workspace / "notes" / "方案.md").exists()

            answered = await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": True}
            )
            assert answered.status_code == 200, answered.text
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert (workspace / "notes" / "方案.md").read_text(encoding="utf-8") == "第一行"

    asyncio.run(body())


def test_ask_denying_a_write_leaves_no_file_and_tells_the_model(make_backend, upstream, tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = make_backend(workspace_dir=workspace)
    upstream.script = [
        tool_calls(write_call("call_1", "notes/方案.md")),
        {"content": "好的，我没写"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct, "把方案写到 notes/方案.md")
            awaiting = await wait_for_awaiting(client, direct, tool_name="file_write")
            answered = await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": False}
            )
            assert answered.status_code == 200, answered.text
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert not (workspace / "notes" / "方案.md").exists()
            assert write_result(upstream.requests) == {"ok": False, "denied": True}

    asyncio.run(body())


# -------------------------------------------------------------------------- auto


def test_auto_writes_inside_without_a_card_but_outside_still_asks(
    make_backend, upstream, tmp_path, tmp_path_factory
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Outside the backend's data directory (which is tmp_path itself), so this exercises the
    # "outside the allowed folders" branch rather than the protected-data one.
    outside = tmp_path_factory.mktemp("outside")
    outside.mkdir(exist_ok=True)
    backend = make_backend(workspace_dir=workspace)

    async def body() -> None:
        async with client_for(backend) as client:
            assert (await set_mode(client, "auto"))["mode"] == "auto"
            direct = (await get_workspace(client))["direct_conversation_id"]

            target = outside / "outside.md"
            # One script for both runs: the fake upstream indexes it by request count.
            upstream.script = [
                tool_calls(write_call("call_1", "notes/inside.md", "里面")),
                {"content": "写好了"},
                tool_calls(write_call("call_2", str(target), "外面")),
                {"content": "等你允许"},
            ]
            run = await run_goal(client, direct, "把内容写到 notes/inside.md")
            assert run["status"] == "completed", run
            assert await awaiting_actions(client, direct) == []
            assert (workspace / "notes" / "inside.md").read_text(encoding="utf-8") == "里面"

            run = await start_run(client, direct, f"写到 {target}")
            awaiting = await wait_for_awaiting(client, direct, tool_name="file_write")
            assert awaiting["action"]["confirmation"]["rule"] == "files:write_outside"
            assert not target.exists()
            await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": True}
            )
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert target.read_text(encoding="utf-8") == "外面"

    asyncio.run(body())


# -------------------------------------------------------------------------- full


def test_full_needs_no_card_but_deleting_a_member_still_asks(
    make_backend, upstream, tmp_path, tmp_path_factory
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path_factory.mktemp("outside")
    backend = make_backend(workspace_dir=workspace)
    member = store_of(backend).create_agent(name="临时成员", instructions="测试", kind="member")
    target = outside / "outside.md"

    upstream.script = [
        tool_calls(write_call("call_1", str(target), "完全权限")),
        tool_calls(call("call_2", "run_code", {"language": "python", "code": "print('跑了')"})),
        tool_calls(call("call_3", "delete_agent", {"agent_id": member["id"], "reason": "测试"})),
        {"content": "都做完了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            assert (await set_mode(client, "full"))["mode"] == "full"
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct, "写文件、跑代码、删掉临时成员")

            # Nine of the ten cards are gone; only deleting a member still parks.
            awaiting = await wait_for_awaiting(client, direct, tool_name="delete_agent")
            assert [a["tool_name"] for a in await awaiting_actions(client, direct)] == [
                "delete_agent"
            ]
            assert target.read_text(encoding="utf-8") == "完全权限"
            assert any("跑了" in text for text in tool_texts(upstream.requests[-1]))

            await client.post(
                f"/actions/{awaiting['action']['id']}/confirm", json={"approve": True}
            )
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert store_of(backend).get_agent(member["id"]) is None

    asyncio.run(body())


# ----------------------------------------------------------------- member override


def test_a_member_mode_overrides_the_workspace(make_backend, upstream, tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = make_backend(workspace_dir=workspace)
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="写文件", kind="member")
    group = store.create_conversation(
        kind="group",
        title="写文件小组",
        purpose="让甲写一个文件",
        member_ids=[supervisor["id"], member["id"]],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            # The workspace asks; 甲 alone is automatic.
            patched = await client.patch(
                f"/agents/{member['id']}", json={"permission_mode": "auto"}
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["permission_mode"] == "auto"
            assert (await client.get("/settings/permissions")).json()["mode"] == "ask"

            upstream.script = [
                tool_calls(
                    call(
                        "call_ask",
                        "ask_agent",
                        {
                            "group_id": group["id"],
                            "agent_id": member["id"],
                            "task": "把结论写到 member.md",
                        },
                    )
                ),
                tool_calls(write_call("call_write", "member.md", "甲写的")),
                {"content": "写好了"},
                {"content": "甲写完了"},
            ]
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct, "让甲写一份结论")
            assert run["status"] == "completed", run
            assert (workspace / "member.md").read_text(encoding="utf-8") == "甲写的"
            assert await awaiting_actions(client, group["id"]) == []
            assert await awaiting_actions(client, direct) == []

    asyncio.run(body())


# ------------------------------------------------------------------ always allow


def test_always_allow_remembers_the_inside_rule_only(
    make_backend, upstream, tmp_path, tmp_path_factory
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path_factory.mktemp("outside")
    backend = make_backend(workspace_dir=workspace)

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]

            target = outside / "third.md"
            # One script for all three runs: the fake upstream indexes it by request count.
            upstream.script = [
                tool_calls(write_call("call_1", "first.md", "第一次")),
                {"content": "写好了"},
                tool_calls(write_call("call_2", "second.md", "第二次")),
                {"content": "又写好了"},
                tool_calls(write_call("call_3", str(target), "外面")),
                {"content": "等你决定"},
            ]
            run = await start_run(client, direct, "写 first.md")
            awaiting = await wait_for_awaiting(client, direct, tool_name="file_write")
            await client.post(
                f"/actions/{awaiting['action']['id']}/confirm",
                json={"approve": True, "always": True},
            )
            await wait_for_run(client, run["id"])

            rules = (await client.get("/approval-rules")).json()["rules"]
            assert [rule["tool_name"] for rule in rules] == ["files:write_inside"]

            # The next write inside needs no card.
            run = await start_run(client, direct, "再写一个")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert await awaiting_actions(client, direct) == []
            assert (workspace / "second.md").read_text(encoding="utf-8") == "第二次"

            # Outside is a different promise, so it still asks.
            run = await start_run(client, direct, "写到外面")
            awaiting = await wait_for_awaiting(client, direct, tool_name="file_write")
            assert awaiting["action"]["confirmation"]["rule"] == "files:write_outside"
            assert not target.exists()

    asyncio.run(body())


# ----------------------------------------------------------------- folder settings


def test_an_added_folder_counts_as_inside_and_bad_folders_are_refused(
    make_backend, upstream, tmp_path, tmp_path_factory
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path_factory.mktemp("outside")
    shared = outside / "shared"
    shared.mkdir()
    backend = make_backend(workspace_dir=workspace)
    target = shared / "from-bot.md"

    async def body() -> None:
        async with client_for(backend) as client:
            # 自动审批 writes inside the allowed folders without a card; ask would still ask.
            await set_mode(client, "auto")
            added = await client.patch(
                "/settings/permissions", json={"folders": [str(shared)]}
            )
            assert added.status_code == 200, added.text
            assert added.json()["folders"] == [str(shared)]

            upstream.script = [
                tool_calls(write_call("call_1", str(target), "共享文件夹")),
                {"content": "写好了"},
            ]
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct, "写到共享文件夹")
            assert run["status"] == "completed", run
            assert await awaiting_actions(client, direct) == []

            missing = await client.patch(
                "/settings/permissions", json={"folders": [str(tmp_path / "nope")]}
            )
            assert missing.status_code == 422, missing.text
            assert "不存在" in missing.json()["detail"]

            data = await client.patch(
                "/settings/permissions",
                json={"folders": [str(backend.settings.database_path.parent)]},
            )
            assert data.status_code == 422, data.text
            assert "Cubes 自己的数据" in data.json()["detail"]

    asyncio.run(body())


# --------------------------------------------------------------------- protection


def test_cubes_own_data_stays_off_limits_even_in_full_mode(
    make_backend, upstream, tmp_path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = make_backend(workspace_dir=workspace)
    database = str(backend.settings.database_path)
    upstream.script = [
        tool_calls(call("call_1", "file_write", {"path": database, "content": "覆盖"})),
        tool_calls(call("call_2", "file_read", {"path": database})),
        {"content": "我被拒绝了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await set_mode(client, "full")
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct, "读写数据库文件")
            assert run["status"] == "completed", run
            results = tool_texts(upstream.requests[-1])
            assert len(results) == 2
            for result in results:
                assert "Cubes 自己的数据" in result
            assert await awaiting_actions(client, direct) == []

    asyncio.run(body())


# ---------------------------------------------------------------------- scheduled


def test_a_scheduled_run_in_ask_mode_cannot_write(make_backend, upstream, tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = make_backend(workspace_dir=workspace, schedule_tick_seconds=0.05)
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    schedule = store.create_schedule(
        title="定时写文件",
        prompt="把内容写到 scheduled.md",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="interval",
        at=None,
        every_minutes=30,
        cron=None,
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=schedules.utc_iso(datetime.now(timezone.utc) - timedelta(seconds=2)),
        created_by="主管",
    )
    upstream.script = [
        tool_calls(write_call("call_1", "scheduled.md", "不该落盘")),
        {"content": "没写成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            deadline = time.monotonic() + 15
            fired = store.get_schedule(schedule["id"])
            while time.monotonic() < deadline:
                fired = store.get_schedule(schedule["id"])
                if fired is not None and fired["run_count"] == 1:
                    break
                await asyncio.sleep(0.05)
            assert fired is not None and fired["run_count"] == 1
            run = await wait_for_run(client, fired["last_run_id"])
            assert run["status"] == "completed", run

    asyncio.run(body())
    assert not (workspace / "scheduled.md").exists()
    refused = "\n".join(tool_texts(upstream.requests[-1]))
    assert approvals.UNATTENDED_REFUSAL in refused or "没有人能确认" in refused


# -------------------------------------------------------------------------- hint


def test_the_runtime_hint_names_the_working_folder(make_backend, upstream, tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    backend = make_backend(workspace_dir=workspace)

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct, "你好")
            assert run["status"] == "completed", run

    asyncio.run(body())
    first = upstream.request(0)
    hint = last_user_text(first)
    assert str(workspace) in hint
    assert "工作文件夹" in hint
    # It rides with the turn, never in the cached system prefix.
    assert str(workspace) not in system_prompt(first)
