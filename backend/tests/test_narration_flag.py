"""The exact narration signal: a text row says whether its own response called tools.

The chat folds sandbox work with the narration leading into it. While a reply streams there is
no later step to look at yet, so the only exact sign is the model response itself: text that
came with tool calls is narration, text that ended without them is the answer. The engine
writes that verdict on the row, and publishes it the moment the first tool-call fragment
arrives — before the action row exists, so the chat never has to guess and re-fold.
"""

from __future__ import annotations

import asyncio

from fake_upstream import call
from helpers import (
    client_for,
    collect_events,
    get_conversation,
    get_workspace,
    payloads,
    settled,
    start_run,
)


def test_text_leading_into_tools_is_marked_before_the_action_row(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        {
            "content": "超时发生在配置里，我打开日志看看：",
            "tool_calls": [
                call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})
            ],
        },
        {"content": "建好了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events_task = asyncio.create_task(
                collect_events(
                    client, f"/events?conversation_id={direct_id}", until=settled
                )
            )
            await start_run(client, direct_id, "看看为什么超时")
            events = await events_task

            names = [name for name, _ in events]
            update_ids = {
                payload["id"]
                for name, payload in events
                if name == "message_update" and payload.get("leads_to_tools") is True
            }

            ends = payloads(events, "message_end")
            narration = next(
                payload
                for payload in ends
                if payload["kind"] == "agent" and "打开日志看看" in payload["content"]
            )
            assert narration["leads_to_tools"] is True
            # The exact signal reached the row before the action row it leads into existed.
            assert narration["id"] in update_ids
            first_action = next(
                index
                for index, (name, payload) in enumerate(events)
                if name == "message_start" and payload["kind"] == "action"
            )
            first_update = next(
                index
                for index, (name, payload) in enumerate(events)
                if name == "message_update"
                and payload["id"] == narration["id"]
                and payload.get("leads_to_tools") is True
            )
            assert first_update < first_action

            answer = next(
                payload
                for payload in ends
                if payload["kind"] == "agent" and payload["content"] == "建好了。"
            )
            assert answer["leads_to_tools"] is False
            assert names.count("message_update") >= 1

    asyncio.run(body())


def test_a_plain_answer_is_false_and_old_rows_stay_null(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "没有工具，直接回答。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events_task = asyncio.create_task(
                collect_events(
                    client, f"/events?conversation_id={direct_id}", until=settled
                )
            )
            await start_run(client, direct_id, "直接说")
            events = await events_task

            answers = [
                payload
                for payload in payloads(events, "message_end")
                if payload["kind"] == "agent" and payload["content"] == "没有工具，直接回答。"
            ]
            assert answers and answers[-1]["leads_to_tools"] is False
            # Persisted state says the same, and rows that are not agent text stay undecided.
            detail = await get_conversation(client, direct_id)
            user = next(row for row in detail["messages"] if row["kind"] == "user")
            assert user["leads_to_tools"] is None

    asyncio.run(body())
