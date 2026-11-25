"""Activities: one bot turn's record, its steps, the turns it caused and the work log."""

from __future__ import annotations

import asyncio
import time

from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    collect_events,
    get_conversation,
    payloads,
    run_goal,
    settled,
    start_run,
    stop_run,
    store_of,
    wait_for_run,
)


def build_group(backend, *, with_supervisor: bool = False) -> dict:
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="核对发布数据", kind="member")
    members = [member["id"]]
    if with_supervisor:
        members.insert(0, supervisor["id"])
    group = store.create_conversation(
        kind="group",
        title="发布核对",
        purpose="核对发布页的数据",
        member_ids=members,
    )
    return {"supervisor": supervisor, "member": member, "group": group}


async def activities_of(client, agent_id: str) -> list[dict]:
    response = await client.get(f"/agents/{agent_id}/activities")
    assert response.status_code == 200, response.text
    return response.json()["activities"]


async def wait_for_title(client, agent_id: str, title: str, *, timeout: float = 10.0) -> str:
    """Wait for the naming call to land: it runs beside the turn, never inside it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await activities_of(client, agent_id)
        if rows and rows[0]["title"] == title:
            return title
        await asyncio.sleep(0.05)
    raise AssertionError(f"no activity titled {title!r} appeared")


def test_a_group_turn_with_two_tools_is_one_completed_activity(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [
        tool_calls(
            call("c1", "remind_me", {"in_minutes": 30, "note": "复核数字"}),
            call("c2", "remind_me", {"in_minutes": 40, "note": "看日志"}),
        ),
        {"content": "核对完了，数据没问题。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            events_task = asyncio.create_task(
                collect_events(client, f"/events?conversation_id={gid}", until=settled)
            )
            run = await start_run(client, gid, "@甲 核对发布页的数据")
            done = await wait_for_run(client, run["id"])
            assert done["status"] == "completed", done
            events = await events_task

            listed = await activities_of(client, team["member"]["id"])
            assert len(listed) == 1, listed
            activity = listed[0]
            assert activity["id"].startswith("ac_")
            assert (activity["conversation_id"], activity["agent_id"]) == (gid, team["member"]["id"])
            assert activity["status"] == "completed"
            assert activity["steps"] == 2, activity
            assert activity["task_id"] is None and activity["parent_id"] is None
            assert activity["finished_at"] and activity["current_step"] == ""
            assert 0 < len(activity["title"]) <= 16, activity["title"]
            assert activity["summary"], activity
            # The naming call renamed it: the heuristic would have kept the whole request.
            assert upstream.activity_request_count >= 1
            assert await wait_for_title(client, team["member"]["id"], "核对数据")
            assert activity["latest_text"]

            detail = await client.get(f"/activities/{activity['id']}")
            assert detail.status_code == 200, detail.text
            page = detail.json()
            assert page["conversation_title"] == "发布核对"
            assert page["activity"]["id"] == activity["id"]
            assert [section["label"] for section in page["sections"]] == ["甲"]
            steps = page["sections"][0]["steps"]
            assert [step["kind"] for step in steps] == ["tool", "tool"], steps
            assert all(step["status"] == "completed" for step in steps)
            assert all(step["action_id"] for step in steps)
            assert page["answer"] is not None
            assert page["answer"]["id"] == activity["answer_message_id"]
            assert page["answer"]["activity_id"] == activity["id"]
            assert page["answer"]["leads_to_tools"] is False

            # Every row the turn wrote carries its id; the answer is the only non-narration.
            conversation = await get_conversation(client, gid)
            stamped = [
                message
                for message in conversation["messages"]
                if message["activity_id"] == activity["id"]
            ]
            assert len(stamped) == 3, stamped
            assert {message["kind"] for message in stamped} == {"action", "agent"}
            answers = [
                message
                for message in stamped
                if message["kind"] == "agent" and message["leads_to_tools"] is False
            ]
            assert [message["id"] for message in answers] == [activity["answer_message_id"]]

            seen = payloads(events, "activity")
            assert seen, [name for name, _ in events]
            assert {payload["id"] for payload in seen} == {activity["id"]}
            assert seen[0]["status"] == "running"
            assert seen[-1]["status"] == "completed"
            assert seen[-1]["steps"] == 2

    asyncio.run(body())


def test_a_sections_messages_carry_its_rows_and_attachments(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    shot = store_of(backend).save_attachment(b"\xff\xd8\xff\xe0" + b"\x00" * 24, "image/jpeg")
    upstream.script = [
        tool_calls(call("c1", "remind_me", {"in_minutes": 30, "note": "复核数字"})),
        {"content": f"看完了，截图如下。\n\n<images>{shot['id']}</images>"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            started = await client.post(
                f"/conversations/{gid}/runs",
                json={"content": "@甲 核对发布页的数据", "attachment_ids": [shot["id"]]},
            )
            assert started.status_code == 202, started.text
            run = await wait_for_run(client, started.json()["id"])
            assert run["status"] == "completed", run
            [activity] = await activities_of(client, team["member"]["id"])

            detail = await client.get(f"/activities/{activity['id']}")
            assert detail.status_code == 200, detail.text
            page = detail.json()
            [section] = page["sections"]
            messages = section["messages"]
            # The rows are the conversation's own payload, in the order the turn wrote them.
            assert [message["kind"] for message in messages] == ["action", "agent"], messages
            assert all(message["activity_id"] == activity["id"] for message in messages)
            tool = messages[0]
            assert tool["action"]["tool_name"] == "remind_me"
            assert tool["action"]["summary"]
            answer = messages[-1]
            assert answer["id"] == page["answer"]["id"]
            assert answer["content"] == "看完了，截图如下。"
            assert [image["id"] for image in answer["attachments"]] == [shot["id"]]

            conversation = await get_conversation(client, gid)
            stamped = [
                message for message in conversation["messages"] if message["activity_id"] == activity["id"]
            ]
            assert [message["id"] for message in messages] == [message["id"] for message in stamped]

    asyncio.run(body())


def test_the_naming_call_asks_a_reasoning_model_for_low_effort(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    model = store.default_model()
    assert model is not None
    store.update_provider(
        model["provider_id"],
        model_caps={model["model"]: {"reasoning": True, "reasoning_levels": ["low", "high"]}},
    )
    team = build_group(backend)
    upstream.script = [
        tool_calls(call("c1", "remind_me", {"in_minutes": 30, "note": "复核数字"})),
        {"content": "核对完了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 核对发布页的数据")
            assert run["status"] == "completed", run
            [naming] = upstream.activity_requests
            # The naming output needs room past a reasoning model's thinking, and asks for
            # the same low effort the dispatcher uses: otherwise no content arrives at all.
            assert naming["max_tokens"] == 400
            assert naming["reasoning_effort"] == "low"

    asyncio.run(body())


def test_a_stopped_turn_closes_its_activity_with_a_plain_line(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 40
    upstream.script = [{"content": "我先慢慢看一眼日志，然后再决定下一步怎么走。", "slow": True}]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            run = await start_run(client, gid, "@甲 看看为什么超时")
            await wait_for_run(client, run["id"], timeout=5.0, statuses=("running",))
            await asyncio.sleep(0.3)
            await stop_run(client, run["id"])
            await wait_for_run(client, run["id"])

            listed = await activities_of(client, team["member"]["id"])
            assert len(listed) == 1, listed
            activity = listed[0]
            assert activity["status"] == "stopped", activity
            assert activity["summary"].startswith("已停下"), activity
            assert activity["answer_message_id"] is None
            assert activity["finished_at"]

    asyncio.run(body())


def test_an_asked_member_is_a_second_section_of_the_same_activity(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend, with_supervisor=True)
    gid = team["group"]["id"]
    upstream.script = [
        tool_calls(
            call(
                "c1",
                "ask_agent",
                {"group_id": gid, "agent_id": team["member"]["id"], "task": "评估一下风险"},
            )
        ),
        {"content": "风险不大，可以发。"},
        {"content": "我问过甲了，风险不大。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, gid, "@主管 评估一下发布风险")
            assert run["status"] == "completed", run

            boss_activities = await activities_of(client, team["supervisor"]["id"])
            assert len(boss_activities) == 1, boss_activities
            parent = boss_activities[0]
            assert parent["steps"] == 1, parent  # the ask_agent call

            detail = await client.get(f"/activities/{parent['id']}")
            assert detail.status_code == 200, detail.text
            page = detail.json()
            labels = [section["label"] for section in page["sections"]]
            assert labels == ["主管", "甲"], labels
            [handoff] = page["sections"][0]["steps"]
            assert handoff["kind"] == "handoff"
            assert handoff["target_activity_id"] == page["sections"][1]["activity_id"]
            child_id = page["sections"][1]["activity_id"]
            assert page["sections"][1]["id"] == child_id
            assert page["answer"]["content"] == "我问过甲了，风险不大。"

            child = await client.get(f"/activities/{child_id}")
            assert child.status_code == 200, child.text
            child_page = child.json()
            assert child_page["activity"]["parent_id"] == parent["id"]
            assert child_page["activity"]["agent_id"] == team["member"]["id"]
            assert child_page["answer"]["content"] == "风险不大，可以发。"

            member_list = await activities_of(client, team["member"]["id"])
            assert [row["id"] for row in member_list] == [child_id]

    asyncio.run(body())


def test_the_work_log_pages_newest_first(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="干活", kind="member")
    direct = store.ensure_direct_conversation(member["id"])
    assert direct is not None
    made = [
        store.create_activity(
            conversation_id=direct["id"],
            agent_id=member["id"],
            agent_name=member["name"],
            request=f"第 {index + 1} 件事",
            run_id=None,
        )
        for index in range(3)
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            first = await client.get(f"/agents/{member['id']}/activities?limit=2")
            assert first.status_code == 200, first.text
            page = first.json()
            assert page["has_more"] is True
            assert [row["id"] for row in page["activities"]] == [made[2]["id"], made[1]["id"]]

            older = page["activities"][-1]["started_at"]
            second = await client.get(
                f"/agents/{member['id']}/activities", params={"before": older}
            )
            assert second.status_code == 200, second.text
            rest = second.json()
            assert rest["has_more"] is False
            assert [row["id"] for row in rest["activities"]] == [made[0]["id"]]
            assert rest["activities"][0]["steps"] == 0

    asyncio.run(body())


def test_unknown_activity_and_member_are_404(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            missing = await client.get("/activities/ac_nope")
            assert missing.status_code == 404
            assert missing.json()["detail"] == "这条记录不存在。"
            unknown = await client.get("/agents/a_nope/activities")
            assert unknown.status_code == 404
            assert unknown.json()["detail"] == "成员不存在。"

    asyncio.run(body())


def test_a_task_thread_turn_is_a_child_activity_with_its_task(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [
        tool_calls(call("s1", "start_task", {"title": "写一份方案"})),
        {"content": "方案写好了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            run = await run_goal(client, gid, "@甲 写一份方案")
            assert run["status"] == "completed", run
            deadline = time.monotonic() + 15
            tasks: list[dict] = []
            while time.monotonic() < deadline:
                response = await client.get(f"/conversations/{gid}/tasks")
                tasks = response.json()["tasks"]
                if tasks and tasks[0]["status"] == "done":
                    break
                await asyncio.sleep(0.05)
            [task] = tasks

            listed = await activities_of(client, team["member"]["id"])
            assert len(listed) == 2, listed
            by_conversation = {row["conversation_id"]: row for row in listed}
            group_activity = by_conversation[gid]
            thread_activity = by_conversation[task["thread_id"]]
            assert thread_activity["task_id"] == task["id"]
            assert thread_activity["parent_id"] == group_activity["id"]
            assert thread_activity["status"] == "completed"
            assert thread_activity["answer_message_id"]

            detail = await client.get(f"/activities/{group_activity['id']}")
            page = detail.json()
            assert len(page["sections"]) == 2, page["sections"]
            [handoff] = page["sections"][0]["steps"]
            assert handoff["kind"] == "handoff"
            assert handoff["target_activity_id"] == thread_activity["id"]

    asyncio.run(body())


def test_a_turn_waiting_on_a_question_is_awaiting(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="安排事情", kind="member")
    direct = store.ensure_direct_conversation(member["id"])
    assert direct is not None
    upstream.script = [
        tool_calls(
            call(
                "q1",
                "ask_user",
                {
                    "questions": [
                        {
                            "header": "方案",
                            "question": "先做哪一块？",
                            "options": [
                                {"label": "后端", "description": "先做接口"},
                                {"label": "前端", "description": "先做界面"},
                            ],
                        }
                    ]
                },
            )
        ),
        {"content": "那就先做后端。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], "帮我安排一下")
            deadline = time.monotonic() + 15
            activity: dict | None = None
            while time.monotonic() < deadline:
                rows = await activities_of(client, member["id"])
                if rows and rows[0]["status"] == "awaiting":
                    activity = rows[0]
                    break
                await asyncio.sleep(0.05)
            assert activity is not None
            assert activity["current_step"].startswith("等待你回答"), activity

            detail = await get_conversation(client, direct["id"])
            [card] = [
                message
                for message in detail["messages"]
                if message["kind"] == "action" and message["action"]["status"] == "awaiting"
            ]
            answered = await client.post(
                f"/actions/{card['action']['id']}/answer",
                json={
                    "answers": [{"question_index": 0, "labels": ["后端"], "other": None}],
                    "skip": False,
                },
            )
            assert answered.status_code == 200, answered.text
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            [activity] = await activities_of(client, member["id"])
            assert activity["status"] == "completed", activity
            detail_page = await client.get(f"/activities/{activity['id']}")
            [step] = detail_page.json()["sections"][0]["steps"]
            assert step["kind"] == "ask"
            assert step["status"] == "completed"

    asyncio.run(body())
