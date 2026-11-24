"""A model call that takes long: thinking shows on the run, and a dead stream is cut and retried."""

from __future__ import annotations

import asyncio

import pytest

from helpers import agent_messages, client_for, get_conversation, get_workspace, start_run, wait_for_run


@pytest.fixture
def quick_clocks(monkeypatch):
    monkeypatch.setattr("app.agent.streaming.WAIT_REPORT_SECONDS", 0.2)
    monkeypatch.setattr("app.model_stream.SILENCE_SECONDS", 1.0)


def test_a_long_think_shows_on_the_run_and_is_not_cut(make_backend, upstream, quick_clocks) -> None:
    # 2 s of reasoning: twice the silence limit, yet alive the whole time.
    upstream.chunk_delay = 0.1
    upstream.script = [{"reasoning": 20, "content": "想好了。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "想一想")
            steps: set[str] = set()
            for _ in range(60):
                current = (await client.get(f"/runs/{run['id']}")).json()
                steps.add(current.get("current_step") or "")
                if current["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            assert current["status"] == "completed"
            assert any("正在思考" in step and "秒" in step for step in steps), steps
            replies = agent_messages(await get_conversation(client, direct_id))
            assert [m["content"] for m in replies] == ["想好了。"]

    asyncio.run(body())
    assert upstream.request_count == 1


def test_a_silent_stream_is_retried_once_then_answers(make_backend, upstream, quick_clocks) -> None:
    upstream.script = [{"silent": 3, "content": "迟到的回复"}, {"content": "重试后回复。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "在吗")
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            replies = agent_messages(await get_conversation(client, direct_id))
            assert [m["content"] for m in replies] == ["重试后回复。"]

    asyncio.run(body())
    assert upstream.request_count == 2


def test_two_silences_end_the_run_with_the_reason(make_backend, upstream, quick_clocks) -> None:
    upstream.script = [{"silent": 3, "content": "迟到的回复"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "在吗")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "error"
            assert "秒没有任何回应" in (finished.get("error") or "")

    asyncio.run(body())
    assert upstream.request_count == 2


def test_a_dropped_connection_before_any_text_is_retried_once(make_backend, upstream) -> None:
    # The real fault: the gateway closed the chunked body before the reply ended.
    upstream.script = [{"cut": True}, {"content": "重连后回复。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "在吗")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            replies = agent_messages(await get_conversation(client, direct_id))
            assert [m["content"] for m in replies] == ["重连后回复。"]

    asyncio.run(body())
    assert upstream.request_count == 2


def test_two_dropped_connections_end_the_run_with_the_reason(make_backend, upstream) -> None:
    upstream.script = [{"cut": True}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "在吗")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "error"
            assert "连接" in (finished.get("error") or ""), finished

    asyncio.run(body())
    assert upstream.request_count == 2


def test_a_connection_dropped_after_text_reached_the_user_is_not_retried(make_backend, upstream) -> None:
    # Text the user already saw cannot be taken back, so a retry would repeat it: the run ends.
    upstream.chunk_count = 2
    upstream.script = [{"content": "说到一半", "cut_after_content": True}, {"content": "不该出现"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "在吗")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "error"
            replies = agent_messages(await get_conversation(client, direct_id))
            assert [m["content"] for m in replies] == ["说到一半"]

    asyncio.run(body())
    assert upstream.request_count == 1
