"""The engine and the database disagreeing about a chat's run must never lock that chat.

Seen in the field: another process opened the live database and its restart recovery marked a
running run as stopped. The task kept replying (Stop found nothing to stop, the row already
said stopped) and every new send was refused as busy, forever. The opposite drift, a row left
running with no task, queued every send behind a run that would never end.
"""

from __future__ import annotations

import asyncio
import sqlite3

from fake_upstream import call, tool_calls
from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    start_run,
    stop_run,
    store_of,
    wait_for_message,
    wait_for_run,
)


def _end_row_elsewhere(backend, run_id: str) -> None:
    """What another process's restart recovery did: the row says stopped, the task runs on."""
    connection = sqlite3.connect(str(backend.settings.database_path))
    try:
        connection.execute(
            "UPDATE runs SET status = 'stopped', finished_at = '2026-11-23T05:04:09Z' WHERE id = ?",
            (run_id,),
        )
        connection.commit()
    finally:
        connection.close()


def test_a_task_whose_row_was_ended_elsewhere_stops_and_the_chat_takes_new_sends(
    make_backend, upstream
) -> None:
    upstream.mode = "slow"
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 80
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct, "写一篇长文")
            await wait_for_message(client, direct, status="streaming")
            _end_row_elsewhere(backend, first["id"])

            # Stop still reaches the task even though its row already says stopped.
            await stop_run(client, first["id"])
            await asyncio.sleep(0.3)
            assert backend.app.state.engine.live_run(first["id"]) is None

            upstream.mode = "ok"
            upstream.chunk_delay = 0.0
            upstream.script = [{"content": "收到"}]
            response = await client.post(
                f"/conversations/{direct}/runs", json={"content": "换个话题", "mode": "interrupt"}
            )
            assert response.status_code == 202, response.text
            second = await wait_for_run(client, response.json()["id"])
            assert second["status"] == "completed", second

    asyncio.run(body())


def test_a_task_notices_its_row_was_ended_and_stops_by_itself(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.0
    backend = make_backend()
    calls: list[int] = []

    def slow_tool_turn(requests, index):
        calls.append(index)
        # Every answer asks for one more tool call, so the task would go on for a while.
        return tool_calls(call(f"c{index}", "current_time", {}))

    upstream.script = [slow_tool_turn]

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct, "一直查时间")
            while len(calls) < 2:
                await asyncio.sleep(0.02)
            _end_row_elsewhere(backend, run["id"])
            ended_at = len(calls)
            for _ in range(100):
                if backend.app.state.engine.live_run(run["id"]) is None:
                    break
                await asyncio.sleep(0.05)
            assert backend.app.state.engine.live_run(run["id"]) is None
            assert (await get_run(client, run["id"]))["status"] == "stopped"
            # It stopped at its next model call, not after spending the rest of its budget.
            assert len(calls) <= ended_at + 1, calls

    asyncio.run(body())


def test_a_row_left_running_with_no_task_does_not_hold_the_chat(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    upstream.script = [{"content": "好的"}]

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]
            ghost, _ = store.begin_run(direct, "没有人在跑的任务", [])
            response = await client.post(
                f"/conversations/{direct}/runs", json={"content": "在吗", "mode": "queue"}
            )
            assert response.status_code == 202, response.text
            run = await wait_for_run(client, response.json()["id"])
            assert run["status"] == "completed", run
            assert (await get_run(client, ghost["id"]))["status"] == "stopped"
            replies = agent_messages(await get_conversation(client, direct))
            assert replies[-1]["content"] == "好的"

    asyncio.run(body())
