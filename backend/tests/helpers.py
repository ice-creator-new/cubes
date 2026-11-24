"""Shared helpers for the team backend behaviour tests (real HTTP, no mocks)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import httpx

TERMINAL_STATUSES = ("completed", "stopped", "error", "limited")


def client_for(backend) -> httpx.AsyncClient:
    # trust_env=False keeps the shell's proxy variables out of loopback tests.
    return httpx.AsyncClient(base_url=backend.url, timeout=30.0, trust_env=False)


def store_of(backend):
    """The app's own store, used for seeding data and asserting persisted state."""
    return backend.app.state.store


# ------------------------------------------------------------------------ setup


def use_standard_budget(backend) -> None:
    """Pin the workspace to the standard preset, i.e. the 24/32 turn and 64/128 run caps."""
    store_of(backend).set_team_settings(step_budget="standard")


# ------------------------------------------------------------------------ requests


async def get_workspace(client: httpx.AsyncClient) -> dict:
    response = await client.get("/workspace")
    assert response.status_code == 200, response.text
    return response.json()


async def get_conversation(client: httpx.AsyncClient, conversation_id: str) -> dict:
    response = await client.get(f"/conversations/{conversation_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def get_run(client: httpx.AsyncClient, run_id: str) -> dict:
    response = await client.get(f"/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def start_run(
    client: httpx.AsyncClient,
    conversation_id: str,
    content: str = "请帮我完成这个目标",
    *,
    mentions: list[str] | None = None,
    reply_to_id: str | None = None,
) -> dict:
    body: dict = {"content": content}
    if mentions is not None:
        body["mentions"] = mentions
    if reply_to_id is not None:
        body["reply_to_id"] = reply_to_id
    response = await client.post(f"/conversations/{conversation_id}/runs", json=body)
    assert response.status_code == 202, response.text
    return response.json()


async def stop_run(client: httpx.AsyncClient, run_id: str) -> dict:
    response = await client.post(f"/runs/{run_id}/stop", json={})
    assert response.status_code == 200, response.text
    return response.json()


async def wait_for_run(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    timeout: float = 25.0,
    statuses: tuple[str, ...] = TERMINAL_STATUSES,
) -> dict:
    deadline = time.monotonic() + timeout
    run = await get_run(client, run_id)
    while time.monotonic() < deadline:
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.05)
        run = await get_run(client, run_id)
    raise AssertionError(f"timed out waiting for run: {json.dumps(run, ensure_ascii=False)}")


async def wait_for_message(
    client: httpx.AsyncClient,
    conversation_id: str,
    *,
    status: str = "streaming",
    kind: str = "agent",
    timeout: float = 15.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await get_conversation(client, conversation_id)
        for message in detail["messages"]:
            if message["status"] == status and message["kind"] == kind:
                return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {status} {kind} message appeared in {conversation_id}")


async def run_goal(
    client: httpx.AsyncClient,
    conversation_id: str,
    content: str = "请帮我完成这个目标",
    *,
    timeout: float = 25.0,
) -> dict:
    run = await start_run(client, conversation_id, content)
    return await wait_for_run(client, run["id"], timeout=timeout)


# --------------------------------------------------------------------------- SSE


def settled(events: list[tuple[str, dict]]) -> bool:
    """True once any run reported a terminal status, i.e. the interesting part is over."""
    return any(
        name == "run" and payload.get("status") in TERMINAL_STATUSES
        for name, payload in events
    )


async def collect_events(
    client: httpx.AsyncClient,
    url: str,
    *,
    until: Callable[[list[tuple[str, dict]]], bool] | None = None,
    timeout: float = 30.0,
    duration: float | None = None,
) -> list[tuple[str, dict]]:
    """Read SSE frames until ``until`` holds (or for ``duration`` seconds) and return them.

    The server never ends the stream by itself, so every reader has to decide when it has
    seen enough; ``duration`` reads whatever shows up in that window and keeps the
    connection open.
    """
    events: list[tuple[str, dict]] = []
    window = duration if duration is not None else timeout

    async def read() -> None:
        async with client.stream("GET", url) as response:
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
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
                    event, data = None, []
                else:
                    events.append(("comment", {"line": line}))
                if until is not None and event is None and until(events):
                    return

    try:
        await asyncio.wait_for(read(), timeout=window)
    except asyncio.TimeoutError:
        if duration is None:
            raise AssertionError(
                f"timed out waiting for events: {event_names(events)}"
            ) from None
    return events


def event_names(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


def payloads(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [payload for event, payload in events if event == name]


def first_payload(events: list[tuple[str, dict]], name: str) -> dict:
    found = payloads(events, name)
    assert found, f"no {name} event in {event_names(events)}"
    return found[0]


# ------------------------------------------------------------------ request context


def tool_results(request: dict) -> list[tuple[str, dict]]:
    """``(tool_call_id, parsed result)`` for every role=tool message in one request."""
    results: list[tuple[str, dict]] = []
    for message in request["messages"]:
        if message.get("role") != "tool":
            continue
        results.append((message.get("tool_call_id"), json.loads(message["content"])))
    return results


def assistant_tool_calls(request: dict) -> list[dict]:
    """All tool calls the request replays as assistant turns, in order."""
    calls: list[dict] = []
    for message in request["messages"]:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            calls.extend(message["tool_calls"])
    return calls


def system_prompt(request: dict) -> str:
    return next(message["content"] for message in request["messages"] if message["role"] == "system")


def last_user_text(request: dict) -> str:
    """The newest user turn's text: where the clock and the live hints ride."""
    for message in reversed(request["messages"]):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
    return ""


def without_runtime(text: str) -> str:
    """``text`` with the per-turn ``<runtime …/>`` block (and the hints before it) removed."""
    index = text.rfind("<runtime ")
    if index < 0:
        return text
    # The block is ``\n\n<hints>\n\n<tag>`` (the hints paragraph is absent when there are
    # none). Cut at the blank line before the hints, not the one between hints and the tag.
    inner = text.rfind("\n\n", 0, index)
    if inner < 0:
        return text[:index].rstrip("\n")
    start = text.rfind("\n\n", 0, inner)
    return (text[: start if start >= 0 else inner]).rstrip("\n")


def runtime_of(request: dict) -> str:
    """The ``<runtime …/>`` line of one request, or an empty string when it has none."""
    text = last_user_text(request)
    start = text.rfind("<runtime")
    if start < 0:
        return ""
    end = text.find("/>", start)
    return text[start : end + 2] if end >= 0 else text[start:]


def tool_names(request: dict) -> list[str]:
    """Names offered in one request's ``tools`` array (empty when it has none)."""
    return [entry["function"]["name"] for entry in request.get("tools") or []]


def find_result_key(requests: list[dict], key: str) -> str | None:
    """First value of ``key`` seen in any tool result, e.g. a created agent id."""
    for request in requests:
        for _, result in tool_results(request):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def result_keys(requests: list[dict], key: str) -> list[str]:
    values: list[str] = []
    for request in requests:
        for _, result in tool_results(request):
            value = result.get(key)
            if isinstance(value, str) and value and value not in values:
                values.append(value)
    return values


def action_messages(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "action"]


def agent_messages(detail: dict) -> list[dict]:
    return [message for message in detail["messages"] if message["kind"] == "agent"]


def kinds(agents: list[dict]) -> list[str]:
    return [agent["kind"] for agent in agents]


def names_of(items: list[dict]) -> list[str]:
    return [item["name"] for item in items]


def jsonable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
