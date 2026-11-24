"""Hidden task threads: the member opens one, the scheduler runs it in parallel."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fake_upstream import call, tool_calls
from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    last_user_text,
    payloads,
    collect_events,
    run_goal,
    start_run,
    store_of,
    system_prompt,
    wait_for_message,
    wait_for_run,
)


def build_group(backend) -> dict:
    store = store_of(backend)
    supervisor = store.supervisor()
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"], reviewer["id"]],
    )
    return {"supervisor": supervisor, "designer": designer, "reviewer": reviewer, "group": group}


async def tasks_of(client, group_id: str) -> list[dict]:
    response = await client.get(f"/conversations/{group_id}/tasks")
    assert response.status_code == 200, response.text
    return response.json()["tasks"]


async def wait_for_task(
    client,
    group_id: str,
    *,
    status: str = "running",
    exclude: str | None = None,
    timeout: float = 15.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for task in await tasks_of(client, group_id):
            if task["status"] == status and task["id"] != exclude:
                return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {status} task appeared in {group_id}")


def start_task(title: str, plan: str | None = None) -> Any:
    payload = {"title": title}
    if plan:
        payload["plan"] = plan
    return tool_calls(call("s1", "start_task", payload))


def slow_reply(seconds: float = 6.0, text: str = "正在仔细检查并修改这一部分。") -> dict:
    """A visible reply paced over ``seconds``: the run stays genuinely ``running`` that long.

    ``silent`` would leave no text at all, which the engine treats as an empty reply, and a
    fixed ``chunk_count`` splits a short string into only a few pieces; the length sets it.
    """
    body = text * 8
    return {"content": body, "chunk_count": len(body), "chunk_delay": seconds / len(body)}


def cards(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "task"]


def in_thread(request: dict) -> bool:
    """Whether one fake request is a thread turn rather than a group turn."""
    messages = request.get("messages") or []
    system = str((messages[0] if messages else {}).get("content") or "")
    return "群组里做一件活" in system


def responder(group_reply, thread_reply):
    """A fake script that answers by where the request came from, not by global order.

    Threads run in parallel, so a positional script would give one task's answer to another.
    """

    def choose(requests, index):
        request = requests[index - 1]
        if in_thread(request):
            return thread_reply(request, requests, index) if callable(thread_reply) else thread_reply
        return group_reply(request, requests, index) if callable(group_reply) else group_reply

    return choose


# ------------------------------------------------------------------ parallel starts


def test_start_task_returns_the_group_immediately_and_the_thread_runs_alongside(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    # 1) the group turn opens the task; 2) the thread works slowly; 3) a second group
    # message is answered while that thread is still going.
    upstream.script = [
        start_task("写方案", "先查资料再写"),
        # The thread waits on the model longer than the second group reply takes.
        slow_reply(6),
        {"content": "我在，先说一句。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            first = await start_run(client, gid, "@甲 做一份完整方案")
            finished = await wait_for_run(client, first["id"], timeout=5)
            assert finished["status"] == "completed", finished
            # The card is already there while the thread works.
            task = await wait_for_task(client, gid)
            assert task["thread_id"]
            assert task["status"] == "running"
            detail = await get_conversation(client, gid)
            [card] = cards(detail)
            assert card["task_id"] == task["id"]
            workspace = await get_workspace(client)
            entry = next(t for t in workspace["group_threads"] if t["task_id"] == task["id"])
            assert (entry["group_id"], entry["thread_id"], entry["status"]) == (
                gid,
                task["thread_id"],
                "running",
            )

            second = await start_run(client, gid, "@乙 顺便说一句")
            answered = await wait_for_run(client, second["id"], timeout=10)
            assert answered["status"] == "completed", answered
            # The thread is still running: the two really overlapped.
            current = next(t for t in await tasks_of(client, gid) if t["id"] == task["id"])
            assert current["status"] == "running"

            await client.post(f"/conversations/{gid}/tasks/{task['id']}/stop")

    asyncio.run(body())


def test_a_member_that_answers_without_start_task_stays_in_the_group(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [{"content": "现在三点。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 现在几点了？")
            assert run["status"] == "completed"
            group = await get_conversation(client, team["group"]["id"])
            assert [m["kind"] for m in group["messages"]] == ["user", "agent"]
            assert await tasks_of(client, team["group"]["id"]) == []
            assert "群组里做一件活" not in system_prompt(upstream.request(0))

    asyncio.run(body())


# ------------------------------------------------------------------ queue and cap


def test_a_second_task_for_a_busy_member_waits_then_starts(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    def thread_reply(request, requests, index):
        if "第二件" in last_user_text(request):
            return slow_reply(2, "第二件做完了")
        return slow_reply(6)

    def group_reply(request, requests, index):
        if "第二件" in last_user_text(request):
            return start_task("第二件")
        return start_task("第一件")

    upstream.script = [responder(group_reply, thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 先做第一件")
            first = await wait_for_task(client, gid)
            await wait_for_task(client, gid, status="running")
            await run_goal(client, gid, "@甲 再做第二件")
            queued = await wait_for_task(client, gid, status="queued")
            assert queued["owner_id"] == team["designer"]["id"]

            await wait_for_run(client, first["run_id"], timeout=15)
            second = await wait_for_task(client, gid, status="running")
            assert second["id"] == queued["id"]
            await wait_for_run(client, second["run_id"], timeout=15)
            done = [t for t in await tasks_of(client, gid) if t["status"] == "done"]
            assert len(done) == 2

    asyncio.run(body())


def test_the_global_cap_queues_the_extra_threads(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    team = build_group(backend)
    # A third member so three tasks can be opened before the cap is tested.
    writer = store.create_agent(name="丙", instructions="写文档", kind="member")
    store.update_conversation(
        team["group"]["id"], member_ids=[team["supervisor"]["id"], team["designer"]["id"], team["reviewer"]["id"], writer["id"]]
    )
    store.set_team_settings(max_parallel_tasks=1)

    def thread_reply(request, requests, index):
        if "乙的任务" in last_user_text(request):
            return slow_reply(2, "乙做完")
        return slow_reply(6)

    def group_reply(request, requests, index):
        if "乙" in last_user_text(request):
            return start_task("乙的任务")
        return start_task("甲的任务")

    upstream.script = [responder(group_reply, thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 做甲的")
            first = await wait_for_task(client, gid, status="running")
            await run_goal(client, gid, "@乙 做乙的")
            second = await wait_for_task(client, gid, status="queued")
            assert first["id"] != second["id"]
            await wait_for_run(client, first["run_id"], timeout=15)
            promoted = await wait_for_task(client, gid, status="running")
            assert promoted["id"] == second["id"]
            await wait_for_run(client, promoted["run_id"], timeout=15)
            assert all(
                t["status"] == "done" for t in await tasks_of(client, gid)
            )

    asyncio.run(body())


def test_merge_folds_a_queued_task_into_the_running_thread(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    thread_calls = {"n": 0}

    def thread_reply(request, requests, index):
        thread_calls["n"] += 1
        if thread_calls["n"] == 1:
            return {"content": None, "slow": True, "chunk_count": 60, "chunk_delay": 0.1}
        return {"content": "两件都算进去了"}

    def group_reply(request, requests, index):
        if "小件" in last_user_text(request):
            return start_task("排队的")
        return start_task("正在做的")

    upstream.script = [responder(group_reply, thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 做大件")
            running = await wait_for_task(client, gid, status="running")
            # Let the thread stream, then open a second task for the same member.
            await wait_for_message(client, running["thread_id"])
            await run_goal(client, gid, "@甲 再做小件")
            queued = await wait_for_task(client, gid, status="queued")

            merged = await client.post(f"/conversations/{gid}/tasks/{queued['id']}/merge")
            assert merged.status_code == 200, merged.text
            assert merged.json()["status"] == "done"
            assert merged.json()["current_step"].startswith("已并入")
            # The running thread received the folded request as a steer.
            thread = await get_conversation(client, running["thread_id"])
            assert "排队的" in " ".join(
                m["content"] for m in thread["messages"] if m["kind"] == "user"
            )
            await wait_for_run(client, running["run_id"], timeout=15)

    asyncio.run(body())


def test_stop_one_thread_leaves_the_others_running(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    def thread_reply(request, requests, index):
        return {"content": None, "slow": True, "chunk_count": 60, "chunk_delay": 0.1}

    def group_reply(request, requests, index):
        if "乙" in last_user_text(request):
            return start_task("乙的任务")
        return start_task("甲的任务")

    upstream.script = [responder(group_reply, thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 慢慢做")
            first = await wait_for_task(client, gid, status="running")
            await run_goal(client, gid, "@乙 也慢慢做")
            second = await wait_for_task(client, gid, status="running", exclude=first["id"])

            stopped = await client.post(f"/conversations/{gid}/tasks/{first['id']}/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "stopped"
            other = await client.get(f"/conversations/{gid}/tasks")
            remaining = next(t for t in other.json()["tasks"] if t["id"] == second["id"])
            assert remaining["status"] == "running"
            await client.post(f"/conversations/{gid}/tasks/{second['id']}/stop")

    asyncio.run(body())


# ------------------------------------------------------------------ resume and consent


def test_resume_of_a_stopped_thread_reuses_its_thread(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    def thread_reply(request, requests, index):
        if "第 2 轮" in last_user_text(request):
            return {"content": "接着写完了"}
        return {"content": None, "slow": True, "chunk_count": 60, "chunk_delay": 0.1}

    upstream.script = [responder(start_task("写方案"), thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 写方案")
            task = await wait_for_task(client, gid, status="running")
            await client.post(f"/conversations/{gid}/tasks/{task['id']}/stop")
            stopped = await wait_for_task(client, gid, status="stopped")
            thread_id = stopped["thread_id"]

            # The card's own button resumes exactly this task in its thread.
            asserted = await client.post(f"/conversations/{gid}/tasks/{task['id']}/resume")
            assert asserted.status_code == 200, asserted.text
            resumed = await wait_for_task(client, gid, status="done")
            assert resumed["id"] == task["id"]
            assert resumed["thread_id"] == thread_id
            assert resumed["rounds"] == 2
            group = await get_conversation(client, gid)
            assert agent_messages(group)[-1]["content"] == "接着写完了"
            assert "第 2 轮" in last_user_text(upstream.request(2))

    asyncio.run(body())


def test_progress_streams_the_latest_line_and_a_waiting_card_is_visible(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_group(backend)
    store = store_of(backend)
    supervisor = team["supervisor"]
    group = team["group"]
    # Make a sandbox step need the user's yes.
    store.set_permission_settings(mode="ask")
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 6
    thread_calls = {"n": 0}

    def thread_reply(request, requests, index):
        thread_calls["n"] += 1
        if thread_calls["n"] == 1:
            return {
                "content": "我先看一眼文件。",
                "tool_calls": [call("w1", "file_write", {"path": "plan.md", "content": "x"})],
            }
        return {"content": "写好了"}

    upstream.script = [responder(start_task("改一下文件"), thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = group["id"]
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda seen: any(
                        name == "task_progress" and payload.get("latest_text")
                        for name, payload in seen
                    ),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.3)
            await run_goal(client, gid, "@甲 写个文件")
            task = await wait_for_task(client, gid, status="running")
            progress = payloads(await reader, "task_progress")
            assert any(p["task_id"] == task["id"] for p in progress)
            assert any(p.get("latest_text") for p in progress if p["task_id"] == task["id"])

            # A consent card inside the thread shows up on the task and in the decisions list.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                current = next(
                    t for t in await tasks_of(client, gid) if t["id"] == task["id"]
                )
                if current.get("awaiting_action_id"):
                    break
                await asyncio.sleep(0.05)
            # A thread parked on a card is really ``awaiting``, not still running.
            assert current["status"] == "awaiting"
            action_id = current["awaiting_action_id"]
            assert action_id
            decisions = (await get_workspace(client))["decisions"]
            assert any(row["id"] == action_id for row in decisions)
            approved = await client.post(f"/actions/{action_id}/confirm", json={"approve": True})
            assert approved.status_code == 200, approved.text
            await wait_for_run(client, current["run_id"], timeout=15)
            # The answer clears ``awaiting``: the task is working again (or already done).
            cleared = next(
                t for t in await tasks_of(client, gid) if t["id"] == task["id"]
            )
            assert cleared["awaiting_action_id"] is None
            assert cleared["status"] in ("running", "done")

    asyncio.run(body())


def test_a_failed_thread_task_keeps_why_it_failed(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)

    def thread_reply(requests, index):
        return {"error_text": "upstream exploded"}

    upstream.script = [responder(start_task("会失败的任务"), thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 做个会失败的任务")
            task = await wait_for_task(client, gid, status="failed")
            assert task["end_reason"]
            assert "模型服务失败" in task["end_reason"]

    asyncio.run(body())


def test_the_team_setting_controls_the_parallel_cap(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            state = (await client.get("/settings/team")).json()
            assert state["max_parallel_tasks"] == 3
            patched = await client.patch("/settings/team", json={"max_parallel_tasks": 5})
            assert patched.status_code == 200, patched.text
            assert patched.json()["max_parallel_tasks"] == 5
            too_many = await client.patch("/settings/team", json={"max_parallel_tasks": 9})
            assert too_many.status_code == 422
            too_few = await client.patch("/settings/team", json={"max_parallel_tasks": 0})
            assert too_few.status_code == 422

    asyncio.run(body())
