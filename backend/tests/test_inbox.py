"""Inbox: unread bot replies, the read marker, and the 已完成 list."""

from __future__ import annotations

import asyncio

from helpers import client_for, get_workspace, store_of


def test_unread_dot_clears_when_the_conversation_is_read(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    supervisor = store.supervisor()
    assert direct is not None and supervisor is not None
    reply = store.append_message(
        conversation_id=direct["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content="做完了，你看一下。",
        status="complete",
    )

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            row = next(item for item in workspace["conversations"] if item["id"] == direct["id"])
            assert row["unread"] is True

            response = await client.post(
                f"/conversations/{direct['id']}/read", json={"seq": reply["seq"]}
            )
            assert response.status_code == 204, response.text

            workspace = await get_workspace(client)
            row = next(item for item in workspace["conversations"] if item["id"] == direct["id"])
            assert row["unread"] is False

    asyncio.run(body())


def test_finished_runs_list_until_the_chat_is_opened(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    supervisor = store.supervisor()
    assert direct is not None and supervisor is not None
    run, _message = store.begin_run(direct["id"], "帮我整理一下")
    last = store.append_message(
        conversation_id=direct["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content="整理好了。",
        status="complete",
        run_id=run["id"],
    )
    store.update_run(run["id"], status="completed", current_step="任务已完成", finished_at="2026-11-20T10:00:00Z")

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            finished = workspace["finished_runs"]
            assert [item["run_id"] for item in finished] == [run["id"]]
            assert finished[0]["message_id"] == last["id"]
            assert finished[0]["preview"] == "整理好了。"

            response = await client.post(
                f"/conversations/{direct['id']}/read", json={"seq": last["seq"]}
            )
            assert response.status_code == 204, response.text
            workspace = await get_workspace(client)
            assert workspace["finished_runs"] == []

    asyncio.run(body())
