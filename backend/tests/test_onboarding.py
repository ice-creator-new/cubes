"""First-run onboarding: naming the supervisor, its look, and the old-install escape hatch."""

from __future__ import annotations

import asyncio

from helpers import client_for, collect_events, get_workspace, store_of


def test_a_fresh_workspace_waits_for_onboarding(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert workspace["onboarded"] is False
            supervisor = workspace["agents"][0]
            assert supervisor["name"] == "主管"
            assert workspace["conversations"][0]["title"] == "主管"

    asyncio.run(body())


def test_onboarding_renames_the_supervisor_and_publishes_the_workspace(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            supervisor = (await get_workspace(client))["agents"][0]
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(
                        name == "workspace" and payload.get("onboarded")
                        for name, payload in events
                    ),
                )
            )
            await asyncio.sleep(0.2)
            response = await client.post(
                "/onboarding",
                json={
                    "name": "阿崔",
                    "appearance": {"shape": "star", "color": "#3366cc", "eye": "#ffffff"},
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["onboarded"] is True
            renamed = next(agent for agent in payload["agents"] if agent["id"] == supervisor["id"])
            assert renamed["name"] == "阿崔"
            assert renamed["appearance"] == {"shape": "star", "color": "#3366cc", "eye": "#ffffff"}

            events = await reader
            published = [
                payload
                for name, payload in events
                if name == "workspace" and payload["supervisor_id"] == supervisor["id"]
            ]
            assert published and published[-1]["onboarded"] is True

            workspace = await get_workspace(client)
            assert workspace["onboarded"] is True
            assert next(
                agent for agent in workspace["agents"] if agent["id"] == supervisor["id"]
            )["name"] == "阿崔"
            # The supervisor's own thread follows the rename.
            assert workspace["conversations"][0]["title"] == "阿崔"
            assert workspace["conversations"][0]["id"] == workspace["direct_conversation_id"]

    asyncio.run(body())


def test_onboarding_persists_across_a_restart(make_backend) -> None:
    backend = make_backend()

    async def complete() -> None:
        async with client_for(backend) as client:
            response = await client.post(
                "/onboarding", json={"name": "老周", "appearance": {"shape": "pebble", "color": "jade"}}
            )
            assert response.status_code == 200, response.text

    asyncio.run(complete())
    backend.restart()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert workspace["onboarded"] is True
            supervisor = next(
                agent for agent in workspace["agents"] if agent["id"] == workspace["supervisor_id"]
            )
            assert supervisor["name"] == "老周"
            assert supervisor["appearance"] == {"shape": "pebble", "color": "jade"}

    asyncio.run(body())


def test_an_existing_install_with_messages_is_already_onboarded(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    store.append_message(
        conversation_id=direct["id"],
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="升级前就在用的对话",
        status="complete",
    )

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert workspace["onboarded"] is True
            assert workspace["agents"][0]["name"] == "主管"

    asyncio.run(body())


def test_an_existing_install_with_a_group_is_already_onboarded(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    store.create_conversation(
        kind="group", title="升级前的群组", purpose="旧的工作", member_ids=[]
    )

    async def body() -> None:
        async with client_for(backend) as client:
            assert (await get_workspace(client))["onboarded"] is True

    asyncio.run(body())


def test_onboarding_rejects_an_unknown_appearance(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.post(
                "/onboarding", json={"name": "阿崔", "appearance": {"shape": "not-a-shape", "color": "sky"}}
            )
            assert response.status_code == 422, response.text
            assert "形状" in response.json()["detail"]
            assert (await get_workspace(client))["onboarded"] is False

    asyncio.run(body())
