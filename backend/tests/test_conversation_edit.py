"""Editing a sent message and taking the tail back.

Every case goes through HTTP on a real server; what the store must look like afterwards
(search, reactions, memories, runs) is checked on the same store the app uses.
"""

from __future__ import annotations

import asyncio

from helpers import client_for, get_conversation, start_run, store_of, wait_for_run


def seed(store, conversation_id: str, author: dict, text: str, kind: str = "agent") -> dict:
    return store.append_message(
        conversation_id=conversation_id,
        author_id=author["id"],
        author_name=author["name"],
        avatar_seed=author["avatar_seed"],
        kind=kind,
        content=text,
        status="complete",
    )


def test_rewind_deletes_the_tail_and_what_hung_off_it(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct = store.direct_conversation()
    first = store.append_message(
        conversation_id=direct["id"],
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="第一句",
        status="complete",
    )
    second = seed(store, direct["id"], supervisor, "第二句")
    third = seed(store, direct["id"], supervisor, "第三句")
    store.add_reaction(third["id"], "👍", "user", "你")
    memory = store.create_memory(
        content="第三句说过的事情",
        scope="team",
        origin="extract",
        source_conversation_id=direct["id"],
        source_message_ids=[third["id"]],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.post(
                f"/conversations/{direct['id']}/messages/{second['id']}/rewind"
            )
            assert response.status_code == 200, response.text
            detail = response.json()
            assert [item["content"] for item in detail["messages"]] == ["第一句", "第二句"]
            assert store.get_message(third["id"]) is None
            assert store.get_message(second["id"]) is not None
            # Search never answers with a row that is gone.
            hits = store.search_messages(
                "第三句", agent_id=supervisor["id"], is_supervisor=True
            )
            assert hits == []
            # A reaction cascaded with its message, and a memory kept its text.
            assert store.reactions_payload(third["id"]) == []
            kept = store.get_memory(memory["id"])
            assert kept is not None
            assert kept["source_message_ids"] == []

    asyncio.run(body())


def test_rewind_is_refused_while_a_run_is_live(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    seeded = seed(store, direct["id"], store.supervisor(), "先放着")
    live, _ = store.begin_run(direct["id"], "正在跑的事情")

    async def body() -> None:
        async with client_for(backend) as client:
            refused = await client.post(
                f"/conversations/{direct['id']}/messages/{seeded['id']}/rewind"
            )
            assert refused.status_code == 409, refused.text
            assert "正在运行" in refused.json()["detail"]
            store.update_run(live["id"], status="stopped")

    asyncio.run(body())


def test_editing_a_message_replaces_its_answer(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    upstream.text = "旧回答"

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], "原始的问题")
            await wait_for_run(client, run["id"])
            detail = await get_conversation(client, direct["id"])
            mine = [item for item in detail["messages"] if item["kind"] == "user"][-1]
            upstream.text = "新回答"
            edited = await client.post(
                f"/conversations/{direct['id']}/messages/{mine['id']}/edit",
                json={"content": "改过的问题"},
            )
            assert edited.status_code == 202, edited.text
            fresh = edited.json()
            assert fresh["status"] in ("running", "completed")
            await wait_for_run(client, fresh["id"])
            detail = await get_conversation(client, direct["id"])
            texts = [(item["kind"], item["content"]) for item in detail["messages"]]
            assert ("user", "原始的问题") not in texts
            assert ("user", "改过的问题") in texts
            assert "旧回答" not in [content for _, content in texts]
            assert "新回答" in [content for _, content in texts]
            # Only the newest question and its newest answer are left.
            assert [kind for kind, _ in texts] == ["user", "agent"]

    asyncio.run(body())


def test_only_your_own_messages_can_be_edited(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    reply = seed(store, direct["id"], store.supervisor(), "机器人说的话")

    async def body() -> None:
        async with client_for(backend) as client:
            refused = await client.post(
                f"/conversations/{direct['id']}/messages/{reply['id']}/edit",
                json={"content": "改成别的话"},
            )
            assert refused.status_code == 422, refused.text
            missing = await client.post(
                f"/conversations/{direct['id']}/messages/m_nowhere/edit",
                json={"content": "随便"},
            )
            assert missing.status_code == 404, missing.text

    asyncio.run(body())
