"""Always-allow rules: their card button, the settings list, and the skip path."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app import approvals
from app.tools import supervisor as supervisor_tools
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    get_conversation,
    get_workspace,
    start_run,
    store_of,
    wait_for_run,
)


def seed_member(backend, name: str) -> dict:
    return store_of(backend).create_agent(name=name, instructions="研究", kind="member")


def delete_call(call_id: str, agent_id: str, reason: str = "任务完成") -> dict:
    return call(call_id, "delete_agent", {"agent_id": agent_id, "reason": reason})


async def wait_for_awaiting(client, conversation_id: str, *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await get_conversation(client, conversation_id)
        for message in action_messages(detail):
            if message["action"]["status"] == "awaiting":
                return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no awaiting action appeared in {conversation_id}; messages={[(m['kind'], m['content'][:40], m.get('action') and m['action']['status']) for m in detail['messages']]}")


async def no_awaiting(client, conversation_id: str) -> bool:
    detail = await get_conversation(client, conversation_id)
    return all(
        message["action"]["status"] != "awaiting" for message in action_messages(detail)
    )


def test_always_allow_skips_the_next_card_and_deleting_the_rule_restores_it(
    make_backend, upstream
) -> None:
    backend = make_backend()
    first = seed_member(backend, "甲")
    second = seed_member(backend, "乙")
    third = seed_member(backend, "丙")
    upstream.script = [
        tool_calls(delete_call("call_1", first["id"])),
        {"content": "甲删掉了"},
        tool_calls(delete_call("call_2", second["id"])),
        {"content": "乙也删掉了"},
        tool_calls(delete_call("call_3", third["id"])),
        {"content": "等你决定"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]

            run = await start_run(client, direct_id, "删掉甲")
            awaiting = await wait_for_awaiting(client, direct_id)
            confirmed = await client.post(
                f"/actions/{awaiting['action']['id']}/confirm",
                json={"approve": True, "always": True},
            )
            assert confirmed.status_code == 200, confirmed.text
            await wait_for_run(client, run["id"])

            rules = (await client.get("/approval-rules")).json()["rules"]
            assert [(rule["tool_name"], rule["title"], rule["agent_name"]) for rule in rules] == [
                ("delete_agent", "删除成员", "主管")
            ]

            # The next identical call by the same bot shows no card at all.
            run = await start_run(client, direct_id, "删掉乙")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert await no_awaiting(client, direct_id)
            assert store_of(backend).get_agent(second["id"]) is None

            # Deleting the rule brings the card back for the next member.
            removed = await client.delete(f"/approval-rules/{rules[0]['id']}")
            assert removed.status_code == 204
            assert (await client.get("/approval-rules")).json()["rules"] == []

            run = await start_run(client, direct_id, "删掉丙")
            await wait_for_awaiting(client, direct_id)
            assert store_of(backend).get_agent(third["id"]) is not None

    asyncio.run(body())


def test_scheduled_run_with_a_rule_may_delete_and_without_one_may_not(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    handler = next(h for h in supervisor_tools.handlers() if h.name == "delete_agent")
    context = SimpleNamespace(store=store, author=supervisor)

    assert approvals.refusal(context, handler, scheduled=True) == approvals.UNATTENDED_REFUSAL
    store.add_approval_rule(tool_name="delete_agent", agent_id=supervisor["id"])
    assert approvals.refusal(context, handler, scheduled=True) is None
