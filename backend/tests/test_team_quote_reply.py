"""Replying to one message: stored target, quoted model context and group routing."""

from __future__ import annotations

import asyncio

from app.agent.history import QUOTE_EXCERPT_CHARS, quote_excerpt
from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    start_run,
    store_of,
    wait_for_run,
    without_runtime,
)


def build_team(backend) -> dict:
    """Supervisor plus two members in one group, seeded straight into the store."""
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"], reviewer["id"]],
    )
    return {
        "store": store,
        "supervisor": supervisor,
        "designer": designer,
        "reviewer": reviewer,
        "group": group,
    }


def seed_agent_message(store, conversation_id: str, author: dict, content: str) -> dict:
    return store.append_message(
        conversation_id=conversation_id,
        author_id=author["id"],
        author_name=author["name"],
        avatar_seed=author.get("avatar_seed"),
        kind="agent",
        content=content,
        status="complete",
    )


def test_the_reply_target_is_stored_and_quoted_to_the_model(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct = store.direct_conversation()
    quoted = seed_agent_message(
        store, direct["id"], supervisor, "第一版方案：先做登录。\n再做设置页面。"
    )
    upstream.script = [{"content": "先做登录。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(
                client, direct["id"], "把你刚才说的再简短一点", reply_to_id=quoted["id"]
            )
            run = await wait_for_run(client, run["id"])
            assert run["status"] == "completed", run
            detail = await get_conversation(client, direct["id"])
            goal = [message for message in detail["messages"] if message["kind"] == "user"][-1]
            assert goal["reply_to_id"] == quoted["id"]
            assert goal["content"] == "把你刚才说的再简短一点"
            reply = agent_messages(detail)[-1]
            assert reply["reply_to_id"] == goal["id"]

        turn = upstream.requests[-1]["messages"][-1]
        assert turn["role"] == "user"
        assert without_runtime(turn["content"]) == (
            "用户在回复「主管」的这条消息：\n"
            "> 第一版方案：先做登录。\n"
            "> 再做设置页面。\n"
            "\n"
            "把你刚才说的再简短一点"
        )

    asyncio.run(body())


def test_a_reply_in_a_group_quotes_the_message_and_routes_to_its_author(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_team(backend)
    group_id = team["group"]["id"]
    quoted = seed_agent_message(team["store"], group_id, team["designer"], "我建议先做移动端")
    upstream.script = [{"content": "那就只做移动端。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(
                client, group_id, "只做移动端可以吗", reply_to_id=quoted["id"]
            )
            run = await wait_for_run(client, run["id"])
            assert run["status"] == "completed", run
            assert run["hops"] == 0
            assert run["current_agent_id"] is None

            detail = await get_conversation(client, group_id)
            goal = [message for message in detail["messages"] if message["kind"] == "user"][-1]
            assert goal["mentions"] == [team["designer"]["id"]]
            assert goal["reply_to_id"] == quoted["id"]
            reply = agent_messages(detail)[-1]
            assert reply["author_id"] == team["designer"]["id"]
            assert reply["reply_to_id"] == goal["id"]
            assert reply["content"] == "那就只做移动端。"

        assert upstream.request_count == 1
        contents = [message["content"] for message in upstream.requests[0]["messages"]]
        assert any(
            "用户在回复「甲」的这条消息：\n> 我建议先做移动端\n\n只做移动端可以吗" in content
            for content in contents
        )

    asyncio.run(body())


def test_replying_to_the_supervisor_in_a_group_addresses_the_supervisor(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_team(backend)
    group_id = team["group"]["id"]
    quoted = seed_agent_message(team["store"], group_id, team["supervisor"], "我先来安排")
    upstream.script = [{"content": "好，按你说的办"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, group_id, "就按这个来", reply_to_id=quoted["id"])
            run = await wait_for_run(client, run["id"])
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group_id)
            goal = [message for message in detail["messages"] if message["kind"] == "user"][-1]
            # Quoting a supervisor's message now addresses it like anyone else's; the
            # dispatcher must keep it, and it is the one who answers.
            assert goal["mentions"] == [team["supervisor"]["id"]]
            assert goal["route"]["speakers"] == [team["supervisor"]["id"]]
            assert agent_messages(detail)[-1]["author_id"] == team["supervisor"]["id"]

    asyncio.run(body())


def test_a_run_without_a_reply_target_starts_normally(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    seed_agent_message(store, direct["id"], store.supervisor(), "旧的一条回复")
    upstream.script = [{"content": "新的答复"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], "换个话题")
            run = await wait_for_run(client, run["id"])
            assert run["status"] == "completed", run
            detail = await get_conversation(client, direct["id"])
            goal = [message for message in detail["messages"] if message["kind"] == "user"][-1]
            assert goal["reply_to_id"] is None

        contents = [message["content"] for message in upstream.requests[-1]["messages"]]
        assert any(without_runtime(text).startswith("换个话题") for text in contents)
        assert not any(content.startswith("用户在回复") for content in contents)

    asyncio.run(body())


def test_an_unknown_or_foreign_message_cannot_be_replied_to(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    direct = team["store"].ensure_direct_conversation(team["reviewer"]["id"])
    assert direct is not None
    foreign = seed_agent_message(team["store"], direct["id"], team["reviewer"], "别处的发言")

    async def body() -> None:
        async with client_for(backend) as client:
            for target in ("m_missing", foreign["id"]):
                response = await client.post(
                    f"/conversations/{team['group']['id']}/runs",
                    json={"content": "回复一下", "reply_to_id": target},
                )
                assert response.status_code == 422, response.text
                assert response.json()["detail"] == "引用的消息不在这个会话里。"
            assert upstream.request_count == 0

    asyncio.run(body())


def test_only_an_agent_message_can_be_replied_to(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    group_id = team["group"]["id"]
    store = team["store"]
    user_message = store.append_message(
        conversation_id=group_id,
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="我自己说的一句话",
        status="complete",
    )
    action_message = store.append_message(
        conversation_id=group_id,
        author_id=team["designer"]["id"],
        author_name="甲",
        avatar_seed=team["designer"]["avatar_seed"],
        kind="action",
        content="",
        status="complete",
    )
    relay_marker = store.append_message(
        conversation_id=group_id,
        author_id=team["designer"]["id"],
        author_name="甲",
        avatar_seed=team["designer"]["avatar_seed"],
        kind="agent",
        content="",
        status="complete",
        relay={
            "conversation_id": group_id,
            "direction": "in",
            "peer_id": team["reviewer"]["id"],
            "peer_name": "乙",
        },
    )

    async def body() -> None:
        async with client_for(backend) as client:
            for target in (user_message, action_message, relay_marker):
                response = await client.post(
                    f"/conversations/{group_id}/runs",
                    json={"content": "回复一下", "reply_to_id": target["id"]},
                )
                assert response.status_code == 422, response.text
                assert response.json()["detail"] == "只能回复成员的消息。"
            assert upstream.request_count == 0

    asyncio.run(body())


def test_a_long_quote_is_truncated_and_kept_as_quote_lines() -> None:
    long = "第一段" * 120 + "\n" + "第二段"
    excerpt = quote_excerpt(long)
    assert excerpt.startswith("> 第一段")
    assert excerpt.endswith("…")
    # The cap applies to the quoted text itself; the "> " prefix is the only extra.
    assert len(excerpt) <= QUOTE_EXCERPT_CHARS + 2
    assert "第二段" not in excerpt

    assert quote_excerpt("一\n\n二\n") == "> 一\n> 二"
    assert quote_excerpt("  ") == ""
