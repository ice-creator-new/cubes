"""The workspace backup model: when a call may switch away from the primary.

A retryable fault (5xx, 429, a connection failure, a quota sentence) before any text was
shown retries the same request once on the configured backup, records a quiet note in the
conversation, and lets the turn complete. A request the provider refused (401) or a fault
after the user already saw text ends the turn exactly as before.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import ServerThread
from fake_upstream import FakeUpstream
from helpers import client_for, get_conversation, get_workspace, run_goal, agent_messages

BACKUP_MODEL = "backup-model"


@pytest.fixture
def quick_retries(monkeypatch):
    # The adapter itself retries 5xx/429 twice; make those waits instant in tests.
    monkeypatch.setattr("app.model_stream.RETRY_DELAYS", (0.01, 0.01))


@pytest.fixture
def backup_upstream():
    """A second, healthy upstream the backup provider points at."""
    fake = FakeUpstream()
    server = ServerThread(fake.app).start()
    fake.base_url = f"{server.base_url}/v1"  # type: ignore[attr-defined]
    try:
        yield fake
    finally:
        server.stop()


async def configure_fallback(client, backup: FakeUpstream) -> dict:
    """Create a provider for the backup upstream and point the workspace backup at it."""
    response = await client.post(
        "/providers",
        json={
            "name": "备用服务",
            "protocol": "openai-chat",
            "base_url": backup.base_url,  # type: ignore[attr-defined]
            "api_key": "backup-key",
            "models": [BACKUP_MODEL],
        },
    )
    assert response.status_code == 201, response.text
    provider = response.json()
    response = await client.put(
        "/providers/fallback",
        json={"provider_id": provider["id"], "model": BACKUP_MODEL},
    )
    assert response.status_code == 200, response.text
    return response.json()


def notes_of(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "notice"]


def test_a_503_switches_to_the_backup_and_leaves_a_note(
    make_backend, upstream, backup_upstream, quick_retries
) -> None:
    upstream.mode = "unavailable"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            payload = await configure_fallback(client, backup_upstream)
            assert payload["fallback"] == {
                "provider_id": payload["providers"][-1]["id"],
                "model": BACKUP_MODEL,
            }
            listed = (await client.get("/providers")).json()
            assert listed["fallback"]["model"] == BACKUP_MODEL

            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "在吗")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, direct_id)
            assert [message["content"] for message in agent_messages(detail)] == ["回复正文"]
            notes = notes_of(detail)
            assert len(notes) == 1, notes
            assert notes[0]["author_name"] == "系统"
            assert "test-model" in notes[0]["content"]
            assert BACKUP_MODEL in notes[0]["content"]
            assert "HTTP 503" in notes[0]["content"]

    asyncio.run(body())
    # Three attempts on the primary (the adapter's own 5xx retries), then one on the backup.
    assert upstream.request_count == 3
    assert backup_upstream.request_count == 1


def test_a_401_does_not_fall_back(make_backend, upstream, backup_upstream) -> None:
    upstream.mode = "unauthorized"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await configure_fallback(client, backup_upstream)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "在吗")
            assert run["status"] == "error", run
            assert "HTTP 401" in (run.get("error") or "")
            assert notes_of(await get_conversation(client, direct_id)) == []

    asyncio.run(body())
    assert upstream.request_count == 1
    assert backup_upstream.request_count == 0


def test_a_quota_error_falls_back(make_backend, upstream, backup_upstream) -> None:
    # A spent balance often arrives as a 403 with a billing sentence: that is retryable.
    upstream.mode = "quota"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await configure_fallback(client, backup_upstream)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "在吗")
            assert run["status"] == "completed", run
            notes = notes_of(await get_conversation(client, direct_id))
            assert len(notes) == 1
            assert "额度" in notes[0]["content"]

    asyncio.run(body())
    assert backup_upstream.request_count == 1


def test_no_fallback_configured_fails_as_before(
    make_backend, upstream, quick_retries
) -> None:
    upstream.mode = "error"
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "在吗")
            assert run["status"] == "error", run
            assert "HTTP 500" in (run.get("error") or "")
            detail = await get_conversation(client, direct_id)
            assert agent_messages(detail) == []
            assert notes_of(detail) == []

    asyncio.run(body())
    assert upstream.request_count == 3


def test_a_failure_after_streamed_text_does_not_retry(
    make_backend, upstream, backup_upstream
) -> None:
    upstream.chunk_count = 2
    upstream.script = [{"content": "说到一半", "cut_after_content": True}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await configure_fallback(client, backup_upstream)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "在吗")
            assert run["status"] == "error", run
            detail = await get_conversation(client, direct_id)
            assert [m["content"] for m in agent_messages(detail)] == ["说到一半"]
            assert notes_of(detail) == []

    asyncio.run(body())
    assert upstream.request_count == 1
    assert backup_upstream.request_count == 0
