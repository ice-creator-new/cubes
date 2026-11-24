"""Steering a running turn and queueing a send."""

from __future__ import annotations

import asyncio

from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    last_user_text,
    start_run,
    store_of,
    wait_for_message,
    wait_for_run,
)

STEER_PREFIX = "（这条消息是在你这一轮还没做完时插进来的"


def user_messages(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "user"]


# ------------------------------------------------------------------ steer


def test_a_steer_reaches_the_next_model_call_of_the_same_run(make_backend, upstream) -> None:
    """The default choice while a reply streams: it joins the turn, nothing is restarted."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 8
    upstream.script = [{"content": None, "slow": True}, {"content": "好，改成第二个题目。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")
            await asyncio.sleep(0.2)

            steered = await client.post(
                f"/conversations/{direct_id}/runs",
                json={"content": "换个题目，写第二个", "mode": "steer"},
            )
            assert steered.status_code == 202, steered.text
            # The steer joined the live run instead of starting another one.
            assert steered.json()["id"] == first["id"]
            assert steered.json()["status"] == "running"

            finished = await wait_for_run(client, first["id"])
            assert finished["status"] == "completed"
            # One run, two model calls: the first reply ended, then the steer was answered.
            assert upstream.request_count == 2

            detail = await get_conversation(client, direct_id)
            assert [m["content"] for m in user_messages(detail)] == ["写一篇长文", "换个题目，写第二个"]
            assert [m["status"] for m in user_messages(detail)] == ["complete", "complete"]
            replies = agent_messages(detail)
            assert replies[-1]["content"] == "好，改成第二个题目。"

        # The second request of that same run saw the reply so far and the steer as a user turn.
        second = upstream.request(1)
        assert second["messages"][-1]["role"] == "user"
        latest = last_user_text(second)
        assert latest.startswith(STEER_PREFIX)
        assert "换个题目，写第二个" in latest
        assert any(
            message["role"] == "assistant" and "[1]" in (message.get("content") or "")
            for message in second["messages"]
        )

    asyncio.run(body())


def test_a_steer_does_not_interrupt_the_reply_being_written(make_backend, upstream) -> None:
    """No row is cut off: the streaming reply finishes and then answers the steer."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 6
    upstream.script = [{"content": None, "slow": True}, {"content": "收到补充。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "写一份清单")
            await wait_for_message(client, direct_id, status="streaming")
            await asyncio.sleep(0.15)
            posted = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "再加上第三项"}
            )
            assert posted.status_code == 202, posted.text
            await wait_for_run(client, run["id"])
            replies = agent_messages(await get_conversation(client, direct_id))
            # Neither reply was cut off, and the steer took the default mode.
            assert [reply["status"] for reply in replies] == ["complete", "complete"]

    asyncio.run(body())


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
            assert "这条不要了" not in [m["content"] for m in user_messages(await get_conversation(client, direct_id))]

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

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


async def patch_send_mode(client, mode: str) -> dict:
    response = await client.patch("/settings/team", json={"send_mode": mode})
    assert response.status_code == 200, response.text
    return response.json()


async def supervisor_id(client) -> str:
    workspace = await get_workspace(client)
    return next(agent["id"] for agent in workspace["agents"] if agent["kind"] == "supervisor")


def test_the_team_default_answers_a_send_that_names_no_mode(make_backend, upstream) -> None:
    """With the workspace set to 做完再发, an unlabelled send waits instead of steering."""
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
    """The workspace says 做完再发; this bot is set to 插进这一轮, so the send joins instead."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 8
    upstream.script = [{"content": None, "slow": True}, {"content": "好，收到补充。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await patch_send_mode(client, "queue")
            patched = await client.patch(
                f"/agents/{await supervisor_id(client)}", json={"send_mode": "steer"}
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["send_mode"] == "steer"

            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")
            await asyncio.sleep(0.2)

            joined = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "换个题目"}
            )
            assert joined.status_code == 202, joined.text
            assert joined.json()["id"] == first["id"]
            assert joined.json()["status"] == "running"

            finished = await wait_for_run(client, first["id"])
            assert finished["status"] == "completed"
            assert [m["content"] for m in user_messages(await get_conversation(client, direct_id))] == [
                "写一篇长文",
                "换个题目",
            ]

    asyncio.run(body())


def test_an_explicit_api_mode_still_wins(make_backend, upstream) -> None:
    """A script that names the mode gets it, whatever the team and the bot were set to."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 30
    upstream.script = [{"content": None, "slow": True}, {"content": "好的。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await patch_send_mode(client, "queue")
            await client.patch(
                f"/agents/{await supervisor_id(client)}", json={"send_mode": "queue"}
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


def test_images_still_fall_back_to_a_queue_under_steer(make_backend, upstream) -> None:
    """A photo cannot join a request already on the wire, so it waits even on 插进这一轮."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 6
    upstream.script = [{"content": None, "slow": True}, {"content": "看到图了。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            uploaded = await client.post(
                "/attachments", content=PNG, headers={"Content-Type": "image/png"}
            )
            assert uploaded.status_code == 201, uploaded.text
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await start_run(client, direct_id, "写一篇长文")
            await wait_for_message(client, direct_id, status="streaming")

            waiting = await client.post(
                f"/conversations/{direct_id}/runs",
                json={"content": "看这张图", "attachment_ids": [uploaded.json()["id"]]},
            )
            assert waiting.status_code == 202, waiting.text
            assert waiting.json()["status"] == "queued"

            await client.delete(f"/runs/{waiting.json()['id']}")
            await client.post(f"/runs/{first['id']}/stop", json={})

    asyncio.run(body())


def test_a_group_uses_the_mode_of_the_bot_speaking(make_backend, upstream) -> None:
    """In a group no single bot owns the chat: the one mid-reply decides."""
    upstream.chunk_delay = 0.1
    upstream.chunk_count = 6
    upstream.script = [{"content": None, "slow": True}, {"content": "第二条回复。"}]
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
            patched = await client.patch(f"/agents/{supervisor['id']}", json={"send_mode": "steer"})
            assert patched.status_code == 200, patched.text
            first = await start_run(client, group["id"], "画一张图", mentions=[member["id"]])
            # The member answers in the group itself: its streaming reply keeps the group busy.
            await wait_for_message(client, group["id"], status="streaming", kind="agent")

            joined = await client.post(
                f"/conversations/{group['id']}/runs", json={"content": "换成蓝色"}
            )
            assert joined.status_code == 202, joined.text
            # The member speaking has no override of its own, so the team default (queue) wins
            # even though the supervisor — who is not talking — is set to steer.
            assert joined.json()["status"] == "queued"

            await client.delete(f"/runs/{joined.json()['id']}")
            await client.post(f"/runs/{first['id']}/stop", json={})

    asyncio.run(body())
