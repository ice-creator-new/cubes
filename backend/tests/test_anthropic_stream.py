"""The Anthropic Messages adapter: SSE events, tool accumulation and message translation.

Every streaming case runs against the real :class:`FakeUpstream` over HTTP, so the SSE
frames are decoded exactly as Anthropic sends them (``message_start``,
``content_block_start/delta/stop``, ``message_delta``, ``message_stop``, ``error``).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.anthropic_stream import to_anthropic_messages, to_anthropic_tools
from app.errors import UpstreamError
from app.model_stream import ModelStream, open_stream
from app.providers import ANTHROPIC_MAX_TOKENS, ResolvedModel

KEY = "anthropic-key-1234"
MODEL = "claude-test"


def anthropic_model(fake, *, key: str = KEY, model: str = MODEL) -> ResolvedModel:
    return ResolvedModel(
        protocol="anthropic-messages",
        base_url=fake.root_url,
        api_key=key,
        model=model,
        provider_id="p_anthropic",
        provider_name="Anthropic",
    )


def client() -> httpx.AsyncClient:
    # trust_env=False keeps the shell's proxy variables out of loopback tests.
    return httpx.AsyncClient(timeout=15.0, trust_env=False)


async def drain(stream: ModelStream, *, allow_tool_calls: bool = True) -> str:
    pieces: list[str] = []
    async for chunk in stream.chunks():
        pieces.append(chunk)
    stream.validate(allow_tool_calls=allow_tool_calls)
    return "".join(pieces)


def request_sent(fake) -> dict:
    assert fake.anthropic_requests, "no Anthropic request reached the upstream"
    return fake.anthropic_requests[0]


# ------------------------------------------------------------------- streaming


def test_text_streams_with_anthropic_headers(upstream) -> None:
    upstream.text = "先说结论，再说理由。"
    upstream.chunk_count = 4

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "在吗"}],
                tools=[],
            )
            text = await drain(stream)
            assert text == "先说结论，再说理由。"
            assert stream.result.content == text
            assert stream.result.finish_reason == "stop"
            assert stream.result.saw_done is True
            assert stream.tool_calls == []

    asyncio.run(body())
    sent = request_sent(upstream)
    assert sent["path"] == "/v1/messages"
    assert sent["model"] == MODEL
    assert sent["stream"] is True
    assert sent["max_tokens"] == ANTHROPIC_MAX_TOKENS
    assert sent["x_api_key"] == KEY
    assert sent["anthropic_version"] == "2023-06-01"
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "在吗"}]}]


def test_single_tool_call_is_reassembled_from_input_json_deltas(upstream) -> None:
    upstream.anthropic_script = [
        {
            "content": "我来建一个成员。",
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "name": "create_agent",
                    "arguments": {"name": "甲", "instructions": "出方案"},
                }
            ],
        }
    ]

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "建个成员"}],
                tools=[],
            )
            text = await drain(stream)
            assert text == "我来建一个成员。"
            assert stream.result.finish_reason == "tool_calls"
            calls = stream.tool_calls
            assert len(calls) == 1
            assert calls[0].id == "toolu_1"
            assert calls[0].name == "create_agent"
            assert json.loads(calls[0].arguments) == {"name": "甲", "instructions": "出方案"}

    asyncio.run(body())


def test_two_tool_calls_keep_their_arguments_apart(upstream) -> None:
    upstream.anthropic_script = [
        {
            "tool_calls": [
                {"id": "toolu_a", "name": "read_note", "arguments": {"note_id": "n_1"}},
                {"id": "toolu_b", "name": "list_tasks", "arguments": {"status": "todo"}},
            ]
        }
    ]

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "看看"}],
                tools=[],
            )
            assert await drain(stream) == ""
            calls = stream.tool_calls
            assert [(call.id, call.name) for call in calls] == [
                ("toolu_a", "read_note"),
                ("toolu_b", "list_tasks"),
            ]
            assert json.loads(calls[0].arguments) == {"note_id": "n_1"}
            assert json.loads(calls[1].arguments) == {"status": "todo"}

    asyncio.run(body())


def test_max_tokens_stop_reason_is_an_incomplete_reply(upstream) -> None:
    upstream.anthropic_mode = "max_tokens"

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "长一点"}],
            )
            with pytest.raises(UpstreamError) as caught:
                await drain(stream)
            assert "被长度限制截断" in str(caught.value)

    asyncio.run(body())


def test_refusal_stop_reason_is_a_content_policy_reply(upstream) -> None:
    upstream.anthropic_mode = "refusal"

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "不行的事"}],
            )
            with pytest.raises(UpstreamError) as caught:
                await drain(stream)
            assert "内容策略" in str(caught.value)

    asyncio.run(body())


def test_a_mid_stream_error_event_ends_the_reply(upstream) -> None:
    upstream.anthropic_mode = "overloaded"

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "很忙"}],
            )
            with pytest.raises(UpstreamError) as caught:
                await drain(stream)
            assert "Overloaded" in str(caught.value)

    asyncio.run(body())


def test_a_missing_message_stop_means_the_reply_was_truncated(upstream) -> None:
    upstream.anthropic_mode = "truncated"

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "断掉"}],
            )
            with pytest.raises(UpstreamError) as caught:
                await drain(stream)
            assert "完成标记" in str(caught.value)

    asyncio.run(body())


def test_unparseable_stream_data_is_reported(upstream) -> None:
    upstream.anthropic_mode = "invalid_json"

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "乱码"}],
            )
            with pytest.raises(UpstreamError) as caught:
                await drain(stream)
            assert "无法解析" in str(caught.value)

    asyncio.run(body())


def test_an_http_error_never_echoes_the_key(upstream) -> None:
    upstream.anthropic_mode = "error_echoing_key"

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "401"}],
            )
            with pytest.raises(UpstreamError) as caught:
                await drain(stream)
            detail = str(caught.value)
            assert "HTTP 401" in detail
            assert KEY not in detail
            assert "***" in detail

    asyncio.run(body())


def test_tools_and_choice_follow_the_anthropic_schema(upstream) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_note",
                "description": "读取一条笔记",
                "parameters": {
                    "type": "object",
                    "properties": {"note_id": {"type": "string"}},
                    "required": ["note_id"],
                },
            },
        }
    ]

    async def body() -> None:
        async with client() as http:
            stream = open_stream(
                http,
                anthropic_model(upstream),
                messages=[{"role": "user", "content": "读笔记"}],
                tools=tools,
            )
            await drain(stream)

    asyncio.run(body())
    sent = request_sent(upstream)
    assert sent["tools"] == [
        {
            "name": "read_note",
            "description": "读取一条笔记",
            "input_schema": {
                "type": "object",
                "properties": {"note_id": {"type": "string"}},
                "required": ["note_id"],
            },
        }
    ]
    assert sent["tool_choice"] == {"type": "auto"}


# ------------------------------------------------------------------ translation


def test_system_messages_merge_into_one_cached_block() -> None:
    system, messages = to_anthropic_messages(
        [
            {"role": "system", "content": "第一段"},
            {"role": "user", "content": "问题"},
            {"role": "system", "content": "第二段"},
        ]
    )
    # The system prompt is a block list with a cache breakpoint: the whole conversation
    # prefix is then served from the prompt cache between two turns.
    assert system == [
        {
            "type": "text",
            "text": "第一段\n\n第二段",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "问题"}]}]


def test_consecutive_tool_results_become_one_user_turn() -> None:
    _, messages = to_anthropic_messages(
        [
            {"role": "user", "content": "做两件事"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {"name": "read_note", "arguments": '{"note_id": "n_1"}'},
                    },
                    {
                        "id": "toolu_2",
                        "type": "function",
                        "function": {"name": "list_tasks", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "笔记正文"},
            {"role": "tool", "tool_call_id": "toolu_2", "content": "任务列表"},
        ]
    )
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == [
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "read_note",
            "input": {"note_id": "n_1"},
        },
        {
            "type": "tool_use",
            "id": "toolu_2",
            "name": "list_tasks",
            "input": {},
            # The assistant turn before the newest one carries the cache breakpoint.
            "cache_control": {"type": "ephemeral"},
        },
    ]
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "笔记正文"},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "任务列表"},
    ]


def test_adjacent_same_role_messages_are_merged() -> None:
    _, messages = to_anthropic_messages(
        [
            {"role": "user", "content": "第一句"},
            {"role": "user", "content": "第二句"},
            {"role": "assistant", "content": "回答"},
            {"role": "assistant", "content": "补充"},
        ]
    )
    assert [message["role"] for message in messages] == ["user", "assistant"]
    # The last message before the newest turn carries the second cache breakpoint.
    assert messages[0]["content"] == [
        {"type": "text", "text": "第一句"},
        {"type": "text", "text": "第二句", "cache_control": {"type": "ephemeral"}},
    ]
    assert messages[1]["content"] == [
        {"type": "text", "text": "回答"},
        {"type": "text", "text": "补充"},
    ]


def test_a_malformed_tool_call_keeps_an_empty_input() -> None:
    _, messages = to_anthropic_messages(
        [
            {"role": "user", "content": "建成员"},
            {
                "role": "assistant",
                "content": "试试",
                "tool_calls": [
                    {
                        "id": "toolu_bad",
                        "type": "function",
                        "function": {"name": "create_agent", "arguments": "{not json"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_bad", "content": "参数不是合法 JSON。"},
        ]
    )
    assert messages[1]["content"] == [
        {"type": "text", "text": "试试"},
        {
            "type": "tool_use",
            "id": "toolu_bad",
            "name": "create_agent",
            "input": {},
            # This assistant turn is the one before the newest turn: its last block caches.
            "cache_control": {"type": "ephemeral"},
        },
    ]
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_bad", "content": "参数不是合法 JSON。"}
    ]


def test_a_truncated_history_never_starts_with_assistant() -> None:
    _, messages = to_anthropic_messages(
        [
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "新问题"},
        ]
    )
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "新问题"}]}]


def test_tool_without_an_id_is_dropped() -> None:
    _, messages = to_anthropic_messages(
        [
            {"role": "user", "content": "问题"},
            {"role": "tool", "content": "没有 id 的结果"},
        ]
    )
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "问题"}]}]


def test_tool_schemas_without_parameters_get_an_empty_object_schema() -> None:
    assert to_anthropic_tools(
        [{"type": "function", "function": {"name": "ping", "description": ""}}]
    ) == [{"name": "ping", "input_schema": {"type": "object", "properties": {}}}]
    assert to_anthropic_tools(None) == []


def test_a_pdf_file_part_becomes_a_document_block() -> None:
    _, messages = to_anthropic_messages(
        [
            {"role": "system", "content": "规则"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "[PDF 报告.pdf（att_000000000001）]"},
                    {
                        "type": "file",
                        "file": {
                            "filename": "报告.pdf",
                            "file_data": "data:application/pdf;base64,JVBERi0xLjc=",
                        },
                    },
                    # A file part of an unknown type never reaches the request.
                    {"type": "file", "file": {"filename": "x.bin", "file_data": "data:application/octet-stream;base64,AA"}},
                ],
            },
        ]
    )

    blocks = messages[-1]["content"]
    assert {"type": "text", "text": "[PDF 报告.pdf（att_000000000001）]"} in blocks
    documents = [block for block in blocks if block["type"] == "document"]
    assert documents == [
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjc="},
        }
    ]
