"""Paged history: ``GET /conversations/{id}`` with ``limit`` and ``before``."""

from __future__ import annotations

import asyncio

from helpers import client_for, get_conversation, store_of


def seed_thread(backend, count: int) -> dict:
    """The supervisor's own thread with ``count`` alternating messages, older ones first."""
    store = store_of(backend)
    supervisor = store.supervisor()
    direct = store.direct_conversation()
    messages = []
    for index in range(count):
        messages.append(
            store.append_message(
                conversation_id=direct["id"],
                author_id=None if index % 2 == 0 else supervisor["id"],
                author_name="你" if index % 2 == 0 else supervisor["name"],
                avatar_seed=None if index % 2 == 0 else supervisor["avatar_seed"],
                kind="user" if index % 2 == 0 else "agent",
                content=f"第{index + 1}条",
                status="complete",
            )
        )
    return {"store": store, "supervisor": supervisor, "conversation": direct, "messages": messages}


def seqs(page: dict) -> list[int]:
    return [message["seq"] for message in page["messages"]]


def test_the_newest_page_is_the_tail_oldest_first(make_backend) -> None:
    backend = make_backend()
    thread = seed_thread(backend, 12)
    conversation_id = thread["conversation"]["id"]

    async def body() -> None:
        async with client_for(backend) as client:
            page = await get_conversation(client, f"{conversation_id}?limit=5")
            assert seqs(page) == [8, 9, 10, 11, 12]
            assert page["has_more"] is True
            assert page["references"] == []
            assert page["messages"][-1]["content"] == "第12条"

    asyncio.run(body())


def test_before_steps_back_until_the_first_message(make_backend) -> None:
    backend = make_backend()
    thread = seed_thread(backend, 12)
    conversation_id = thread["conversation"]["id"]

    async def body() -> None:
        async with client_for(backend) as client:
            second = await get_conversation(client, f"{conversation_id}?limit=5&before=9")
            assert seqs(second) == [4, 5, 6, 7, 8]
            assert second["has_more"] is True
            first = await get_conversation(client, f"{conversation_id}?limit=5&before=4")
            assert seqs(first) == [1, 2, 3]
            assert first["has_more"] is False
            # Walking past the first message is an empty page, never an error.
            empty = await get_conversation(client, f"{conversation_id}?limit=5&before=1")
            assert empty["messages"] == []
            assert empty["has_more"] is False

    asyncio.run(body())


def test_a_page_that_reaches_the_start_reports_no_more(make_backend) -> None:
    backend = make_backend()
    thread = seed_thread(backend, 4)
    conversation_id = thread["conversation"]["id"]

    async def body() -> None:
        async with client_for(backend) as client:
            page = await get_conversation(client, f"{conversation_id}?limit=4")
            assert seqs(page) == [1, 2, 3, 4]
            assert page["has_more"] is False
            wider = await get_conversation(client, f"{conversation_id}?limit=80")
            assert seqs(wider) == [1, 2, 3, 4]
            assert wider["has_more"] is False

    asyncio.run(body())


def test_a_quote_whose_parent_is_outside_the_page_comes_back_as_a_reference(make_backend) -> None:
    backend = make_backend()
    thread = seed_thread(backend, 6)
    store = thread["store"]
    conversation_id = thread["conversation"]["id"]
    quoted = thread["messages"][0]
    reply = store.append_message(
        conversation_id=conversation_id,
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="回第一条",
        status="complete",
        reply_to_id=quoted["id"],
    )
    inside = store.append_message(
        conversation_id=conversation_id,
        author_id=thread["supervisor"]["id"],
        author_name=thread["supervisor"]["name"],
        avatar_seed=thread["supervisor"]["avatar_seed"],
        kind="agent",
        content="回刚才那条",
        status="complete",
        reply_to_id=reply["id"],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            page = await get_conversation(client, f"{conversation_id}?limit=2")
            assert [message["id"] for message in page["messages"]] == [reply["id"], inside["id"]]
            # The parent quoted from outside the page travels with it; the one inside does not.
            assert [parent["id"] for parent in page["references"]] == [quoted["id"]]
            assert page["references"][0]["content"] == quoted["content"]
            # A page whose rows quote each other needs nothing extra.
            whole = await get_conversation(client, f"{conversation_id}?limit=8")
            assert {message["id"] for message in whole["messages"]} >= {quoted["id"], reply["id"], inside["id"]}
            assert whole["references"] == []

    asyncio.run(body())


def test_without_a_limit_every_message_comes_back(make_backend) -> None:
    backend = make_backend()
    thread = seed_thread(backend, 30)
    conversation_id = thread["conversation"]["id"]

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await get_conversation(client, conversation_id)
            assert seqs(detail) == list(range(1, 31))
            assert detail["has_more"] is False
            assert detail["references"] == []
            # A ``before`` without a limit changes nothing: older clients read it all.
            stray = await get_conversation(client, f"{conversation_id}?before=10")
            assert seqs(stray) == list(range(1, 31))
            assert stray["has_more"] is False

    asyncio.run(body())


def test_a_limit_outside_the_range_is_rejected(make_backend) -> None:
    backend = make_backend()
    thread = seed_thread(backend, 3)
    conversation_id = thread["conversation"]["id"]

    async def body() -> None:
        async with client_for(backend) as client:
            for query in ("limit=0", "limit=201"):
                response = await client.get(f"/conversations/{conversation_id}?{query}")
                assert response.status_code == 422
                detail = response.json()["detail"]
                assert detail == "「limit」需要在 1 到 200 之间。"
                assert detail.endswith("。")

    asyncio.run(body())
