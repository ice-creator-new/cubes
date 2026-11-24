"""Long-term memory: the REST contract, the tools, the prompt block and the extraction.

Everything here runs over real HTTP against a temporary database, and the extraction call
is scripted through the isolated upstream so the tests assert what the model was asked and
what the store then kept.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import httpx

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    collect_events,
    event_names,
    first_payload,
    get_conversation,
    get_workspace,
    run_goal,
    store_of,
    system_prompt,
    tool_names,
)

DEFAULTS = {"paused": False, "auto_extract": True, "core_chars": 1200}
# Extraction waits for a quiet conversation; tests that read its result shorten the wait.
QUICK = {"memory_idle_seconds": 0.2}


def seed_member(backend, name: str, instructions: str = "做研究") -> dict:
    return store_of(backend).create_agent(name=name, instructions=instructions, kind="member")


def member_chat(backend, agent_id: str) -> str:
    conversation = store_of(backend).ensure_direct_conversation(agent_id)
    assert conversation is not None
    return conversation["id"]


async def state(client: httpx.AsyncClient) -> dict:
    response = await client.get("/memories")
    assert response.status_code == 200, response.text
    return response.json()


async def set_memory(client: httpx.AsyncClient, **patch: Any) -> dict:
    response = await client.patch("/memories/settings", json=patch)
    assert response.status_code == 200, response.text
    return response.json()


async def wait_for_state(
    client: httpx.AsyncClient,
    predicate: Callable[[dict], bool],
    *,
    timeout: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = await state(client)
        if predicate(current):
            return current
        await asyncio.sleep(0.05)
    raise AssertionError(f"memory state never matched: {json.dumps(await state(client), ensure_ascii=False)}")


def extraction_requests(upstream) -> list[dict]:
    """The calls that asked for no tools at all: only a memory extraction does that."""
    return upstream.memory_requests


def user_message(detail: dict) -> dict:
    return next(message for message in detail["messages"] if message["kind"] == "user")


# ------------------------------------------------------------------------- contract


def test_crud_keeps_a_timeline_and_a_content_edit_makes_a_new_row(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/memories",
                json={"content": "用户每天早上喝咖啡", "topic": "饮食", "scope": "user"},
            )
            assert created.status_code == 201, created.text
            memory = created.json()
            assert memory["scope"] == "user"
            assert memory["agent_id"] is None
            assert memory["topic"] == "饮食"
            assert memory["content"] == "用户每天早上喝咖啡"
            assert memory["pinned"] is False
            assert memory["status"] == "active"
            assert memory["superseded_by"] is None
            assert memory["origin"] == "user"
            assert memory["author_id"] is None
            assert memory["source_conversation_id"] is None
            assert memory["source_message_ids"] == []
            assert memory["valid_from"] == memory["created_at"]
            assert memory["valid_to"] is None

            whole = await state(client)
            assert whole["settings"] == DEFAULTS
            assert [row["id"] for row in whole["memories"]] == [memory["id"]]

            # A pin alone is the same row.
            pinned = await client.patch(
                f"/memories/{memory['id']}", json={"pinned": True}
            )
            assert pinned.status_code == 200, pinned.text
            assert pinned.json()["id"] == memory["id"]
            assert pinned.json()["pinned"] is True
            assert len((await state(client))["memories"]) == 1

            # A topic alone is the same row too.
            retopic = await client.patch(
                f"/memories/{memory['id']}", json={"topic": "口味"}
            )
            assert retopic.json()["id"] == memory["id"]
            assert retopic.json()["topic"] == "口味"

            # A content edit answers a new row; the old one keeps the timeline.
            edited = await client.patch(
                f"/memories/{memory['id']}", json={"content": "用户只喝美式咖啡"}
            )
            assert edited.status_code == 200, edited.text
            replacement = edited.json()
            assert replacement["id"] != memory["id"]
            assert replacement["content"] == "用户只喝美式咖啡"
            assert replacement["topic"] == "口味"
            assert replacement["pinned"] is True
            assert replacement["scope"] == "user"
            assert replacement["agent_id"] is None
            assert replacement["superseded_by"] is None
            assert replacement["valid_to"] is None

            rows = (await state(client))["memories"]
            assert [row["id"] for row in rows] == [replacement["id"], memory["id"]]
            old = rows[1]
            assert old["status"] == "retired"
            assert old["superseded_by"] == replacement["id"]
            assert old["valid_to"] is not None
            assert old["content"] == "用户每天早上喝咖啡"

            # A delete is physical, retired or not.
            deleted = await client.delete(f"/memories/{memory['id']}")
            assert deleted.status_code == 204
            assert [row["id"] for row in (await state(client))["memories"]] == [
                replacement["id"]
            ]
            gone = await client.delete(f"/memories/{replacement['id']}")
            assert gone.status_code == 204
            assert (await state(client))["memories"] == []

    asyncio.run(body())


def test_unknown_ids_and_impossible_bodies_are_refused_in_chinese(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            missing = await client.patch("/memories/mem_nope", json={"pinned": True})
            assert missing.status_code == 404
            assert missing.json()["detail"] == "记忆不存在。"
            assert (await client.delete("/memories/mem_nope")).status_code == 404

            no_agent = await client.post(
                "/memories", json={"content": "用户喜欢简洁的回答", "scope": "agent"}
            )
            assert no_agent.status_code == 422
            detail = no_agent.json()["detail"]
            assert "agent_id" in detail and detail.endswith("。")

            bad_scope = await client.post(
                "/memories", json={"content": "用户喜欢简洁的回答", "scope": "global"}
            )
            assert bad_scope.status_code == 422
            assert bad_scope.json()["detail"].endswith("。")
            assert "user、agent、team 或 group" in bad_scope.json()["detail"]

            blank = await client.post(
                "/memories", json={"content": "   ", "scope": "user"}
            )
            assert blank.status_code == 422

            empty_patch = await client.patch(
                "/memories/mem_any", json={}
            )
            assert empty_patch.status_code == 422

            empty_settings = await client.patch("/memories/settings", json={})
            assert empty_settings.status_code == 422

    asyncio.run(body())


def test_a_change_publishes_the_whole_memory_state(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(name == "memories" for name, _ in events),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.2)
            created = await client.post(
                "/memories",
                json={"content": "用户住在杭州", "topic": "城市", "scope": "user"},
            )
            assert created.status_code == 201
            events = await reader
            published = first_payload(events, "memories")
            assert published["settings"] == DEFAULTS
            assert [row["id"] for row in published["memories"]] == [created.json()["id"]]
            assert published["memories"][0]["content"] == "用户住在杭州"

            # The switch answers the whole state and rides the same event.
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(
                        name == "memories"
                        and payload["settings"]["paused"] is True
                        for name, payload in events
                    ),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.2)
            paused = await client.patch("/memories/settings", json={"paused": True})
            assert paused.status_code == 200
            assert paused.json()["settings"]["paused"] is True
            assert len(paused.json()["memories"]) == 1
            events = await reader
            assert "memories" in event_names(events)

    asyncio.run(body())


def test_a_deleted_conversation_leaves_its_memories_without_a_source(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = seed_member(backend, "阿甲")
    group = store.create_conversation(
        kind="group",
        title="项目组",
        purpose="一起做记忆",
        member_ids=[supervisor["id"], member["id"]],
    )
    store.create_memory(
        content="团队决定统一用 SQLite",
        topic="技术栈",
        scope="team",
        origin="extract",
        author_id=supervisor["id"],
        source_conversation_id=group["id"],
        source_message_ids=["m_one"],
    )

    async def body() -> None:
        async with client_for(backend) as client:
            removed = await client.delete(f"/conversations/{group['id']}")
            assert removed.status_code == 204
            rows = (await state(client))["memories"]
            assert len(rows) == 1
            assert rows[0]["content"] == "团队决定统一用 SQLite"
            assert rows[0]["source_conversation_id"] is None
            assert rows[0]["source_message_ids"] == ["m_one"]

    asyncio.run(body())


# ------------------------------------------------------------------------------ tools


def test_recall_memory_finds_two_characters_inside_a_sentence(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    store.create_memory(
        content="我每天早上喝咖啡", topic="作息", scope="user", origin="user"
    )
    store.create_memory(
        content="用户喜欢爬山", topic="爱好", scope="user", origin="user"
    )
    other = seed_member(backend, "阿甲")
    store.create_memory(
        content="阿甲的私有经验：咖啡机在茶水间",
        topic="经验",
        scope="agent",
        agent_id=other["id"],
        origin="agent",
        author_id=other["id"],
    )
    upstream.script = [
        tool_calls(
            call("call_1", "recall_memory", {"query": "咖啡"}),
            call("call_2", "recall_memory", {"query": "糖"}),
            call("call_3", "recall_memory", {"query": "喝咖啡"}),
        ),
        {"content": "我查过了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await set_memory(client, auto_extract=False)
            workspace = await get_workspace(client)
            await run_goal(client, workspace["direct_conversation_id"], "想想我平时喝什么")
            texts = [
                message["content"]
                for message in upstream.request(1)["messages"]
                if message.get("role") == "tool"
            ]
            assert len(texts) == 3
            # A bigram index finds 咖啡 inside 我每天早上喝咖啡, and never leaks another
            # bot's own experience.
            assert "我每天早上喝咖啡" in texts[0]
            assert "阿甲的私有经验" not in texts[0]
            assert "喝咖啡" in texts[2]
            # A one-character query falls back to a substring scan.
            assert "没有找到" in texts[1]

    asyncio.run(body())


def test_remember_and_forget_tools_speak_and_write(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "阿甲")
    conversation_id = member_chat(backend, member["id"])
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "remember",
                {"content": "用户不吃香菜", "topic": "饮食", "scope": "user"},
            )
        ),
        {"content": "记住了。"},
        tool_calls(call("call_2", "forget", {"topic": "饮食"})),
        {"content": "忘了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await set_memory(client, auto_extract=False)
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(name == "memories" for name, _ in events),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.2)
            run = await run_goal(client, conversation_id, "记住我不吃香菜")
            assert run["status"] == "completed", run
            events = await reader
            published = first_payload(events, "memories")
            row = next(
                memory for memory in published["memories"] if memory["topic"] == "饮食"
            )
            assert row["content"] == "用户不吃香菜"
            assert row["origin"] == "agent"
            assert row["author_id"] == member["id"]
            assert row["scope"] == "user"
            assert row["status"] == "active"
            assert row["source_conversation_id"] == conversation_id
            detail = await get_conversation(client, conversation_id)
            trigger = user_message(detail)
            assert row["source_message_ids"] == [trigger["id"]]

            action = next(
                message
                for message in action_messages(detail)
                if message["content"].startswith("记住：")
            )
            assert "用户不吃香菜" in action["content"]

            # Forgetting is soft: the row stays, retired and without a successor.
            run = await run_goal(client, conversation_id, "忘掉我不吃香菜这件事")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, conversation_id)
            summaries = [message["content"] for message in action_messages(detail)]
            assert any(text.startswith("忘记：") for text in summaries), summaries
            rows = (await state(client))["memories"]
            target = next(memory for memory in rows if memory["id"] == row["id"])
            assert target["status"] == "retired"
            assert target["superseded_by"] is None
            assert target["valid_to"] is not None

    asyncio.run(body())


def test_remember_on_the_same_topic_supersedes_the_row(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(
            call("call_1", "remember", {"content": "用户喜欢喝咖啡", "topic": "饮品"}),
            call("call_2", "remember", {"content": "用户现在只喝美式", "topic": "饮品"}),
        ),
        {"content": "记住了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await set_memory(client, auto_extract=False)
            workspace = await get_workspace(client)
            workspace_direct = workspace["direct_conversation_id"]
            await run_goal(client, workspace_direct, "记一下我的饮品偏好")
            rows = (await state(client))["memories"]
            lively = [row for row in rows if row["status"] == "active"]
            assert len(lively) == 1
            assert lively[0]["content"] == "用户现在只喝美式"
            retired = [row for row in rows if row["status"] == "retired"]
            assert len(retired) == 1
            assert retired[0]["superseded_by"] == lively[0]["id"]

    asyncio.run(body())


# ------------------------------------------------------------------------- read path


def test_the_memory_block_orders_and_cuts_whole_lines(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    for index in range(20):
        store.create_memory(
            content=f"用户偏好第 {index} 条：" + "每天固定时间散步" * 10,
            topic=f"偏好{index}",
            scope="user",
            origin="user",
        )
    store.create_memory(
        content="用户的名字叫小林",
        topic="称呼",
        scope="user",
        origin="user",
        pinned=True,
    )
    store.create_memory(
        content="团队决定统一用 SQLite", topic="技术栈", scope="team", origin="user"
    )
    upstream.script = [{"content": "好的。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            await set_memory(client, auto_extract=False)
            workspace = await get_workspace(client)
            await run_goal(client, workspace["direct_conversation_id"], "继续吧")
            prompt = system_prompt(upstream.request(0))
            assert "<memory>" in prompt
            assert "关于用户：" in prompt
            block = prompt[prompt.index("<memory>") : prompt.index("</memory>")]
            lines = [line for line in block.splitlines() if line.startswith("- ")]
            # Pinned first, and the budget keeps only rows that fit whole.
            assert lines[0] == "- 用户的名字叫小林"
            assert 2 <= len(lines) < 21
            assert "还有" in block and "recall_memory" in block
            # Every kept line is one whole seeded fact, never a cut sentence.
            seeded = {"用户的名字叫小林", "团队决定统一用 SQLite"} | {
                f"用户偏好第 {index} 条：" + "每天固定时间散步" * 10
                for index in range(20)
            }
            fact_lines = [line[2:] for line in lines]
            assert all(content in seeded for content in fact_lines), fact_lines
            dropped = int(block.split("还有 ")[1].split(" 条")[0])
            assert len(fact_lines) + dropped == len(seeded)
            # The budget is measured on the fact lines, and the block stays inside it.
            assert sum(len(content) + 3 for content in fact_lines) <= 1200

    asyncio.run(body())


def test_agent_memories_are_only_read_by_their_bot(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    a = seed_member(backend, "阿甲")
    b = seed_member(backend, "阿乙")
    store.create_memory(
        content="阿甲的经验：测试先跑单个文件",
        topic="经验",
        scope="agent",
        agent_id=a["id"],
        origin="agent",
        author_id=a["id"],
    )
    upstream.script = [{"content": "收到。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            await set_memory(client, auto_extract=False)
            await run_goal(client, member_chat(backend, a["id"]), "开始吧")
            mine = system_prompt(upstream.request(0))
            assert "你的经验：" in mine
            assert "阿甲的经验：测试先跑单个文件" in mine

            await run_goal(client, member_chat(backend, b["id"]), "你那边怎么样")
            theirs = system_prompt(upstream.request(1))
            assert "阿甲的经验" not in theirs
            assert "<memory>" not in theirs

    asyncio.run(body())


def test_auto_recall_adds_related_rows_the_block_did_not_show(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    # Older and far longer than every other row, so the resident budget can never fit it;
    # the search for this very message has to bring it back.
    store.create_memory(
        content="杭州的项目负责人是老王，" + "下午的会不要打断他，" * 30,
        topic="杭州",
        scope="user",
        origin="user",
    )
    for index in range(25):
        store.create_memory(
            content=f"用户偏好第 {index} 条：" + "每天固定时间散步" * 12,
            topic=f"偏好{index}",
            scope="user",
            origin="user",
        )
    third = seed_member(backend, "阿丙")

    async def body() -> None:
        async with client_for(backend) as client:
            await set_memory(client, auto_extract=False)
            upstream.script = [{"content": "好的。"}]
            await run_goal(client, member_chat(backend, third["id"]), "杭州那边的项目负责人是谁")
            prompt = system_prompt(upstream.request(0))
            assert "<memory>" in prompt
            assert "还有" in prompt
            # The row was cut from 关于用户, and the search for this very message brings it
            # back in its own section — once, not twice.
            assert "相关：" in prompt
            assert prompt.count("杭州的项目负责人是老王") == 1
            assert prompt.index("杭州的项目负责人是老王") > prompt.index("相关：")

    asyncio.run(body())


# ------------------------------------------------------------------------ write path


def test_extraction_applies_add_update_delete_and_noop(make_backend, upstream) -> None:
    backend = make_backend(**QUICK)
    store = store_of(backend)
    supervisor = store.supervisor()
    city = store.create_memory(
        content="用户住在上海", topic="城市", scope="user", origin="user"
    )
    job = store.create_memory(
        content="用户在美团工作", topic="工作", scope="user", origin="user"
    )
    upstream.script = [{"content": "好的，我记下了。"}]
    upstream.memory_script = [
        {
            "content": json.dumps(
                [
                    {
                        "op": "add",
                        "topic": "饮食",
                        "content": "用户每天早上喝咖啡",
                        "scope": "user",
                    },
                    {
                        "op": "update",
                        "id": city["id"],
                        "topic": "城市",
                        "content": "用户搬到了杭州",
                        "scope": "user",
                    },
                    {"op": "delete", "id": job["id"]},
                    {"op": "noop"},
                ],
                ensure_ascii=False,
            )
        }
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            conversation_id = workspace["direct_conversation_id"]
            run = await run_goal(
                client, conversation_id, "上海那边的工作先放一放，我最近搬到杭州了"
            )
            assert run["status"] == "completed", run
            rows = await wait_for_state(
                client,
                lambda current: any(
                    row["topic"] == "城市" and row["content"] == "用户搬到了杭州"
                    for row in current["memories"]
                ),
            )
            detail = await get_conversation(client, conversation_id)
            reply = next(
                message
                for message in detail["messages"]
                if message["kind"] == "agent" and message["content"] == "好的，我记下了。"
            )
            added = next(
                row for row in rows["memories"] if row["topic"] == "饮食"
            )
            assert added["origin"] == "extract"
            assert added["author_id"] == supervisor["id"]
            assert added["source_conversation_id"] == conversation_id
            assert added["source_message_ids"] == [reply["id"]]
            assert added["scope"] == "user"
            assert added["agent_id"] is None

            moved = next(
                row
                for row in rows["memories"]
                if row["topic"] == "城市" and row["content"] == "用户搬到了杭州"
            )
            old_city = next(row for row in rows["memories"] if row["id"] == city["id"])
            assert old_city["status"] == "retired"
            assert old_city["superseded_by"] == moved["id"]
            assert moved["origin"] == "extract"

            forgotten = next(row for row in rows["memories"] if row["id"] == job["id"])
            assert forgotten["status"] == "retired"
            assert forgotten["superseded_by"] is None

            # Only the named rows changed: the noop wrote nothing.
            assert len(rows["memories"]) == 4
            assert len(extraction_requests(upstream)) == 1
            extraction = extraction_requests(upstream)[0]
            assert extraction["model"]
            joined = json.dumps(extraction["messages"], ensure_ascii=False)
            assert "我最近搬到杭州了" in joined
            assert city["id"] in joined and job["id"] in joined
            assert "长期记忆" in extraction["messages"][0]["content"]

    asyncio.run(body())


def test_extraction_sees_rows_the_search_would_miss(make_backend, upstream) -> None:
    # Seen live: the bot had just remembered 「用户住在杭州」 under 居住地, and extraction
    # added the same fact again under 居住城市 because only search hits were candidates.
    backend = make_backend(**QUICK)
    store = store_of(backend)
    known = store.create_memory(content="用户住在杭州。", topic="居住地", scope="user", origin="agent")
    upstream.script = [{"content": "好的。"}]
    upstream.memory_script = [{"content": "[{\"op\": \"noop\"}]"}]

    async def body() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, conversation_id, "嗯，就这样定了，下周三出发")
            assert run["status"] == "completed", run
            for _ in range(200):
                if extraction_requests(upstream):
                    break
                await asyncio.sleep(0.02)

    asyncio.run(body())
    sent = json.dumps(extraction_requests(upstream)[0]["messages"], ensure_ascii=False)
    assert known["id"] in sent


def test_extraction_runs_once_per_run_and_never_twice(make_backend, upstream) -> None:
    backend = make_backend(**QUICK)
    store = store_of(backend)
    upstream.script = [{"content": "好的。"}]
    upstream.memory_script = [
        {
            "content": json.dumps(
                [{"op": "add", "topic": "饮食", "content": "用户每天喝咖啡", "scope": "user"}],
                ensure_ascii=False,
            )
        }
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            conversation_id = workspace["direct_conversation_id"]
            run = await run_goal(client, conversation_id, "我每天都要喝咖啡")
            rows = await wait_for_state(
                client, lambda current: len(current["memories"]) == 1
            )
            assert rows["memories"][0]["content"] == "用户每天喝咖啡"
            assert len(extraction_requests(upstream)) == 1

            # A restart that somehow finalized the same run again cannot repeat it.
            from app.agent.run_state import ActiveRun

            detail = await get_conversation(client, conversation_id)
            reply = next(
                message
                for message in detail["messages"]
                if message["kind"] == "agent"
            )
            trigger = user_message(detail)
            engine = backend.app.state.engine
            engine._schedule_memory_extraction(
                ActiveRun(
                    run_id=run["id"],
                    conversation_id=conversation_id,
                    trigger_message_id=trigger["id"],
                    replies=[(store.supervisor()["id"], reply["id"])],
                ),
                "completed",
            )
            await asyncio.sleep(0.6)
            assert len(extraction_requests(upstream)) == 1
            assert len((await state(client))["memories"]) == 1
            # The claim is persisted, so it survives a restart too.
            assert store.claim_memory_run(run["id"]) is False

    asyncio.run(body())


def test_turns_of_one_conversation_are_read_together_once_it_goes_quiet(make_backend, upstream) -> None:
    backend = make_backend(memory_idle_seconds=1.0)
    upstream.script = [{"content": "记下了。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            for text in ("我住在杭州", "我每天早上喝美式", "周末一般去爬山"):
                await run_goal(client, conversation_id, text)
            # Still talking: each turn pushed the quiet period back, so nothing was sent yet.
            assert extraction_requests(upstream) == []
            for _ in range(200):
                if extraction_requests(upstream):
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)

    asyncio.run(body())
    requests = extraction_requests(upstream)
    assert len(requests) == 1
    sent = requests[0]["messages"][1]["content"]
    assert sent.index("我住在杭州") < sent.index("我每天早上喝美式") < sent.index("周末一般去爬山")


def test_a_full_batch_is_read_without_waiting_for_quiet(make_backend, upstream) -> None:
    backend = make_backend(memory_idle_seconds=60.0, memory_batch_turns=2)
    upstream.script = [{"content": "好。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, conversation_id, "我住在杭州")
            await asyncio.sleep(0.3)
            assert extraction_requests(upstream) == []
            await run_goal(client, conversation_id, "我在做前端开发")
            for _ in range(200):
                if extraction_requests(upstream):
                    break
                await asyncio.sleep(0.02)

    asyncio.run(body())
    assert len(extraction_requests(upstream)) == 1


def test_acknowledgements_and_turns_that_already_remembered_are_not_read(make_backend, upstream) -> None:
    backend = make_backend(**QUICK)
    upstream.script = [
        {"content": "不客气。"},
        tool_calls(call("call_r", "remember", {"content": "用户住在杭州", "topic": "城市"})),
        {"content": "记住了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, conversation_id, "好的，谢谢你啦！")
            await run_goal(client, conversation_id, "记住，我住在杭州")
            await asyncio.sleep(0.8)

    asyncio.run(body())
    assert extraction_requests(upstream) == []


def test_turns_parked_before_a_restart_are_read_after_it(make_backend, upstream) -> None:
    backend = make_backend(memory_idle_seconds=0.5)
    upstream.script = [{"content": "好。"}]

    async def first() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, conversation_id, "我住在杭州")

    asyncio.run(first())
    backend.restart()  # inside the quiet period: the timer dies, the parked row stays
    assert extraction_requests(upstream) == []

    async def second() -> None:
        for _ in range(200):
            if extraction_requests(upstream):
                return
            await asyncio.sleep(0.02)

    asyncio.run(second())
    assert len(extraction_requests(upstream)) == 1
    assert "我住在杭州" in extraction_requests(upstream)[0]["messages"][1]["content"]


def test_extraction_is_off_when_auto_extract_is_switched_off(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "好的。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            state_after = await set_memory(client, auto_extract=False)
            assert state_after["settings"]["auto_extract"] is False
            workspace = await get_workspace(client)
            await run_goal(client, workspace["direct_conversation_id"], "随便聊聊")
            await asyncio.sleep(0.5)
            assert upstream.request_count == 1
            assert (await state(client))["memories"] == []

    asyncio.run(body())


def test_a_malformed_extraction_answer_changes_nothing(make_backend, upstream) -> None:
    backend = make_backend(**QUICK)
    store = store_of(backend)
    seeded = store.create_memory(
        content="用户不吃香菜", topic="饮食", scope="user", origin="user"
    )
    upstream.script = [{"content": "好的。"}]
    upstream.memory_text = "我想记一点东西，但忘了格式。"

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            await run_goal(client, workspace["direct_conversation_id"], "今天吃什么好")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not store.memory_last_error():
                await asyncio.sleep(0.05)
            assert "JSON" in (store.memory_last_error() or "")
            rows = (await state(client))["memories"]
            assert [row["id"] for row in rows] == [seeded["id"]]
            assert rows[0]["status"] == "active"

    asyncio.run(body())


def test_paused_stops_reading_and_writing(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    seeded = store.create_memory(
        content="用户不吃香菜", topic="饮食", scope="user", origin="user"
    )
    upstream.script = [
        tool_calls(
            call("call_1", "remember", {"content": "用户喜欢猫", "topic": "宠物"})
        ),
        {"content": "好的。"},
        tool_calls(
            call("call_2", "remember", {"content": "用户喜欢猫", "topic": "宠物"})
        ),
        {"content": "好的。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await set_memory(client, paused=True)
            workspace = await get_workspace(client)
            await run_goal(client, workspace["direct_conversation_id"], "记一下我喜欢猫")
            prompt = system_prompt(upstream.request(0))
            assert "<memory>" not in prompt
            assert "用户不吃香菜" not in prompt
            assert "remember" not in tool_names(upstream.request(0))
            assert "recall_memory" not in tool_names(upstream.request(0))
            # The call is refused rather than executed.
            refused = [
                message["content"]
                for message in upstream.request(1)["messages"]
                if message.get("role") == "tool"
            ]
            assert refused and "未知工具" in refused[0]
            rows = (await state(client))["memories"]
            assert [row["id"] for row in rows] == [seeded["id"]]

            # Turning it back on restores both directions.
            await set_memory(client, paused=False)
            await run_goal(client, workspace["direct_conversation_id"], "记一下我喜欢猫")
            assert "remember" in tool_names(upstream.request(2))
            assert "<memory>" in system_prompt(upstream.request(2))
            assert any(
                row["topic"] == "宠物" for row in (await state(client))["memories"]
            )

    asyncio.run(body())


def test_private_bot_chats_get_no_memory_and_no_memory_tools(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = seed_member(backend, "阿甲", "安静")
    store.create_memory(
        content="用户不吃香菜", topic="饮食", scope="user", origin="user"
    )
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "direct_message",
                {"agent_id": member["id"], "content": "私下确认一件事"},
            )
        ),
        {"content": "好的，我知道了。"},
        {"content": "成员确认过了。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            run = await run_goal(client, workspace["direct_conversation_id"], "去问问阿甲")
            assert run["status"] == "completed", run
            # The nested private reply is the second model call in the run.
            private_request = upstream.request(1)
            assert "remember" not in tool_names(private_request)
            assert "recall_memory" not in tool_names(private_request)
            assert "<memory>" not in system_prompt(private_request)
            await asyncio.sleep(0.3)
            rows = (await state(client))["memories"]
            assert [row["topic"] for row in rows] == ["饮食"]
            assert rows[0]["source_conversation_id"] is None

    asyncio.run(body())


def test_a_hidden_schedule_workspace_is_never_extracted(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    upstream.script = [{"content": "整理好了。"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            created = await client.post(
                "/schedules",
                json={
                    "title": "每天整理",
                    "prompt": "整理今天的事，最后写一小段给用户的总结。",
                    "conversation_id": workspace["direct_conversation_id"],
                    "kind": "interval",
                    "every_minutes": 5,
                },
            )
            assert created.status_code == 201, created.text
            schedule_id = created.json()["id"]
            started = await client.post(f"/schedules/{schedule_id}/run")
            assert started.status_code == 202, started.text
            deadline = time.monotonic() + 20
            run = None
            while time.monotonic() < deadline:
                detail = (await client.get(f"/schedules/{schedule_id}/run")).json()
                if detail["run"] is not None and detail["run"]["status"] in (
                    "completed",
                    "stopped",
                    "error",
                    "limited",
                ):
                    run = detail["run"]
                    break
                await asyncio.sleep(0.05)
            assert run is not None and run["status"] == "completed", run
            await asyncio.sleep(0.4)
            # The run answered in a workspace nobody sees, so nothing was extracted from it.
            assert extraction_requests(upstream) == []
            assert (await state(client))["memories"] == []
            assert store.list_memories() == []

    asyncio.run(body())
