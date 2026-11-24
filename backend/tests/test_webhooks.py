"""Webhook triggers: the secret URL that starts a schedule or a workflow from outside.

The public path is the one endpoint the launch token does not guard, so these tests check
the hook's own secret, the rate limit, rotation, deletion, and that everything else still
answers 401 without the launch token.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from app import webhooks
from app.main import create_app
from helpers import client_for, get_run, wait_for_run

TOKEN = "9a3f0c7d5b1e4826a0c3f7d9e2b50814"


async def make_schedule(client) -> dict:
    workspace = (await client.get("/workspace")).json()
    response = await client.post(
        "/schedules",
        json={
            "title": "被触发的任务",
            "prompt": "整理一下今天的消息",
            "conversation_id": workspace["direct_conversation_id"],
            "kind": "interval",
            "every_minutes": 5,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_workflow(client, name: str, *, enabled: bool = True) -> dict:
    response = await client.post(
        "/workflows",
        json={
            "name": name,
            "description": "测试用",
            "steps": [{"title": "整理", "instruction": "把输入整理成三条要点"}],
            "enabled": enabled,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_hook(client, target_kind: str, target_id: str) -> dict:
    response = await client.post(
        "/webhooks", json={"target_kind": target_kind, "target_id": target_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def read_hook(client, target_kind: str, target_id: str) -> dict | None:
    response = await client.get(
        "/webhooks", params={"target_kind": target_kind, "target_id": target_id}
    )
    assert response.status_code == 200, response.text
    return response.json()["webhook"]


async def call_hook(client, hook: dict, secret: str | None, **kwargs):
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    return await client.post(hook["path"], headers=headers, **kwargs)


def test_a_hook_starts_the_schedule_only_with_its_secret(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            schedule = await make_schedule(client)
            hook = await make_hook(client, "schedule", schedule["id"])
            assert hook["secret"] and hook["path"] == f"/hooks/{hook['id']}"
            assert hook["target_kind"] == "schedule"
            assert hook["last_called_at"] is None

            # The secret is shown once: a later read has only the URL.
            listed = await read_hook(client, "schedule", schedule["id"])
            assert listed is not None and "secret" not in listed
            duplicate = await client.post(
                "/webhooks", json={"target_kind": "schedule", "target_id": schedule["id"]}
            )
            assert duplicate.status_code == 409, duplicate.text

            anonymous = await call_hook(client, hook, None)
            assert anonymous.status_code == 401, anonymous.text
            assert anonymous.json()["detail"] == "Webhook 密钥不正确。"
            wrong = await call_hook(client, hook, "not-the-secret")
            assert wrong.status_code == 401, wrong.text

            accepted = await call_hook(
                client, hook, hook["secret"], json={"input": "今天只看 AI 新闻"}
            )
            assert accepted.status_code == 202, accepted.text
            started = accepted.json()
            assert started["kind"] == "schedule"
            assert started["status"] == "started"
            assert started["run_id"], started

            schedules = (await client.get("/schedules")).json()["schedules"]
            row = next(item for item in schedules if item["id"] == schedule["id"])
            assert row["last_run_id"] == started["run_id"]
            after = await read_hook(client, "schedule", schedule["id"])
            assert after is not None and after["last_called_at"]

            await wait_for_run(client, started["run_id"])
            detail = (await client.get(f"/schedules/{schedule['id']}/run")).json()
            asked = [
                message["content"]
                for message in detail["messages"]
                if message["kind"] == "user"
            ]
            assert any("今天只看 AI 新闻" in text for text in asked), asked

    asyncio.run(body())


def test_rate_limit_rotation_and_deletion(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            schedule = await make_schedule(client)
            hook = await make_hook(client, "schedule", schedule["id"])
            first = await call_hook(client, hook, hook["secret"])
            assert first.status_code == 202, first.text
            second = await call_hook(client, hook, hook["secret"])
            assert second.status_code == 429, second.text
            assert int(second.headers["retry-after"]) >= 1

            # A body that is too large never reaches the run.
            webhooks.throttle.forget(hook["id"])
            huge = await call_hook(client, hook, hook["secret"], content=b"x" * (65 * 1024))
            assert huge.status_code == 413, huge.text

            rotated = await client.post(f"/webhooks/{hook['id']}/rotate")
            assert rotated.status_code == 200, rotated.text
            fresh = rotated.json()
            assert fresh["secret"] != hook["secret"]
            assert fresh["id"] == hook["id"]
            old = await call_hook(client, hook, hook["secret"])
            assert old.status_code == 401, old.text
            new = await call_hook(client, hook, fresh["secret"])
            assert new.status_code == 202, new.text

            removed = await client.delete(f"/webhooks/{hook['id']}")
            assert removed.status_code == 204, removed.text
            gone = await call_hook(client, hook, fresh["secret"])
            assert gone.status_code == 404, gone.text
            assert await read_hook(client, "schedule", schedule["id"]) is None

    asyncio.run(body())


def test_a_hook_starts_a_workflow_with_the_body_as_input(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await make_workflow(client, "被触发的流程")
            hook = await make_hook(client, "workflow", workflow["id"])
            accepted = await client.post(
                hook["path"],
                headers={"Authorization": f"Bearer {hook['secret']}", "Content-Type": "text/plain"},
                content="来自 CI 的输入",
            )
            assert accepted.status_code == 202, accepted.text
            started = accepted.json()
            assert started["kind"] == "workflow" and started["status"] == "started"
            walk = (await client.get(f"/workflow-runs/{started['workflow_run_id']}")).json()
            assert walk["input"] == "来自 CI 的输入"
            assert walk["started_by"] == "webhook"
            assert walk["workflow_id"] == workflow["id"]

            paused = await make_workflow(client, "停用的流程", enabled=False)
            idle_hook = await make_hook(client, "workflow", paused["id"])
            refused = await call_hook(client, idle_hook, idle_hook["secret"])
            assert refused.status_code == 409, refused.text

            missing = await client.post(
                "/webhooks", json={"target_kind": "workflow", "target_id": "w_nowhere"}
            )
            assert missing.status_code == 404, missing.text

    asyncio.run(body())


def test_the_launch_token_still_guards_everything_else(make_backend) -> None:
    settings = replace(make_backend().settings, auth_token=TOKEN)
    secret = "hook-secret-for-the-auth-test"
    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        hook = store.create_webhook(
            target_kind="schedule",
            target_id="s_missing",
            secret_hash=webhooks.hash_secret(secret),
        )
        assert hook is not None
        path = webhooks.webhook_path(hook["id"])

        # The hook path is exempt from the launch token: the right secret reaches the route,
        # which then says the target is gone rather than asking for the token.
        reached = client.post(path, headers={"Authorization": f"Bearer {secret}"})
        assert reached.status_code == 404, reached.text
        # Without its secret the hook answers with its own refusal, not the token's.
        refused = client.post(path)
        assert refused.status_code == 401, refused.text
        assert refused.json()["detail"] == "Webhook 密钥不正确。"
        # Everything else, the hook management included, still needs the launch token.
        for method, url in (
            ("get", "/workspace"),
            ("get", "/schedules"),
            ("get", "/webhooks?target_kind=schedule&target_id=s_missing"),
            ("post", "/webhooks"),
        ):
            response = getattr(client, method)(url, json={}) if method == "post" else getattr(client, method)(url)
            assert response.status_code == 401, (url, response.text)
            assert response.json()["detail"] == "缺少或错误的访问令牌。"


def test_run_payload_is_readable_after_a_hook_fire(make_backend) -> None:
    """The 202 names a real run: the caller can follow it with the existing run endpoint."""
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            schedule = await make_schedule(client)
            hook = await make_hook(client, "schedule", schedule["id"])
            accepted = await call_hook(client, hook, hook["secret"])
            assert accepted.status_code == 202, accepted.text
            run = await get_run(client, accepted.json()["run_id"])
            assert run["conversation_id"]
            assert run["schedule_id"] == schedule["id"]

    asyncio.run(body())
