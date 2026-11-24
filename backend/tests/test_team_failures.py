"""Upstream failure boundaries: refusals, truncation, bad data and empty replies."""

from __future__ import annotations

import asyncio

import pytest

from fake_upstream import call, tool_calls
from helpers import (
    agent_messages,
    client_for,
    find_result_key,
    get_conversation,
    get_workspace,
    names_of,
    result_keys,
    run_goal,
)


async def _failed_run(client, content: str = "开始任务") -> tuple[dict, str]:
    direct_id = (await get_workspace(client))["direct_conversation_id"]
    run = await run_goal(client, direct_id, content)
    return run, direct_id


@pytest.fixture
def quick_retries(monkeypatch):
    monkeypatch.setattr("app.model_stream.RETRY_DELAYS", (0.01, 0.01))


def test_a_lasting_upstream_http_error_ends_the_run_after_the_retries(
    make_backend, upstream, quick_retries
) -> None:
    upstream.mode = "error"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            assert "HTTP 500" in run["error"]
            assert run["finished_at"] is not None
            assert upstream.request_count == 3
            detail = await get_conversation(client, direct_id)
            assert agent_messages(detail) == []
            assert [m["kind"] for m in detail["messages"]] == ["user"]

    asyncio.run(body())


def test_a_transient_upstream_500_is_retried_and_the_reply_arrives(
    make_backend, upstream, quick_retries
) -> None:
    # The gateway in front of MiMo refuses a few percent of calls with HTTP 500.
    upstream.fail_first = 2
    upstream.script = [{"content": "第三次才成"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "completed", run
            replies = agent_messages(await get_conversation(client, direct_id))
            assert [m["content"] for m in replies] == ["第三次才成"]

    asyncio.run(body())
    assert upstream.request_count == 3


def test_a_client_error_is_not_retried(make_backend, upstream) -> None:
    upstream.mode = "bad_request"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, _ = await _failed_run(client)
            assert run["status"] == "error", run
            assert "HTTP 400" in run["error"]

    asyncio.run(body())
    assert upstream.request_count == 1


def test_upstream_errors_never_leak_the_api_key(make_backend, upstream, quick_retries) -> None:
    upstream.mode = "error_echoing_key"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, _ = await _failed_run(client)
            assert run["status"] == "error", run
            assert "test-key" not in run["error"]

    asyncio.run(body())


def test_upstream_error_event_never_leaks_a_key_across_the_truncation_cut(
    make_backend, upstream
) -> None:
    backend = make_backend()
    # The key starts at offset 295, so a 300-char cut would otherwise keep its prefix.
    upstream.script = [{"error_text": "x" * 295 + "test-key" + "tail"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run, _ = await _failed_run(client)
            assert run["status"] == "error", run
            assert "test-key" not in run["error"]
            assert "test-" not in run["error"]

    asyncio.run(body())


def test_http_error_body_never_leaks_a_key_across_the_truncation_cut(
    make_backend, upstream, quick_retries
) -> None:
    upstream.mode = "error_plain_text_key"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, _ = await _failed_run(client)
            assert run["status"] == "error", run
            assert "test-key" not in run["error"]
            assert "test-" not in run["error"]

    asyncio.run(body())


def test_empty_reply_is_not_success(make_backend, upstream) -> None:
    upstream.mode = "empty"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            assert agent_messages(await get_conversation(client, direct_id)) == []

    asyncio.run(body())


def test_truncated_stream_keeps_partial_text_and_errors(make_backend, upstream) -> None:
    upstream.mode = "truncated"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            messages = agent_messages(await get_conversation(client, direct_id))
            assert len(messages) == 1
            assert messages[0]["content"] == "回复正文"
            assert messages[0]["status"] == "interrupted"

    asyncio.run(body())


def test_length_finish_reason_is_not_success(make_backend, upstream) -> None:
    upstream.mode = "length"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            messages = agent_messages(await get_conversation(client, direct_id))
            assert messages[0]["status"] == "interrupted"
            assert messages[0]["content"] == "回复正文"

    asyncio.run(body())


def test_content_filter_finish_reason_is_not_success(make_backend, upstream) -> None:
    upstream.mode = "content_filter"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            assert agent_messages(await get_conversation(client, direct_id))[0]["status"] == (
                "interrupted"
            )

    asyncio.run(body())


def test_upstream_error_event_is_reported_truthfully(make_backend, upstream) -> None:
    upstream.mode = "refusal"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, _ = await _failed_run(client)
            assert run["status"] == "error", run

    asyncio.run(body())


def test_partial_refusal_keeps_partial_text(make_backend, upstream) -> None:
    upstream.mode = "refusal_partial"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            messages = agent_messages(await get_conversation(client, direct_id))
            assert messages[0]["content"] == "回复正"
            assert messages[0]["status"] == "interrupted"

    asyncio.run(body())


def test_unparseable_stream_data_is_an_error(make_backend, upstream) -> None:
    upstream.mode = "invalid_json"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            # Invalid framing before any text must not invent an empty assistant reply.
            assert agent_messages(await get_conversation(client, direct_id)) == []

    asyncio.run(body())


def test_tool_call_without_id_is_a_real_error(make_backend, upstream) -> None:
    upstream.mode = "tool_call_missing_id"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, direct_id = await _failed_run(client)
            assert run["status"] == "error", run
            detail = await get_conversation(client, direct_id)
            assert agent_messages(detail) == []
            assert [m["kind"] for m in detail["messages"]] == ["user"]
            assert names_of((await get_workspace(client))["agents"][1:]) == []

    asyncio.run(body())


def test_tool_call_without_name_is_a_real_error(make_backend, upstream) -> None:
    upstream.mode = "tool_call_missing_name"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            run, _ = await _failed_run(client)
            assert run["status"] == "error", run
            assert upstream.request_count == 1

    asyncio.run(body())


def test_a_member_cannot_use_supervisor_only_tools(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [
        tool_calls(call("call_1", "create_agent", {"name": "甲", "instructions": "研究"})),
        lambda requests, index: tool_calls(
            call(
                "call_2",
                "create_group",
                {
                    "title": "小组",
                    "purpose": "做事",
                    "member_ids": result_keys(requests, "agent_id"),
                },
            )
        ),
        lambda requests, index: tool_calls(
            call(
                "call_3",
                "ask_agent",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "agent_id": result_keys(requests, "agent_id")[0],
                    "task": "给结论",
                },
            )
        ),
        # The member tries a supervisor-only tool: refused, nothing created, run goes on.
        tool_calls(call("call_9", "create_agent", {"name": "偷偷创建", "instructions": "x"})),
        {"content": "我只能给结论，不能建成员。"},
        {"content": "汇总完成。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "让成员帮忙")
            assert run["status"] == "completed", run

            workspace = await get_workspace(client)
            assert names_of(workspace["agents"][1:]) == ["甲"]
            group = next(c for c in workspace["conversations"] if c["kind"] == "group")
            group_detail = await get_conversation(client, group["id"])
            refused = [m for m in group_detail["messages"] if m["kind"] == "action"]
            assert len(refused) == 1
            assert refused[0]["action"]["tool_name"] == "create_agent"
            assert refused[0]["action"]["status"] == "error"
            assert "未知工具" in refused[0]["action"]["error"]
            assert [m["content"] for m in agent_messages(group_detail)] == [
                "我只能给结论，不能建成员。"
            ]

            actions = [
                m
                for m in (await get_conversation(client, direct_id))["messages"]
                if m["kind"] == "action"
            ]
            assert [action["action"]["status"] for action in actions] == [
                "completed",
                "completed",
                "completed",
            ]

    asyncio.run(body())


def test_usage_chunk_with_empty_choices_does_not_break_parsing(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "正常回复", "usage_chunk": True}]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "随便聊聊")
            assert run["status"] == "completed", run
            messages = agent_messages(await get_conversation(client, direct_id))
            assert messages[-1]["content"] == "正常回复"
            assert messages[-1]["status"] == "complete"

    asyncio.run(body())
