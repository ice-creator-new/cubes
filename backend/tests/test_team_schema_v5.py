"""Forward migration 4 -> 5: relay columns are additive, every row survives."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from app.store import SCHEMA_VERSION
from helpers import client_for, get_conversation, get_workspace

V4_SCHEMA = """
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
"""


def build_v4_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(V4_SCHEMA)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '4')")
        connection.execute(
            "INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at) "
            "VALUES ('a_sup', '主管', '组建团队', 'supervisor', 'seed-sup', "
            "'2026-10-10T09:00:00Z', '2026-10-10T09:00:00Z')"
        )
        connection.execute(
            "INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at) "
            "VALUES ('a_member', '甲', '出方案', 'member', 'seed-jia', "
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
            "VALUES ('m_user', 'c_direct', NULL, '你', NULL, 'user', '旧目标', 'complete', "
            "NULL, '[]', NULL, NULL, 1, '2026-10-10T09:10:00Z')"
        )
        connection.execute(
            "INSERT INTO messages (id, conversation_id, author_id, author_name, avatar_seed, "
            "kind, content, status, action_id, mentions, reply_to_id, mood, seq, created_at) "
            "VALUES ('m_reply', 'c_direct', 'a_sup', '主管', 'seed-sup', 'agent', '旧回复', "
            "'complete', NULL, '[\"a_member\"]', 'm_user', 'happy', 2, "
            "'2026-10-10T09:20:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


def test_a_v4_database_upgrades_to_v5_without_losing_rows(tmp_path, make_backend) -> None:
    path = tmp_path / "cubes-v4.sqlite3"
    build_v4_database(path)
    backend = make_backend(database_path=path)

    async def body() -> None:
        async with client_for(backend) as client:
            detail = await get_conversation(client, "c_direct")
            assert [m["id"] for m in detail["messages"]] == ["m_user", "m_reply"]
            reply = detail["messages"][1]
            assert reply["mentions"] == ["a_member"]
            assert reply["reply_to_id"] == "m_user"
            assert reply["mood"] == "happy"
            assert reply["relay"] is None

            workspace = await get_workspace(client)
            assert workspace["direct_conversation_id"] == "c_direct"
            supervisor = workspace["agents"][0]
            assert supervisor["id"] == "a_sup"
            # Private conversations are never auto-filled with the supervisor.
            assert workspace["conversations"][0]["member_ids"] == ["a_sup"]

    asyncio.run(body())

    connection = sqlite3.connect(str(path))
    try:
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        connection.close()
    assert str(version) == str(SCHEMA_VERSION)
    assert {"relay_conversation_id", "relay_direction", "relay_peer_id", "relay_peer_name"} <= columns
