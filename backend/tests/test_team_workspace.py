"""Workspace, health, agent and conversation route behaviour (real HTTP)."""

from __future__ import annotations

import asyncio

from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    jsonable,
    kinds,
    run_goal,
    start_run,
    stop_run,
    store_of,
    wait_for_message,
)

SUPERVISOR_INSTRUCTIONS = "帮助用户完成目标，自主组建团队"


def test_health_reports_missing_configuration(make_backend) -> None:
    backend = make_backend(api_key="", model="")

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.get("/health")
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "ok"
            assert payload["configured"] is False
            assert payload["model"] == ""
            assert payload["base_url"] == ""
            assert payload["provider_name"] == ""
            assert "模型服务" in payload["configuration_error"]
            assert payload["configuration_error"].endswith("。")
            assert payload["auth"] is False

    asyncio.run(body())


def test_health_and_workspace_never_leak_the_api_key(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            health = (await client.get("/health")).json()
            assert health["configured"] is True
            assert health["configuration_error"] is None
            assert health["model"] == "test-model"
            assert health["provider_name"] == "mify"
            assert "test-key" not in jsonable(health)
            assert "test-key" not in jsonable(await get_workspace(client))

    asyncio.run(body())


def test_new_workspace_bootstraps_one_supervisor_and_direct_chat(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            assert kinds(workspace["agents"]) == ["supervisor"]
            supervisor = workspace["agents"][0]
            assert supervisor["id"] == workspace["supervisor_id"]
            assert supervisor["name"] == "主管"
            assert supervisor["instructions"] == SUPERVISOR_INSTRUCTIONS
            assert len(supervisor["avatar_seed"]) == 32
            assert len(workspace["conversations"]) == 1
            direct = workspace["conversations"][0]
            assert direct["id"] == workspace["direct_conversation_id"]
            assert direct["kind"] == "direct"
            assert direct["title"] == "主管"
            assert direct["purpose"] == "主管私聊"
            assert direct["member_ids"] == [supervisor["id"]]
            assert direct["last_message"] is None
            assert workspace["active_runs"] == []

            detail = await get_conversation(client, direct["id"])
            assert detail["conversation"]["id"] == direct["id"]
            assert detail["messages"] == []
            assert detail["latest_run"] is None
            assert detail["legacy_roles"] == []

    asyncio.run(body())


def test_conversation_last_message_is_a_text_preview(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, direct_id, "先说句话")
            workspace = await get_workspace(client)
            direct = workspace["conversations"][0]
            # Frozen contract: a plain text preview, not a nested message object.
            assert direct["last_message"] == "回复正文"
            assert isinstance(direct["last_message"], str)
            detail = await get_conversation(client, direct_id)
            assert detail["conversation"]["last_message"] == "回复正文"

    asyncio.run(body())


def test_bootstrap_survives_restart_with_stable_identity(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            first = await get_workspace(client)
        backend.restart()
        async with client_for(backend) as client:
            second = await get_workspace(client)
        assert second["agents"] == first["agents"]
        assert second["supervisor_id"] == first["supervisor_id"]
        assert second["direct_conversation_id"] == first["direct_conversation_id"]
        assert len(second["conversations"]) == 1

    asyncio.run(body())


def test_supervisor_rename_updates_direct_title_but_not_message_snapshots(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            supervisor = workspace["agents"][0]
            direct_id = workspace["direct_conversation_id"]
            run = await run_goal(client, direct_id, "先打个招呼")
            assert run["status"] == "completed"
            before = agent_messages(await get_conversation(client, direct_id))
            assert before[0]["author_name"] == "主管"
            assert before[0]["avatar_seed"] == supervisor["avatar_seed"]

            response = await client.patch(
                f"/agents/{supervisor['id']}",
                json={"name": "领队", "instructions": "自主组建团队并汇总结果"},
            )
            assert response.status_code == 200, response.text
            updated = response.json()
            assert updated["id"] == supervisor["id"]
            assert updated["kind"] == "supervisor"
            assert updated["avatar_seed"] == supervisor["avatar_seed"]
            assert updated["name"] == "领队"
            assert updated["instructions"] == "自主组建团队并汇总结果"
            assert updated["created_at"] == supervisor["created_at"]
            assert updated["updated_at"] >= supervisor["updated_at"]

            after = await get_workspace(client)
            assert after["conversations"][0]["title"] == "领队"
            detail = await get_conversation(client, direct_id)
            assert agent_messages(detail)[0]["author_name"] == "主管"

    asyncio.run(body())


def test_patch_agent_rejects_bad_bodies_and_unknown_agents(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            supervisor_id = workspace["supervisor_id"]
            assert (
                await client.patch(f"/agents/{supervisor_id}", json={"name": "   "})
            ).status_code == 422
            assert (await client.patch(f"/agents/{supervisor_id}", json={})).status_code == 422
            # No permission change: `kind` is not an accepted field.
            smuggled = await client.patch(f"/agents/{supervisor_id}", json={"kind": "member"})
            assert smuggled.status_code == 422
            assert "kind" in smuggled.json()["detail"]
            assert (await client.patch("/agents/a_missing", json={"name": "x"})).status_code == 404
            renamed = await client.patch(f"/agents/{supervisor_id}", json={"name": "负责人"})
            assert renamed.status_code == 200
            workspace = await get_workspace(client)
            assert workspace["agents"][0]["kind"] == "supervisor"
            assert workspace["agents"][0]["name"] == "负责人"

    asyncio.run(body())

def test_user_can_create_a_member_but_not_a_second_supervisor(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/agents",
                json={"name": "  研究助理  ", "instructions": "核对事实并给出出处。"},
            )
            assert created.status_code == 201, created.text
            agent = created.json()
            assert agent["kind"] == "member"
            assert agent["name"] == "研究助理"
            assert agent["instructions"] == "核对事实并给出出处。"
            workspace = await get_workspace(client)
            assert [item["id"] for item in workspace["agents"] if item["kind"] == "member"] == [agent["id"]]
            assert (await client.post("/agents", json={"name": " ", "instructions": "x"})).status_code == 422
            smuggled = await client.post(
                "/agents", json={"name": "影子主管", "instructions": "接管", "kind": "supervisor"}
            )
            assert smuggled.status_code == 422
            direct_id = workspace["direct_conversation_id"]
            run = await start_run(client, direct_id, "先不要加人")
            # Member edits are workspace operations, not conversation locks.
            added = await client.post("/agents", json={"name": "后来者", "instructions": "等任务结束"})
            assert added.status_code == 201, added.text
            renamed = await client.patch(f"/agents/{agent['id']}", json={"name": "研究助理二号"})
            assert renamed.status_code == 200, renamed.text
            await stop_run(client, run["id"])

    asyncio.run(body())



def test_conversation_patch_edits_title_purpose_and_members(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="产品研究员", instructions="研究竞品与用户", kind="member")
    group = store.create_conversation(
        kind="group", title="产品验证", purpose="验证产品方向", member_ids=[supervisor["id"]]
    )

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.patch(
                f"/conversations/{group['id']}",
                json={
                    "title": "产品验证小组",
                    "purpose": "验证产品方向并给出结论",
                    "member_ids": [member["id"]],
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["kind"] == "group"
            assert payload["title"] == "产品验证小组"
            assert payload["purpose"] == "验证产品方向并给出结论"
            assert payload["member_ids"] == [member["id"]]

            detail = await get_conversation(client, group["id"])
            assert detail["conversation"]["title"] == "产品验证小组"

            assert (
                await client.patch(f"/conversations/{group['id']}", json={"title": " "})
            ).status_code == 422
            assert (
                await client.patch(f"/conversations/{group['id']}", json={})
            ).status_code == 422
            assert (
                await client.patch(
                    f"/conversations/{group['id']}", json={"member_ids": ["a_missing"]}
                )
            ).status_code == 400
            assert (
                await client.patch(
                    f"/conversations/{group['id']}", json={"member_ids": [member["id"], member["id"]]}
                )
            ).status_code == 422

            direct_id = (await get_workspace(client))["direct_conversation_id"]
            # The supervisor direct thread keeps exactly the supervisor as a member.
            assert (
                await client.patch(
                    f"/conversations/{direct_id}", json={"member_ids": [member["id"]]}
                )
            ).status_code == 400
            assert (await get_conversation(client, direct_id))["conversation"]["member_ids"] == [
                supervisor["id"]
            ]
            renamed = await client.patch(f"/conversations/{direct_id}", json={"title": "我的主管"})
            assert renamed.status_code == 200
            assert renamed.json()["title"] == "我的主管"

            assert (await client.get("/conversations/c_missing")).status_code == 404
            assert (
                await client.patch("/conversations/c_missing", json={"title": "x"})
            ).status_code == 404

    asyncio.run(body())


def test_only_the_running_conversation_is_locked(make_backend, upstream) -> None:
    upstream.mode = "slow"
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 50
    backend = make_backend()
    store = store_of(backend)
    other = store.create_conversation(
        kind="group", title="另一个群", purpose="互不影响", member_ids=[store.supervisor()["id"]]
    )

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            supervisor_id = workspace["supervisor_id"]
            run = await start_run(client, direct_id, "写一份长期计划")
            await wait_for_message(client, direct_id, status="streaming")

            # The running conversation explains why it is locked.
            blocked = await client.patch(f"/conversations/{direct_id}", json={"title": "新标题"})
            assert blocked.status_code == 409, blocked.text
            assert "运行" in blocked.json()["detail"]
            # Everything else stays editable while it runs.
            renamed = await client.patch(f"/agents/{supervisor_id}", json={"name": "新名字"})
            assert renamed.status_code == 200, renamed.text
            other_edit = await client.patch(f"/conversations/{other['id']}", json={"title": "新名字"})
            assert other_edit.status_code == 200, other_edit.text

            stopped = await stop_run(client, run["id"])
            assert stopped["status"] == "stopped"
            assert stopped["finished_at"] is not None

            allowed = await client.patch(
                f"/conversations/{direct_id}", json={"title": "任务结束后改名"}
            )
            assert allowed.status_code == 200, allowed.text

    asyncio.run(body())


def test_start_run_requires_configuration_and_a_known_conversation(make_backend) -> None:
    backend = make_backend(api_key="", model="")

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            response = await client.post(
                f"/conversations/{direct_id}/runs", json={"content": "目标"}
            )
            assert response.status_code == 503, response.text
            detail = await get_conversation(client, direct_id)
            assert detail["messages"] == []
            assert detail["latest_run"] is None

            missing = await client.post(
                "/conversations/c_missing/runs", json={"content": "目标"}
            )
            assert missing.status_code == 404
            assert (
                await client.post(f"/conversations/{direct_id}/runs", json={"content": "  "})
            ).status_code == 422
            assert (await client.get("/runs/r_missing")).status_code == 404
            assert (await client.post("/runs/r_missing/stop", json={})).status_code == 404

    asyncio.run(body())


def test_member_names_are_unique_workspace_wide(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            first = await client.post(
                "/agents", json={"name": "原型设计师", "instructions": "画原型"}
            )
            assert first.status_code == 201, first.text
            clash = await client.post(
                "/agents", json={"name": " 原型设计师 ", "instructions": "另一个职责"}
            )
            assert clash.status_code == 409, clash.text
            assert "已被使用" in clash.json()["detail"]

            latin = await client.post("/agents", json={"name": "Designer", "instructions": "x"})
            assert latin.status_code == 201
            assert (
                await client.post("/agents", json={"name": "designer", "instructions": "x"})
            ).status_code == 409
            # The supervisor's name is taken too, so no second 「主管」 can appear.
            assert (
                await client.post("/agents", json={"name": "主管", "instructions": "x"})
            ).status_code == 409

            other = await client.post("/agents", json={"name": "另一个人", "instructions": "x"})
            assert other.status_code == 201
            renamed = await client.patch(
                f"/agents/{other.json()['id']}", json={"name": " DESIGNER "}
            )
            assert renamed.status_code == 409, renamed.text
            # Keeping the same name (or only its spacing) is not a collision with itself.
            same = await client.patch(f"/agents/{other.json()['id']}", json={"name": "另一个人"})
            assert same.status_code == 200, same.text

    asyncio.run(body())


def test_direct_route_returns_one_chat_per_member(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="研究", kind="member")

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            first = await client.post(f"/agents/{member['id']}/direct")
            assert first.status_code == 200, first.text
            chat = first.json()
            assert chat["kind"] == "direct"
            assert chat["member_ids"] == [member["id"]]
            assert chat["title"] == "甲"
            assert chat["last_message"] is None

            again = await client.post(f"/agents/{member['id']}/direct")
            assert again.status_code == 200
            assert again.json()["id"] == chat["id"]
            conversations = (await get_workspace(client))["conversations"]
            assert len([c for c in conversations if c["kind"] == "direct"]) == 2

            # The supervisor keeps the pinned direct thread from the workspace payload.
            own = await client.post(f"/agents/{supervisor['id']}/direct")
            assert own.status_code == 200
            assert own.json()["id"] == workspace["direct_conversation_id"]

            assert (await client.post("/agents/a_missing/direct")).status_code == 404

    asyncio.run(body())
