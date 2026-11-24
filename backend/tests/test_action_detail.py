"""The user-facing ``detail`` under a finished action row.

Covers the wire shape on a real run, the toolkit-level detail of the web and utility
packs, and the additive column migration that keeps older action rows intact.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.store import SCHEMA_VERSION
from app.toolkit import net, split_result, tool_specs
from app.toolkit.base import ToolContext
from fake_upstream import call, tool_calls
from helpers import action_messages, client_for, get_conversation, run_goal

SEARCH_RESULTS = [
    {
        "title": "Tauri 2.12 发布",
        "url": "https://tauri.app/blog/212",
        "content": "Tauri 2.12 已发布，改进了窗口材质与开发服务器。",
        "published_date": "2026-10-01",
    },
    {
        "title": "窗口材质指南",
        "url": "https://tauri.app/learn/material",
        "content": "系统材质与背景模糊的取舍。",
        "published_date": "2024-09-01",
    },
]


class _SearchHandler(BaseHTTPRequestHandler):
    """A Tavily-shaped search endpoint: no network, no keys."""

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        body = json.dumps({"results": SEARCH_RESULTS}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


class _SearchServer:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _SearchHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/search"

    def __enter__(self) -> "_SearchServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


# ------------------------------------------------------------------ the wire


def test_web_search_detail_reaches_the_conversation(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "web_search", {"query": "Tauri 2.12 发布了什么"})),
        {"content": "Tauri 2.12 已经发布。"},
    ]
    with _SearchServer() as search:
        backend = make_backend(
            search_providers=("tavily",),
            tavily_api_url=search.url,
            tavily_api_key="test-key",
        )

        async def body() -> None:
            async with client_for(backend) as client:
                direct_id = (await client.get("/workspace")).json()["direct_conversation_id"]
                run = await run_goal(client, direct_id, "查一下 Tauri 2.12")
                assert run["status"] == "completed", run
                detail = await get_conversation(client, direct_id)
                actions = action_messages(detail)
                assert len(actions) == 1
                action = actions[0]["action"]
                assert action["status"] == "completed"
                assert action["summary"] == "搜索「Tauri 2.12 发布了什么」"
                assert action["detail"] == {
                    "kind": "search",
                    "query": "Tauri 2.12 发布了什么",
                    "provider": "tavily",
                    "items": [
                        {
                            "title": "Tauri 2.12 发布",
                            "url": "https://tauri.app/blog/212",
                            "snippet": "Tauri 2.12 已发布，改进了窗口材质与开发服务器。",
                        },
                        {
                            "title": "窗口材质指南",
                            "url": "https://tauri.app/learn/material",
                            "snippet": "系统材质与背景模糊的取舍。",
                        },
                    ],
                    "title": None,
                    "url": None,
                    "text": None,
                }

        asyncio.run(body())


def test_a_failed_tool_keeps_detail_empty(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "calculate", {"expression": "1 / 0"})),
        {"content": "这个算不出来。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await client.get("/workspace")).json()["direct_conversation_id"]
            run = await run_goal(client, direct_id, "算一下")
            assert run["status"] == "completed", run
            action = action_messages(await get_conversation(client, direct_id))[0]["action"]
            assert action["status"] == "error"
            assert "除以零" in action["error"]
            assert action["detail"] is None

    asyncio.run(body())


# --------------------------------------------------------------- tool detail


def _settings(tmp_path: Path) -> Settings:
    return Settings(database_path=tmp_path / "cubes.sqlite3")


def _spec(settings: Settings, name: str):
    return next(spec for spec in tool_specs(settings, "supervisor") if spec.name == name)


def _context(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> ToolContext:
    return ToolContext(
        store=None,  # type: ignore[arg-type]
        settings=settings,
        run_id="r1",
        conversation_id="c1",
        agent={"id": "a1", "name": "主管", "kind": "supervisor"},
        http=httpx.AsyncClient(transport=transport, trust_env=False, timeout=5.0),
        publish=lambda event, payload: None,
    )


PAGE_HTML = """<!doctype html>
<html><head><title>示例页面</title></head>
<body><h1>一级标题</h1><p>正文段落。</p></body></html>
"""


def test_fetch_url_detail_names_the_page(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    settings = _settings(tmp_path)
    context = _context(settings, httpx.MockTransport(handler))

    async def body() -> None:
        try:
            with net.override_resolver(lambda host: ["93.184.216.34"]):
                text, detail = split_result(
                    await _spec(settings, "fetch_url").run(
                        context, {"url": "http://public.example/page"}
                    )
                )
        finally:
            await context.http.aclose()
        assert "标题：示例页面" in text
        assert detail is not None
        assert detail["kind"] == "page"
        assert detail["url"] == "http://public.example/page"
        assert detail["title"] == "示例页面"
        assert "正文段落。" in detail["text"]
        assert len(detail["text"]) <= 600

    asyncio.run(body())


def test_calculate_detail_is_the_answer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    context = _context(settings)

    async def body() -> None:
        try:
            text, detail = split_result(
                await _spec(settings, "calculate").run(context, {"expression": "12 * 12"})
            )
        finally:
            await context.http.aclose()
        assert text == "12 * 12 = 144"
        assert detail == {"kind": "text", "text": "12 * 12 = 144"}

    asyncio.run(body())


class _EmptyStore:
    """The workbench only needs to list; an empty workspace is a valid answer."""

    def list_notes(self, query: str | None = None) -> list[dict]:
        return []


def test_note_tools_carry_no_detail(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    async def body() -> None:
        context = _context(settings)
        context.store = _EmptyStore()
        try:
            result = await _spec(settings, "note_list").run(context, {})
        finally:
            await context.http.aclose()
        # No detail: notes and tasks are already visible in the workbench.
        assert isinstance(result, str)

    asyncio.run(body())


# ----------------------------------------------------------------- migration


OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    instructions TEXT NOT NULL,
    kind TEXT NOT NULL,
    avatar_seed TEXT NOT NULL,
    appearance TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL,
    legacy_roles TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_order INTEGER NOT NULL
);
CREATE TABLE conversation_members (
    conversation_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, agent_id)
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    author_id TEXT,
    author_name TEXT NOT NULL,
    avatar_seed TEXT,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    action_id TEXT,
    mentions TEXT NOT NULL DEFAULT '[]',
    reply_to_id TEXT,
    mood TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    current_step TEXT NOT NULL,
    error TEXT,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE actions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    result TEXT,
    error TEXT,
    target_agent_id TEXT,
    target_conversation_id TEXT,
    confirmation TEXT,
    message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def build_old_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(OLD_SCHEMA)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '5')")
        connection.execute(
            "INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at) "
            "VALUES ('a_sup', '主管', '组建团队', 'supervisor', 'seed-sup', "
            "'2026-10-10T09:00:00Z', '2026-10-10T09:00:00Z')"
        )
        connection.execute(
            "INSERT INTO conversations (id, kind, title, purpose, legacy_roles, created_at, "
            "updated_at, updated_order) VALUES "
            "('c_direct', 'direct', '主管', '主管私聊', '[]', "
            "'2026-10-10T09:00:00Z', '2026-10-10T09:30:00Z', 2)"
        )
        connection.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id, position) "
            "VALUES ('c_direct', 'a_sup', 0)"
        )
        connection.execute(
            "INSERT INTO messages (id, conversation_id, author_id, author_name, avatar_seed, "
            "kind, content, status, action_id, mentions, reply_to_id, mood, seq, created_at) "
            "VALUES ('m_action', 'c_direct', 'a_sup', '主管', 'seed-sup', 'action', "
            "'搜索旧记录', 'complete', 'ac_old', '[]', NULL, NULL, 1, "
            "'2026-10-10T09:10:00Z')"
        )
        connection.execute(
            "INSERT INTO runs (id, conversation_id, status, current_step, created_at, "
            "updated_at, finished_at) VALUES ('r_old', 'c_direct', 'completed', '完成', "
            "'2026-10-10T09:00:00Z', '2026-10-10T09:30:00Z', '2026-10-10T09:30:00Z')"
        )
        connection.execute(
            "INSERT INTO actions (id, run_id, conversation_id, tool_call_id, tool_name, "
            "arguments, status, summary, result, created_at, updated_at, message_id) "
            "VALUES ('ac_old', 'r_old', 'c_direct', 'call_old', 'web_search', '{}', "
            "'completed', '搜索「旧问题」', '旧结果', '2026-10-10T09:10:00Z', "
            "'2026-10-10T09:10:00Z', 'm_action')"
        )
        connection.commit()
    finally:
        connection.close()


def test_the_detail_column_is_added_without_touching_old_rows(tmp_path, make_backend) -> None:
    path = tmp_path / "cubes-old.sqlite3"
    build_old_database(path)
    backend = make_backend(database_path=path)

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await get_conversation(client, "c_direct")
            actions = action_messages(detail)
            assert len(actions) == 1
            assert actions[0]["action"]["summary"] == "搜索「旧问题」"
            # An action written before the column existed simply has no detail.
            assert actions[0]["action"]["detail"] is None

    asyncio.run(body())

    connection = sqlite3.connect(str(path))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(actions)").fetchall()}
        count = connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "detail" in columns
    assert count == 1
    assert str(version) == str(SCHEMA_VERSION)
