"""Appearance, message mood tags and the streamed tag parser (real HTTP + FakeUpstream)."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.appearance import MOODS, MoodStream, parse_appearance, split_trailing_tags
from app.prompts import MEMBER_DIRECT_SYSTEM, MEMBER_SYSTEM, MOOD_RULE, SUPERVISOR_SYSTEM
from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    payloads,
    run_goal,
    settled,
    start_run,
    store_of,
    system_prompt,
    wait_for_run,
)

MOOD_REPLY = "好的，方案如下。\n<mood>happy</mood>"


def build_team(backend) -> dict:
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
        "supervisor": supervisor,
        "designer": designer,
        "reviewer": reviewer,
        "group": group,
    }


async def run_and_collect(client, conversation_id: str, content: str) -> tuple[list[tuple[str, dict]], dict]:
    """Subscribe first (wait for the snapshot), then run one goal to its end."""
    events: list[tuple[str, dict]] = []
    attached = asyncio.Event()

    async def read() -> None:
        async with client.stream("GET", f"/events?conversation_id={conversation_id}") as response:
            assert response.status_code == 200, response.text
            event: str | None = None
            data: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data.append(line[len("data:") :].strip())
                elif line.strip() == "":
                    if event is not None:
                        events.append((event, json.loads("".join(data))))
                        if event == "snapshot":
                            attached.set()
                    event, data = None, []
                    if settled(events):
                        return

    reader = asyncio.create_task(read())
    await asyncio.wait_for(attached.wait(), timeout=10)
    run = await start_run(client, conversation_id, content)
    finished = await wait_for_run(client, run["id"])
    await asyncio.wait_for(reader, timeout=15)
    return events, finished


# ------------------------------------------------------------------------ streaming


def test_the_mood_tag_never_reaches_a_delta(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.chunk_count = 7
    upstream.script = [{"content": MOOD_REPLY}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events, run = await run_and_collect(client, direct_id, "出个方案")
            assert run["status"] == "completed", run

            pushed = "".join(payload["text"] for payload in payloads(events, "delta"))
            assert "<" not in pushed
            assert "mood" not in pushed
            assert "happy" not in pushed
            assert pushed == "好的，方案如下。\n"

            detail = await get_conversation(client, direct_id)
            reply = agent_messages(detail)[-1]
            assert reply["content"] == "好的，方案如下。"
            assert reply["mood"] == "happy"
            assert reply["status"] == "complete"

    asyncio.run(body())


def test_the_next_request_replays_the_agents_own_mood_tag(make_backend, upstream) -> None:
    """Untagged history teaches the model to drop the tag, so its own past replies keep it."""
    backend = make_backend()
    upstream.script = [{"content": MOOD_REPLY}, {"content": "第二次答复\n<mood>focused</mood>"}]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await run_and_collect(client, direct_id, "出个方案")
            await run_and_collect(client, direct_id, "再细化一下")

    asyncio.run(body())
    assert upstream.request_count == 2
    replayed = [m["content"] for m in upstream.requests[1]["messages"] if m["role"] == "assistant"]
    assert replayed == ["好的，方案如下。\n\n<mood>happy</mood>"]


@pytest.mark.parametrize("chunk_count", [2, 3, 5, 9])
def test_the_tag_is_stripped_wherever_the_chunks_break(make_backend, upstream, chunk_count) -> None:
    backend = make_backend()
    upstream.chunk_count = chunk_count
    upstream.script = [{"content": MOOD_REPLY}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events, run = await run_and_collect(client, direct_id, "再出一版")
            assert run["status"] == "completed", run
            pushed = "".join(payload["text"] for payload in payloads(events, "delta"))
            assert "<" not in pushed
            assert "mood" not in pushed
            detail = await get_conversation(client, direct_id)
            assert agent_messages(detail)[-1]["content"] == "好的，方案如下。"
            assert agent_messages(detail)[-1]["mood"] == "happy"

    asyncio.run(body())


def test_an_unknown_mood_value_is_stripped_but_stored_as_null(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.chunk_count = 4
    upstream.script = [{"content": "我的答复\n<mood>angry</mood>"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events, run = await run_and_collect(client, direct_id, "说说看")
            assert run["status"] == "completed", run
            pushed = "".join(payload["text"] for payload in payloads(events, "delta"))
            assert "<" not in pushed
            reply = agent_messages(await get_conversation(client, direct_id))[-1]
            assert reply["content"] == "我的答复"
            assert reply["mood"] is None

    asyncio.run(body())


def test_a_reply_without_a_tag_keeps_its_text_and_stores_no_mood(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "直接回答，没有标签"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            reply = agent_messages(
                await run_goal_and_detail(client, workspace["direct_conversation_id"])
            )[-1]
            assert reply["content"] == "直接回答，没有标签"
            assert reply["mood"] is None

    asyncio.run(body())


async def run_goal_and_detail(client, conversation_id: str) -> dict:
    run = await run_goal(client, conversation_id, "随便聊聊")
    assert run["status"] == "completed", run
    return await get_conversation(client, conversation_id)


def test_a_stray_less_than_sign_still_reaches_the_client(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.chunk_count = 5
    upstream.script = [{"content": "条件是 a < b 才算通过"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events, run = await run_and_collect(client, direct_id, "判断一下")
            assert run["status"] == "completed", run
            pushed = "".join(payload["text"] for payload in payloads(events, "delta"))
            assert pushed == "条件是 a < b 才算通过"
            reply = agent_messages(await get_conversation(client, direct_id))[-1]
            assert reply["content"] == "条件是 a < b 才算通过"
            assert reply["mood"] is None

    asyncio.run(body())


def test_mentions_are_parsed_from_the_text_without_the_tag(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.chunk_count = 5
    upstream.script = [
        {"content": "结论如下，@乙请复核\n<mood>proud</mood>"},
        {"content": "复核完成\n<mood>focused</mood>"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 请出方案")
            assert run["status"] == "completed", run
            assert run["hops"] == 1
            detail = await get_conversation(client, team["group"]["id"])
            designer, reviewer = agent_messages(detail)
            assert designer["author_id"] == team["designer"]["id"]
            assert designer["content"] == "结论如下，@乙请复核"
            assert designer["mentions"] == [team["reviewer"]["id"]]
            assert designer["mood"] == "proud"
            assert reviewer["author_id"] == team["reviewer"]["id"]
            assert reviewer["content"] == "复核完成"
            assert reviewer["mood"] == "focused"

    asyncio.run(body())


# ---------------------------------------------------------------------- mood stream


def test_the_stream_parser_never_leaks_a_split_tag() -> None:
    for text in (MOOD_REPLY, "只有正文", "结论：a < b 成立\n<mood>focused</mood>"):
        for cut in range(len(text) + 1):
            stream = MoodStream()
            first = stream.push(text[:cut])
            second = stream.push(text[cut:])
            pushed = first + second
            body, mood = stream.finish()
            assert "mood" not in pushed
            assert body == text.split("\n<mood>")[0]
            assert mood == ("happy" if "happy" in text else "focused" if "focused" in text else None)


def test_the_stream_parser_releases_ordinary_text_unchanged() -> None:
    stream = MoodStream()
    pushed = stream.push("条件是 a < b")
    assert pushed == "条件是 a < b"
    body, mood = stream.finish()
    assert body == "条件是 a < b"
    assert mood is None

    stream = MoodStream()
    for chunk in ["好的", "，", "就这样"]:
        assert stream.push(chunk) in ("好的", "，", "就这样")
    body, mood = stream.finish()
    assert body == "好的，就这样"
    assert mood is None


def test_the_stream_parser_drops_a_trailing_answer_marker() -> None:
    """Some models close a reply with ``<answer>done</answer>``; it is never part of the reply."""
    for text, expected, mood in (
        ("告诉我！\n\n<answer>done</answer>", "告诉我！", None),
        ("好的。\n<answer>done</answer>\n<mood>happy</mood>", "好的。", "happy"),
        ("好的。\n<mood>happy</mood>\n<answer>完成</answer>", "好的。", "happy"),
    ):
        for cut in range(len(text) + 1):
            stream = MoodStream()
            pushed = stream.push(text[:cut]) + stream.push(text[cut:])
            body, found = stream.finish()
            assert "answer" not in pushed, (cut, pushed)
            assert (body, found) == (expected, mood), cut

    # Prose that only mentions the tag mid-sentence stays exactly as written.
    stream = MoodStream()
    stream.push("用 <answer> 标签包住结论即可。")
    assert stream.finish()[0] == "用 <answer> 标签包住结论即可。"


def test_a_reply_wrapped_whole_in_answer_tags_keeps_only_its_body() -> None:
    # Seen from MiMo: the final reply, mood tag after it, enclosed in <answer>…</answer>.
    long_body = "今日美元兑人民币约 6.67，较昨日小幅回落，" * 6
    for text in (
        f"<answer>{long_body}</answer>",
        f"<answer>\n{long_body}\n</answer>\n<mood>happy</mood>",
    ):
        stream = MoodStream()
        for piece in (text[i : i + 7] for i in range(0, len(text), 7)):
            stream.push(piece)
        body, _ = stream.finish()
        assert body == long_body.strip(), body


def test_every_trailing_tag_shape_seen_from_models_is_dropped() -> None:
    # Each shape below reached users in an older build; the body and the mood must come out.
    shot = "att_b3257748794d"
    for text, mood, images in (
        ("有具体项目随时找我。\n\n<neutral></neutral>", "neutral", []),
        ("有具体项目随时找我。\n\n<neutral>neutral</neutral>", "neutral", []),
        ("有具体项目随时找我。\n\n< neutral >neutral< /neutral >", "neutral", []),
        ("有具体项目随时找我。\n\n<curious>curious</curious>", "curious", []),
        ("有具体项目随时找我。\n<happy/>", "happy", []),
        ("有具体项目随时找我。\n<mood> Sorry </mood>", "sorry", []),
        ("有具体项目随时找我。\n<mood>excited</mood>", None, []),
        (f"有具体项目随时找我。\n\n<images>{shot}</images>\n\n<neutral>neutral</neutral>", "neutral", [shot]),
        (f"有具体项目随时找我。\n<mood>happy</mood>\n<images>{shot}</images>", "happy", [shot]),
        ("有具体项目随时找我。\n<answer>done</answer>\n<neutral></neutral>", "neutral", []),
    ):
        for cut in range(len(text) + 1):
            stream = MoodStream()
            pushed = stream.push(text[:cut]) + stream.push(text[cut:])
            body, found = stream.finish()
            assert "<" not in pushed, (text, cut, pushed)
            assert (body, found, stream.images) == ("有具体项目随时找我。", mood, images), (text, cut)


def test_angle_brackets_that_are_not_trailing_tags_stay() -> None:
    for text in (
        "List<int> nums = [1, 2, 3];",
        "Map<String, int> scores = {};",
        "用 <neutral> 这个词表示中性。",
        "结尾是 <happy>开心</happy>",
        "a <b> c",
    ):
        stream = MoodStream()
        pushed = "".join(stream.push(text[i : i + 3]) for i in range(0, len(text), 3))
        body, mood = stream.finish()
        assert (body, mood) == (text, None), text
        assert text.startswith(pushed)


def test_a_mood_written_as_a_plain_last_line_is_dropped() -> None:
    # Seen from a member: the tag without its angle brackets, on the reply's last line.
    for text, mood in (
        ("价格维度单列。\n\n@mood focused", "focused"),
        ("价格维度单列。\nmood: happy", "happy"),
        ("价格维度单列。\nMood：Sorry ", "sorry"),
    ):
        for cut in range(len(text) + 1):
            stream = MoodStream()
            pushed = stream.push(text[:cut]) + stream.push(text[cut:])
            body, found = stream.finish()
            assert "mood" not in pushed.lower(), (text, cut, pushed)
            assert (body, found) == ("价格维度单列。", mood), (text, cut)

    # Both forms at once: the final body loses both (the stream may show the line until then).
    assert split_trailing_tags("价格维度单列。\n@mood focused\n<mood>focused</mood>") == (
        "价格维度单列。",
        "focused",
        [],
        None,
    )

    # Prose that merely ends on the word, or names no known mood, stays as written.
    for text in ("今天的 mood 不错", "我的 mood: excited", "a good mood"):
        stream = MoodStream()
        pushed = "".join(stream.push(text[i : i + 2]) for i in range(0, len(text), 2))
        assert stream.finish() == (text, None), text
        assert text.startswith(pushed)


def test_the_v11_migration_cleans_tags_older_builds_stored(tmp_path) -> None:
    from app.store import SCHEMA_VERSION, Store

    path = tmp_path / "cubes.sqlite3"
    offline = Store(path)
    offline.initialize()
    direct = offline.direct_conversation()
    supervisor = offline.supervisor()
    assert direct is not None and supervisor is not None
    shot = offline.save_attachment(b"\xff\xd8\xff\xe0" + b"\x00" * 24, "image/jpeg")

    def reply(content: str, mood: str | None = None) -> str:
        return offline.append_message(
            conversation_id=direct["id"],
            author_id=supervisor["id"],
            author_name=supervisor["name"],
            avatar_seed=supervisor["avatar_seed"],
            kind="agent",
            content=content,
            status="complete",
            mood=mood,
        )["id"]

    tagged = reply("查好了。\n\n<neutral></neutral>")
    with_image = reply(f"截图如下：\n\n<images>{shot['id']}</images>\n\n<curious>curious</curious>")
    kept_mood = reply("好的。\n<happy></happy>", mood="proud")
    code = reply("Map<String, int> scores = {};")
    plain = reply("价格维度单列。\n\n@mood focused", mood="focused")
    offline.close()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE meta SET value = '10' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    migrated = Store(path)
    migrated.initialize()
    try:
        cleaned = migrated.get_message(tagged)
        assert (cleaned["content"], cleaned["mood"]) == ("查好了。", "neutral")
        pictured = migrated.get_message(with_image)
        assert (pictured["content"], pictured["mood"]) == ("截图如下：", "curious")
        assert [image["id"] for image in pictured["attachments"]] == [shot["id"]]
        # A mood the reply already had is never overwritten.
        assert migrated.get_message(kept_mood)["mood"] == "proud"
        assert migrated.get_message(kept_mood)["content"] == "好的。"
        assert migrated.get_message(code)["content"] == "Map<String, int> scores = {};"
        assert migrated.get_message(plain)["content"] == "价格维度单列。"
    finally:
        migrated.close()
    conn = sqlite3.connect(str(path))
    try:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        conn.close()
    assert version == str(SCHEMA_VERSION)


ECHOED = (
    "已经有不少信息了，我再搜一下。\n\n<此前用过的工具>\n"
    "- web_search(怪物猎人 凌越) → 找到 5 条结果：1. 新预告 https://www.3dmgame.com/ne…\n"
    "</此前用过的工具>"
)


def test_a_copied_tool_digest_never_stays_in_a_reply() -> None:
    # Seen live: a model replaying its past replies wrote the digest block into a new one.
    stream = MoodStream()
    for piece in (ECHOED[:30], ECHOED[30:90], ECHOED[90:], "\n<mood>focused</mood>"):
        stream.push(piece)
    assert stream.finish() == ("已经有不少信息了，我再搜一下。", "focused")
    middle = MoodStream()
    middle.push(f"{ECHOED}\n\n查到了，结论如下。")
    assert middle.finish() == ("已经有不少信息了，我再搜一下。\n\n查到了，结论如下。", None)


def test_the_v12_migration_removes_digests_older_replies_copied(tmp_path) -> None:
    from app.store import Store

    path = tmp_path / "cubes.sqlite3"
    offline = Store(path)
    offline.initialize()
    direct = offline.direct_conversation()
    supervisor = offline.supervisor()
    assert direct is not None and supervisor is not None
    copied = offline.append_message(
        conversation_id=direct["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content=ECHOED,
        status="complete",
    )["id"]
    offline.close()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    migrated = Store(path)
    migrated.initialize()
    try:
        assert migrated.get_message(copied)["content"] == "已经有不少信息了，我再搜一下。"
    finally:
        migrated.close()


LONE = "核心教训有三点。风险不可消除，但响应速度可以设计。\n<neutral>"


def test_a_lone_unclosed_mood_tag_is_the_mood_not_text() -> None:
    # Seen live: replies ended in a bare "<neutral>" / "<focused>" with no closing tag.
    stream = MoodStream()
    shown = "".join(stream.push(piece) for piece in (LONE[:20], LONE[20:-4], LONE[-4:]))
    assert "<" not in shown
    assert stream.finish() == ("核心教训有三点。风险不可消除，但响应速度可以设计。", "neutral")
    # A lone tag that is not a mood is ordinary text and stays.
    code = MoodStream()
    code.push("返回值的类型是 List<int>")
    assert code.finish() == ("返回值的类型是 List<int>", None)


def test_the_v14_migration_cleans_lone_mood_tags_already_stored(tmp_path) -> None:
    from app.store import Store

    path = tmp_path / "cubes.sqlite3"
    offline = Store(path)
    offline.initialize()
    direct = offline.direct_conversation()
    supervisor = offline.supervisor()
    assert direct is not None and supervisor is not None
    stored = offline.append_message(
        conversation_id=direct["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content=LONE.replace("<neutral>", "<focused>"),
        status="complete",
    )["id"]
    offline.close()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE meta SET value = '13' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    migrated = Store(path)
    migrated.initialize()
    try:
        message = migrated.get_message(stored)
        assert message["content"] == "核心教训有三点。风险不可消除，但响应速度可以设计。"
        assert message["mood"] == "focused"
    finally:
        migrated.close()


def test_every_system_prompt_carries_the_mood_rule(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [{"content": "主管的答复"}, {"content": "成员的答复"}, {"content": "群里的答复"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            await run_goal(client, direct_id, "先和主管说一句")

            member_direct = await client.post(f"/agents/{team['designer']['id']}/direct")
            assert member_direct.status_code == 200, member_direct.text
            await run_goal(client, member_direct.json()["id"], "私下问一句")

            await run_goal(client, team["group"]["id"], "@甲 在群里说一句")

    asyncio.run(body())

    assert upstream.request_count == 3
    prompts = [system_prompt(request) for request in upstream.requests]
    for prompt in prompts:
        assert MOOD_RULE in prompt
        assert "<mood>值</mood>" in prompt
        for mood in MOODS:
            assert mood in prompt
    assert MOOD_RULE in SUPERVISOR_SYSTEM
    assert MOOD_RULE in MEMBER_SYSTEM
    assert MOOD_RULE in MEMBER_DIRECT_SYSTEM


# ------------------------------------------------------------------------- appearance


def test_appearance_patch_round_trip_and_validation(make_backend) -> None:
    backend = make_backend()
    member = store_of(backend).create_agent(name="甲", instructions="出方案", kind="member")
    appearance = {"shape": "pebble", "color": "moss"}

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.patch(f"/agents/{member['id']}", json={"appearance": appearance})
            assert response.status_code == 200, response.text
            assert response.json()["appearance"] == appearance

            workspace = await get_workspace(client)
            stored = next(agent for agent in workspace["agents"] if agent["id"] == member["id"])
            assert stored["appearance"] == appearance
            # Members without an appearance keep it null so clients can derive one.
            others = [agent for agent in workspace["agents"] if agent["id"] != member["id"]]
            assert all(agent["appearance"] is None for agent in others)

            # A free body colour and a chosen eye colour round-trip, normalised to lowercase.
            free = {"shape": "pebble", "color": "#12AB9F", "eye": "#FFF0E0"}
            custom = await client.patch(f"/agents/{member['id']}", json={"appearance": free})
            assert custom.status_code == 200, custom.text
            assert custom.json()["appearance"] == {"shape": "pebble", "color": "#12ab9f", "eye": "#fff0e0"}
            # A null eye goes back to the automatic eye, and the key is left out.
            auto = await client.patch(
                f"/agents/{member['id']}", json={"appearance": {**appearance, "eye": None}}
            )
            assert auto.json()["appearance"] == appearance

            for broken in (
                {"shape": "nope", "color": "moss"},
                {"shape": "pebble", "color": "mustard"},
                {"shape": "pebble", "color": "#12ab9"},
                {"shape": "pebble", "color": "moss", "eye": "black"},
                {"shape": "pebble", "color": "moss", "eye": "#12345g"},
                {"shape": "pebble", "color": "moss", "expression": "angry"},
                {"shape": "pebble"},
                {},
            ):
                rejected = await client.patch(
                    f"/agents/{member['id']}", json={"appearance": broken}
                )
                assert rejected.status_code == 422, (broken, rejected.text)
                assert rejected.json()["detail"].endswith("。")

            # An explicit null resets the stored appearance.
            reset = await client.patch(f"/agents/{member['id']}", json={"appearance": None})
            assert reset.status_code == 200, reset.text
            assert reset.json()["appearance"] is None

            # The at-least-one-field rule still applies to an empty body.
            empty = await client.patch(f"/agents/{member['id']}", json={})
            assert empty.status_code == 422, empty.text
            assert "至少" in empty.json()["detail"]

            # Name and appearance can travel together.
            both = await client.patch(
                f"/agents/{member['id']}", json={"name": "乙", "appearance": appearance}
            )
            assert both.status_code == 200, both.text
            assert both.json()["name"] == "乙"
            assert both.json()["appearance"] == appearance

            assert (await client.patch("/agents/a_missing", json={"appearance": appearance})).status_code == 404

    asyncio.run(body())


def test_a_retired_expression_is_accepted_and_dropped(make_backend) -> None:
    """An old client still sends ``expression``; it must not 422 and must not be stored."""
    backend = make_backend()
    member = store_of(backend).create_agent(name="甲", instructions="出方案", kind="member")

    async def body() -> None:
        async with client_for(backend) as client:
            sent = {"shape": "hexagon", "color": "plum", "expression": "sleepy"}
            response = await client.patch(f"/agents/{member['id']}", json={"appearance": sent})
            assert response.status_code == 200, response.text
            assert response.json()["appearance"] == {"shape": "hexagon", "color": "plum"}

        # The retired key never reaches the row, so nothing can read it back.
        connection = sqlite3.connect(str(backend.settings.database_path))
        try:
            raw = connection.execute(
                "SELECT appearance FROM agents WHERE id = ?", (member["id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        assert json.loads(raw) == {"shape": "hexagon", "color": "plum"}

        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            stored = next(agent for agent in workspace["agents"] if agent["id"] == member["id"])
            assert "expression" not in stored["appearance"]

    asyncio.run(body())


def test_a_retired_shape_id_is_mapped(make_backend) -> None:
    """bean/cloud/drop/lean are gone: a stored or newly sent id resolves to its replacement."""
    backend = make_backend()
    member = store_of(backend).create_agent(name="甲", instructions="出方案", kind="member")

    async def body() -> None:
        connection = sqlite3.connect(str(backend.settings.database_path))
        try:
            connection.execute(
                "UPDATE agents SET appearance = ? WHERE id = ?",
                (json.dumps({"shape": "cloud", "color": "jade", "expression": "sleepy"}), member["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            stored = next(agent for agent in workspace["agents"] if agent["id"] == member["id"])
            assert stored["appearance"] == {"shape": "clover", "color": "jade"}

            # An old client that posts a retired id keeps working too.
            for legacy, current in (("bean", "blob"), ("cloud", "clover"), ("drop", "teardrop"), ("lean", "arch")):
                sent = {"shape": legacy, "color": "jade", "expression": "wink"}
                response = await client.patch(f"/agents/{member['id']}", json={"appearance": sent})
                assert response.status_code == 200, (legacy, response.text)
                assert response.json()["appearance"] == {"shape": current, "color": "jade"}

    asyncio.run(body())


def test_a_corrupt_stored_appearance_reads_as_null(make_backend) -> None:
    backend = make_backend()
    member = store_of(backend).create_agent(name="甲", instructions="出方案", kind="member")

    async def body() -> None:
        connection = sqlite3.connect(str(backend.settings.database_path))
        try:
            connection.execute(
                "UPDATE agents SET appearance = ? WHERE id = ?", ("{not json", member["id"])
            )
            connection.commit()
        finally:
            connection.close()
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            stored = next(agent for agent in workspace["agents"] if agent["id"] == member["id"])
            assert stored["appearance"] is None

        connection = sqlite3.connect(str(backend.settings.database_path))
        try:
            connection.execute(
                "UPDATE agents SET appearance = ? WHERE id = ?",
                (json.dumps({"shape": "nope", "color": "moss"}), member["id"]),
            )
            connection.commit()
        finally:
            connection.close()
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            stored = next(agent for agent in workspace["agents"] if agent["id"] == member["id"])
            assert stored["appearance"] is None

    asyncio.run(body())


def test_parse_appearance_accepts_only_the_catalog() -> None:
    assert parse_appearance(None) is None
    assert parse_appearance("") is None
    assert parse_appearance("nope") is None
    assert parse_appearance("[1, 2]") is None
    assert parse_appearance('{"shape": "round"}') is None
    assert parse_appearance('{"shape": "nope", "color": "iris"}') is None
    assert parse_appearance('{"shape": "round", "color": "mustard"}') is None
    good = '{"shape": "star", "color": "iris"}'
    assert parse_appearance(good) == {"shape": "star", "color": "iris"}
    free = '{"shape": "star", "color": "#AABBCC", "eye": "#112233"}'
    assert parse_appearance(free) == {"shape": "star", "color": "#aabbcc", "eye": "#112233"}
    # A broken eye colour costs only the eye: the body renders with the automatic one.
    assert parse_appearance('{"shape": "star", "color": "iris", "eye": "red"}') == {"shape": "star", "color": "iris"}
    assert parse_appearance('{"shape": "star", "color": "#abc"}') is None
    # A row written by an older build keeps its shape and colour, and loses the expression.
    old = '{"shape": "drop", "color": "iris", "expression": "wink"}'
    assert parse_appearance(old) == {"shape": "teardrop", "color": "iris"}
