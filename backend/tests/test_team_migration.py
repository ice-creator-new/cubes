"""Legacy roundtable migration: backup, one transaction, history preserved read-only."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import BACKUP_SUFFIX, SCHEMA_VERSION, Store, legacy_avatar_seed
from helpers import client_for, get_conversation, get_workspace, run_goal

OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    topic TEXT NOT NULL,
    rounds INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    agents TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id TEXT,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    round INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
"""

LEGACY_AGENTS = [
    {"id": "proposer", "name": "探索者", "role": "提出可行方案", "color": "blue", "symbol": "sparkles"},
    {"id": "skeptic", "name": "质疑者", "role": "指出方案风险", "color": "orange", "symbol": "bolt"},
]


def build_legacy_database(path: Path, *, corrupt: bool = False) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(OLD_SCHEMA)
        connection.execute(
            """
            INSERT INTO sessions
                (id, title, topic, rounds, status, error, agents, created_at, updated_at, updated_order)
            VALUES ('s1', '旧讨论一', '如何为独立开发者做产品？', 2, 'completed', NULL, ?, ?, ?, 1)
            """,
            (
                json.dumps(LEGACY_AGENTS, ensure_ascii=False),
                "2026-10-01T10:00:00Z",
                "2026-10-01T10:05:00Z",
            ),
        )
        agents_two = "not json at all" if corrupt else json.dumps(LEGACY_AGENTS[:1], ensure_ascii=False)
        connection.execute(
            """
            INSERT INTO sessions
                (id, title, topic, rounds, status, error, agents, created_at, updated_at, updated_order)
            VALUES ('s2', '旧讨论二', '第二个话题', 2, 'stopped', NULL, ?, ?, ?, 2)
            """,
            (agents_two, "2026-10-02T09:00:00Z", "2026-10-02T09:30:00Z"),
        )
        connection.execute(
            """
            INSERT INTO messages (id, session_id, agent_id, author, content, kind, status, round, seq, created_at)
            VALUES ('old_m1', 's1', NULL, '你', '如何为独立开发者做产品？', 'user', 'complete', 0, 1, '2026-10-01T10:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO messages (id, session_id, agent_id, author, content, kind, status, round, seq, created_at)
            VALUES ('old_m2', 's1', 'proposer', '探索者', '先做小范围调研', 'agent', 'complete', 1, 2, '2026-10-01T10:01:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO messages (id, session_id, agent_id, author, content, kind, status, round, seq, created_at)
            VALUES ('old_m3', 's1', 'skeptic', '质疑者', '风险很高，先验证需求', 'agent', 'streaming', 1, 3, '2026-10-01T10:02:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO messages (id, session_id, agent_id, author, content, kind, status, round, seq, created_at)
            VALUES ('old_m4', 's2', NULL, '你', '第二个话题', 'user', 'complete', 0, 1, '2026-10-02T09:00:00Z')
            """
        )
        connection.commit()
    finally:
        connection.close()


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()


def test_legacy_sessions_migrate_into_read_only_history_groups(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    build_legacy_database(path)
    app = create_app(Settings(database_path=path))

    with TestClient(app) as client:
        workspace = client.get("/workspace").json()
        supervisor_id = workspace["supervisor_id"]
        assert [agent["kind"] for agent in workspace["agents"]] == ["supervisor"]
        assert workspace["agents"][0]["name"] == "主管"
        assert workspace["agents"][0]["id"] == supervisor_id

        kinds = [conversation["kind"] for conversation in workspace["conversations"]]
        assert kinds == ["direct", "group", "group"]
        groups = [c for c in workspace["conversations"] if c["kind"] == "group"]
        assert [group["title"] for group in groups] == ["旧讨论二", "旧讨论一"]

        detail = client.get("/conversations/s1").json()
        conversation = detail["conversation"]
        assert conversation["title"] == "旧讨论一"
        assert conversation["purpose"] == "如何为独立开发者做产品？"
        assert conversation["member_ids"] == [supervisor_id]
        assert conversation["created_at"] == "2026-10-01T10:00:00Z"
        assert conversation["updated_at"] == "2026-10-01T10:05:00Z"
        assert detail["latest_run"] is None

        assert [message["id"] for message in detail["messages"]] == [
            "old_m1",
            "old_m2",
            "old_m3",
        ]
        assert [message["seq"] for message in detail["messages"]] == [1, 2, 3]
        assert [message["created_at"] for message in detail["messages"]] == [
            "2026-10-01T10:00:00Z",
            "2026-10-01T10:01:00Z",
            "2026-10-01T10:02:00Z",
        ]
        assert [message["status"] for message in detail["messages"]] == [
            "complete",
            "complete",
            "interrupted",
        ]
        assert [message["author_name"] for message in detail["messages"]] == [
            "你",
            "探索者",
            "质疑者",
        ]
        assert all(message["author_id"] is None for message in detail["messages"])
        assert detail["messages"][0]["avatar_seed"] is None
        assert detail["messages"][2]["content"] == "风险很高，先验证需求"

        expected_roles = [
            {
                "name": agent["name"],
                "instructions": agent["role"],
                "avatar_seed": legacy_avatar_seed("s1", agent["id"]),
            }
            for agent in LEGACY_AGENTS
        ]
        assert detail["legacy_roles"] == expected_roles
        assert detail["messages"][1]["avatar_seed"] == expected_roles[0]["avatar_seed"]
        assert detail["messages"][2]["avatar_seed"] == expected_roles[1]["avatar_seed"]

    names = table_names(path)
    assert "sessions" not in names
    assert "sessions_legacy" not in names
    assert "messages_legacy" not in names
    assert {
        "agents",
        "conversations",
        "conversation_members",
        "messages",
        "runs",
        "actions",
        "tool_invocations",
        "meta",
    } <= names
    connection = sqlite3.connect(str(path))
    try:
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert version[0] == str(SCHEMA_VERSION)
    finally:
        connection.close()

    backup = path.with_name(path.name + BACKUP_SUFFIX)
    assert backup.exists()
    connection = sqlite3.connect(str(backup))
    try:
        rows = connection.execute("SELECT id, title FROM sessions ORDER BY updated_order").fetchall()
        assert rows == [("s1", "旧讨论一"), ("s2", "旧讨论二")]
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
    finally:
        connection.close()


def test_migration_is_idempotent_across_restarts(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    build_legacy_database(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)

    with TestClient(create_app(Settings(database_path=path))) as client:
        first = client.get("/workspace").json()
    backup_stat = backup.stat()

    with TestClient(create_app(Settings(database_path=path))) as client:
        second = client.get("/workspace").json()
        detail = client.get("/conversations/s1").json()

    assert second == first
    assert len([c for c in second["conversations"] if c["kind"] == "group"]) == 2
    assert [message["id"] for message in detail["messages"]] == ["old_m1", "old_m2", "old_m3"]
    assert backup.stat().st_size == backup_stat.st_size


def test_migration_failure_keeps_the_original_database_recoverable(tmp_path) -> None:
    path = tmp_path / "broken.sqlite3"
    build_legacy_database(path, corrupt=True)
    store = Store(path)
    with pytest.raises(Exception):
        store.initialize()
    store.close()

    names = table_names(path)
    assert "sessions" in names
    assert "messages" in names
    assert "meta" not in names
    assert "conversations" not in names

    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
    finally:
        connection.close()

    backup = path.with_name(path.name + BACKUP_SUFFIX)
    assert backup.exists()
    connection = sqlite3.connect(str(backup))
    try:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    finally:
        connection.close()

    # The live database stays usable after the failure is fixed.
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("UPDATE sessions SET agents = ? WHERE id = 's2'", (json.dumps([]),))
        connection.commit()
    finally:
        connection.close()
    with TestClient(create_app(Settings(database_path=path))) as client:
        workspace = client.get("/workspace").json()
        assert len([c for c in workspace["conversations"] if c["kind"] == "group"]) == 2


def test_migration_replaces_a_torn_backup_with_a_complete_snapshot(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    build_legacy_database(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    # A previous attempt died mid-copy and left something that is not a database.
    backup.write_bytes(b"torn backup, not a database")

    with TestClient(create_app(Settings(database_path=path))) as client:
        assert client.get("/workspace").status_code == 200

    connection = sqlite3.connect(str(backup))
    try:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
    finally:
        connection.close()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_backup_failure_aborts_migration_and_keeps_the_previous_backup(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "legacy.sqlite3"
    build_legacy_database(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    shutil.copy2(path, backup)
    previous = backup.read_bytes()

    def failing_copy(source, target) -> None:
        # A real crash mid-copy would leave a partial temp file behind.
        target.write_bytes(b"partial copy")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("app.store.db._copy_database", failing_copy)

    store = Store(path)
    with pytest.raises(sqlite3.OperationalError):
        store.initialize()
    store.close()

    # The migration aborted before touching anything.
    names = table_names(path)
    assert "sessions" in names
    assert "messages" in names
    assert "meta" not in names
    assert "conversations" not in names
    assert not list(tmp_path.glob("*.tmp-*"))
    # The earlier good copy is untouched, byte for byte.
    assert backup.read_bytes() == previous

    # Retrying without the fault migrates and refreshes the snapshot atomically.
    monkeypatch.undo()
    with TestClient(create_app(Settings(database_path=path))) as client:
        workspace = client.get("/workspace").json()
        assert len([c for c in workspace["conversations"] if c["kind"] == "group"]) == 2

    connection = sqlite3.connect(str(backup))
    try:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    finally:
        connection.close()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_legacy_messages_without_sessions_abort_instead_of_dropping_rows(tmp_path) -> None:
    path = tmp_path / "orphan.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(OLD_SCHEMA)
        connection.execute("DROP TABLE sessions")
        connection.execute(
            """
            INSERT INTO messages
                (id, session_id, agent_id, author, content, kind, status, round, seq, created_at)
            VALUES ('m_orphan', 's_gone', NULL, '你', '孤儿消息', 'user', 'complete', 0, 1,
                    '2026-10-01T10:00:00Z')
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = Store(path)
    with pytest.raises(RuntimeError):
        store.initialize()
    store.close()

    names = table_names(path)
    assert "messages" in names
    assert "meta" not in names
    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        connection.close()
    # The rows that were at risk are still in the recovery copy.
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    connection = sqlite3.connect(str(backup))
    try:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        connection.close()


def test_a_migrated_group_can_be_continued_with_a_new_goal(make_backend, upstream, tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    build_legacy_database(path)
    backend = make_backend(database_path=path)
    upstream.script = [{"content": "基于旧记录的新结论"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert [agent["kind"] for agent in workspace["agents"]] == ["supervisor"]
            groups = [c for c in workspace["conversations"] if c["kind"] == "group"]
            legacy = next(group for group in groups if group["title"] == "旧讨论一")

            run = await run_goal(client, legacy["id"], "继续旧话题")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, legacy["id"])
            assert [message["kind"] for message in detail["messages"]] == [
                "user",
                "agent",
                "agent",
                "user",
                "agent",
            ]
            assert detail["messages"][-1]["content"] == "基于旧记录的新结论"
            assert detail["conversation"]["title"] == "旧讨论一"
            assert detail["legacy_roles"][0]["name"] == "探索者"
            # The new run only adds the supervisor: old roles are not team members.
            workspace = await get_workspace(client)
            assert [agent["kind"] for agent in workspace["agents"]] == ["supervisor"]

        contents = [message["content"] for message in upstream.requests[0]["messages"]]
        assert "如何为独立开发者做产品？" in contents
        assert "[探索者] 先做小范围调研" in contents
        # A legacy turn cut off mid-stream is replayed as an interrupted reply, marked as such.
        assert "[质疑者] 风险很高，先验证需求\n\n（这条回复还没说完就被打断了。）" in contents

    asyncio.run(body())


V2_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    instructions TEXT NOT NULL,
    kind TEXT NOT NULL,
    avatar_seed TEXT NOT NULL,
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
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, agent_id)
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    author_id TEXT,
    author_name TEXT NOT NULL,
    avatar_seed TEXT,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    action_id TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
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
    message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE tool_invocations (
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    result TEXT,
    action_id TEXT NOT NULL,
    PRIMARY KEY (run_id, tool_call_id)
);
"""


def build_v2_database(path: Path) -> None:
    """A database written by the previous (mention-free) schema, with real content."""
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(V2_SCHEMA)
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '2')"
        )
        connection.execute(
            """
            INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at)
            VALUES ('a_sup', '主管', '组建团队', 'supervisor', 'seed-sup',
                    '2026-10-10T09:00:00Z', '2026-10-10T09:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO conversations
                (id, kind, title, purpose, legacy_roles, created_at, updated_at, updated_order)
            VALUES ('c_direct', 'direct', '主管', '主管私聊', '[]',
                    '2026-10-10T09:00:00Z', '2026-10-10T09:30:00Z', 2)
            """
        )
        connection.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id, position) "
            "VALUES ('c_direct', 'a_sup', 0)"
        )
        connection.execute(
            """
            INSERT INTO messages
                (id, conversation_id, author_id, author_name, avatar_seed, kind, content,
                 status, action_id, seq, created_at)
            VALUES ('m_user', 'c_direct', NULL, '你', NULL, 'user', '旧的目标', 'complete', NULL, 1,
                    '2026-10-10T09:10:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO messages
                (id, conversation_id, author_id, author_name, avatar_seed, kind, content,
                 status, action_id, seq, created_at)
            VALUES ('m_reply', 'c_direct', 'a_sup', '主管', 'seed-sup', 'agent', '旧的回复',
                    'complete', NULL, 2, '2026-10-10T09:20:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO runs
                (id, conversation_id, goal, status, current_step, error, model_calls, tool_calls,
                 created_at, updated_at, finished_at)
            VALUES ('r_old', 'c_direct', '旧的目标', 'completed', '任务已完成', NULL, 1, 0,
                    '2026-10-10T09:10:00Z', '2026-10-10T09:20:00Z', '2026-10-10T09:20:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO actions
                (id, run_id, conversation_id, tool_call_id, tool_name, arguments, status, summary,
                 result, error, target_agent_id, target_conversation_id, message_id, created_at,
                 updated_at)
            VALUES ('x_old', 'r_old', 'c_direct', 'call_old', 'create_agent', '{}', 'error',
                    '创建成员失败：旧记录', NULL, '旧错误', NULL, NULL, NULL,
                    '2026-10-10T09:15:00Z', '2026-10-10T09:15:00Z')
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_v2_database_migrates_forward_without_losing_rows(tmp_path) -> None:
    path = tmp_path / "cubes.sqlite3"
    build_v2_database(path)

    with TestClient(create_app(Settings(database_path=path))) as client:
        workspace = client.get("/workspace").json()
        assert workspace["supervisor_id"] == "a_sup"
        assert workspace["direct_conversation_id"] == "c_direct"
        assert workspace["active_runs"] == []

        detail = client.get("/conversations/c_direct").json()
        assert [message["id"] for message in detail["messages"]] == ["m_user", "m_reply"]
        assert [message["content"] for message in detail["messages"]] == ["旧的目标", "旧的回复"]
        # The new columns appear with their documented defaults.
        assert all(message["mentions"] == [] for message in detail["messages"])
        assert all(message["reply_to_id"] is None for message in detail["messages"])
        run = detail["latest_run"]
        assert run["id"] == "r_old"
        assert run["status"] == "completed"
        assert run["model_calls"] == 1
        assert run["hops"] == 0
        assert run["current_agent_id"] is None
        assert run["pending_agent_ids"] == []
        actions = [m for m in detail["messages"] if m["kind"] == "action"]
        assert actions == []

    connection = sqlite3.connect(str(path))
    try:
        assert (
            connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
            == str(SCHEMA_VERSION)
        )
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT mentions, reply_to_id FROM messages WHERE id = 'm_reply'"
            ).fetchone()
            == ("[]", None)
        )
        assert (
            connection.execute(
                "SELECT current_agent_id, pending_agent_ids, hops FROM runs WHERE id = 'r_old'"
            ).fetchone()
            == (None, "[]", 0)
        )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert {"mentions", "reply_to_id", "mood"} <= columns
        run_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert {"current_agent_id", "pending_agent_ids", "hops"} <= run_columns
        assert (
            connection.execute("SELECT appearance FROM agents WHERE id = 'a_sup'").fetchone()
            == (None,)
        )
        assert (
            connection.execute("SELECT mood FROM messages WHERE id = 'm_reply'").fetchone()
            == (None,)
        )
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"sidebar_folders", "sidebar_items"} <= tables
    finally:
        connection.close()
    # Only legacy roundtable migrations snapshot the pre-team database.
    assert not path.with_name(path.name + BACKUP_SUFFIX).exists()


def test_v2_migration_is_idempotent_and_keeps_new_columns(tmp_path) -> None:
    path = tmp_path / "cubes.sqlite3"
    build_v2_database(path)

    with TestClient(create_app(Settings(database_path=path))) as client:
        first = client.get("/conversations/c_direct").json()
    with TestClient(create_app(Settings(database_path=path))) as client:
        second = client.get("/conversations/c_direct").json()
    assert second == first

    # A third open with the columns already present is still a no-op.
    store = Store(path)
    store.initialize()
    store.close()
    with TestClient(create_app(Settings(database_path=path))) as client:
        assert client.get("/conversations/c_direct").json() == first


def test_v32_archives_reply_tasks_and_heals_stale_ones(tmp_path) -> None:
    """An old-shape v31 database: reply-made tasks are archived, real threads kept and healed."""
    path = tmp_path / "cubes.sqlite3"
    store = Store(path)
    store.initialize()
    boss = store.supervisor()
    member = store.create_agent(name="甲", instructions="出方案", kind="member")
    group = store.create_conversation(
        kind="group", title="方案组", purpose="出方案", member_ids=[boss["id"], member["id"]]
    )
    keep_thread = store.create_conversation(
        kind="thread", title="甲 · 保留", purpose="保留", member_ids=[member["id"]]
    )
    queued_thread = store.create_conversation(
        kind="thread", title="甲 · 排队", purpose="排队", member_ids=[member["id"]]
    )
    kept = store.create_group_task(
        conversation_id=group["id"], owner_id=member["id"], title="有线程", status="running"
    )
    store.update_group_task(kept["id"], thread_id=keep_thread["id"])
    reply_task = store.create_group_task(
        conversation_id=group["id"], owner_id=member["id"], title="普通回复", status="done"
    )
    old_running = store.create_group_task(
        conversation_id=group["id"], owner_id=member["id"], title="旧的进行中", status="running"
    )
    queued = store.create_group_task(
        conversation_id=group["id"], owner_id=member["id"], title="排队的", status="queued"
    )
    store.update_group_task(queued["id"], thread_id=queued_thread["id"])
    store.close()

    # Rewind the copy to the v31 shape: the v32 columns do not exist yet.
    connection = sqlite3.connect(str(path))
    try:
        for statement in (
            "ALTER TABLE messages DROP COLUMN route",
            "ALTER TABLE messages DROP COLUMN addresses",
            "ALTER TABLE messages DROP COLUMN status_card",
            "ALTER TABLE group_tasks DROP COLUMN end_reason",
            "ALTER TABLE group_tasks DROP COLUMN stopped_by",
            "ALTER TABLE group_tasks DROP COLUMN dismissed_at",
            "ALTER TABLE group_tasks DROP COLUMN seen_at",
            "UPDATE meta SET value = '31' WHERE key = 'schema_version'",
        ):
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()

    store = Store(path)
    store.initialize()
    try:
        assert store.team_settings()["dispatch_model"] is None
        kept_row = store.get_group_task(kept["id"])
        reply_row = store.get_group_task(reply_task["id"])
        running_row = store.get_group_task(old_running["id"])
        queued_row = store.get_group_task(queued["id"])
        # A real thread is never archived; the run a dead process left is healed and says so.
        assert kept_row["dismissed_at"] is None
        assert (kept_row["status"], kept_row["stopped_by"]) == ("stopped", "重启后中断")
        # Tasks a plain reply made are archived, but the timeline's card/history stays.
        assert reply_row["dismissed_at"] is not None
        assert running_row["dismissed_at"] is not None
        assert (running_row["status"], running_row["stopped_by"]) == ("stopped", "重启后中断")
        assert (queued_row["status"], queued_row["stopped_by"]) == ("stopped", "重启后中断")
        # Archived rows are out of the sidebar and the chip list.
        listed = {entry["task_id"] for entry in store.list_group_threads()}
        assert kept["id"] in listed  # stopped and un-dismissed: still shown until dismissed
        assert reply_task["id"] not in listed
    finally:
        store.close()
    connection = sqlite3.connect(str(path))
    try:
        assert (
            connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
            == str(SCHEMA_VERSION)
        )
    finally:
        connection.close()
