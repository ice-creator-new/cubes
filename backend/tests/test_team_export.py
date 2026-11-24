"""Team export/import: one Markdown file, previewed then applied into a fresh workspace."""

from __future__ import annotations

import asyncio

from helpers import client_for, get_workspace, store_of


def test_export_and_import_round_trip_without_secrets(make_backend) -> None:
    source = make_backend()
    store = store_of(source)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="研究员", instructions="负责查资料并给出结论。")
    store.update_agent(
        member["id"],
        token_budget={"limit": 50000, "period": "week"},
        permission_mode="auto",
        tool_permissions={"code": True, "files": True, "computer": False, "connectors": "read", "skills_disabled": ["写周报"]},
    )
    group = store.create_conversation(
        kind="group",
        title="研究小组",
        purpose="一起研究",
        member_ids=[supervisor["id"], member["id"]],
    )
    store.create_workflow(
        name="资料整理",
        description="先查再整理",
        owner_agent_id=member["id"],
        created_by="研究员",
        nodes=[
            {"id": "n1", "title": "查资料", "kind": "do", "instruction": "查资料", "gate": "auto", "on_fail": "stop"},
            {"id": "n2", "title": "整理", "kind": "do", "instruction": "整理成结论", "gate": "auto", "on_fail": "stop"},
        ],
    )
    store.create_provider(
        name="导出不该带的模型服务",
        protocol="openai",
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-this-key-must-not-leave",
        models=["secret-model"],
    )

    async def body() -> str:
        async with client_for(source) as client:
            response = await client.get("/team/export")
            assert response.status_code == 200, response.text
            assert "text/markdown" in response.headers["content-type"]
            text = response.text
        assert "sk-this-key-must-not-leave" not in text
        assert "研究员" in text and "研究小组" in text and "资料整理" in text
        assert "chat history" not in text
        return text

    exported = asyncio.run(body())

    target = make_backend()

    async def apply() -> None:
        async with client_for(target) as client:
            preview = await client.post(
                "/team/import",
                json={"markdown": exported, "apply": False, "overwrite_supervisor": True},
            )
            assert preview.status_code == 200, preview.text
            plan = preview.json()
            assert plan["applied"] is False
            names = {item["name"]: item["action"] for item in plan["members"]}
            assert names["研究员"] == "create"
            assert plan["groups"] and plan["groups"][0]["final_title"] == "研究小组"
            assert plan["workflows"] and plan["workflows"][0]["final_name"] == "资料整理"

            applied = await client.post(
                "/team/import",
                json={"markdown": exported, "apply": True, "overwrite_supervisor": True},
            )
            assert applied.status_code == 200, applied.text
            assert applied.json()["applied"] is True

            workspace = await get_workspace(client)
            assert "研究员" in [agent["name"] for agent in workspace["agents"]]
            assert "研究小组" in [item["title"] for item in workspace["conversations"]]
        target_store = store_of(target)
        assert [workflow["name"] for workflow in target_store.list_workflows()] == ["资料整理"]
        imported = next(agent for agent in target_store.list_agents() if agent["name"] == "研究员")
        assert imported["token_budget"] == {"limit": 50000, "period": "week"}
        assert imported["permission_mode"] == "auto"

    asyncio.run(apply())
