"""Emoji reactions: the HTTP contract, the ``<react>`` tag, reaction-only replies and the
debounced bot reaction response.

Everything runs over real HTTP against a temporary database; replies and the reaction
response are scripted through the isolated upstream so the tests assert what the model was
asked and what the store then kept.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.appearance import MoodStream
from app.prompts import MOOD_RULE, REACT_RULE
from fake_upstream import call, tool_calls
from helpers import (
    agent_messages,
    client_for,
    collect_events,
    event_names,
    get_conversation,
    get_workspace,
    payloads,
    run_goal,
    settled,
    start_run,
    stop_run,
    store_of,
    tool_results,
    wait_for_run,
)


def _reaction_path(message_id: str, emoji: str) -> str:
    return f"/messages/{message_id}/reactions/{quote(emoji)}"


async def exchange(backend, client: httpx.AsyncClient, content: str = "好的") -> dict:
    """Run one goal in the supervisor's direct chat and return its conversation detail."""
    workspace = await get_workspace(client)
    await run_goal(client, workspace["direct_conversation_id"], content)
    return await get_conversation(client, workspace["direct_conversation_id"])


def first_user(detail: dict) -> dict:
    return next(message for message in detail["messages"] if message["kind"] == "user")


def last_agent(detail: dict) -> dict:
    return next(message for message in reversed(detail["messages"]) if message["kind"] == "agent")


async def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 5.0
) -> None:
    """Wait for a (possibly async) predicate to turn truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


def emojis(reactions: list[dict]) -> list[str]:
    return [reaction["emoji"] for reaction in reactions]


# -------------------------------------------------------------------------- HTTP CRUD


def test_add_remove_idempotent_invalid_and_unknown(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await exchange(backend, client)
            message = first_user(detail)

            added = await client.put(_reaction_path(message["id"], "👍"))
            assert added.status_code == 200, added.text
            payload = added.json()
            assert payload["message_id"] == message["id"]
            assert payload["conversation_id"] == detail["conversation"]["id"]
            assert emojis(payload["reactions"]) == ["👍"]
            assert payload["reactions"][0]["count"] == 1
            assert payload["reactions"][0]["mine"] is True
            assert payload["reactions"][0]["actors"] == [{"id": "user", "name": "你"}]

            # Idempotent: adding the same emoji again leaves one row.
            again = await client.put(_reaction_path(message["id"], "👍"))
            assert again.status_code == 200, again.text
            assert emojis(again.json()["reactions"]) == ["👍"]
            assert again.json()["reactions"][0]["count"] == 1

            # Invalid emoji → 422.
            for bad in ("abc", "<", " ", "你好", "a%20b"):
                rejected = await client.put(_reaction_path(message["id"], bad))
                assert rejected.status_code == 422, (bad, rejected.text)

            # Remove → empty list; remove again (absent) is still 200 with the empty list.
            removed = await client.delete(_reaction_path(message["id"], "👍"))
            assert removed.status_code == 200, removed.text
            assert removed.json()["reactions"] == []
            removed_again = await client.delete(_reaction_path(message["id"], "👍"))
            assert removed_again.status_code == 200, removed_again.text
            assert removed_again.json()["reactions"] == []

            # Unknown message → 404.
            missing = await client.put(_reaction_path("m_nope", "👍"))
            assert missing.status_code == 404

    asyncio.run(body())


def test_the_twenty_first_distinct_emoji_is_refused(make_backend) -> None:
    backend = make_backend()
    catalog = "😀😁😂🤣😃😄😅😆😉😊😋😎😍😘😗😙😚🙂🤗🤩"

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await exchange(backend, client)
            message = first_user(detail)
            for emoji in catalog:
                response = await client.put(_reaction_path(message["id"], emoji))
                assert response.status_code == 200, (emoji, response.text)
            assert len(first_user(await get_conversation(client, detail["conversation"]["id"]))["reactions"]) == 20

            refused = await client.put(_reaction_path(message["id"], "🥳"))
            assert refused.status_code == 409, refused.text

    asyncio.run(body())


def test_a_message_that_cannot_take_reactions_is_refused(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await exchange(backend, client)
            # A still-streaming message cannot take a reaction.
            streaming = store.append_message(
                conversation_id=detail["conversation"]["id"],
                author_id=None,
                author_name="你",
                avatar_seed=None,
                kind="user",
                content="还在写",
                status="streaming",
            )
            refused = await client.put(_reaction_path(streaming["id"], "👍"))
            assert refused.status_code == 422, refused.text

    asyncio.run(body())


def test_the_reactions_event_is_published(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await exchange(backend, client)
            message = first_user(detail)
            events_task = asyncio.create_task(
                collect_events(client, "/events", duration=1.0)
            )
            await asyncio.sleep(0.1)
            await client.put(_reaction_path(message["id"], "❤️"))
            events = await events_task
            assert "reactions" in event_names(events)
            assert any(
                payload["message_id"] == message["id"] and payload["reactions"]
                for payload in payloads(events, "reactions")
            )

    asyncio.run(body())


# -------------------------------------------------------------- the <react> tag


def test_the_react_tag_never_reaches_a_delta_at_any_chunk_split() -> None:
    # The stream withholds the tag whatever the chunk boundary; only the body is released.
    text = "好的，收到。\n<react>👍</react>\n<mood>happy</mood>"
    for cut in range(len(text) + 1):
        stream = MoodStream()
        pushed = stream.push(text[:cut]) + stream.push(text[cut:])
        assert "<react" not in pushed, (cut, pushed)
        body, mood = stream.finish()
        assert body == "好的，收到。"
        assert mood == "happy"
        assert stream.reaction == "👍"

    for step in (1, 2, 3, 7, 13):
        stream = MoodStream()
        pushed = "".join(stream.push(text[i : i + step]) for i in range(0, len(text), step))
        assert "<react" not in pushed, (step, pushed)
        assert stream.finish() == ("好的，收到。", "happy")
        assert stream.reaction == "👍"


def test_a_react_tag_written_mid_reply_is_cut_out_too() -> None:
    # Seen from the supervisor: the tag right after the sentence it reacts to, text after it.
    text = "恭喜！技能面板上线是件大实事 🎉\n\n<react>🎉</react>\n\n另外同步一下进度。\n<mood>happy</mood>"
    for cut in range(len(text) + 1):
        stream = MoodStream()
        pushed = stream.push(text[:cut]) + stream.push(text[cut:])
        assert "react" not in pushed, (cut, pushed)
        body, mood = stream.finish()
        assert body == "恭喜！技能面板上线是件大实事 🎉\n\n另外同步一下进度。", (cut, body)
        assert (mood, stream.reaction) == ("happy", "🎉"), cut
    # Inline, inside a sentence, it goes without leaving a double space.
    stream = MoodStream()
    stream.push("好的 <react>👍</react> 马上办。")
    assert stream.finish() == ("好的 马上办。", None)
    assert stream.reaction == "👍"
    # At the very start (seen live), the line it stood on goes with it.
    stream = MoodStream()
    stream.push("<react>😊</react>\n不客气。\n<mood>happy</mood>")
    assert stream.finish() == ("不客气。", "happy")
    assert stream.reaction == "😊"


def test_the_react_tag_lands_on_the_trigger_user_message(make_backend, upstream) -> None:
    upstream.text = "好的，收到。\n<react>👍</react>\n<mood>happy</mood>"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await exchange(backend, client)
            user = first_user(detail)
            agent = last_agent(detail)
            assert agent["content"] == "好的，收到。"
            assert agent["mood"] == "happy"
            assert emojis(user["reactions"]) == ["👍"]
            assert user["reactions"][0]["actors"] == [
                {"id": agent["author_id"], "name": agent["author_name"]}
            ]

    asyncio.run(body())


def test_the_react_tag_is_stripped_from_streamed_deltas(make_backend, upstream) -> None:
    upstream.text = "好的\n<react>👍</react>\n<mood>happy</mood>"
    upstream.chunk_count = 7
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            run = await start_run(client, direct_id, "好的")
            events = await collect_events(
                client, f"/events?conversation_id={direct_id}", until=settled
            )
            for delta in payloads(events, "delta"):
                assert "<react" not in delta["text"], delta
            await wait_for_run(client, run["id"])
            detail = await get_conversation(client, direct_id)
            assert emojis(first_user(detail)["reactions"]) == ["👍"]

    asyncio.run(body())


def test_a_bots_second_reaction_replaces_its_first(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await exchange(backend, client)
            message = first_user(detail)
            agent = store.supervisor()
            store.add_reaction(message["id"], "👍", actor_id=agent["id"], actor_name=agent["name"])
            store.add_reaction(message["id"], "❤️", actor_id=agent["id"], actor_name=agent["name"])
            reactions = store.reactions_payload(message["id"])
            assert emojis(reactions) == ["❤️"]
            assert reactions[0]["count"] == 1

    asyncio.run(body())


def test_a_reaction_only_reply_leaves_no_row_and_completes(make_backend, upstream) -> None:
    upstream.text = "<react>👍</react>\n<mood>happy</mood>"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            run = await start_run(client, direct_id, "好的，谢谢")
            events = await collect_events(
                client, f"/events?conversation_id={direct_id}", until=settled
            )
            assert any(name == "message_removed" for name, _ in events)
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            detail = await get_conversation(client, direct_id)
            assert agent_messages(detail) == []
            assert [message["kind"] for message in detail["messages"]] == ["user"]
            assert emojis(first_user(detail)["reactions"]) == ["👍"]

    asyncio.run(body())


def test_an_empty_reply_without_a_reaction_still_errors(make_backend, upstream) -> None:
    upstream.mode = "empty"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            run = await run_goal(client, workspace["direct_conversation_id"], "开始任务")
            assert run["status"] == "error", run
            assert agent_messages(await get_conversation(client, workspace["direct_conversation_id"])) == []

    asyncio.run(body())


def test_a_consulted_members_reaction_lands_on_the_users_message(make_backend, upstream) -> None:
    """A consulted member's reply points at the supervisor's tool row, not at a user message:
    its reaction still lands on what the user asked, and a reaction-only answer leaves no
    empty bubble behind."""
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="做研究", kind="member")
    group = store.create_conversation(
        kind="group",
        title="研究小组",
        purpose="把研究做出来",
        member_ids=[supervisor["id"], member["id"]],
    )
    upstream.script = [
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "点个赞"})
        ),
        {"content": "<react>👍</react>\n<mood>happy</mood>"},
        {"content": "大家都看到了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "大家给我点个赞")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])
            user = first_user(detail)
            assert [(r["emoji"], r["actors"][0]["name"]) for r in user["reactions"]] == [("👍", "甲")]
            authors = [message["author_name"] for message in agent_messages(detail)]
            assert "甲" not in authors
            results = dict(tool_results(upstream.requests[-1]))
            assert results["c1"]["message_id"] is None
            assert "只贴了一个表情" in results["c1"]["note"]

    asyncio.run(body())



# ---------------------------------------------------------- the reaction response


def test_a_user_reaction_triggers_one_reaction_response_with_each_outcome(
    make_backend, upstream
) -> None:
    backend = make_backend(reaction_debounce_seconds=0.1)

    async def body() -> None:
        async with client_for(backend) as client:
            # outcome "none": nothing is added or said.
            upstream.reaction_text = '{"action": "none"}'
            detail = await exchange(backend, client)
            message = last_agent(detail)
            await client.put(_reaction_path(message["id"], "👍"))
            await wait_until(lambda: upstream.reaction_request_count == 1)
            await asyncio.sleep(0.05)
            assert upstream.reaction_request_count == 1
            after = await get_conversation(client, detail["conversation"]["id"])
            assert emojis(last_agent(after)["reactions"]) == ["👍"]

            # outcome "react": the agent's emoji lands on the same message.
            upstream.reaction_text = '{"action": "react", "emoji": "😊"}'
            detail = await exchange(backend, client)
            message = last_agent(detail)
            await client.put(_reaction_path(message["id"], "👍"))
            await wait_until(lambda: upstream.reaction_request_count == 2)

            async def reacted_emoji_present() -> bool:
                after = await get_conversation(client, detail["conversation"]["id"])
                reactions = last_agent(after)["reactions"]
                return any(reaction["emoji"] == "😊" for reaction in reactions)

            await wait_until(reacted_emoji_present)
            reactions = last_agent(await get_conversation(client, detail["conversation"]["id"]))["reactions"]
            assert emojis(reactions) == ["👍", "😊"]
            agent = store_of(backend).supervisor()
            assert any(
                reaction["emoji"] == "😊"
                and any(actor["id"] == agent["id"] for actor in reaction["actors"])
                for reaction in reactions
            )

            # outcome "reply": a complete agent message is published.
            upstream.reaction_text = '{"action": "reply", "text": "谢谢", "mood": "happy"}'
            detail = await exchange(backend, client)
            message = last_agent(detail)
            await client.put(_reaction_path(message["id"], "👍"))
            await wait_until(lambda: upstream.reaction_request_count == 3)

            async def reply_present() -> bool:
                after = await get_conversation(client, detail["conversation"]["id"])
                return any(m["content"] == "谢谢" for m in after["messages"])

            await wait_until(reply_present)
            after = await get_conversation(client, detail["conversation"]["id"])
            replies = [m for m in after["messages"] if m["content"] == "谢谢"]
            assert len(replies) == 1
            assert replies[0]["mood"] == "happy"
            assert replies[0]["reply_to_id"] == message["id"]

    asyncio.run(body())


def test_two_quick_reactions_merge_into_one_call(make_backend, upstream) -> None:
    upstream.reaction_text = '{"action": "none"}'
    backend = make_backend(reaction_debounce_seconds=0.4)

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await exchange(backend, client)
            message = last_agent(detail)
            await client.put(_reaction_path(message["id"], "👍"))
            await client.put(_reaction_path(message["id"], "❤️"))
            await asyncio.sleep(1.0)
            assert upstream.reaction_request_count == 1

    asyncio.run(body())


def test_no_reaction_call_while_a_run_is_active(make_backend, upstream) -> None:
    upstream.reaction_text = '{"action": "none"}'
    backend = make_backend(reaction_debounce_seconds=0.1)

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            detail = await exchange(backend, client)
            message = last_agent(detail)

            upstream.mode = "slow"
            upstream.chunk_delay = 0.5  # keep the run open long enough to react mid-run
            run = await start_run(client, direct_id, "慢一点")
            await asyncio.sleep(0.2)
            await client.put(_reaction_path(message["id"], "👍"))
            await asyncio.sleep(0.5)
            assert upstream.reaction_request_count == 0
            await stop_run(client, run["id"])

    asyncio.run(body())


# ----------------------------------------------------------------- prompts


def test_the_react_rule_appears_only_in_direct_and_group_prompts() -> None:
    from app import prompts

    sup = {
        "name": "主管",
        "instructions": "帮用户",
        "team": "",
        "groups": "",
        "conversation": "私聊",
    }
    sup_direct = prompts.supervisor_system(**sup, react=True)
    assert REACT_RULE in sup_direct
    assert sup_direct.endswith(MOOD_RULE)
    sup_private = prompts.supervisor_system(**sup, private_peer="甲")
    assert REACT_RULE not in sup_private
    assert sup_private.endswith(MOOD_RULE)

    agent = {"id": "a_1", "name": "甲", "instructions": "做研究"}
    member_direct = prompts.member_system(
        agent=agent, conversation_kind="direct", team_roster="", groups=""
    )
    assert REACT_RULE in member_direct
    assert member_direct.endswith(MOOD_RULE)
    member_group = prompts.member_system(
        agent=agent, conversation_kind="group", team_roster="", groups=""
    )
    assert REACT_RULE in member_group
    assert member_group.endswith(MOOD_RULE)
    member_private = prompts.member_system(
        agent=agent, conversation_kind="private", peer="乙", team_roster="", groups=""
    )
    assert REACT_RULE not in member_private
    assert member_private.endswith(MOOD_RULE)


def test_the_v19_migration_cleans_a_react_tag_older_builds_stored(tmp_path) -> None:
    """A reply that kept ``<react>🤔</react>`` in its text loses it on the next start."""
    import sqlite3

    from app.store import SCHEMA_VERSION, Store

    path = tmp_path / "cubes.sqlite3"
    offline = Store(path)
    offline.initialize()
    direct = offline.direct_conversation()
    supervisor = offline.supervisor()
    assert direct is not None and supervisor is not None

    def reply(content: str) -> str:
        return offline.append_message(
            conversation_id=direct["id"],
            author_id=supervisor["id"],
            author_name=supervisor["name"],
            avatar_seed=supervisor["avatar_seed"],
            kind="agent",
            content=content,
            status="complete",
        )["id"]

    kept = reply("好的，谢谢！\n<react>😊</react>")
    mid = reply("恭喜！\n\n<react>🎉</react>\n\n另外同步一下进度。")
    code = reply("写成 `<react>…</react>` 这个标签即可。")
    offline.close()

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE meta SET value = '18' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    migrated = Store(path)
    migrated.initialize()
    try:
        assert migrated.get_message(kept)["content"] == "好的，谢谢！"
        # A tag in the middle goes too, and the gap it left is closed up.
        assert migrated.get_message(mid)["content"] == "恭喜！\n\n另外同步一下进度。"
        # Prose that merely names the tag keeps the line it is written on.
        assert migrated.get_message(code)["content"] == "写成 `<react>…</react>` 这个标签即可。"
    finally:
        migrated.close()
    conn = sqlite3.connect(str(path))
    try:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        conn.close()
    assert version == str(SCHEMA_VERSION)
