"""The desktop backend is only reachable with the launch token the shell handed to the webview.

In development the token is empty and the API stays open, which is why ``/health`` reports
whether a token guards it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

TOKEN = "9a3f0c7d5b1e4826a0c3f7d9e2b50814"
MISSING = {"detail": "缺少或错误的访问令牌。"}


def _app(make_backend, token: str = TOKEN):
    """An app built on the fixture's settings, plus the launch token."""
    return create_app(replace(make_backend().settings, auth_token=token))


def test_requests_without_the_token_are_refused(make_backend) -> None:
    with TestClient(_app(make_backend)) as client:
        for path in ("/health", "/workspace", "/conversations/c1", "/avatars/whatever.png"):
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.json() == MISSING
            assert TOKEN not in response.text


def test_a_wrong_or_malformed_token_is_refused(make_backend) -> None:
    with TestClient(_app(make_backend)) as client:
        for header in (
            f"Bearer {TOKEN[:-1]}0",
            f"Bearer {TOKEN}extra",
            TOKEN,
            f"Basic {TOKEN}",
            "Bearer ",
        ):
            response = client.get("/health", headers={"Authorization": header})
            assert response.status_code == 401, header
            assert response.json() == MISSING


def test_the_right_token_unlocks_every_read(make_backend) -> None:
    with TestClient(_app(make_backend)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        health = client.get("/health", headers=headers)
        assert health.status_code == 200, health.text
        assert health.json()["auth"] is True
        assert client.get("/workspace", headers=headers).status_code == 200
        # A write needs the same header, and a wrong body still answers 422, not 401.
        refused = client.post("/agents", headers=headers, json={})
        assert refused.status_code == 422
        assert client.post("/agents", json={"name": "小北", "instructions": ""}).status_code == 401


def test_preflight_passes_without_the_token(make_backend) -> None:
    """The browser asks before it sends ``Authorization``; answering CORS itself needs no token."""
    with TestClient(_app(make_backend)) as client:
        for origin in ("app://cubes", "http://127.0.0.1:1420"):
            response = client.options(
                "/workspace",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
            assert response.status_code == 200, response.text
            assert response.headers["access-control-allow-origin"] == origin
            assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_the_event_stream_needs_the_token_too(make_backend) -> None:
    backend = make_backend(auth_token=TOKEN)

    async def body() -> None:
        async with httpx.AsyncClient(
            base_url=backend.url, timeout=15.0, trust_env=False
        ) as anonymous:
            refused = await anonymous.get("/events")
            assert refused.status_code == 401
            assert refused.json() == MISSING

        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with httpx.AsyncClient(
            base_url=backend.url, timeout=15.0, trust_env=False, headers=headers
        ) as client:
            async with client.stream("GET", "/events") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        assert line[len("event:") :].strip() == "snapshot"
                        break

    asyncio.run(body())


def test_no_token_leaves_the_api_open(make_backend) -> None:
    """Development mode: no token, no header, and ``/health`` says so."""
    with TestClient(_app(make_backend, token="")) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        assert health.json()["auth"] is False
        assert client.get("/workspace").status_code == 200


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "bearer  9a3f"])
def test_headers_without_a_usable_token_are_refused(make_backend, header: str) -> None:
    with TestClient(_app(make_backend)) as client:
        assert client.get("/health", headers={"Authorization": header}).status_code == 401


def test_the_stream_keeps_its_frames_under_a_token(make_backend) -> None:
    """Frames are unchanged by the guard: the first frame is still the whole snapshot."""
    backend = make_backend(auth_token=TOKEN)

    async def body() -> None:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with httpx.AsyncClient(
            base_url=backend.url, timeout=15.0, trust_env=False, headers=headers
        ) as client:
            async with client.stream("GET", "/events") as response:
                event = None
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data.append(line[len("data:") :].strip())
                    elif line == "" and event is not None:
                        payload = json.loads("".join(data))
                        assert event == "snapshot"
                        assert payload["workspace"]["agents"][0]["kind"] == "supervisor"
                        break

    asyncio.run(body())
