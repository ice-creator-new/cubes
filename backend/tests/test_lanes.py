"""Parallel lanes: a user send runs beside the work lane instead of steering it."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    last_user_text,
    start_run,
    store_of,
    use_standard_budget,
    system_prompt,
    tool_names,
    tool_results,
    wait_for_message,
    wait_for_run,
)

CONTEXT_PREFIX = "（群里刚刚发生的事，只是背景"
REQUEST_PREFIX = "（用户的新要求，不是新任务"


def user_messages(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "user"]


def _todo(*items: dict) -> dict:
    return {"items": items}


def test_a_send_runs_beside_the_work_lane_and_notes_it(make_backend, upstream) -> None:
    """The work lane keeps streaming; the reply lane answers and leaves a note behind."""
    calls: list[int] = []

    def spec(requests, index):
        calls.append(index)
        if index == 1:
            return {
                "content": "先列计划。",
                "tool_calls": [
                    {
                        "id": "call_todo_1",
                        "name": "todo_write",
                        "arguments": _todo(
                            {"text": "收集数据", "status": "doing"},
                            {"text": "写出结论", "status": "pending"},
                        ),
                    }
                ],
            }
        if index == 2:
            return {
                "content": None,
                "slow": True,
                "chunk_count": 20,
                "chunk_delay": 0.1,
                "tool_calls": [
                    {
                        "id": "call_todo_2",
                        "name": "todo_write",
                        "arguments": _todo(
                            {"text": "收集数据", "status": "done"},
                            {"text": "写出结论", "status": "doing"},
                        ),
                    }
                ],
            }
        if index == 3:
            return {"content": "进度：数据收好了，正在写结论。"}
        return {"content": "第一路完成。"}

    upstream.script = [spec]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            work = await start_run(client, direct_id, "做一份报告")
            # The first call wrote the board and its second call is streaming.
            await asyncio.sleep(0.4)
            reply = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "现在进度如何？"}
            )
            assert reply.status_code == 202, reply.text
            reply_run = reply.json()
            assert reply_run["id"] != work["id"]
            assert reply_run["lane"] == "reply"
            assert reply_run["status"] == "running"

            finished = await wait_for_run(client, reply_run["id"])
            assert finished["status"] == "completed"
            # The work lane was not stopped by the send.
            assert (await get_run(client, work["id"]))["status"] == "running"

            done = await wait_for_run(client, work["id"])
            assert done["status"] == "completed"

            # The reply's prompt carried the real board, not a guess.
            reply_request = next(
                request
                for request in upstream.requests
                if "现在进度如何？" in last_user_text(request)
            )
            system = system_prompt(reply_request)
            assert "你另一路正在做" in system
            assert "收集数据" in system
            assert "写出结论" in system

            # The work lane's next call saw the reply as background.
            work_requests = [
                request
                for request in upstream.requests
                if "第一路完成" not in last_user_text(request)
            ]
            assert any(
                CONTEXT_PREFIX in last_user_text(request) and "进度：数据收好了" in last_user_text(request)
                for request in work_requests
            )
            # The working lane is told to keep the board.
            assert "进度板" in system_prompt(upstream.request(0))
            detail = await get_conversation(client, direct_id)
            assert [m["status"] for m in user_messages(detail)] == ["complete", "complete"]
            assert agent_messages(detail)[-1]["content"] == "第一路完成。"

    asyncio.run(body())


def test_a_reply_lane_cannot_write_files(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 25
    calls: list[int] = []

    def spec(requests, index):
        calls.append(index)
        if index == 1:
            return {"content": None, "slow": True}
        if index == 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "name": "file_write",
                        "arguments": {"path": "x.txt", "content": "hi"},
                    }
                ],
            }
        return {"content": "我只回答进度，不改文件。"}

    upstream.script = [spec]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            work = await start_run(client, direct_id, "慢慢写")
            await wait_for_message(client, direct_id, status="streaming")
            reply = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "顺便把文件改了"}
            )
            assert reply.status_code == 202, reply.text
            await wait_for_run(client, reply.json()["id"])
            await client.post(f"/runs/{work['id']}/stop", json={})
            reply_request = next(
                request
                for request in upstream.requests
                if "顺便把文件改了" in last_user_text(request)
            )
            names = tool_names(reply_request)
            assert "file_read" in names
            assert "team_progress" in names
            assert "file_write" not in names
            assert "todo_write" not in names
            refused = next(
                result
                for _, result in tool_results(upstream.requests[-1])
                if "file_write" in json.dumps(result, ensure_ascii=False)
            )
            assert refused["ok"] is False
            assert "未知工具" in refused["error"]

    asyncio.run(body())


def test_the_third_lane_for_one_bot_queues(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_count = 60
    upstream.chunk_delay = 0.1
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            work = await start_run(client, direct_id, "第一件事")
            await wait_for_message(client, direct_id, status="streaming")
            reply = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "第二件事"}
            )
            assert reply.status_code == 202, reply.text
            assert reply.json()["status"] == "running"
            third = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "第三件事"}
            )
            assert third.status_code == 202, third.text
            assert third.json()["status"] == "queued"
            # Several sends can wait at once; each keeps its own row and message.
            fourth = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "第四件事"}
            )
            assert fourth.status_code == 202, fourth.text
            assert fourth.json()["status"] == "queued"
            assert fourth.json()["lane"] == "reply"

            await client.delete(f"/runs/{fourth.json()['id']}")
            await client.delete(f"/runs/{third.json()['id']}")
            await client.post(f"/runs/{reply.json()['id']}/stop", json={})
            await client.post(f"/runs/{work['id']}/stop", json={})

    asyncio.run(body())


def test_the_fifth_lane_in_a_group_queues(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_count = 80
    upstream.chunk_delay = 0.1
    backend = make_backend()
    store = store_of(backend)
    members = [
        store.create_agent(name=f"成员{number}", instructions="做事", kind="member")
        for number in range(1, 6)
    ]
    group = store.create_conversation(
        kind="group",
        title="五人小组",
        purpose="同时处理几件事",
        member_ids=[member["id"] for member in members],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            runs = []
            first = await start_run(client, group["id"], "第一件", mentions=[members[0]["id"]])
            runs.append(first)
            await wait_for_message(client, group["id"], status="streaming", kind="agent")
            for member in members[1:4]:
                response = await client.post(
                    f"/conversations/{group['id']}/runs",
                    json={"content": f"给{member['name']}的事", "mentions": [member["id"]]},
                )
                assert response.status_code == 202, response.text
                assert response.json()["status"] == "running"
                runs.append(response.json())
            fifth = await client.post(
                f"/conversations/{group['id']}/runs",
                json={"content": f"给{members[4]['name']}的事", "mentions": [members[4]["id"]]},
            )
            assert fifth.status_code == 202, fifth.text
            assert fifth.json()["status"] == "queued"

            await client.delete(f"/runs/{fifth.json()['id']}")
            for run in runs:
                await client.post(f"/runs/{run['id']}/stop", json={})

    asyncio.run(body())


def test_a_request_note_reaches_the_working_lane(make_backend, upstream) -> None:
    """A dispatcher intent with ``affects`` changes the running work, not a second answer."""
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="匠师", instructions="做东西", kind="member")
    group = store.create_conversation(
        kind="group",
        title="工坊",
        purpose="把东西做出来",
        member_ids=[member["id"]],
    )

    def member_spec(requests, index):
        if index == 1:
            return {
                "content": "先列计划。",
                "tool_calls": [
                    {
                        "id": "call_todo_1",
                        "name": "todo_write",
                        "arguments": _todo(
                            {"text": "做出第一版", "status": "doing"},
                            {"text": "改颜色", "status": "pending"},
                        ),
                    }
                ],
            }
        if index == 2:
            return {
                "content": None,
                "slow": True,
                "chunk_count": 18,
                "chunk_delay": 0.1,
                "tool_calls": [
                    {
                        "id": "call_todo_2",
                        "name": "todo_write",
                        "arguments": _todo(
                            {"text": "做出第一版", "status": "done"},
                            {"text": "改颜色", "status": "doing"},
                        ),
                    }
                ],
            }
        return {"content": "改好了，现在是蓝色。"}

    upstream.script = [member_spec]

    def dispatch_spec(requests, index):
        context = upstream.dispatch_context(requests[-1])
        if index == 1:
            speakers = [str(member["id"])]
            return {
                "content": json.dumps(
                    {"kind": "answer", "speakers": speakers, "reason": "让匠师做"},
                    ensure_ascii=False,
                )
            }
        activity = (context.get("activities") or [{}])[0]
        return {
            "content": json.dumps(
                {
                    "kind": "steer",
                    "affects": activity.get("id"),
                    "speakers": [],
                    "reason": "给正在做的东西补充要求",
                },
                ensure_ascii=False,
            )
        }

    upstream.dispatch_script = [dispatch_spec]

    async def body() -> None:
        async with client_for(backend) as client:
            work = await start_run(client, group["id"], "做一版界面", mentions=[member["id"]])
            await asyncio.sleep(0.4)
            extra = await client.post(
                f"/conversations/{group['id']}/runs",
                json={"content": "改成蓝色"},
            )
            assert extra.status_code == 202, extra.text
            assert extra.json()["lane"] == "reply"
            await wait_for_run(client, extra.json()["id"])
            live = store.live_work_activities(group["id"])
            assert live
            notes = store.list_activity_notes(str(live[0]["id"]))
            assert any(
                note["kind"] == "request" and "改成蓝色" in note["text"] for note in notes
            )
            done = await wait_for_run(client, work["id"])
            assert done["status"] == "completed"

            request = next(
                request
                for request in upstream.requests
                if "改好了，现在是蓝色" not in last_user_text(request)
                and any(
                    message.get("role") == "user"
                    and "改成蓝色" in str(message.get("content") or "")
                    for message in request["messages"]
                )
            )
            user_text = "\n".join(
                str(message.get("content") or "")
                for message in request["messages"]
                if message.get("role") == "user"
            )
            assert REQUEST_PREFIX in user_text

    asyncio.run(body())


def test_stop_one_lane_leaves_the_other(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_count = 60
    upstream.chunk_delay = 0.1
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            work = await start_run(client, direct_id, "做第一件事")
            await wait_for_message(client, direct_id, status="streaming")
            reply = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "第二件事"}
            )
            assert reply.json()["status"] == "running"

            stopped = await client.post(f"/runs/{reply.json()['id']}/stop", json={})
            assert stopped.json()["status"] == "stopped"
            assert (await get_run(client, work["id"]))["status"] == "running"
            await client.post(f"/runs/{work['id']}/stop", json={})

    asyncio.run(body())


def test_rewind_is_refused_while_any_lane_is_live(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_count = 60
    upstream.chunk_delay = 0.1
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            work = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")
            reply = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "再看看"}
            )
            assert reply.json()["status"] == "running"
            detail = await get_conversation(client, direct_id)
            first_user = user_messages(detail)[0]
            refused = await client.post(
                f"/conversations/{direct_id}/messages/{first_user['id']}/rewind", json={}
            )
            assert refused.status_code == 409, refused.text
            await client.post(f"/runs/{reply.json()['id']}/stop", json={})
            await client.post(f"/runs/{work['id']}/stop", json={})

    asyncio.run(body())


def test_a_status_decision_posts_the_board(make_backend, upstream) -> None:
    """A progress question is answered from the live board, with no model turn."""
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="资料员", instructions="查资料", kind="member")
    group = store.create_conversation(
        kind="group",
        title="资料组",
        purpose="整理资料",
        member_ids=[member["id"]],
    )

    def member_spec(requests, index):
        if index == 1:
            return {
                "content": "先列计划。",
                "tool_calls": [
                    {
                        "id": "call_todo",
                        "name": "todo_write",
                        "arguments": _todo(
                            {"text": "读完三份资料", "status": "doing"},
                            {"text": "写成摘要", "status": "pending"},
                        ),
                    }
                ],
            }
        return {"content": None, "slow": True, "chunk_count": 40, "chunk_delay": 0.1}

    upstream.script = [member_spec]

    def dispatch_spec(requests, index):
        if index == 1:
            return {
                "content": json.dumps(
                    {"kind": "answer", "speakers": [member["id"]], "reason": "让资料员做"},
                    ensure_ascii=False,
                )
            }
        return {
            "content": json.dumps(
                {"kind": "status", "reason": "问进度"}, ensure_ascii=False
            )
        }

    upstream.dispatch_script = [dispatch_spec]

    async def body() -> None:
        async with client_for(backend) as client:
            work = await start_run(client, group["id"], "整理资料", mentions=[member["id"]])
            await asyncio.sleep(0.4)
            asked = await client.post(
                f"/conversations/{group['id']}/runs", json={"content": "进度如何？"}
            )
            assert asked.status_code == 202, asked.text
            await wait_for_run(client, asked.json()["id"])
            detail = await get_conversation(client, group["id"])
            cards = [message for message in detail["messages"] if message["kind"] == "status"]
            assert cards
            activities = cards[-1]["status_card"]["activities"]
            assert activities
            assert any(
                item["text"] == "读完三份资料" for item in activities[0]["todos"]
            )
            await client.post(f"/runs/{work['id']}/stop", json={})

    asyncio.run(body())


def test_a_reply_lane_opens_a_task_behind_the_work(make_backend, upstream) -> None:
    """``start_task`` is allowed in a reply lane, but the task waits for the work lane."""
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="工程师", instructions="做东西", kind="member")
    group = store.create_conversation(
        kind="group",
        title="工程组",
        purpose="把东西做出来",
        member_ids=[member["id"]],
    )

    def spec(requests, index):
        if index == 1:
            return {"content": None, "slow": True, "chunk_count": 80, "chunk_delay": 0.1}
        if index == 2:
            return {
                "content": "好，我开个任务。",
                "tool_calls": [
                    {
                        "id": "call_task",
                        "name": "start_task",
                        "arguments": {"title": "做第二件事", "plan": "先收集再动手"},
                    }
                ],
            }
        return {"content": "任务线程做完了"}

    upstream.script = [spec]

    async def body() -> None:
        async with client_for(backend) as client:
            work = await start_run(client, group["id"], "先做第一件", mentions=[member["id"]])
            await wait_for_message(client, group["id"], status="streaming", kind="agent")
            asked = await client.post(
                f"/conversations/{group['id']}/runs", json={"content": "顺便开个任务"}
            )
            assert asked.status_code == 202, asked.text
            await wait_for_run(client, asked.json()["id"])
            tasks = (await client.get(f"/conversations/{group['id']}/tasks")).json()["tasks"]
            assert tasks and tasks[0]["status"] == "queued"
            assert (await get_run(client, work["id"]))["status"] == "running"
            await client.post(f"/conversations/{group['id']}/tasks/{tasks[0]['id']}/stop")
            await client.post(f"/runs/{work['id']}/stop", json={})

    asyncio.run(body())


def test_the_old_steer_setting_migrates_to_parallel(tmp_path: Path) -> None:
    from app.store import Store
    from app.store.db import SCHEMA_VERSION

    database = tmp_path / "migrate.sqlite3"
    store = Store(str(database))
    store.set_team_settings(send_mode="parallel")
    supervisor = store.supervisor()
    store.update_agent(supervisor["id"], send_mode="steer")
    with sqlite3.connect(database) as conn:
        raw = conn.execute(
            "SELECT value FROM meta WHERE key = 'team_settings'"
        ).fetchone()[0]
        data = json.loads(raw)
        data["send_mode"] = "steer"
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'team_settings'",
            (json.dumps(data, ensure_ascii=False),),
        )
        conn.execute("UPDATE meta SET value = '33' WHERE key = 'schema_version'")
        conn.commit()
    store.close()

    migrated = Store(str(database))
    try:
        assert migrated.team_settings()["send_mode"] == "parallel"
        assert migrated.get_agent(supervisor["id"])["send_mode"] == "parallel"
        with sqlite3.connect(database) as conn:
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        assert version == str(SCHEMA_VERSION)
    finally:
        migrated.close()


@pytest.mark.parametrize("preset", ["unlimited", "standard"])
def test_a_reply_lane_caps_its_lookups_and_still_hands_off(make_backend, upstream, preset) -> None:
    """Six lookups at most; the seventh is refused, the answer stands, start_task still runs."""
    reply_calls: list[int] = []
    work_calls: list[int] = []

    def spec(requests, index):
        text = last_user_text(requests[index - 1])
        if "看看现在几点" not in text:
            work_calls.append(index)
            if len(work_calls) == 1:
                return {
                    "content": "先列计划。",
                    "tool_calls": [
                        {
                            "id": "call_board_1",
                            "name": "todo_write",
                            "arguments": _todo(
                                {"text": "慢慢做第一件事", "status": "doing"},
                                {"text": "再收个尾", "status": "pending"},
                            ),
                        }
                    ],
                }
            return {"content": None, "slow": True, "chunk_count": 60, "chunk_delay": 0.1}
        reply_calls.append(index)
        step = len(reply_calls)
        if step <= 7:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_time_{step}",
                        "name": "team_progress",
                        "arguments": {},
                    }
                ],
            }
        if step == 8:
            # The cap's refusal named start_task; the hand-off is not refused with it.
            return {
                "content": "好，交给任务线。",
                "tool_calls": [
                    {
                        "id": "call_task_1",
                        "name": "start_task",
                        "arguments": {"title": "数时间", "plan": "交给任务线去数"},
                    }
                ],
            }
        return {"content": "交给任务线了。"}

    upstream.script = [spec]
    backend = make_backend()
    # The standard preset has a finite turn budget, so the refusal's wrap becomes real; the
    # default workspace has no ceiling, where the reply cap asks for the same wrap by itself.
    # The hand-off must pass through either.
    if preset == "standard":
        use_standard_budget(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            work = await start_run(client, direct_id, "先慢慢做一件事")
            await wait_for_message(client, direct_id, status="streaming")
            reply = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "顺便看看现在几点"}
            )
            assert reply.status_code == 202, reply.text
            assert reply.json()["lane"] == "reply"
            finished = await wait_for_run(client, reply.json()["id"])
            assert finished["status"] == "completed"

            reply_requests = [
                request
                for request in upstream.requests
                if "顺便看看现在几点" in last_user_text(request)
            ]
            assert "第一步就调用 start_task" in system_prompt(reply_requests[0])
            results = tool_results(reply_requests[-1])
            ran = [result for _, result in results if "activities" in result]
            refused = [
                result
                for _, result in results
                if result.get("ok") is False
                and "回答这一路只做简短查阅" in str(result.get("error"))
            ]
            assert len(ran) == 6
            assert refused
            detail = await get_conversation(client, direct_id)
            # ``start_task`` hands the turn off: its own text is the reply.
            assert agent_messages(detail)[-1]["content"] == "好，交给任务线。"
            # A ``todo_write`` row carries its board, so the chat can draw it without a read.
            boards = [
                message["action"]["todos"]
                for message in detail["messages"]
                if message["kind"] == "action"
                and message["action"]
                and message["action"]["tool_name"] == "todo_write"
            ]
            assert boards and [item["text"] for item in boards[0]] == [
                "慢慢做第一件事",
                "再收个尾",
            ]
            tasks = (await client.get(f"/conversations/{direct_id}/tasks")).json()["tasks"]
            assert tasks and tasks[0]["title"] == "数时间"
            await client.post(f"/conversations/{direct_id}/tasks/{tasks[0]['id']}/stop")
            await client.post(f"/runs/{work['id']}/stop", json={})

    asyncio.run(body())
