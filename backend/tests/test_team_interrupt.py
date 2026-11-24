"""Sending while a reply is running: the old run stops, and the model knows it was cut off."""

from __future__ import annotations

import asyncio

from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    start_run,
    wait_for_message,
    wait_for_run,
    without_runtime,
)

CUT_OFF = "（这条回复还没说完就被打断了。）"
INTERRUPTS = "（这条消息发出时上一轮回复还没结束，已被它打断。）"


def contents(request: dict) -> list[tuple[str, str]]:
    return [(m["role"], m.get("content") or "") for m in request["messages"] if m["role"] != "system"]


def test_sending_while_a_reply_streams_interrupts_it(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 40
    upstream.script = [{"content": None, "slow": True}, {"content": "好的，改成第二个。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")
            await asyncio.sleep(0.35)

            second = await client.post(
                f"/conversations/{direct_id}/runs",
                json={"content": "换个题目", "mode": "interrupt"},
            )
            assert second.status_code == 202, second.text
            assert (await client.get(f"/runs/{first['id']}")).json()["status"] == "stopped"
            finished = await wait_for_run(client, second.json()["id"])
            assert finished["status"] == "completed"

            detail = await get_conversation(client, direct_id)
            users = [m for m in detail["messages"] if m["kind"] == "user"]
            assert [m["interrupts"] for m in users] == [False, True]
            replies = agent_messages(detail)
            assert [m["status"] for m in replies] == ["interrupted", "complete"]
            partial = replies[0]["content"]
            assert partial.startswith("[1]")

        # The new turn sees its own cut-off text and that the user interrupted it.
        history = contents(upstream.request(1))
        assert ("assistant", f"{partial}\n\n{CUT_OFF}") in history
        # The newest user turn also carries this turn's <runtime …/> line.
        assert history[-1][0] == "user"
        assert without_runtime(history[-1][1]) == f"{INTERRUPTS}\n换个题目"

    asyncio.run(body())


def test_interrupting_before_any_text_still_tells_the_model(make_backend, upstream) -> None:
    """Cut off before the first chunk: no partial text exists, the user turn carries the note."""
    upstream.chunk_delay = 0.5
    upstream.chunk_count = 3
    upstream.script = [{"content": None, "slow": True}, {"content": "收到。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "查一下")
            await asyncio.sleep(0.1)
            second = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "算了", "mode": "interrupt"}
            )
            assert second.status_code == 202, second.text
            await wait_for_run(client, second.json()["id"])
            assert (await client.get(f"/runs/{first['id']}")).json()["status"] == "stopped"
            assert agent_messages(await get_conversation(client, direct_id))[-1]["content"] == "收到。"

        last = upstream.requests[-1]
        assert without_runtime(contents(last)[-1][1]) == f"{INTERRUPTS}\n算了"
        assert not any(CUT_OFF in text for _, text in contents(last))

    asyncio.run(body())


def test_two_sends_at_once_leave_one_running_run(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 60
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await start_run(client, direct_id, "第一个")
            await wait_for_message(client, direct_id, status="streaming")
            url = f"/conversations/{direct_id}/runs"
            a, b = await asyncio.gather(
                client.post(url, json={"content": "第二个", "mode": "interrupt"}),
                client.post(url, json={"content": "第三个", "mode": "interrupt"}),
            )
            assert (a.status_code, b.status_code) == (202, 202), (a.text, b.text)
            active = (await get_workspace(client))["active_runs"]
            assert len(active) == 1
            # The request that took the lock last owns the live run and the newest message.
            detail = await get_conversation(client, direct_id)
            last_user = [m for m in detail["messages"] if m["kind"] == "user"][-1]
            owner = a if active[0]["id"] == a.json()["id"] else b
            assert last_user["content"] == ("第二个" if owner is a else "第三个")
            await client.post(f"/runs/{active[0]['id']}/stop", json={})

    asyncio.run(body())
