"""Self-reminders: the tool stores one, the scheduler fires it in the chat, cancel and cap."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from app import schedules
from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    get_conversation,
    get_workspace,
    start_run,
    store_of,
    tool_names,
    wait_for_run,
)


def _past() -> str:
    return schedules.utc_iso(datetime.now(timezone.utc) - timedelta(seconds=2))


def _future() -> str:
    return schedules.utc_iso(datetime.now(timezone.utc) + timedelta(hours=2))


async def get_schedules(client) -> dict:
    response = await client.get("/schedules")
    assert response.status_code == 200, response.text
    return response.json()


def test_remind_me_fires_in_the_same_chat_and_can_be_cancelled(make_backend, upstream) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]

            upstream.script = [
                tool_calls(call("r1", "remind_me", {"in_minutes": 1, "note": "喝水"})),
                {"content": "好，一分钟后提醒你。"},
            ]
            run = await start_run(client, direct_id, "一分钟之后提醒我喝水")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            state = await get_schedules(client)
            reminders = [item for item in state["schedules"] if item["reminder"]]
            assert len(reminders) == 1, state
            reminder = reminders[0]
            assert reminder["kind"] == "reminder"
            assert reminder["title"] == "喝水"
            assert reminder["conversation_id"] == direct_id
            assert reminder["enabled"] is True
            # The model was offered the tool.
            assert "remind_me" in tool_names(upstream.requests[0])

            # Move it due: the scheduler starts the bot again in this very chat.
            store = store_of(backend)
            store.update_schedule(reminder["id"], next_run_at=_past())
            deadline = time.monotonic() + 15
            trigger = None
            while time.monotonic() < deadline:
                detail = await get_conversation(client, direct_id)
                trigger = next((m for m in detail["messages"] if m.get("reminder")), None)
                if trigger is not None:
                    break
                await asyncio.sleep(0.05)
            assert trigger is not None, "no reminder trigger appeared"
            assert trigger["content"] == "提醒：喝水"
            assert trigger["kind"] == "user"
            stored = store.get_schedule(reminder["id"])
            assert stored is not None and stored["enabled"] is False
            assert stored["run_count"] == 1
            fired = await wait_for_run(client, stored["last_run_id"])
            assert fired["status"] == "completed", fired

            # A second reminder can be cancelled by the bot that owns it.
            second = store.create_schedule(
                title="站起来",
                prompt="站起来",
                conversation_id=direct_id,
                conversation_title="私聊",
                kind="reminder",
                at="2026-11-21T09:00",
                every_minutes=None,
                cron=None,
                timezone_name="Asia/Shanghai",
                use_computer=False,
                next_run_at=_future(),
                created_by="主管",
                owner_agent_id=workspace["supervisor_id"],
            )
            def cancel_reply(requests, index):
                last = requests[-1]["messages"]
                start = max(
                    (i for i, message in enumerate(last) if message.get("role") == "user"),
                    default=0,
                )
                if any(message.get("role") == "tool" for message in last[start:]):
                    return {"content": "取消了。"}
                return tool_calls(call("r2", "cancel_reminder", {"reminder_id": second["id"]}))

            upstream.script = [cancel_reply]
            run = await start_run(client, direct_id, "把站起来那个提醒取消")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            results = [
                message["content"]
                for message in upstream.requests[-1]["messages"]
                if message.get("role") == "tool"
            ]
            assert any("已取消提醒" in text for text in results)
            assert store.get_schedule(second["id"]) is None

    asyncio.run(body())


def test_remind_me_refuses_more_than_twenty(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    supervisor = store.supervisor()
    assert direct is not None and supervisor is not None
    for index in range(20):
        store.create_schedule(
            title=f"提醒 {index}",
            prompt=f"第 {index} 件事",
            conversation_id=direct["id"],
            conversation_title=direct["title"],
            kind="reminder",
            at="2026-11-21T09:00",
            every_minutes=None,
            cron=None,
            timezone_name="Asia/Shanghai",
            use_computer=False,
            next_run_at=_future(),
            created_by=supervisor["name"],
            owner_agent_id=supervisor["id"],
        )

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            upstream.script = [
                tool_calls(call("r3", "remind_me", {"in_minutes": 5, "note": "多一个"})),
                {"content": "已经到上限了。"},
            ]
            run = await start_run(client, workspace["direct_conversation_id"], "再提醒我一个")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            results = [
                message["content"]
                for message in upstream.requests[-1]["messages"]
                if message.get("role") == "tool"
            ]
            assert any("20" in text and "未执行" in text for text in results)
            assert store.pending_reminder_count(supervisor["id"]) == 20

    asyncio.run(body())
