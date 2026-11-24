"""Sidebar layout: one-time supervisor pin, PUT round trip, folders, cascade and SSE."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx

from app.store import SCHEMA_VERSION
from helpers import (
    client_for,
    first_payload,
    get_workspace,
    payloads,
    store_of,
)

# A database written by the previous (appearance-free) schema version, with real content.
V3_SCHEMA = """
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
    mentions TEXT NOT NULL DEFAULT '[]',
    reply_to_id TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def build_v3_database(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(V3_SCHEMA)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '3')")
        connection.execute(
            """
            INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at)
            VALUES ('a_sup', '主管', '组建团队', 'supervisor', 'seed-sup',
                    '2026-10-10T09:00:00Z', '2026-10-10T09:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at)
            VALUES ('a_member', '甲', '出方案', 'member', 'seed-jia',
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
            """
            INSERT INTO conversations
                (id, kind, title, purpose, legacy_roles, created_at, updated_at, updated_order)
            VALUES ('c_group', 'group', '方案小组', '出方案', '[]',
                    '2026-10-10T09:00:00Z', '2026-10-10T09:20:00Z', 1)
            """
        )
        connection.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id, position) "
            "VALUES ('c_direct', 'a_sup', 0)"
        )
        connection.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id, position) "
            "VALUES ('c_group', 'a_sup', 0)"
        )
        connection.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id, position) "
            "VALUES ('c_group', 'a_member', 1)"
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


def column_names(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
    finally:
        connection.close()


def schema_version(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return str(
            connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        )
    finally:
        connection.close()


def build_sidebar_team(backend) -> dict:
    """Supervisor plus one member and three conversations to sort into the sidebar."""
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="甲", instructions="出方案", kind="member")
    return {
        "supervisor": supervisor,
        "member": member,
        "direct": store.direct_conversation(),
        "member_direct": store.ensure_direct_conversation(member["id"]),
        "group_a": store.create_conversation(
            kind="group",
            title="方案小组",
            purpose="把方案做出来",
            member_ids=[supervisor["id"], member["id"]],
        ),
        "group_b": store.create_conversation(
            kind="group",
            title="验证小组",
            purpose="验证方向",
            member_ids=[supervisor["id"], member["id"]],
        ),
    }


async def get_sidebar(client: httpx.AsyncClient) -> dict:
    response = await client.get("/sidebar")
    assert response.status_code == 200, response.text
    return response.json()


async def put_sidebar(client: httpx.AsyncClient, items: list[dict], folder_ids: list[str]):
    return await client.put("/sidebar", json={"items": items, "folder_ids": folder_ids})


async def create_folder(client: httpx.AsyncClient, name: str) -> dict:
    response = await client.post("/sidebar/folders", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def folder_named(layout: dict, name: str) -> dict:
    return next(folder for folder in layout["folders"] if folder["name"] == name)


def item_of(layout: dict, conversation_id: str) -> dict | None:
    return next(
        (item for item in layout["items"] if item["conversation_id"] == conversation_id), None
    )


# ------------------------------------------------------------------ one-time seeding


def test_a_new_database_pins_the_supervisor_direct_thread_once(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            layout = await get_sidebar(client)
            assert layout["folders"] == []
            assert layout["items"] == [
                {
                    "conversation_id": direct_id,
                    "pinned": True,
                    "pin_position": 0,
                    "folder_id": None,
                    "position": 0,
                }
            ]

        backend.restart()
        async with client_for(backend) as client:
            again = await get_sidebar(client)
            assert again == layout

            # Unpinning survives a restart: the seeding only ever runs once.
            response = await put_sidebar(client, [], [])
            assert response.status_code == 200, response.text
            assert response.json()["items"] == []

        backend.restart()
        async with client_for(backend) as client:
            assert (await get_sidebar(client))["items"] == []

    asyncio.run(body())


def test_an_existing_v3_database_upgrades_and_pins_the_supervisor_once(tmp_path, make_backend) -> None:
    path = tmp_path / "cubes.sqlite3"
    build_v3_database(path)
    backend = make_backend(database_path=path)

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert workspace["supervisor_id"] == "a_sup"
            assert [agent["id"] for agent in workspace["agents"]] == ["a_sup", "a_member"]
            # New columns read as NULL on migrated rows instead of breaking the payload.
            assert all(agent["appearance"] is None for agent in workspace["agents"])

            detail = await client.get("/conversations/c_direct")
            assert [message["content"] for message in detail.json()["messages"]] == [
                "旧的目标",
                "旧的回复",
            ]
            assert all(message["mood"] is None for message in detail.json()["messages"])

            layout = await get_sidebar(client)
            assert [item["conversation_id"] for item in layout["items"]] == ["c_direct"]
            assert layout["items"][0]["pinned"] is True

    asyncio.run(body())

    assert schema_version(path) == str(SCHEMA_VERSION)
    assert {"appearance"} <= column_names(path, "agents")
    assert {"mood"} <= column_names(path, "messages")
    assert {"sidebar_folders", "sidebar_items"} <= table_names(path)

    # A second open neither re-pins nor rewrites the migrated rows.
    backend.restart()

    async def restart_body() -> None:
        async with client_for(backend) as client:
            layout = await get_sidebar(client)
            assert [item["conversation_id"] for item in layout["items"]] == ["c_direct"]
            detail = (await client.get("/conversations/c_direct")).json()
            assert [message["id"] for message in detail["messages"]] == ["m_user", "m_reply"]

    asyncio.run(restart_body())


# -------------------------------------------------------------------- full PUT layout


def test_put_round_trip_renumbers_in_array_order(make_backend) -> None:
    backend = make_backend()
    team = build_sidebar_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            first = await create_folder(client, "工作")
            second = await create_folder(client, "生活")
            work = folder_named(second, "工作")["id"]
            life = folder_named(second, "生活")["id"]
            assert first["folders"] == [{"id": work, "name": "工作", "position": 0}]
            assert second["folders"] == [
                {"id": work, "name": "工作", "position": 0},
                {"id": life, "name": "生活", "position": 1},
            ]

            response = await put_sidebar(
                client,
                items=[
                    {"conversation_id": team["group_b"]["id"], "pinned": True, "folder_id": None},
                    {"conversation_id": team["direct"]["id"], "pinned": True, "folder_id": None},
                    {
                        "conversation_id": team["member_direct"]["id"],
                        "pinned": False,
                        "folder_id": work,
                    },
                    {
                        "conversation_id": team["group_a"]["id"],
                        "pinned": False,
                        "folder_id": work,
                    },
                    {"conversation_id": team["group_a"]["id"], "pinned": False},
                ],
                folder_ids=[life, work],
            )
            assert response.status_code == 422, response.text  # group_a appears twice

            response = await put_sidebar(
                client,
                items=[
                    {"conversation_id": team["group_b"]["id"], "pinned": True, "folder_id": None},
                    {"conversation_id": team["direct"]["id"], "pinned": True, "folder_id": None},
                    {
                        "conversation_id": team["member_direct"]["id"],
                        "pinned": False,
                        "folder_id": work,
                    },
                    {
                        "conversation_id": team["group_a"]["id"],
                        "pinned": False,
                        "folder_id": work,
                    },
                ],
                folder_ids=[life, work],
            )
            assert response.status_code == 200, response.text
            layout = response.json()
            # Folder positions follow the request array, not creation order.
            assert layout["folders"] == [
                {"id": life, "name": "生活", "position": 0},
                {"id": work, "name": "工作", "position": 1},
            ]
            pinned = [item for item in layout["items"] if item["pinned"]]
            assert [(item["conversation_id"], item["pin_position"]) for item in pinned] == [
                (team["group_b"]["id"], 0),
                (team["direct"]["id"], 1),
            ]
            work_items = [item for item in layout["items"] if item["folder_id"] == work]
            assert [(item["conversation_id"], item["position"]) for item in work_items] == [
                (team["member_direct"]["id"], 0),
                (team["group_a"]["id"], 1),
            ]
            assert len(layout["items"]) == 4
            assert await get_sidebar(client) == layout

            # Only written entries are returned: a fresh PUT replaces the whole layout.
            response = await put_sidebar(
                client,
                items=[
                    {"conversation_id": team["direct"]["id"], "pinned": False, "folder_id": None}
                ],
                folder_ids=[work, life],
            )
            assert response.status_code == 200, response.text
            trimmed = response.json()
            assert [(item["conversation_id"], item["pinned"]) for item in trimmed["items"]] == [
                (team["direct"]["id"], False)
            ]
            assert trimmed["items"][0]["position"] == 0

    asyncio.run(body())


def test_put_rejects_layouts_it_cannot_apply(make_backend) -> None:
    backend = make_backend()
    team = build_sidebar_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            layout = await create_folder(client, "工作")
            work = folder_named(layout, "工作")["id"]

            unknown_conversation = await put_sidebar(
                client,
                items=[{"conversation_id": "c_missing", "pinned": True, "folder_id": None}],
                folder_ids=[work],
            )
            assert unknown_conversation.status_code == 422, unknown_conversation.text
            detail = unknown_conversation.json()["detail"]
            assert "c_missing" in detail
            assert detail.endswith("。")

            unknown_folder = await put_sidebar(
                client,
                items=[
                    {
                        "conversation_id": team["direct"]["id"],
                        "pinned": False,
                        "folder_id": "f_missing",
                    }
                ],
                folder_ids=[work],
            )
            assert unknown_folder.status_code == 422, unknown_folder.text
            assert unknown_folder.json()["detail"].endswith("。")

            duplicate = await put_sidebar(
                client,
                items=[
                    {"conversation_id": team["direct"]["id"], "pinned": True, "folder_id": None},
                    {"conversation_id": team["direct"]["id"], "pinned": False, "folder_id": None},
                ],
                folder_ids=[work],
            )
            assert duplicate.status_code == 422, duplicate.text

            # folder_ids must be a permutation of the folders that exist.
            not_a_permutation = await put_sidebar(client, items=[], folder_ids=[])
            assert not_a_permutation.status_code == 422, not_a_permutation.text
            duplicated_folders = await put_sidebar(
                client, items=[], folder_ids=[work, work]
            )
            assert duplicated_folders.status_code == 422, duplicated_folders.text

            bad_shape = await client.put(
                "/sidebar",
                json={"items": [{"conversation_id": team["direct"]["id"], "pinned": "yes"}], "folder_ids": [work]},
            )
            assert bad_shape.status_code == 422, bad_shape.text

            # A rejected request leaves the stored layout untouched.
            assert [folder["id"] for folder in (await get_sidebar(client))["folders"]] == [work]

    asyncio.run(body())


# ------------------------------------------------------------------------- folders


def test_folder_lifecycle_and_membership(make_backend) -> None:
    backend = make_backend()
    team = build_sidebar_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            created = await create_folder(client, "  工作  ")
            assert created["folders"] == [{"id": created["folders"][0]["id"], "name": "工作", "position": 0}]
            work = created["folders"][0]["id"]

            duplicate = await client.post("/sidebar/folders", json={"name": " 工作 "})
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.json()["detail"] == "已有同名分组。"

            blank = await client.post("/sidebar/folders", json={"name": "   "})
            assert blank.status_code == 422, blank.text
            too_long = await client.post("/sidebar/folders", json={"name": "组" * 25})
            assert too_long.status_code == 422, too_long.text

            renamed = await client.patch(f"/sidebar/folders/{work}", json={"name": "重点工作"})
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["folders"] == [
                {"id": work, "name": "重点工作", "position": 0}
            ]
            assert (await client.patch("/sidebar/folders/f_missing", json={"name": "x"})).status_code == 404

            response = await put_sidebar(
                client,
                items=[
                    {"conversation_id": team["group_a"]["id"], "pinned": False, "folder_id": work},
                    {"conversation_id": team["direct"]["id"], "pinned": True, "folder_id": None},
                ],
                folder_ids=[work],
            )
            assert response.status_code == 200, response.text

            deleted = await client.delete(f"/sidebar/folders/{work}")
            assert deleted.status_code == 200, deleted.text
            after = deleted.json()
            assert after["folders"] == []
            moved = item_of(after, team["group_a"]["id"])
            assert moved is not None and moved["folder_id"] is None
            # The conversation itself is untouched.
            assert (await client.get(f"/conversations/{team['group_a']['id']}")).status_code == 200
            assert (await client.delete(f"/sidebar/folders/{work}")).status_code == 404

    asyncio.run(body())


def test_deleting_a_conversation_cascades_its_sidebar_item(make_backend) -> None:
    backend = make_backend()
    team = build_sidebar_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            layout = await create_folder(client, "工作")
            work = layout["folders"][0]["id"]
            response = await put_sidebar(
                client,
                items=[
                    {"conversation_id": team["group_a"]["id"], "pinned": True, "folder_id": work},
                    {"conversation_id": team["direct"]["id"], "pinned": True, "folder_id": None},
                ],
                folder_ids=[work],
            )
            assert response.status_code == 200, response.text

        connection = sqlite3.connect(str(backend.settings.database_path))
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM conversations WHERE id = ?", (team["group_a"]["id"],))
            connection.commit()
        finally:
            connection.close()

        async with client_for(backend) as client:
            after = await get_sidebar(client)
            assert [item["conversation_id"] for item in after["items"]] == [team["direct"]["id"]]

    asyncio.run(body())


# ----------------------------------------------------------------------------- SSE


def test_each_layout_write_publishes_a_sidebar_event_and_the_snapshot_carries_it(
    make_backend,
) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            events: list[tuple[str, dict]] = []
            attached = asyncio.Event()

            async def read() -> None:
                async with client.stream("GET", "/events") as response:
                    assert response.status_code == 200, response.text
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
                                if event == "snapshot":
                                    attached.set()
                            event, data = None, []
                            if any(name == "sidebar" for name, _ in events):
                                return

            reader = asyncio.create_task(read())
            await asyncio.wait_for(attached.wait(), timeout=10)

            snapshot = first_payload(events, "snapshot")
            assert snapshot["sidebar"] == {
                "folders": [],
                "items": [
                    {
                        "conversation_id": direct_id,
                        "pinned": True,
                        "pin_position": 0,
                        "folder_id": None,
                        "position": 0,
                    }
                ],
            }

            response = await put_sidebar(client, [], [])
            assert response.status_code == 200, response.text
            await asyncio.wait_for(reader, timeout=10)

            pushed = payloads(events, "sidebar")
            assert pushed, "no sidebar event"
            assert pushed[-1] == response.json()
            assert pushed[-1]["items"] == []

    asyncio.run(body())


def test_every_layout_endpoint_publishes_a_sidebar_event(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            events: list[tuple[str, dict]] = []
            attached = asyncio.Event()

            async def read(until_events: int) -> None:
                async with client.stream("GET", "/events") as response:
                    assert response.status_code == 200, response.text
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
                                if event == "snapshot" and not attached.is_set():
                                    attached.set()
                            event, data = None, []
                            if len(payloads(events, "sidebar")) >= until_events:
                                return

            reader = asyncio.create_task(read(4))
            await asyncio.wait_for(attached.wait(), timeout=10)

            created = await client.post("/sidebar/folders", json={"name": "工作"})
            assert created.status_code == 201, created.text
            folder_id = created.json()["folders"][0]["id"]
            assert (await client.patch(f"/sidebar/folders/{folder_id}", json={"name": "生活"})).status_code == 200
            assert (await put_sidebar(client, [], [folder_id])).status_code == 200
            assert (await client.delete(f"/sidebar/folders/{folder_id}")).status_code == 200
            await asyncio.wait_for(reader, timeout=10)

            pushed = payloads(events, "sidebar")
            assert len(pushed) >= 4
            assert pushed[0]["folders"][0]["name"] == "工作"
            assert pushed[1]["folders"][0]["name"] == "生活"
            assert pushed[2]["folders"][0]["name"] == "生活"
            assert pushed[3]["folders"] == []

    asyncio.run(body())
