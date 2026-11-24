"""The composer's context bar: usage, its breakdown, and compaction."""

from __future__ import annotations

import asyncio
import time

from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    collect_events,
    get_workspace,
    run_goal,
    start_run,
    stop_run,
    store_of,
)
from app import prompts
from app.agent import budget as budget_rules


def seed_exchange(store, conversation_id: str, supervisor: dict, rounds: int) -> None:
    """Fill a chat with plain turns, the way a long conversation would look."""
    for index in range(rounds):
        store.append_message(
            conversation_id=conversation_id,
            author_id=None,
            author_name="你",
            avatar_seed=None,
            kind="user",
            content=f"第 {index} 个问题：请把这份材料的要点整理出来，尽量详细一些。",
            status="complete",
        )
        store.append_message(
            conversation_id=conversation_id,
            author_id=supervisor["id"],
            author_name=supervisor["name"],
            avatar_seed=supervisor["avatar_seed"],
            kind="agent",
            content=f"第 {index} 个回答：要点是甲、乙、丙三件事，分别对应材料的三个章节。",
            status="complete",
        )


def test_context_meter_reports_usage_and_compaction_lowers_it(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct_id = store.direct_conversation()["id"]
    seed_exchange(store, direct_id, supervisor, rounds=12)
    upstream.script = [{"content": "较早的对话已整理：用户要过三份材料的要点，都按甲乙丙列出。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            assert (await get_workspace(client))["direct_conversation_id"] == direct_id

            meter = await client.get(f"/conversations/{direct_id}/context")
            assert meter.status_code == 200, meter.text
            before = meter.json()
            assert before["agent_id"] == supervisor["id"]
            assert before["tokens"] > 0
            assert before["window"] > 0
            assert before["usable"] <= before["window"]
            assert 0 < before["threshold"] <= before["usable"]
            assert before["percent"] >= 0

            compacted = await client.post(f"/conversations/{direct_id}/compact")
            assert compacted.status_code == 200, compacted.text
            result = compacted.json()
            assert result["compacted"] is True
            assert result["after"] < result["before"]
            assert result["tokens"] == result["after"]
            # The summary the model will read from now on is stored.
            summary = store.latest_summary(direct_id)
            assert summary is not None and summary["summary"].startswith("较早的对话已整理")

            again = (await client.get(f"/conversations/{direct_id}/context")).json()
            assert again["tokens"] < before["tokens"]

    asyncio.run(body())
    # The compaction was one real summariser call, booked with its own purpose.
    assert upstream.request_count == 1
    assert "较早的部分" in str(upstream.request(0)["messages"][-1].get("content"))


def test_context_meter_needs_a_conversation_and_a_model(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct_id = store.direct_conversation()["id"]

    async def body() -> None:
        async with client_for(backend) as client:
            missing = await client.get("/conversations/c_missing/context")
            assert missing.status_code == 404

    asyncio.run(body())


def test_the_breakdown_splits_the_request_that_would_be_sent(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct_id = store.direct_conversation()["id"]
    seed_exchange(store, direct_id, supervisor, rounds=3)
    upstream.script = [
        tool_calls(call("call_1", "calculate", {"expression": "2 ** 10"})),
        {"content": "算完了：1024。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, direct_id, "算一下 2 的 10 次方。")
            assert run["status"] == "completed", run

            meter = (await client.get(f"/conversations/{direct_id}/context")).json()
            categories = {category["id"]: category for category in meter["categories"]}
            # The rows are shares of one total, never more and never less.
            assert sum(category["tokens"] for category in meter["categories"]) == meter["tokens"]
            assert meter["provider"] and meter["model"]
            assert categories["system"]["tokens"] > 0
            assert categories["tools"]["tokens"] > 0
            assert categories["user"]["tokens"] > 0
            assert categories["reply"]["tokens"] > 0
            assert categories["calls"]["tokens"] > 0
            # A replayed tool result is split out per tool, biggest first.
            items = categories["results"]["items"]
            assert items and items[0]["name"] == "calculate" and items[0]["tokens"] > 0
            assert categories["results"]["tokens"] >= items[0]["tokens"]

            # The auto-compaction point is one constant, and it sits inside the usable budget.
            assert meter["threshold"] == meter["usable"] * budget_rules.COMPACT_PERCENT // 100
            assert meter["threshold"] <= meter["usable"] <= meter["window"]
            assert meter["threshold_percent"] == round(
                meter["threshold"] * 100 / meter["window"], 1
            )
            assert meter["buffer"] == meter["window"] - meter["threshold"]
            assert meter["free"] == max(0, meter["threshold"] - meter["tokens"])

    asyncio.run(body())


def seed_long_exchange(store, conversation_id: str, supervisor: dict, rounds: int) -> None:
    """Long turns, so a test can reach a small declared window's threshold."""
    for index in range(rounds):
        store.append_message(
            conversation_id=conversation_id,
            author_id=None,
            author_name="你",
            avatar_seed=None,
            kind="user",
            content=f"第 {index} 份材料：" + "甲" * 1_200,
            status="complete",
        )
        store.append_message(
            conversation_id=conversation_id,
            author_id=supervisor["id"],
            author_name=supervisor["name"],
            avatar_seed=supervisor["avatar_seed"],
            kind="agent",
            content=f"第 {index} 份要点：" + "乙" * 1_200,
            status="complete",
        )


def test_a_run_ending_over_the_threshold_compacts_on_its_own(make_backend, upstream) -> None:
    backend = make_backend(context_window_default=80_000, context_output_default=1_024)
    store = store_of(backend)
    supervisor = store.supervisor()
    direct_id = store.direct_conversation()["id"]
    seed_long_exchange(store, direct_id, supervisor, rounds=12)

    async def body() -> None:
        async with client_for(backend) as client:
            before = (await client.get(f"/conversations/{direct_id}/context")).json()
            # The run's own first request must still fit: the reply is what pushes it over.
            assert before["tokens"] + 500 < before["threshold"], before
            assert store.latest_summary(direct_id) is None
            remaining = before["threshold"] - before["tokens"]
            upstream.script = [
                # One CJK character is one token: just past the threshold, and still small
                # enough that the turns a compaction keeps fit under it again.
                {"content": "长" * (remaining + 2_000)},
                {"content": "较早的对话已整理。"},
            ]
            run = await run_goal(client, direct_id, "继续，展开说说。", timeout=30)
            assert run["status"] == "completed", run

            deadline = time.monotonic() + 15
            while store.latest_summary(direct_id) is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            summary = store.latest_summary(direct_id)
            assert summary is not None and summary["summary"] == "较早的对话已整理。"
            # The summariser is a real call to the bot's own model.
            assert any(
                prompts.SUMMARY_SYSTEM in str(request["messages"][0].get("content"))
                for request in upstream.requests
            )

            after = (await client.get(f"/conversations/{direct_id}/context")).json()
            assert after["tokens"] <= after["threshold"], after
            assert after["categories"]

    asyncio.run(body())


def test_a_forced_compaction_publishes_working_and_done(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct_id = store.direct_conversation()["id"]
    seed_exchange(store, direct_id, supervisor, rounds=8)
    upstream.script = [{"content": "较早的对话已整理。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(
                        name == "context" and payload["state"] == "idle"
                        for name, payload in events
                    ),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.2)
            response = await client.post(f"/conversations/{direct_id}/compact")
            assert response.status_code == 200, response.text
            events = await reader
            states = [
                payload["state"]
                for name, payload in events
                if name == "context"
            ]
            assert states[0] == "compacting"
            done = next(
                payload
                for name, payload in events
                if name == "context" and payload["state"] == "idle"
            )
            assert done["auto"] is False
            assert done["compacted"] is True and done["after"] < done["before"]

    asyncio.run(body())


def test_compacting_while_a_run_is_live_is_refused(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct_id = store.direct_conversation()["id"]
    upstream.chunk_delay = 0.5
    upstream.chunk_count = 8

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, direct_id)
            refused = await client.post(f"/conversations/{direct_id}/compact")
            assert refused.status_code == 409
            assert "正在回复" in refused.json()["detail"]
            await stop_run(client, run["id"])

    asyncio.run(body())

