"""Queueing a send, interrupting, and the send mode a message without one carries."""

from __future__ import annotations

import asyncio

from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    start_run,
    store_of,
    wait_for_message,
    wait_for_run,
)


def user_messages(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "user"]


# ------------------------------------------------------------------ queue


def test_a_queued_send_starts_after_the_running_reply_ends(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 6
    upstream.script = [{"content": None, "slow": True}, {"content": "第二条回复。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "先做第一件事")
            await wait_for_message(client, direct_id, status="streaming")

            queued = await client.post(
                f"/conversations/{direct_id}/runs",
                json={"content": "做完再做这一件", "mode": "queue"},
            )
            assert queued.status_code == 202, queued.text
            assert queued.json()["status"] == "queued"
            assert queued.json()["id"] != first["id"]
            # The message is already in the transcript, marked as waiting.
            waiting = user_messages(await get_conversation(client, direct_id))[-1]
            assert waiting["content"] == "做完再做这一件"
            assert waiting["status"] == "queued"

            done = await wait_for_run(client, queued.json()["id"])
            assert done["status"] == "completed"
            assert (await client.get(f"/runs/{first['id']}")).json()["status"] == "completed"

            detail = await get_conversation(client, direct_id)
            assert [m["status"] for m in user_messages(detail)] == ["complete", "complete"]
            assert [reply["content"] for reply in agent_messages(detail)][-1] == "第二条回复。"

    asyncio.run(body())


def test_a_queued_send_can_be_taken_back(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 20
    upstream.mode = "slow"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "慢慢写")
            await wait_for_message(client, direct_id, status="streaming")
            queued = await client.post(
                f"/conversations/{direct_id}/runs",
                json={"content": "这条不要了", "mode": "queue"},
            )
            assert queued.json()["status"] == "queued"

            removed = await client.delete(f"/runs/{queued.json()['id']}")
            assert removed.status_code == 204, removed.text
            assert (await client.get(f"/runs/{queued.json()['id']}")).status_code == 404
            assert "这条不要了" not in [
                m["content"] for m in user_messages(await get_conversation(client, direct_id))
            ]

            await client.post(f"/runs/{run['id']}/stop", json={})

    asyncio.run(body())


def test_interrupt_mode_still_stops_the_running_reply(make_backend, upstream) -> None:
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 30
    upstream.script = [{"content": None, "slow": True}, {"content": "好的。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")
            second = await client.post(
                f"/conversations/{direct_id}/runs",
                json={"content": "停止并发送", "mode": "interrupt"},
            )
            assert second.status_code == 202, second.text
            await wait_for_run(client, second.json()["id"])
            assert (await client.get(f"/runs/{first['id']}")).json()["status"] == "stopped"
            detail = await get_conversation(client, direct_id)
            replies = agent_messages(detail)
            assert [reply["status"] for reply in replies] == ["interrupted", "complete"]

    asyncio.run(body())


# ------------------------------------------------- the mode a send without one carries


async def patch_send_mode(client, mode: str) -> dict:
    response = await client.patch("/settings/team", json={"send_mode": mode})
    assert response.status_code == 200, response.text
    return response.json()


async def supervisor_id(client) -> str:
    workspace = await get_workspace(client)
    return next(agent["id"] for agent in workspace["agents"] if agent["kind"] == "supervisor")


def test_the_team_default_answers_a_send_that_names_no_mode(make_backend, upstream) -> None:
    """With the workspace set to 做完再发, an unlabelled send waits."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 6
    upstream.script = [{"content": None, "slow": True}, {"content": "第二条回复。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            state = await patch_send_mode(client, "queue")
            assert state["send_mode"] == "queue"
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "先做第一件事")
            await wait_for_message(client, direct_id, status="streaming")

            waiting = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "做完再做这一件"}
            )
            assert waiting.status_code == 202, waiting.text
            assert waiting.json()["status"] == "queued"
            assert waiting.json()["id"] != first["id"]
            assert user_messages(await get_conversation(client, direct_id))[-1]["status"] == "queued"

            await client.delete(f"/runs/{waiting.json()['id']}")
            await client.post(f"/runs/{first['id']}/stop", json={})

    asyncio.run(body())


def test_a_bots_own_mode_wins_over_the_team_default(make_backend, upstream) -> None:
    """The workspace says 做完再发; this bot is set to 同时回，so the send runs beside it."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 40
    upstream.mode = "slow"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await patch_send_mode(client, "queue")
            patched = await client.patch(
                f"/agents/{await supervisor_id(client)}", json={"send_mode": "parallel"}
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["send_mode"] == "parallel"

            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")
            await asyncio.sleep(0.2)

            beside = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "换个题目"}
            )
            assert beside.status_code == 202, beside.text
            assert beside.json()["id"] != first["id"]
            assert beside.json()["lane"] == "reply"
            assert beside.json()["status"] == "running"

            await client.post(f"/runs/{beside.json()['id']}/stop", json={})
            await client.post(f"/runs/{first['id']}/stop", json={})

    asyncio.run(body())


def test_an_explicit_api_mode_still_wins(make_backend, upstream) -> None:
    """A script that names the mode gets it, whatever the team and the bot were set to."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 30
    upstream.script = [{"content": None, "slow": True}, {"content": "好的。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await patch_send_mode(client, "parallel")
            await client.patch(
                f"/agents/{await supervisor_id(client)}", json={"send_mode": "parallel"}
            )
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")
            second = await client.post(
                f"/conversations/{direct_id}/runs",
                json={"content": "停止并发送", "mode": "interrupt"},
            )
            assert second.status_code == 202, second.text
            assert second.json()["id"] != first["id"]
            await wait_for_run(client, second.json()["id"])
            assert (await client.get(f"/runs/{first['id']}")).json()["status"] == "stopped"

    asyncio.run(body())


def test_a_group_uses_the_mode_of_the_bot_speaking(make_backend, upstream) -> None:
    """In a group no single bot owns the chat: the one mid-reply decides."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 40
    upstream.mode = "slow"
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="绘图员", instructions="画图", kind="member")
    group = store.create_conversation(
        kind="group",
        title="绘图小组",
        purpose="把图做出来",
        member_ids=[supervisor["id"], member["id"]],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            await patch_send_mode(client, "queue")
            patched = await client.patch(
                f"/agents/{supervisor['id']}", json={"send_mode": "parallel"}
            )
            assert patched.status_code == 200, patched.text
            first = await start_run(client, group["id"], "画一张图", mentions=[member["id"]])
            # The member answers in the group itself: its streaming reply keeps the group busy.
            await wait_for_message(client, group["id"], status="streaming", kind="agent")

            waiting = await client.post(
                f"/conversations/{group['id']}/runs", json={"content": "换成蓝色"}
            )
            assert waiting.status_code == 202, waiting.text
            # The member speaking has no override of its own, so the team default (queue) wins
            # even though the supervisor — who is not talking — is set to 同时回.
            assert waiting.json()["status"] == "queued"

            await client.delete(f"/runs/{waiting.json()['id']}")
            await client.post(f"/runs/{first['id']}/stop", json={})

    asyncio.run(body())
