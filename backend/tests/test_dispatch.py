"""The invisible dispatcher: one no-tool call decides who answers a group message.

Everything here goes through real HTTP against the fake upstream, whose dispatch calls carry
the ``x-cubes-purpose: dispatch`` header and are recorded apart from a run's own replies, so a
scripted reply can opt into any decision without disturbing anyone else's routing.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from app import dispatch
from app.engine import SupervisorEngine
from fake_upstream import call, tool_calls
from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    last_user_text,
    run_goal,
    start_run,
    store_of,
    wait_for_run,
)


def build_group(backend, *, supervisor: bool = True) -> dict:
    store = store_of(backend)
    boss = store.supervisor()
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    members = [designer["id"], reviewer["id"]]
    if supervisor:
        members.insert(0, boss["id"])
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=members,
    )
    return {
        "supervisor": boss if supervisor else None,
        "designer": designer,
        "reviewer": reviewer,
        "group": group,
    }


def decision(**payload) -> dict:
    return {"content": json.dumps(payload, ensure_ascii=False)}


def dispatch_by(upstream, *, marker: str, status: str, kind: str) -> dict:
    """A script entry that routes one message to a task, and everything else by address.

    A dispatcher script answers every group message of a test, so the opening message must
    keep its normal route; only the message carrying ``marker`` becomes a steer/resume.
    """

    def entry(records, index):
        context = upstream.dispatch_context(records[index - 1])
        message = str(context.get("message") or "")
        if marker not in message:
            speakers = [row["id"] for row in context.get("addressed") or []]
            if not speakers:
                speakers = [row["id"] for row in (context.get("members") or [])[:1]]
            return decision(kind="answer", speakers=speakers, reason="按点名")
        task = next(task for task in context.get("tasks") or [] if task["status"] == status)
        return decision(kind=kind, task_id=task["id"], reason="按任务状态")

    return entry


async def wait_until(check, timeout: float = 10.0) -> None:
    """Poll ``check``; a sync predicate and an async one are both accepted."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


async def tasks_of(client, group_id: str) -> list[dict]:
    response = await client.get(f"/conversations/{group_id}/tasks")
    assert response.status_code == 200, response.text
    return response.json()["tasks"]


# ------------------------------------------------------------------ decisions


def test_the_dispatcher_picks_a_speaker_and_its_route_is_stored(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_script = [
        decision(kind="answer", speakers=[team["reviewer"]["id"]], reason="乙更合适")
    ]
    upstream.script = [{"content": "我来评估"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "这个方案谁来把关？")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        assert upstream.dispatch_request_count == 1
        [reply] = agent_messages(detail)
        assert reply["author_id"] == team["reviewer"]["id"]
        goal = detail["messages"][0]
        assert goal["route"] == {
            "speakers": [team["reviewer"]["id"]],
            "kind": "answer",
            "task_id": None,
            "reason": "乙更合适",
            "by": "dispatcher",
            "pending": False,
        }

    asyncio.run(body())


def test_an_opening_at_member_is_kept_even_when_the_model_omits_them(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_script = [
        decision(kind="answer", speakers=[team["reviewer"]["id"]], reason="交给乙")
    ]
    upstream.script = [{"content": "甲的方案"}, {"content": "乙的评估"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 出个方案")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        authors = [reply["author_id"] for reply in agent_messages(detail)]
        # The addressed member speaks first, then the dispatcher's addition.
        assert authors == [team["designer"]["id"], team["reviewer"]["id"]]
        assert detail["messages"][0]["route"]["speakers"] == [
            team["designer"]["id"],
            team["reviewer"]["id"],
        ]

    asyncio.run(body())


def test_a_mid_sentence_at_is_not_forced(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_script = [
        decision(kind="answer", speakers=[team["reviewer"]["id"]], reason="只是引用甲")
    ]
    upstream.script = [{"content": "我来说"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(
                client,
                team["group"]["id"],
                "请按 @甲 之前的想法继续，这次让更懂风险的人来说",
            )
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        # The mention is a reference, not an address: only the dispatcher's pick answers.
        assert [reply["author_id"] for reply in agent_messages(detail)] == [
            team["reviewer"]["id"]
        ]
        assert detail["messages"][0]["route"]["speakers"] == [team["reviewer"]["id"]]

    asyncio.run(body())


def test_none_ends_the_run_with_no_reply(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_script = [decision(kind="none", reason="只是道谢")]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "好的，谢谢")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        assert upstream.request_count == 0
        assert agent_messages(detail) == []
        assert detail["messages"][0]["route"]["kind"] == "none"

    asyncio.run(body())


def test_a_failed_dispatcher_falls_back_and_still_names_a_speaker(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_mode = "error"
    upstream.script = [{"content": "我先来"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 说说看")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        assert [(reply["author_id"], reply["content"]) for reply in agent_messages(detail)] == [
            (team["designer"]["id"], "我先来")
        ]
        assert detail["messages"][0]["route"]["by"] == "fallback"

    asyncio.run(body())


def test_an_unreadable_dispatcher_answer_falls_back_too(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_mode = "garbage"
    upstream.script = [{"content": "我在"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "有人吗")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        # No address, no last speaker: the group's first member is the fallback.
        assert agent_messages(detail)[-1]["author_id"] == team["supervisor"]["id"]
        assert detail["messages"][0]["route"]["by"] == "fallback"

    asyncio.run(body())


# ------------------------------------------------------------------ tasks by decision


def test_status_posts_a_card_with_no_agent_request(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    task = store.create_group_task(
        conversation_id=team["group"]["id"],
        owner_id=team["designer"]["id"],
        title="改登录页",
        status="running",
    )
    store.update_group_task(task["id"], current_step="正在改样式")
    store.bump_group_task_step(task["id"], "正在改样式")
    upstream.dispatch_script = [decision(kind="status", reason="用户问进度")]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "进展如何")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        assert upstream.request_count == 0
        [card] = [m for m in detail["messages"] if m["kind"] == "status"]
        assert card["author_id"] is None
        assert card["status_card"]["tasks"] == [
            {
                "owner": "甲",
                "title": "改登录页",
                "status": "running",
                "step": 1,
                "current": "正在改样式",
                "latest": "",
                "waiting_for": None,
            }
        ]
        assert card["status_card"]["idle"] == ["乙"]
        assert detail["messages"][0]["route"]["kind"] == "status"

    asyncio.run(body())


def test_steer_reaches_a_running_thread(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [
        tool_calls(call("s1", "start_task", {"title": "写方案"})),
        {"content": "正在检查这一部分的每一个细节。" * 10, "chunk_count": 150, "chunk_delay": 0.1},
    ]
    upstream.dispatch_script = [dispatch_by(upstream, marker="记得先查资料", status="进行中", kind="steer")]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 写一份完整方案")

            async def running():
                rows = await tasks_of(client, gid)
                return rows and rows[0]["status"] == "running"

            await wait_until(running)
            [task] = await tasks_of(client, gid)

            run = await run_goal(client, gid, "记得先查资料")
            assert run["status"] == "completed", run
            thread = await get_conversation(client, task["thread_id"])

            # The group message itself is the trigger, and its route says it steered.
            goal = [m for m in (await get_conversation(client, gid))["messages"] if m["kind"] == "user"][-1]
            assert goal["route"]["kind"] == "steer"
            assert goal["route"]["task_id"] == task["id"]
            notes = [
                m for m in thread["messages"] if m["kind"] == "user" and "记得先查资料" in m["content"]
            ]
            assert notes and "用户在群里对这个任务补了一句" in notes[0]["content"]

            await client.post(f"/conversations/{gid}/tasks/{task['id']}/stop")

    asyncio.run(body())


def test_resume_resumes_the_named_task(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    def thread_reply(requests, index):
        if "第 2 轮" in last_user_text(requests[index - 1]):
            return {"content": "接着写完了"}
        return {"content": "正在检查这一部分的每一个细节。" * 10, "chunk_count": 150, "chunk_delay": 0.1}

    upstream.script = [
        tool_calls(call("s1", "start_task", {"title": "写方案"})),
        thread_reply,
    ]
    upstream.dispatch_script = [dispatch_by(upstream, marker="接着做", status="停下了", kind="resume")]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 写方案")

            async def running():
                rows = await tasks_of(client, gid)
                return rows and rows[0]["status"] == "running"

            async def stopped():
                rows = await tasks_of(client, gid)
                return rows and rows[0]["status"] == "stopped"

            await wait_until(running)
            [task] = await tasks_of(client, gid)
            await client.post(f"/conversations/{gid}/tasks/{task['id']}/stop")
            await wait_until(stopped)

            run = await run_goal(client, gid, "接着做吧")
            assert run["status"] == "completed", run
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                current = (await tasks_of(client, gid))[0]
                if current["status"] == "done":
                    break
                await asyncio.sleep(0.05)
            assert (current["status"], current["rounds"]) == ("done", 2)
            assert current["thread_id"] == task["thread_id"]
            goal = [m for m in (await get_conversation(client, gid))["messages"] if m["kind"] == "user"][-1]
            assert goal["route"]["kind"] == "resume"
            assert goal["route"]["task_id"] == task["id"]

    asyncio.run(body())


# ------------------------------------------------------------------ reroute


def test_reroute_stops_the_running_run_and_answers_with_the_chosen_members(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 30
    upstream.script = [{"content": None, "slow": True}, {"content": "乙来说"}]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            first = await start_run(client, gid, "@甲 慢慢说")
            await wait_until(lambda: upstream.request_count > 0)

            detail = await get_conversation(client, gid)
            goal = detail["messages"][0]
            response = await client.post(
                f"/conversations/{gid}/messages/{goal['id']}/reroute",
                json={"speakers": [team["reviewer"]["id"]]},
            )
            assert response.status_code == 202, response.text
            second = response.json()
            assert second["id"] != first["id"]
            finished = await wait_for_run(client, second["id"])
            assert finished["status"] == "completed", finished
            stopped = await wait_for_run(client, first["id"])
            assert stopped["status"] == "stopped", stopped

            detail = await get_conversation(client, gid)
            users = [m for m in detail["messages"] if m["kind"] == "user"]
            assert len(users) == 1  # no new user message was written
            assert users[0]["route"]["by"] == "user"
            assert users[0]["route"]["speakers"] == [team["reviewer"]["id"]]
            replies = agent_messages(detail)
            assert replies[-1]["author_id"] == team["reviewer"]["id"]

    asyncio.run(body())



# ------------------------------------------------------------------ plain replies


def test_a_plain_reply_creates_no_task(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [{"content": "现在三点。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 现在几点了？")
            assert run["status"] == "completed", run
            assert await tasks_of(client, team["group"]["id"]) == []
            detail = await get_conversation(client, team["group"]["id"])

        assert [m["kind"] for m in detail["messages"]] == ["user", "agent"]

    asyncio.run(body())


# ------------------------------------------------------------------ group lifecycle


def test_a_group_without_a_supervisor_works_end_to_end(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend, supervisor=False)
    upstream.script = [{"content": "甲说"}, {"content": "乙说"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 说说看")
            assert run["status"] == "completed", run
            run = await run_goal(client, team["group"]["id"], "@所有人 各自说一句")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        replies = agent_messages(detail)
        assert replies[0]["author_id"] == team["designer"]["id"]
        # @所有人: both members answer side by side; there is no supervisor to close.
        assert {reply["author_id"] for reply in replies[1:]} == {
            team["designer"]["id"],
            team["reviewer"]["id"],
        }
        assert len(replies) == 3

    asyncio.run(body())


def test_rest_group_create_no_longer_adds_the_supervisor(make_backend) -> None:
    backend = make_backend()
    team = build_group(backend, supervisor=False)

    async def body() -> None:
        async with client_for(backend) as client:
            boss = store_of(backend).supervisor()
            response = await client.post(
                "/conversations",
                json={
                    "title": "评审组",
                    "purpose": "评审方案",
                    "member_ids": [team["designer"]["id"]],
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["member_ids"] == [team["designer"]["id"]]

            with_boss = await client.post(
                "/conversations",
                json={
                    "title": "带主管的组",
                    "purpose": "评审方案",
                    "member_ids": [team["designer"]["id"]],
                    "include_supervisor": True,
                },
            )
            assert with_boss.status_code == 201, with_boss.text
            assert with_boss.json()["member_ids"] == [
                boss["id"],
                team["designer"]["id"],
            ]

            empty = await client.post(
                "/conversations", json={"title": "空组", "purpose": "没人", "member_ids": []}
            )
            assert empty.status_code == 422, empty.text

    asyncio.run(body())


# ------------------------------------------------------------------ task endpoints


def test_resume_dismiss_and_seen_endpoints(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    thread = store.create_conversation(
        kind="thread", title="甲 · 任务", purpose="任务", member_ids=[team["designer"]["id"]]
    )
    stopped = store.create_group_task(
        conversation_id=team["group"]["id"],
        owner_id=team["designer"]["id"],
        title="停下的任务",
        status="stopped",
    )
    store.update_group_task(
        stopped["id"], thread_id=thread["id"], end_reason="出错了：boom", stopped_by="你"
    )
    done_thread = store.create_conversation(
        kind="thread", title="甲 · 完成", purpose="任务", member_ids=[team["designer"]["id"]]
    )
    done = store.create_group_task(
        conversation_id=team["group"]["id"],
        owner_id=team["designer"]["id"],
        title="做完的任务",
        status="done",
    )
    store.update_group_task(done["id"], thread_id=done_thread["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            # A done task is not resumable; a stopped one may be dismissed.
            refused = await client.post(
                f"/conversations/{team['group']['id']}/tasks/{done['id']}/resume"
            )
            assert refused.status_code == 409, refused.text
            dismissed = await client.post(
                f"/conversations/{team['group']['id']}/tasks/{stopped['id']}/dismiss"
            )
            assert dismissed.status_code == 200, dismissed.text
            assert dismissed.json()["dismissed_at"]
            # A stopped task cannot be marked seen; a done one can.
            not_done = await client.post(
                f"/conversations/{team['group']['id']}/tasks/{stopped['id']}/seen"
            )
            assert not_done.status_code == 409, not_done.text
            seen = await client.post(
                f"/conversations/{team['group']['id']}/tasks/{done['id']}/seen"
            )
            assert seen.status_code == 200, seen.text
            assert seen.json()["seen_at"]
            workspace = await get_workspace(client)

        assert [entry["task_id"] for entry in workspace["group_threads"]] == []

    asyncio.run(body())


def test_group_threads_list_a_stopped_task_until_dismissed_and_a_done_one_until_seen(
    make_backend,
) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)

    def task(title: str, status: str, *, seen: bool = False) -> dict:
        thread = store.create_conversation(
            kind="thread", title=f"甲 · {title}", purpose=title, member_ids=[team["designer"]["id"]]
        )
        created = store.create_group_task(
            conversation_id=team["group"]["id"],
            owner_id=team["designer"]["id"],
            title=title,
            status=status,
        )
        store.update_group_task(created["id"], thread_id=thread["id"])
        if seen:
            store.mark_task_seen(created["id"])
        return created

    stopped = task("停下的", "stopped")
    failed = task("失败的", "failed")
    done = task("做完的", "done")
    seen = task("看过的", "done", seen=True)

    listed = {entry["task_id"] for entry in store.list_group_threads()}
    assert listed == {stopped["id"], failed["id"], done["id"]}
    assert seen["id"] not in listed

    store.mark_task_dismissed(stopped["id"])
    store.mark_task_dismissed(failed["id"])
    store.mark_task_seen(done["id"])
    assert store.list_group_threads() == []


# ------------------------------------------------------------------ why a task ended


def test_end_reasons_and_stoppers_are_recorded(make_backend) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    thread = store.create_conversation(
        kind="thread", title="甲 · 任务", purpose="任务", member_ids=[team["designer"]["id"]]
    )
    task = store.create_group_task(
        conversation_id=team["group"]["id"],
        owner_id=team["designer"]["id"],
        title="任务",
        status="running",
        run_id="run_x",
    )
    store.update_group_task(task["id"], thread_id=thread["id"])
    settled = store.settle_group_tasks(
        "run_x", "stopped", end_reason="步数用完了", stopped_by="你"
    )
    assert [(row["id"], row["status"], row["end_reason"], row["stopped_by"]) for row in settled] == [
        (task["id"], "stopped", "步数用完了", "你")
    ]
    # The engine's own words for a limited / failed run.
    assert (
        SupervisorEngine._task_end_reason(
            "stopped", "limited", "上限", SimpleNamespace(upstream_error=False)
        )
        == "步数用完了"
    )
    assert "模型服务失败" in SupervisorEngine._task_end_reason(
        "failed", "error", "连接中断", SimpleNamespace(upstream_error=True)
    )
    assert "出错了" in SupervisorEngine._task_end_reason(
        "failed", "error", "boom", SimpleNamespace(upstream_error=False)
    )
    assert SupervisorEngine._task_end_reason(
        "done", "completed", None, SimpleNamespace(upstream_error=False)
    ) is None


def test_a_restart_heals_unfinished_tasks_and_says_who_stopped_them(make_backend) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    thread = store.create_conversation(
        kind="thread", title="甲 · 任务", purpose="任务", member_ids=[team["designer"]["id"]]
    )
    task = store.create_group_task(
        conversation_id=team["group"]["id"],
        owner_id=team["designer"]["id"],
        title="做一半",
        status="running",
        run_id="run_dead",
    )
    store.update_group_task(task["id"], thread_id=thread["id"])

    backend.restart()
    healed = store_of(backend).get_group_task(task["id"])
    assert (healed["status"], healed["stopped_by"]) == ("stopped", "重启后中断")


# ------------------------------------------------------------------ limits and health


def test_the_dispatch_call_waits_long_enough_for_a_thinking_model() -> None:
    # A thinking model spends seconds before its first token; routing must not rush it.
    assert dispatch.TIMEOUT_SECONDS == 120.0
    assert dispatch.reasoning_effort(True, ["low", "high"]) == "low"
    assert dispatch.reasoning_effort(True, ["minimal"]) == "minimal"
    assert dispatch.reasoning_effort(False, ["low"]) is None
    assert dispatch.reasoning_effort(True, ["high"]) is None


def test_the_pending_route_is_published_then_replaced(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_script = [
        {**decision(kind="answer", speakers=[team["reviewer"]["id"]], reason="乙来"), "chunk_delay": 0.4, "chunk_count": 2}
    ]
    upstream.chunk_count = 2

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, team["group"]["id"], "这个方案谁来把关？")
            deadline = time.monotonic() + 10
            pending_route = None
            pending_step = None
            while time.monotonic() < deadline:
                detail = await get_conversation(client, team["group"]["id"])
                if detail["messages"]:
                    pending_route = detail["messages"][0].get("route")
                current = await get_run(client, run["id"])
                pending_step = current["current_step"]
                if pending_route and pending_route.get("pending"):
                    break
                await asyncio.sleep(0.03)
            assert pending_route and pending_route["pending"] is True
            assert pending_route["speakers"] == []
            assert pending_step == "正在决定谁来接"

            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            detail = await get_conversation(client, team["group"]["id"])
            route = detail["messages"][0]["route"]
            assert route["by"] == "dispatcher"
            assert route["speakers"] == [team["reviewer"]["id"]]
            assert not route.get("pending")

    asyncio.run(body())


def test_a_failed_dispatch_falls_back_to_who_the_last_reply_at_ed(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    store.append_message(
        conversation_id=team["group"]["id"],
        author_id=team["designer"]["id"],
        author_name="甲",
        avatar_seed=None,
        kind="agent",
        content="风险这块交给乙。",
        status="complete",
        mentions=[team["reviewer"]["id"]],
    )
    upstream.dispatch_mode = "error"
    upstream.script = [{"content": "我来评估"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "你们继续看看")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        route = next(
            message["route"] for message in detail["messages"] if message["kind"] == "user"
        )
        assert route["by"] == "fallback"
        assert route["speakers"] == [team["reviewer"]["id"]]
        assert "被 @ 的成员" in route["reason"]
        assert agent_messages(detail)[-1]["author_id"] == team["reviewer"]["id"]

    asyncio.run(body())


def test_dispatch_health_counts_failures_in_the_team_settings(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_mode = "error"

    async def body() -> None:
        async with client_for(backend) as client:
            upstream.script = [{"content": "我先来"}]
            await run_goal(client, team["group"]["id"], "有人吗")
            response = await client.get("/settings/team")
            assert response.status_code == 200
            health = response.json()["dispatch_health"]
            assert health["failures_24h"] == 1
            assert health["last_error"] == "UpstreamError"
            assert health["last_at"]

            upstream.dispatch_mode = "garbage"
            upstream.script = [{"content": "我还是来"}]
            await run_goal(client, team["group"]["id"], "再来一次")
            health = (await client.get("/settings/team")).json()["dispatch_health"]
            assert health["failures_24h"] == 2
            assert health["last_error"] == "Unreadable"

    asyncio.run(body())


# ------------------------------------------------------------------ intents and invites


def test_parse_reads_fenced_array_with_commentary_around_it() -> None:
    text = (
        "好的，这条消息有两件事：\n"
        "```json\n"
        '[{"text": "改 react", "kind": "answer", "speakers": ["a"]},'
        ' {"text": "让画师来", "kind": "invite", "invite": ["p"], "speakers": ["p"]}]\n'
        "```\n"
        "就按这个分。"
    )
    decision = dispatch.parse(text, member_ids=["a"], task_ids=[], invite_ids=["p"])
    assert decision is not None
    assert [intent.kind for intent in decision.intents] == ["answer", "invite"]
    assert decision.intents[1].invite == ("p",)
    assert decision.intents[1].speakers == ("p",)


def test_parse_keeps_six_intents_and_truncates_the_seventh() -> None:
    text = json.dumps(
        {
            "intents": [
                {"text": f"第{i}件", "kind": "answer", "speakers": ["a"]}
                for i in range(7)
            ]
        },
        ensure_ascii=False,
    )
    decision = dispatch.parse(text, member_ids=["a"], task_ids=[])
    assert decision is not None
    assert len(decision.intents) == dispatch.MAX_INTENTS == 6
    assert decision.intents[-1].text == "第5件"


def test_parse_still_reads_the_old_single_decision() -> None:
    text = "决定如下：\n" + json.dumps(
        {"kind": "answer", "speakers": ["a"], "reason": "甲更合适"}, ensure_ascii=False
    ) + "\n以上。"
    decision = dispatch.parse(text, member_ids=["a"], task_ids=[])
    assert decision is not None
    assert decision.intents == ()
    assert decision.speakers == ("a",)
    assert decision.reason == "甲更合适"


def test_two_intents_run_their_own_slices_with_focus_notes(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.dispatch_script = [
        decision(
            intents=[
                {"text": "先评估风险", "kind": "answer", "speakers": [team["reviewer"]["id"]]},
                {"text": "出个方案", "kind": "answer", "speakers": [team["designer"]["id"]]},
            ],
            reason="一句话两件事",
        )
    ]
    upstream.script = [{"content": "风险在这里"}, {"content": "方案是这样"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "先评估风险，再出个方案")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        replies = agent_messages(detail)
        assert [reply["author_id"] for reply in replies] == [
            team["reviewer"]["id"],
            team["designer"]["id"],
        ]
        route = detail["messages"][0]["route"]
        assert [intent["text"] for intent in route["intents"]] == ["先评估风险", "出个方案"]
        assert route["speakers"] == [team["reviewer"]["id"], team["designer"]["id"]]
        first = last_user_text(upstream.requests[0])
        second = last_user_text(upstream.requests[1])
        assert "用户这条消息里交给你的部分：先评估风险" in first
        assert "交给你的部分：出个方案" in second
        assert "「甲」" in first and "「乙」" in second

    asyncio.run(body())


def test_an_invite_has_the_supervisor_add_the_member_then_the_invited_bot_answers(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    painter = store.create_agent(name="画师", instructions="出规格和视觉稿", kind="member")
    group = team["group"]
    upstream.dispatch_script = [
        decision(
            intents=[
                {
                    "text": "让画师出规格",
                    "kind": "invite",
                    "invite": [painter["id"]],
                    "speakers": [painter["id"]],
                }
            ],
            reason="画师不在群里",
        )
    ]
    upstream.script = [
        tool_calls(
            call(
                "s1",
                "set_group_members",
                {
                    "group_id": group["id"],
                    "member_ids": [*group["member_ids"], painter["id"]],
                },
            )
        ),
        {"content": "把画师拉进来，规格由他来出。"},
        {"content": "规格我来写。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@画师 出规格")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])

        assert painter["id"] in detail["conversation"]["member_ids"]
        replies = agent_messages(detail)
        assert replies[0]["author_id"] == team["supervisor"]["id"]
        assert replies[-1]["author_id"] == painter["id"]
        supervisor_request = last_user_text(upstream.requests[0])
        assert "set_group_members" in supervisor_request
        assert "「画师」" in supervisor_request
        painter_request = last_user_text(upstream.requests[2])
        assert "用户这条消息里交给你的部分：让画师出规格" in painter_request

    asyncio.run(body())


def test_an_invite_without_a_supervisor_adds_the_member_and_says_so(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend, supervisor=False)
    store = store_of(backend)
    painter = store.create_agent(name="画师", instructions="出规格和视觉稿", kind="member")
    group = team["group"]
    upstream.dispatch_script = [
        decision(
            intents=[
                {
                    "text": "让画师出规格",
                    "kind": "invite",
                    "invite": [painter["id"]],
                    "speakers": [painter["id"]],
                }
            ],
            reason="画师不在群里",
        )
    ]
    upstream.script = [{"content": "规格我出。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@画师 出规格")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])

        assert painter["id"] in detail["conversation"]["member_ids"]
        notices = [message["content"] for message in detail["messages"] if message["kind"] == "notice"]
        assert "已把 画师 拉进群" in notices
        assert agent_messages(detail)[-1]["author_id"] == painter["id"]

    asyncio.run(body())


def test_a_members_at_to_an_outsider_becomes_a_supervisor_invite(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    painter = store.create_agent(name="画师", instructions="出规格和视觉稿", kind="member")
    group = team["group"]
    upstream.dispatch_script = [
        decision(kind="answer", speakers=[team["designer"]["id"]], reason="甲先答")
    ]
    upstream.script = [
        {"content": "这个交给 @画师 出规格。"},
        tool_calls(
            call(
                "s1",
                "set_group_members",
                {
                    "group_id": group["id"],
                    "member_ids": [*group["member_ids"], painter["id"]],
                },
            )
        ),
        {"content": "把画师拉进来。"},
        {"content": "规格在这里。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@甲 看一下")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])

        assert painter["id"] in detail["conversation"]["member_ids"]
        assert agent_messages(detail)[-1]["author_id"] == painter["id"]
        assert "「甲」在群里 @ 了 「画师」" in last_user_text(upstream.requests[1])

    asyncio.run(body())


def test_an_outsider_at_without_a_supervisor_leaves_a_notice_and_the_button_works(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend, supervisor=False)
    store = store_of(backend)
    painter = store.create_agent(name="画师", instructions="出规格和视觉稿", kind="member")
    group = team["group"]
    upstream.dispatch_script = [
        decision(kind="answer", speakers=[team["designer"]["id"]], reason="甲先答")
    ]
    upstream.script = [{"content": "这个交给 @画师 出规格。"}, {"content": "规格在这里。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@甲 看一下")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])
            [notice] = [m for m in detail["messages"] if m["kind"] == "notice"]
            assert notice["content"] == "画师 不在这个群里"
            assert notice["invite"]["agent_id"] == painter["id"]
            assert notice["invite"]["message_id"] == agent_messages(detail)[0]["id"]

            response = await client.post(
                f"/conversations/{group['id']}/invite",
                json={
                    "agent_id": painter["id"],
                    "message_id": notice["invite"]["message_id"],
                    "notice_id": notice["id"],
                },
            )
            assert response.status_code == 202, response.text
            invited = await wait_for_run(client, response.json()["id"])
            assert invited["status"] == "completed", invited
            detail = await get_conversation(client, group["id"])

        assert painter["id"] in detail["conversation"]["member_ids"]
        assert agent_messages(detail)[-1]["author_id"] == painter["id"]
        kept = [m for m in detail["messages"] if m["kind"] == "notice" and m["content"] == "画师 不在这个群里"]
        assert len(kept) == 1 and kept[0].get("invite") is None

    asyncio.run(body())
