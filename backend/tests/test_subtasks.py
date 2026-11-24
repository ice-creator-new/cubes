"""run_subtasks: siblings really overlap, results come back together, stop cancels them all."""

from __future__ import annotations

import asyncio
import time

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    get_conversation,
    get_workspace,
    start_run,
    stop_run,
    tool_results,
    wait_for_run,
)

SUBTASKS = {
    "subtasks": [
        {"title": "查甲方案", "brief": "查清甲方案的利弊，给出三点结论。"},
        {"title": "查乙方案", "brief": "查清乙方案的利弊，给出三点结论。"},
    ]
}


async def wait_for_subtask_row(client, conversation_id: str, *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await get_conversation(client, conversation_id)
        for message in action_messages(detail):
            action = message["action"]
            if action["tool_name"] == "run_subtasks" and action["status"] == "running":
                return message
        await asyncio.sleep(0.05)
    raise AssertionError("no run_subtasks row appeared")


def test_two_subtasks_overlap_and_both_answers_return(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.25
    upstream.chunk_count = 5
    upstream.script = [
        tool_calls(call("call_1", "run_subtasks", SUBTASKS)),
        {"content": "甲方案的结论"},
        {"content": "乙方案的结论"},
        {"content": "两个方案都看完了。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            started = time.monotonic()
            run = await start_run(client, direct_id, "比较两个方案")
            finished = await wait_for_run(client, run["id"], timeout=25.0)
            elapsed = time.monotonic() - started
            assert finished["status"] == "completed", finished

            # Main turn, both subtasks, and the summary: one after another would be about
            # 5 s of stream time; two siblings at once is about 3.75 s.
            assert elapsed < 4.6, elapsed

            result = dict(tool_results(upstream.requests[3]))["call_1"]
            answers = {row["title"]: row["answer"] for row in result["subtasks"]}
            assert answers == {"查甲方案": "甲方案的结论", "查乙方案": "乙方案的结论"}
            assert all(row["status"] == "done" for row in result["subtasks"])

            detail = await get_conversation(client, direct_id)
            rows = action_messages(detail)
            assert [row["action"]["summary"] for row in rows] == ["并行完成了 2 个小任务"]
            steps = rows[0]["action"]["detail"]["steps"]
            assert [(step["title"], step["status"]) for step in steps] == [
                ("查甲方案", "done"),
                ("查乙方案", "done"),
            ]

    asyncio.run(body())


def test_stop_cancels_every_subtask(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.3
    upstream.chunk_count = 50  # long enough that only a stop ends the turns
    upstream.script = [tool_calls(call("call_1", "run_subtasks", SUBTASKS)), {"content": "不该出现"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "比较两个方案")
            await wait_for_subtask_row(client, direct_id)
            # Both siblings are streaming by now.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and upstream.request_count < 3:
                await asyncio.sleep(0.05)
            assert upstream.request_count >= 3

            stopped_at = time.monotonic()
            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped", stopped
            assert time.monotonic() - stopped_at < 6.0

            # Nothing keeps running in the background: the subtask tasks were cancelled.
            count = upstream.request_count
            await asyncio.sleep(0.8)
            assert upstream.request_count == count

            rows = action_messages(await get_conversation(client, direct_id))
            assert rows[0]["action"]["status"] == "error"
            assert "停止" in rows[0]["action"]["summary"]

    asyncio.run(body())
