"""Context management: the budget holds, old turns become a summary, tool traces survive.

Every assertion here is on what a model was really sent (``FakeUpstream.requests``), so the
tests pin the actual wire behaviour: the size of a request, the shape of a replayed tool
turn, the prefix that must stay byte-identical, and the usage a run records.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import app.engine as engine_module
from app.agent import budget as budget_rules
from app.agent import history
from fake_upstream import call, tool_calls
from helpers import (
    assistant_tool_calls,
    client_for,
    get_workspace,
    last_user_text,
    runtime_of,
    start_run,
    store_of,
    system_prompt,
    tool_names,
    wait_for_run,
)

# A seeded turn's body: 600 CJK characters, i.e. about 600 tokens by the local estimate.
BODY = "数据" * 300


def add_user(store, conversation_id: str, content: str, **extra) -> dict:
    return store.append_message(
        conversation_id=conversation_id,
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content=content,
        status="complete",
        **extra,
    )


def add_agent(store, conversation_id: str, agent: dict, content: str) -> dict:
    return store.append_message(
        conversation_id=conversation_id,
        author_id=agent["id"],
        author_name=agent["name"],
        avatar_seed=agent["avatar_seed"],
        kind="agent",
        content=content,
        status="complete",
    )


def seed(store, conversation_id: str, agent: dict, turns: int, *, tag: str = "历史") -> None:
    for index in range(turns):
        add_user(store, conversation_id, f"{tag}第{index}条：{BODY}")
        add_agent(store, conversation_id, agent, f"收到{tag}第{index}条：{BODY}")


def request_tokens(request: dict) -> int:
    """The size a request claims, measured with the engine's own estimator."""
    return budget_rules.estimate_request(request["messages"], request.get("tools"))


def small_window(**overrides: object):
    """Settings for a service that declares a small window, so compaction really happens."""
    return {"context_window_default": 32_000, "context_output_default": 1_024, **overrides}


# ------------------------------------------------------------------ the budget


def test_a_conversation_over_budget_is_summarised_instead_of_failing(make_backend, upstream) -> None:
    backend = make_backend(**small_window())
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    seed(store, direct["id"], supervisor, 30)
    upstream.script = [{"content": "早期进展：整理完三十条数据，结论是需求集中在搜索。"}, {"content": "好的"}]

    async def body() -> dict:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], "继续")
            return await wait_for_run(client, run["id"])

    run = asyncio.run(body())
    assert run["status"] == "completed"
    assert run["compactions"] == 1

    # One summary now stands in for the turns that were folded away.
    saved = store.latest_summary(direct["id"])
    assert saved is not None and saved["upto_seq"] > 0
    assert saved["summary"].startswith("早期进展")

    summariser, final = upstream.requests
    # The summariser is the same model with no tools and a small output budget.
    assert summariser["tools"] is None
    assert summariser["max_tokens"] == budget_rules.SUMMARY_MAX_TOKENS
    # The request the model answered opens with the summary and ends with the runtime line.
    assert final["messages"][1]["content"].startswith("<摘要>")
    assert "早期进展" in final["messages"][1]["content"]
    assert runtime_of(final).startswith("<runtime time=")
    # And it fits: today this conversation would have overflowed the provider's window.
    usable = budget_rules.usable_budget(32_000, 1_024)
    assert request_tokens(final) <= usable


def test_a_summary_cut_by_the_output_cap_is_still_used(make_backend, upstream) -> None:
    # Seen live: a reasoning model spent part of the summariser's output budget thinking and
    # the checkpoint ended on finish_reason "length". What it wrote is kept, not thrown away.
    backend = make_backend(**small_window())
    store = store_of(backend)
    direct = store.direct_conversation()
    supervisor = store.supervisor()
    assert direct is not None and supervisor is not None
    seed(store, direct["id"], supervisor, 30)
    upstream.script = [
        {"content": "早期进展：需求集中在搜索", "finish_reason": "length", "reasoning": 2},
        {"content": "好的"},
    ]

    async def body() -> dict:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], "继续")
            return await wait_for_run(client, run["id"])

    run = asyncio.run(body())
    assert run["status"] == "completed"
    assert run["compactions"] == 1
    saved = store.latest_summary(direct["id"])
    assert saved is not None and saved["summary"] == "早期进展：需求集中在搜索"
    assert "早期进展" in upstream.requests[1]["messages"][1]["content"]


def test_usage_is_captured_and_asked_for(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.usage = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    upstream.script = [{"content": "回复正文"}]

    async def body() -> dict:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            run = await start_run(client, workspace["direct_conversation_id"], "说句话")
            return await wait_for_run(client, run["id"])

    run = asyncio.run(body())
    assert run["input_tokens"] == 120
    assert run["output_tokens"] == 30
    assert run["cached_tokens"] == 80
    # The request asked for the usage chunk; without it nothing could be recorded.
    assert upstream.requests[0]["stream_options"] == {"include_usage": True}


def test_the_estimate_counts_cjk_by_character_and_an_image_flat() -> None:
    assert budget_rules.estimate_tokens("数据") == 2
    assert budget_rules.estimate_tokens("abcd") == 1
    image_turn = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[沙盒截图，红圈是鼠标位置]"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            ],
        }
    ]
    assert budget_rules.estimate_messages(image_turn) >= budget_rules.IMAGE_TOKENS


# ------------------------------------------------------------------ tool traces


def test_a_turn_keeps_its_tool_trace_and_older_turns_become_a_digest(make_backend, upstream) -> None:
    backend = make_backend()

    def script(requests, index):
        if index % 2 == 1:  # every turn opens with a tool call, then answers
            return tool_calls(call(f"call_{index}", "calculate", {"expression": "2 ** 10"}))
        return {"content": "算好了"}

    upstream.script = [script]

    async def body() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            for number in range(5):
                run = await start_run(client, conversation_id, f"第{number}轮")
                assert (await wait_for_run(client, run["id"]))["status"] == "completed"

    asyncio.run(body())
    # The fifth turn's first request replays the three newest traced turns natively.
    final = upstream.requests[8]
    replayed = [entry["id"] for entry in assistant_tool_calls(final)]
    assert replayed == ["call_3", "call_5", "call_7"]
    results = [m for m in final["messages"] if m.get("role") == "tool"]
    assert [message["tool_call_id"] for message in results] == replayed
    assert all("1024" in message["content"] for message in results)
    # The oldest traced turn is one line instead, and no tool_calls block was left without it.
    assert "call_1" not in replayed
    # The digest is a separate note after the reply, never part of the bot's own words: a
    # model copies the shape of its past replies, and the block came back in new answers.
    assistant_texts = [m["content"] or "" for m in final["messages"] if m.get("role") == "assistant"]
    assert not any("calculate(2 ** 10)" in text for text in assistant_texts)
    notes = [m["content"] for m in final["messages"] if m.get("role") == "user" and isinstance(m["content"], str)]
    assert any("calculate(2 ** 10)" in text and "→" in text for text in notes)


def test_a_long_trace_is_capped_newest_first(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    note = store.create_note(title="长笔记", content="细节" * 1_500, author_id=None)

    def script(requests, index):
        if index == 1:
            return tool_calls(
                *[
                    call(f"call_{number}", "note_read", {"note_id": note["id"]})
                    for number in range(20)
                ]
            )
        return {"content": "看完了"}

    upstream.script = [script]

    async def body() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            for number in range(2):
                run = await start_run(client, conversation_id, f"第{number}轮")
                assert (await wait_for_run(client, run["id"]))["status"] == "completed"

    asyncio.run(body())
    plan = budget_rules.replay_plan(128_000)
    trace = {
        message["tool_call_id"]: message["content"]
        for message in upstream.requests[2]["messages"]
        if message.get("role") == "tool"
    }
    assert len(trace) == 20
    assert all(len(text) <= plan.result_chars for text in trace.values())
    carried = [text for text in trace.values() if text != budget_rules.TRACE_OMITTED]
    assert sum(len(text) for text in carried) <= plan.budget_chars
    # The newest results fill the budget; the placeholders are fixed-size labels.
    assert len(carried) == plan.budget_chars // plan.result_chars
    # Newest results keep their text; the oldest ones only say they were omitted.
    assert trace["call_19"] != budget_rules.TRACE_OMITTED
    assert "细节" in trace["call_19"]
    assert trace["call_0"] == budget_rules.TRACE_OMITTED


# ------------------------------------------------------------------ the summary


def test_a_second_compaction_merges_into_the_first(make_backend, upstream) -> None:
    backend = make_backend(**small_window())
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    seed(store, direct["id"], supervisor, 30, tag="最早")
    upstream.script = [{"content": "第一份摘要：需求集中在搜索。"}, {"content": "好的"}]

    async def run_once(content: str) -> None:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], content)
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"

    asyncio.run(run_once("继续"))
    first = store.latest_summary(direct["id"])
    assert first is not None
    seed(store, direct["id"], supervisor, 12, tag="后来的")
    upstream.script = [{"content": "第二份摘要：需求集中在搜索，另加了三项偏好。"}, {"content": "好的"}]
    asyncio.run(run_once("再继续"))

    rows = store.latest_summary(direct["id"])
    assert rows is not None
    assert rows["upto_seq"] > first["upto_seq"]
    # Exactly one newer summary step was written, and it was given the previous one to merge.
    prompt = upstream.requests[2]["messages"][-1]["content"]
    assert "<摘要>" in prompt and "第一份摘要" in prompt
    assert "后来的第0条" in prompt
    assert "最早第0条" not in prompt


def test_a_failed_summariser_does_not_fail_the_run(make_backend, upstream) -> None:
    backend = make_backend(**small_window())
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    seed(store, direct["id"], supervisor, 30)
    upstream.script = [{"error_text": "摘要服务炸了"}, {"content": "照样回答"}]

    async def body() -> dict:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], "继续")
            return await wait_for_run(client, run["id"])

    run = asyncio.run(body())
    assert run["status"] == "completed"
    assert run["compactions"] == 0
    assert store.latest_summary(direct["id"]) is None
    # The request was still trimmed to fit instead of the run failing.
    usable = budget_rules.usable_budget(32_000, 1_024)
    assert request_tokens(upstream.requests[1]) <= usable


# ------------------------------------------------------------------ stable prefix


class FrozenDatetime(datetime):
    """A clock that never moves, so the two turns differ only by what they must."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - mirrors datetime.now
        fixed = datetime(2026, 10, 23, 8, 35, tzinfo=timezone.utc)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)


def test_the_system_prompt_and_tools_are_byte_identical_between_turns(
    make_backend, upstream, monkeypatch
) -> None:
    monkeypatch.setattr(engine_module, "datetime", FrozenDatetime)
    backend = make_backend()
    upstream.script = [{"content": "第一轮回答"}, {"content": "第二轮回答"}]

    async def body() -> None:
        async with client_for(backend) as client:
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            for content in ("第一问", "第二问"):
                run = await start_run(client, conversation_id, content)
                assert (await wait_for_run(client, run["id"]))["status"] == "completed"

    asyncio.run(body())
    first, second = upstream.requests
    assert system_prompt(first) == system_prompt(second)
    assert first["tools"] == second["tools"]
    assert "现在的本地时间" not in system_prompt(second)
    # The clock rides in the newest user turn only.
    assert "2026-10-23 16:35" in runtime_of(second)
    assert "2026-10-23 16:35" in last_user_text(second)
    # Everything before the newest turn is byte-identical, which is what makes a prefix
    # cache hit: only the last message differs (it gained the runtime line last turn).
    assert first["messages"][:-1] == second["messages"][: len(first["messages"]) - 1]
    assert "2026-10-23" not in json.dumps(second["messages"][:-1], ensure_ascii=False)


def test_anthropic_gets_two_cache_breakpoints_and_keeps_tool_pairing(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.anthropic_script = [
        {
            "content": "先算一下。",
            "tool_calls": [{"id": "toolu_1", "name": "calculate", "arguments": {"expression": "1 + 1"}}],
        },
        {"content": "算完了。"},
        {"content": "好的。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={
                    "name": "Anthropic",
                    "protocol": "anthropic-messages",
                    "base_url": upstream.root_url,
                    "api_key": "sk-ant-test",
                    "models": ["claude-x"],
                },
            )
            assert created.status_code == 201, created.text
            supervisor = store_of(backend).supervisor()
            await client.patch(
                f"/agents/{supervisor['id']}",
                json={"model_ref": {"provider_id": created.json()["id"], "model": "claude-x"}},
            )
            conversation_id = (await get_workspace(client))["direct_conversation_id"]
            for content in ("先算个题", "第二问"):
                run = await start_run(client, conversation_id, content)
                assert (await wait_for_run(client, run["id"]))["status"] == "completed"

    asyncio.run(body())
    request = upstream.anthropic_requests[-1]
    system = request["system"]
    assert isinstance(system, list)
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    messages = request["messages"]
    assert messages[0]["role"] == "user"
    # The message before the newest turn carries the second breakpoint.
    assert messages[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # The replayed trace is still a valid tool_use / tool_result pair after trimming.
    uses = [
        block["id"]
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_use"
    ]
    results = [
        block["tool_use_id"]
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert uses == ["toolu_1"]
    assert results == ["toolu_1"]


# ------------------------------------------------------------------ screenshots


def test_screenshots_are_pruned_per_label_across_the_request() -> None:
    screen = "[沙盒截图，红圈是鼠标位置]"
    browser = "[浏览器截图，红圈是鼠标位置]"
    messages: list[dict] = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "看屏幕"},
    ]
    for label in (screen, browser, screen, browser, screen):
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": label},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                ],
            }
        )

    history.drop_old_screenshots(messages, keep={screen: 2, browser: 1})

    kept = [
        message["content"][0]["text"]
        for message in messages
        if isinstance(message.get("content"), list)
        and any(part.get("type") == "image_url" for part in message["content"])
    ]
    # Turns were screen, browser, screen, browser, screen: the newest two screens and the
    # newest browser keep their pixels, and the two oldest turns lose theirs.
    assert kept == [screen, browser, screen]
    dropped = [message for message in messages if message["content"] == history.COMPUTER_SHOT_DROPPED]
    assert len(dropped) == 2


# ------------------------------------------------------------------ other conversations


def test_a_private_chat_keeps_fewer_turns_than_a_conversation() -> None:
    rows: list[dict] = []
    for number in range(10):
        rows.append(
            {
                "id": f"m{number}",
                "kind": "user" if number % 2 == 0 else "agent",
                "status": "complete",
                "content": f"第{number}条",
                "attachments": [],
                "seq": number + 1,
            }
        )
    # The kept turns follow the model's window: 7 here, 5 in a private chat, 19 at 1M.
    keep = budget_rules.keep_turns(128_000, private=False)
    private = budget_rules.keep_turns(128_000, private=True)
    assert (keep, private) == (7, 5)
    assert budget_rules.keep_turns(1_048_576, private=False) == 19
    assert history.keep_boundary(rows, keep=keep) == 3
    assert history.keep_boundary(rows, keep=private) == 5
    # Too short to fold anything: a conversation that fits keeps every turn.
    assert history.keep_boundary(rows[:4], keep=keep) is None
    # A boundary never moves backwards, so a second compaction only summarises the delta.
    assert history.keep_boundary(rows, keep=keep, since_seq=2) == 3
    assert history.keep_boundary(rows, keep=keep, since_seq=6) is None


def test_a_scheduled_fire_works_in_its_own_conversation(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    upstream.usage = {"prompt_tokens": 90, "completion_tokens": 10}
    upstream.script = [{"content": "看完了新闻，没有新结论。"}]

    async def body() -> dict:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = await client.post(
                "/schedules",
                json={
                    "title": "每日新闻",
                    "prompt": "看一眼今天的新闻",
                    "conversation_id": direct_id,
                    "kind": "interval",
                    "every_minutes": 60,
                },
            )
            assert created.status_code == 201, created.text
            started = await client.post(f"/schedules/{created.json()['id']}/run")
            assert started.status_code == 202, started.text
            fired = store.get_schedule(created.json()["id"])
            for _ in range(400):
                if fired is not None and fired["last_status"] is not None:
                    return fired
                await asyncio.sleep(0.05)
                fired = store.get_schedule(created.json()["id"])
            raise AssertionError(f"the scheduled fire never finished: {fired}")

    fired = asyncio.run(body())
    assert fired["last_status"] == "completed"
    request = upstream.requests[-1]
    assert "后台工作区" in system_prompt(request)
    assert runtime_of(request).startswith("<runtime time=")
    # Its history and its budget are the workspace's own: nothing is written to the chat the
    # result is posted in, and the run's usage is recorded on the workspace run.
    workspace_id = fired["workspace_conversation_id"]
    assert workspace_id is not None
    assert store.latest_summary(workspace_id) is None
    assert store.latest_summary(store.direct_conversation()["id"]) is None
    run = store.latest_run(workspace_id)
    assert run is not None and run["status"] == "completed"
    assert run["input_tokens"] == 90 and run["output_tokens"] == 10


# ------------------------------------------------------------------ wiring


def test_the_runtime_line_is_not_in_the_tool_list(make_backend, upstream) -> None:
    """The tool list is stable too: nothing per-turn may leak into it."""
    backend = make_backend()
    upstream.script = [{"content": "好"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            run = await start_run(client, workspace["direct_conversation_id"], "说句话")
            await wait_for_run(client, run["id"])

    asyncio.run(body())
    request = upstream.requests[0]
    assert "web_search" in tool_names(request)
    assert runtime_of(request).startswith("<runtime time=")
    assert "<runtime" not in json.dumps(request["tools"], ensure_ascii=False)


def test_a_pdf_is_charged_by_its_size_instead_of_nothing() -> None:
    small = "data:application/pdf;base64," + "A" * 1_000
    large = "data:application/pdf;base64," + "A" * 40_000_000
    assert budget_rules.pdf_tokens(small) == budget_rules.PDF_MIN_TOKENS
    assert budget_rules.pdf_tokens(large) == budget_rules.PDF_MAX_TOKENS
    turn = [{"role": "user", "content": [{"type": "file", "file": {"file_data": small}}]}]
    assert budget_rules.estimate_messages(turn) >= budget_rules.PDF_MIN_TOKENS
