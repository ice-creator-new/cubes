"""Group tasks: one per member answering a request; threads, stop, 「继续」, replies, parts."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fake_upstream import call, tool_calls
from helpers import (
    agent_messages,
    client_for,
    collect_events,
    get_conversation,
    get_workspace,
    last_user_text,
    payloads,
    run_goal,
    start_run,
    stop_run,
    store_of,
    system_prompt,
    tool_results,
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
    client, group_id: str, *, status: str = "running", timeout: float = 15.0
) -> dict:
    """Wait for the group's first task in one state; the card appears before the reply."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for task in await tasks_of(client, group_id):
            if task["status"] == status:
                return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {status} task appeared in {group_id}")


def cards(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "task"]


def in_thread(request: dict) -> bool:
    messages = request.get("messages") or []
    return "群组里做一件活" in str((messages[0] if messages else {}).get("content") or "")


def responder(group_reply, thread_reply):
    def choose(requests, index):
        request = requests[index - 1]
        if in_thread(request):
            return thread_reply(request, requests, index) if callable(thread_reply) else thread_reply
        return group_reply(request, requests, index) if callable(group_reply) else group_reply

    return choose


def full_permissions(client) -> Any:
    return client.patch("/settings/permissions", json={"mode": "full"})


def test_a_stopped_task_goes_on_when_the_user_says_continue(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 30

    def thread_reply(request, requests, index):
        if "第 2 轮" in last_user_text(request):
            return {"content": "方案写完了"}
        return {"content": None, "slow": True}

    upstream.script = [
        responder(tool_calls(call("s1", "start_task", {"title": "写一份方案"})), thread_reply)
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            run = await run_goal(client, gid, "@甲 写一份方案")
            assert run["status"] == "completed", run
            # The work is its own thread run now; the group turn only opened it.
            task = await wait_for_task(client, gid)
            thread = await get_conversation(client, task["thread_id"])
            assert thread["conversation"]["kind"] == "thread"
            await wait_for_message(client, task["thread_id"], status="streaming")
            stopped = await client.post(f"/conversations/{gid}/tasks/{task['id']}/stop")
            assert stopped.status_code == 200, stopped.text
            task = await wait_for_task(client, gid, status="stopped")
            assert (task["owner_id"], task["title"]) == (
                team["designer"]["id"], "写一份方案"
            )
            assert task["stopped_by"] == "你"
            detail = await get_conversation(client, gid)
            [card] = cards(detail)
            assert (card["task_id"], card["status"]) == (task["id"], "interrupted")
            assert detail["conversation"]["unread"] is True

            done = await client.post(f"/conversations/{gid}/tasks/{task['id']}/resume")
            assert done.status_code == 200, done.text
            task = await wait_for_task(client, gid, status="done")
            assert (task["id"], task["rounds"]) == (stopped.json()["id"], 2)
            detail = await get_conversation(client, gid)
            assert [card["status"] for card in cards(detail)] == ["complete"]
            resumed = await get_conversation(client, task["thread_id"])
            assert agent_messages(resumed)[-1]["content"] == "方案写完了"
            assert task["result_message_id"] == agent_messages(detail)[-1]["id"]
            assert "你在继续你之前在这个群里接下的任务（第 2 轮）" in last_user_text(
                upstream.requests[-1]
            )

    asyncio.run(body())


def test_continue_with_nothing_stopped_is_a_new_message(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [{"content": "好的"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "继续")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])

        # Nobody has spoken in this group yet, so the supervisor answers it as new work.
        assert agent_messages(detail)[-1]["author_id"] == team["supervisor"]["id"]

    asyncio.run(body())


def test_a_member_hands_a_part_on_and_gets_the_answer_back(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    called = {"甲": 0}

    def thread_reply(request, requests, index):
        if "甲指派给你的任务" in last_user_text(request):
            return {"content": "风险在工期"}
        called["甲"] += 1
        if called["甲"] == 1:
            return tool_calls(
                call("a1", "group_assign", {"agent_id": "乙", "instruction": "评估方案 A 的风险"})
            )
        return {"content": "方案 A，风险在工期"}

    upstream.script = [
        responder(
            tool_calls(call("s1", "start_task", {"title": "出个方案并评估风险"})),
            thread_reply,
        )
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            run = await run_goal(client, gid, "@甲 出个方案并评估风险")
            assert run["status"] == "completed", run
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                tasks = await tasks_of(client, gid)
                if len(tasks) == 2 and all(t["status"] == "done" for t in tasks):
                    break
                await asyncio.sleep(0.05)
            tasks = await tasks_of(client, gid)

        root = next(task for task in tasks if task["parent_id"] is None)
        child = next(task for task in tasks if task["parent_id"] is not None)
        assert (root["owner_id"], root["status"]) == (team["designer"]["id"], "done")
        assert (child["owner_id"], child["assigned_by"], child["status"]) == (
            team["reviewer"]["id"], team["designer"]["id"], "done"
        )
        assert child["request_message_id"] == root["request_message_id"]
        assert child["thread_id"] and child["thread_id"] != root["thread_id"]
        # 乙's own thread is told 甲 gave it the part, and 甲 reads 乙's answer as the result.
        child_request = next(
            r for r in upstream.requests if "甲指派给你的任务" in last_user_text(r)
        )
        assert "评估方案 A 的风险" in last_user_text(child_request)
        results = {
            key: value
            for request in upstream.requests
            if system_prompt(request).startswith("你是「甲」")
            for key, value in tool_results(request)
        }
        assert results["a1"]["content"] == "风险在工期"

    asyncio.run(body())


def test_a_part_for_a_member_already_answering_is_refused(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)
    called = {"甲": 0}

    def group_reply(request, requests, index):
        if system_prompt(request).startswith("你是「甲」"):
            return tool_calls(call("s_甲", "start_task", {"title": "甲的任务"}))
        return tool_calls(call("s_乙", "start_task", {"title": "乙的任务"}))

    def thread_reply(request, requests, index):
        if system_prompt(request).startswith("你是「甲」"):
            called["甲"] += 1
            if called["甲"] == 1:
                return tool_calls(
                    call("a1", "group_assign", {"agent_id": "乙", "instruction": "也说说"})
                )
            return {"content": "我的看法"}
        return {"content": "乙的看法"}

    upstream.script = [responder(group_reply, thread_reply)]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 @乙 各开一个任务")
            assert run["status"] == "completed", run
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                tasks = await tasks_of(client, team["group"]["id"])
                if len(tasks) == 2:
                    break
                await asyncio.sleep(0.05)
            assert len(tasks) == 2

        results = {
            key: value
            for request in upstream.requests
            if system_prompt(request).startswith("你是「甲」")
            for key, value in tool_results(request)
        }
        assert results["a1"]["ok"] is False
        assert "把同样的话说两遍" in results["a1"]["error"]

    asyncio.run(body())


def test_a_transfer_ends_the_turn_and_the_new_owner_finishes(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)

    def thread_reply(request, requests, index):
        if "「甲」把一个任务整个交给了你" in last_user_text(request):
            return {"content": "评估完了：风险在工期"}
        return tool_calls(
            call("t1", "group_transfer", {"agent_id": "乙", "note": "这是风险评估"})
        )

    upstream.script = [
        responder(tool_calls(call("s1", "start_task", {"title": "评估一下风险"})), thread_reply)
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            run = await run_goal(client, gid, "@甲 评估一下风险")
            assert run["status"] == "completed", run
            task = await wait_for_task(client, gid, status="done")
            detail = await get_conversation(client, gid)

        assert (task["owner_id"], task["assigned_by"], task["status"]) == (
            team["reviewer"]["id"], team["designer"]["id"], "done"
        )
        assert agent_messages(detail)[-1]["author_id"] == team["reviewer"]["id"]
        assert task["result_message_id"] == agent_messages(detail)[-1]["id"]
        told = next(
            last_user_text(r)
            for r in upstream.requests
            if "「甲」把一个任务整个交给了你" in last_user_text(r)
        )
        assert "这是风险评估" in told

    asyncio.run(body())


def test_the_group_hears_as_its_tasks_finish(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_group(backend)

    def group_reply(request, requests, index):
        if system_prompt(request).startswith("你是「甲」"):
            return tool_calls(call("s_甲", "start_task", {"title": "甲的工作"}))
        return tool_calls(call("s_乙", "start_task", {"title": "乙的工作"}))

    upstream.script = [responder(group_reply, {"content": "做完了"})]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]

            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda seen: sum(
                        payload.get("done", 0)
                        for name, payload in seen
                        if name == "group_work_done"
                    )
                    >= 2,
                    timeout=15,
                )
            )
            await asyncio.sleep(0.3)
            await run_goal(client, gid, "@甲 @乙 各做一件")
            events = await reader

        done = payloads(events, "group_work_done")
        # A task's thread is its own run, so each finished task reports once; together the
        # group hears its two tasks complete, all naming this group.
        assert all(payload["conversation_id"] == gid for payload in done)
        assert sum(payload["done"] for payload in done) == 2
        assert all(payload["status"] == "completed" for payload in done)

    asyncio.run(body())


# ------------------------------------------------------------------ task threads


def test_a_members_tool_turn_runs_in_its_thread_and_the_group_sees_a_card(
    make_backend, upstream
) -> None:
    """Prose, tool rows and steps live in the thread; the group gets one card and one result."""
    backend = make_backend()
    team = build_group(backend)
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 4
    thread_calls = {"n": 0}

    def thread_reply(request, requests, index):
        thread_calls["n"] += 1
        if thread_calls["n"] == 1:
            return tool_calls(call("c1", "file_glob", {"pattern": "*.md"}))
        if thread_calls["n"] == 2:
            return tool_calls(call("c2", "file_glob", {"pattern": "*.py"}))
        return {"content": "看完了，项目里只有方案文档。"}

    upstream.script = [
        responder(
            tool_calls(call("s1", "start_task", {"title": "看看项目文件"})),
            thread_reply,
        )
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            assert (await full_permissions(client)).status_code == 200
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 看看项目里有哪些文件")
            task = await wait_for_task(client, gid, status="done")
            assert task["thread_id"] is not None
            assert task["steps"] == 2
            assert task["current_step"].startswith("正在")
            [card] = cards(await get_conversation(client, gid))
            assert (card["task_id"], card["status"]) == (task["id"], "complete")

            # The group holds the request, the start_task tool row, the card and the result.
            group = await get_conversation(client, gid)
            kinds = [m["kind"] for m in group["messages"]]
            assert [kind for kind in kinds if kind != "action"] == ["user", "task", "agent"]
            assert group["messages"][-1]["reply_to_id"] == group["messages"][0]["id"]
            assert group["messages"][-1]["content"] == "看完了，项目里只有方案文档。"

            thread = await get_conversation(client, task["thread_id"])
            assert thread["conversation"]["kind"] == "thread"
            assert thread["conversation"]["member_ids"] == [team["designer"]["id"]]
            assert [m["kind"] for m in thread["messages"]] == ["action", "action", "agent"]
            workspace = await get_workspace(client)
            assert task["thread_id"] not in [c["id"] for c in workspace["conversations"]]
            assert group["conversation"]["unread"] is True

            # The thread's own turn states the group and the task, not the group chat.
            thread_requests = [r for r in upstream.requests if in_thread(r)]
            assert thread_requests
            assert "群组里做一件活" in system_prompt(thread_requests[0])
            assert "看看项目文件" in last_user_text(thread_requests[0])

    asyncio.run(body())


def test_an_un_at_followup_is_new_chat_for_the_last_speaker(make_backend, upstream) -> None:
    """An un-@ message after 甲 spoke goes to 甲, but a plain reply opens no task at all."""
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [{"content": "方案 A"}]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 出个方案")
            assert await tasks_of(client, gid) == []
            upstream.script = [{"content": "改成方案 B"}]
            run = await run_goal(client, gid, "你似乎没按照我们要的风格来")

            assert run["status"] == "completed", run
            assert await tasks_of(client, gid) == []
            group = await get_conversation(client, gid)
            replies = agent_messages(group)
            assert [reply["author_id"] for reply in replies] == [team["designer"]["id"]] * 2
            assert replies[-1]["content"] == "改成方案 B"
            assert all("接着做" not in str(m["content"]) for m in group["messages"])

    asyncio.run(body())


def test_an_at_name_overrides_the_last_speaker(make_backend, upstream) -> None:
    """Naming someone always wins: 乙 answers even though 甲 spoke last."""
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [{"content": "方案 A"}, {"content": "风险还好"}]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 出个方案")
            run = await run_goal(client, gid, "@乙 那风险评估呢")

            assert run["status"] == "completed", run
            group = await get_conversation(client, gid)
            assert agent_messages(group)[-1]["author_id"] == team["reviewer"]["id"]
            assert all("已经在群里发过言了" not in str(m["content"]) for m in group["messages"])

    asyncio.run(body())


def test_a_quick_question_stays_a_group_reply(make_backend, upstream) -> None:
    """A question the member answers in one line is chat: no task row and no card."""
    backend = make_backend()
    team = build_group(backend)
    upstream.script = [{"content": "现在三点。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 现在几点了？")
            assert await tasks_of(client, gid) == []
            group = await get_conversation(client, gid)
            assert [m["kind"] for m in group["messages"]] == ["user", "agent"]
            assert group["messages"][-1]["content"] == "现在三点。"

    asyncio.run(body())


def test_posting_into_a_thread_resumes_that_task(make_backend, upstream) -> None:
    """The thread is a direct line to the member about that task: a post continues it."""
    backend = make_backend()
    team = build_group(backend)
    thread_calls = {"n": 0}

    def thread_reply(request, requests, index):
        thread_calls["n"] += 1
        return {"content": "第一版方案" if thread_calls["n"] == 1 else "改成第二版"}

    upstream.script = [
        responder(tool_calls(call("s1", "start_task", {"title": "出个方案"})), thread_reply)
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            gid = team["group"]["id"]
            await run_goal(client, gid, "@甲 出个方案")
            task = await wait_for_task(client, gid, status="done")
            thread_id = task["thread_id"]

            run = await run_goal(client, thread_id, "预算再少一半")
            assert run["status"] == "completed", run
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                current = [t for t in await tasks_of(client, gid) if t["id"] == task["id"]][0]
                if current["rounds"] == 2 and current["status"] == "done":
                    break
                await asyncio.sleep(0.05)
            assert (current["status"], current["rounds"]) == ("done", 2)
            group = await get_conversation(client, gid)
            assert [m["content"] for m in agent_messages(group)] == ["第一版方案", "改成第二版"]
            assert current["result_message_id"] == agent_messages(group)[-1]["id"]
            thread = await get_conversation(client, thread_id)
            assert [m["content"] for m in thread["messages"] if m["kind"] == "user"] == [
                "预算再少一半"
            ]

    asyncio.run(body())
