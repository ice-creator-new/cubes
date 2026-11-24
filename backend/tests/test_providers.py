"""Model providers over real HTTP: storage, defaults, per-bot resolution and probes."""

from __future__ import annotations

import asyncio
import json
import os
import stat

from helpers import (
    client_for,
    collect_events,
    event_names,
    get_conversation,
    get_workspace,
    start_run,
    store_of,
    wait_for_run,
)

from app.providers import NO_MODEL

OPENAI_KEY = "openai-key-1234"
ANTHROPIC_KEY = "anthropic-key-1234"
ENV_KEY = "env-key-abcdefgh"
KEEP_KEY = "keep-this-123"
REPLACED_KEY = "replacement-key-789"
PROBE_KEY = "probe-key-abcdefg"
STORED_KEY = "stored-key-abcdef"
LIST_KEY = "listkey-abcdefg"


async def create_provider(client, **fields) -> dict:
    body = {
        "name": "新服务",
        "protocol": "openai-chat",
        "base_url": "https://example.test/v1",
        "api_key": "sk-abcdefghij",
        "models": ["model-a"],
    }
    body.update(fields)
    response = await client.post("/providers", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def providers_of(client) -> dict:
    response = await client.get("/providers")
    assert response.status_code == 200, response.text
    return response.json()


def build_team(backend) -> dict:
    """Supervisor plus two members in one group, seeded straight into the store."""
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"], reviewer["id"]],
    )
    return {"supervisor": supervisor, "designer": designer, "reviewer": reviewer, "group": group}


def test_model_overrides_round_trip_and_reach_the_resolved_model(make_backend) -> None:
    # Set on create and on patch, read back as the same object with the user layer's
    # source, and the workspace payload (which resolves every bot's model) keeps working.
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await create_provider(
                client,
                model_overrides={"model-a": {"context": 64000, "output": 4096, "images": True}},
            )
            assert created["model_overrides"] == {
                "model-a": {"context": 64000, "output": 4096, "images": True}
            }
            caps = created["model_caps"]["model-a"]
            assert (caps["context_window"], caps["max_output"]) == (64000, 4096)
            assert caps["images"] is True
            assert caps["sources"]["context_window"] == "user"
            assert caps["sources"]["images"] == "user"
            patched = await client.patch(
                f"/providers/{created['id']}",
                json={"model_overrides": {"model-a": {"context": 32000, "output": 2048}}},
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["model_overrides"] == {
                "model-a": {"images": None, "context": 32000, "output": 2048}
            }
            listed = next(
                p for p in (await providers_of(client))["providers"] if p["id"] == created["id"]
            )
            assert listed["model_overrides"] == {
                "model-a": {"images": None, "context": 32000, "output": 2048}
            }
            assert listed["model_caps"]["model-a"]["images"] is False
            assert (await client.get("/workspace")).status_code == 200

    asyncio.run(body())
    from app.providers import resolve_model

    store = store_of(backend)
    provider = next(p for p in store.list_providers() if p["name"] == "新服务")
    agent = {"model_ref": {"provider_id": provider["id"], "model": "model-a"}}
    resolved = resolve_model(store, agent, backend.settings)
    assert (resolved.context_window, resolved.max_output) == (32000, 2048)
    assert resolved.vision is False


# ------------------------------------------------------------------- first start


def test_first_start_imports_the_env_provider_and_only_shows_a_hint(make_backend) -> None:
    backend = make_backend(api_key="env-key-abcdefgh", model="mify-model")

    async def body() -> None:
        async with client_for(backend) as client:
            payload = await providers_of(client)
            assert [provider["name"] for provider in payload["providers"]] == ["mify"]
            provider = payload["providers"][0]
            assert provider["protocol"] == "openai-chat"
            assert provider["base_url"] == backend.settings.base_url
            assert provider["models"] == ["mify-model"]
            assert provider["api_key_hint"] == "env…efgh"
            assert payload["default"] == {
                "provider_id": provider["id"],
                "model": "mify-model",
            }
            # The key itself never leaves the store.
            text = json.dumps(await providers_of(client), ensure_ascii=False)
            assert "env-key-abcdefgh" not in text
            assert "api_key\"" not in text

            health = (await client.get("/health")).json()
            assert health["configured"] is True
            assert health["model"] == "mify-model"
            assert health["provider_name"] == "mify"
            assert health["base_url"] == backend.settings.base_url
            assert health["configuration_error"] is None

    asyncio.run(body())


def test_a_provider_without_a_key_still_lists_it_as_unset(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await create_provider(
                client, name="Ollama", base_url="http://127.0.0.1:11434/v1", api_key=""
            )
            assert created["api_key_hint"] is None
            short = await create_provider(
                client, name="短钥匙", base_url="https://short.test/v1", api_key="abc"
            )
            assert short["api_key_hint"] == "已设置"

    asyncio.run(body())


# ------------------------------------------------------------------------ crud


def test_provider_crud_and_default_selection(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            first = await create_provider(
                client, name="服务一", models=["m1", "m2"], api_key=""
            )
            assert first["models"] == ["m1", "m2"]
            assert first["id"].startswith("p_")
            # The imported mify provider was already the default, so this one is not.
            payload = await providers_of(client)
            assert [provider["name"] for provider in payload["providers"]] == ["mify", "服务一"]
            assert payload["default"]["model"] == "test-model"

            renamed = await client.patch(f"/providers/{first['id']}", json={"name": "服务一号"})
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["name"] == "服务一号"

            chosen = await client.put(
                "/providers/default", json={"provider_id": first["id"], "model": "m2"}
            )
            assert chosen.status_code == 200, chosen.text
            assert chosen.json() == {"provider_id": first["id"], "model": "m2"}
            assert (await providers_of(client))["default"]["model"] == "m2"

            bad_model = await client.put(
                "/providers/default", json={"provider_id": first["id"], "model": "m9"}
            )
            assert bad_model.status_code == 422
            assert "不在这个模型服务的列表里" in bad_model.json()["detail"]

            removed = await client.delete(f"/providers/{first['id']}")
            assert removed.status_code == 204
            remaining = await providers_of(client)
            assert [provider["name"] for provider in remaining["providers"]] == ["mify"]
            missing = await client.delete("/providers/p_missing")
            assert missing.status_code == 404
            assert missing.json()["detail"] == "模型服务不存在。"

    asyncio.run(body())


def test_duplicate_names_are_refused_case_insensitively(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await create_provider(client, name="OpenAI")
            clash = await client.post(
                "/providers",
                json={
                    "name": "  openai ",
                    "protocol": "openai-chat",
                    "base_url": "https://example.test/v1",
                    "api_key": "",
                    "models": ["m"],
                },
            )
            assert clash.status_code == 409, clash.text
            assert "已被使用" in clash.json()["detail"]

            # The imported provider's name is taken too, in either case.
            mify = await client.post(
                "/providers",
                json={
                    "name": "MIFY",
                    "protocol": "openai-chat",
                    "base_url": "https://example.test/v1",
                    "models": ["m"],
                },
            )
            assert mify.status_code == 409

    asyncio.run(body())


def test_invalid_fields_are_refused_in_chinese(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            bad_protocol = await client.post(
                "/providers",
                json={
                    "name": "x",
                    "protocol": "grpc",
                    "base_url": "https://example.test/v1",
                    "models": ["m"],
                },
            )
            assert bad_protocol.status_code == 422
            assert (
                bad_protocol.json()["detail"]
                == "协议无效，只能是 openai-chat、anthropic-messages、claude-code 或 codex。"
            )

            bad_url = await client.post(
                "/providers",
                json={
                    "name": "x",
                    "protocol": "openai-chat",
                    "base_url": "example.test/v1",
                    "models": ["m"],
                },
            )
            assert bad_url.status_code == 422
            assert bad_url.json()["detail"].endswith("。")
            assert "http://" in bad_url.json()["detail"]

            no_models = await client.post(
                "/providers",
                json={
                    "name": "x",
                    "protocol": "openai-chat",
                    "base_url": "https://example.test/v1",
                    "models": ["  "],
                },
            )
            assert no_models.status_code == 422
            assert no_models.json()["detail"] == "至少要填写一个模型名称。"

            no_name = await client.post(
                "/providers",
                json={
                    "name": "   ",
                    "protocol": "openai-chat",
                    "base_url": "https://example.test/v1",
                    "models": ["m"],
                },
            )
            assert no_name.status_code == 422
            assert no_name.json()["detail"] == "名称不能为空。"

    asyncio.run(body())


def test_trailing_slashes_and_duplicate_models_are_normalized(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await create_provider(
                client,
                name="服务",
                base_url="https://example.test/v1/",
                models=[" m1 ", "m1", "m2"],
            )
            assert created["base_url"] == "https://example.test/v1"
            assert created["models"] == ["m1", "m2"]

    asyncio.run(body())


def test_api_key_is_kept_when_omitted_and_cleared_when_empty(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await create_provider(client, api_key="keepthe-this-123")
            assert created["api_key_hint"] == "kee…-123"
            renamed = await client.patch(f"/providers/{created['id']}", json={"name": "改名"})
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["api_key_hint"] == "kee…-123"
            cleared = await client.patch(f"/providers/{created['id']}", json={"api_key": ""})
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["api_key_hint"] is None
            replaced = await client.patch(
                f"/providers/{created['id']}", json={"api_key": "replacement-key-789"}
            )
            assert replaced.json()["api_key_hint"] == "rep…-789"

    asyncio.run(body())


def test_deleting_the_default_provider_picks_the_next_one(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            first = (await providers_of(client))["providers"][0]
            second = await create_provider(client, name="服务二", models=["x1", "x2"], api_key="")
            await client.put(
                "/providers/default", json={"provider_id": first["id"], "model": "test-model"}
            )
            deleted = await client.delete(f"/providers/{first['id']}")
            assert deleted.status_code == 204
            payload = await providers_of(client)
            assert payload["default"] == {"provider_id": second["id"], "model": "x1"}

            # Removing the last provider leaves the workspace without a default.
            assert (await client.delete(f"/providers/{second['id']}")).status_code == 204
            payload = await providers_of(client)
            assert payload["providers"] == []
            assert payload["default"] is None

    asyncio.run(body())


def test_moving_the_default_model_out_of_the_list_picks_the_first(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            first = (await providers_of(client))["providers"][0]
            patched = await client.patch(
                f"/providers/{first['id']}", json={"models": ["m-next", "m-later"]}
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["models"] == ["m-next", "m-later"]
            payload = await providers_of(client)
            assert payload["default"] == {
                "provider_id": first["id"],
                "model": "m-next",
            }

    asyncio.run(body())


def test_env_import_happens_only_once(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            first = (await providers_of(client))["providers"][0]
            assert first["name"] == "mify"
            assert (await client.delete(f"/providers/{first['id']}")).status_code == 204

    asyncio.run(body())
    backend.restart()

    async def after() -> None:
        async with client_for(backend) as client:
            payload = await providers_of(client)
            assert payload["providers"] == []
            assert payload["default"] is None
            health = (await client.get("/health")).json()
            assert health["configured"] is False
            assert health["configuration_error"] == NO_MODEL

    asyncio.run(after())


def test_the_database_file_stays_private(make_backend) -> None:
    backend = make_backend()

    path = backend.settings.database_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            assert stat.S_IMODE(os.stat(sidecar).st_mode) == 0o600


# ------------------------------------------------------------------ per-bot model


def test_agent_model_ref_validation_and_status(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            provider = await create_provider(
                client, name="服务", base_url=upstream.base_url, models=["m1"], api_key=""
            )
            agent = team["designer"]["id"]

            missing = await client.patch(
                f"/agents/{agent}",
                json={"model_ref": {"provider_id": "p_missing", "model": "m1"}},
            )
            assert missing.status_code == 422
            assert missing.json()["detail"] == "选择的模型服务不存在，可能刚被删除。"

            wrong_model = await client.patch(
                f"/agents/{agent}",
                json={"model_ref": {"provider_id": provider["id"], "model": "m9"}},
            )
            assert wrong_model.status_code == 422
            assert wrong_model.json()["detail"].endswith("。")

            chosen = await client.patch(
                f"/agents/{agent}",
                json={"model_ref": {"provider_id": provider["id"], "model": "m1"}},
            )
            assert chosen.status_code == 200, chosen.text
            assert chosen.json()["model_ref"] == {"provider_id": provider["id"], "model": "m1"}
            assert chosen.json()["model_status"] == "custom"

            reset = await client.patch(f"/agents/{agent}", json={"model_ref": None})
            assert reset.status_code == 200, reset.text
            assert reset.json()["model_ref"] is None
            assert reset.json()["model_status"] == "default"

            workspace = await get_workspace(client)
            statuses = {agent_row["id"]: agent_row["model_status"] for agent_row in workspace["agents"]}
            assert statuses[agent] == "default"
            assert statuses[team["supervisor"]["id"]] == "default"

    asyncio.run(body())


def test_two_bots_reach_two_protocols_in_one_group(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            openai = await create_provider(
                client,
                name="OpenAI",
                base_url=upstream.base_url,
                api_key=OPENAI_KEY,
                models=["gpt-x"],
            )
            anthropic = await create_provider(
                client,
                name="Anthropic",
                protocol="anthropic-messages",
                base_url=upstream.root_url,
                api_key=ANTHROPIC_KEY,
                models=["claude-x"],
            )
            for agent, provider, model in (
                (team["designer"]["id"], openai, "gpt-x"),
                (team["reviewer"]["id"], anthropic, "claude-x"),
            ):
                patched = await client.patch(
                    f"/agents/{agent}",
                    json={"model_ref": {"provider_id": provider["id"], "model": model}},
                )
                assert patched.status_code == 200, patched.text
                assert patched.json()["model_status"] == "custom"

            started = await start_run(
                client,
                team["group"]["id"],
                "请两位各说一句",
                mentions=[team["designer"]["id"], team["reviewer"]["id"]],
            )
            run = await wait_for_run(client, started["id"])
            assert run["status"] == "completed", run

            detail = await get_conversation(client, team["group"]["id"])
            replies = [message for message in detail["messages"] if message["kind"] == "agent"]
            assert sorted(message["author_name"] for message in replies) == ["乙", "甲"]
            assert all(message["content"] == "回复正文" for message in replies)

    asyncio.run(body())

    assert upstream.request_count == 1
    sent = upstream.requests[0]
    assert sent["model"] == "gpt-x"
    assert sent["authorization"] == f"Bearer {OPENAI_KEY}"

    assert upstream.anthropic_request_count == 1
    anthropic_request = upstream.anthropic_requests[0]
    assert anthropic_request["model"] == "claude-x"
    assert anthropic_request["x_api_key"] == ANTHROPIC_KEY
    assert anthropic_request["anthropic_version"] == "2023-06-01"
    assert anthropic_request["max_tokens"] == 8192


def test_a_deleted_provider_falls_back_and_still_replies(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            provider = await create_provider(
                client,
                name="一次性",
                base_url=upstream.base_url,
                api_key=OPENAI_KEY,
                models=["gone-model"],
            )
            await client.patch(
                f"/agents/{team['designer']['id']}",
                json={"model_ref": {"provider_id": provider["id"], "model": "gone-model"}},
            )
            assert (await client.delete(f"/providers/{provider['id']}")).status_code == 204

            workspace = await get_workspace(client)
            designer = next(
                agent for agent in workspace["agents"] if agent["id"] == team["designer"]["id"]
            )
            # The row keeps the old pick; the status is what tells the client to warn.
            assert designer["model_ref"] == {
                "provider_id": provider["id"],
                "model": "gone-model",
            }
            assert designer["model_status"] == "fallback"

            started = await start_run(
                client,
                team["group"]["id"],
                "@甲 再说一句",
                mentions=[team["designer"]["id"]],
            )
            run = await wait_for_run(client, started["id"])
            assert run["status"] == "completed", run
            detail = await get_conversation(client, team["group"]["id"])
            # A plain answer stays in the group: no thread and no card.
            assert [message["kind"] for message in detail["messages"]] == ["user", "agent"]

    asyncio.run(body())

    assert upstream.request_count == 1
    # The workspace default (the imported mify provider) served the reply instead.
    assert upstream.requests[0]["model"] == "test-model"
    assert upstream.requests[0]["authorization"] == "Bearer test-key"


# ---------------------------------------------------------------------- probing


def test_provider_test_reports_latency_and_tools(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            ok = await client.post(
                "/providers/test",
                json={
                    "protocol": "openai-chat",
                    "base_url": upstream.base_url,
                    "api_key": "sk-probe-123456",
                    "model": "test-model",
                },
            )
            assert ok.status_code == 200, ok.text
            payload = ok.json()
            assert payload["ok"] is True
            assert payload["supports_tools"] is True
            assert isinstance(payload["latency_ms"], int)
            assert payload["detail"].endswith("。")

            upstream.probe_supports_tools = False
            text_only = await client.post(
                "/providers/test",
                json={
                    "protocol": "anthropic-messages",
                    "base_url": upstream.root_url,
                    "api_key": ANTHROPIC_KEY,
                    "model": "claude-x",
                },
            )
            assert text_only.status_code == 200, text_only.text
            assert text_only.json()["supports_tools"] is False
            assert "没有调用工具" in text_only.json()["detail"]

            invalid = await client.post(
                "/providers/test",
                json={
                    "protocol": "openai-chat",
                    "base_url": upstream.base_url,
                    "model": "  ",
                },
            )
            assert invalid.status_code == 422
            assert invalid.json()["detail"].endswith("。")

    asyncio.run(body())


def test_provider_test_failure_is_chinese_and_redacted(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.mode = "error_echoing_key"

    async def body() -> None:
        async with client_for(backend) as client:
            failed = await client.post(
                "/providers/test",
                json={
                    "protocol": "openai-chat",
                    "base_url": upstream.base_url,
                    "api_key": "probe-key-abcdefg",
                    "model": "test-model",
                },
            )
            assert failed.status_code == 200, failed.text
            payload = failed.json()
            assert payload["ok"] is False
            assert payload["supports_tools"] is False
            assert payload["detail"].endswith("。")
            assert "HTTP 502" in payload["detail"]
            assert "probe-key-abcdefg" not in payload["detail"]
            assert "***" in payload["detail"]

    asyncio.run(body())


def test_provider_models_lists_and_uses_the_stored_key(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            listed = await client.post(
                "/providers/models",
                json={
                    "protocol": "openai-chat",
                    "base_url": upstream.base_url,
                    "api_key": "sk-list-123456",
                },
            )
            assert listed.status_code == 200, listed.text
            assert listed.json()["models"] == upstream.model_names
            assert upstream.model_requests[-1]["authorization"] == "Bearer sk-list-123456"

            anthropic = await client.post(
                "/providers/models",
                json={
                    "protocol": "anthropic-messages",
                    "base_url": upstream.root_url,
                    "api_key": ANTHROPIC_KEY,
                },
            )
            assert anthropic.status_code == 200, anthropic.text
            assert anthropic.json()["models"] == upstream.model_names
            assert upstream.model_requests[-1]["x_api_key"] == ANTHROPIC_KEY
            assert upstream.model_requests[-1]["anthropic_version"] == "2023-06-01"

            stored = await create_provider(
                client, name="已存钥匙", base_url=upstream.base_url, api_key="stored-key-abcdef"
            )
            reused = await client.post(
                "/providers/models",
                json={
                    "id": stored["id"],
                    "protocol": "openai-chat",
                    "base_url": upstream.base_url,
                },
            )
            assert reused.status_code == 200, reused.text
            assert upstream.model_requests[-1]["authorization"] == "Bearer stored-key-abcdef"

    asyncio.run(body())


def test_provider_models_failure_is_502_and_redacted(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.model_list_mode = "error_echoing_key"

    async def body() -> None:
        async with client_for(backend) as client:
            failed = await client.post(
                "/providers/models",
                json={
                    "protocol": "openai-chat",
                    "base_url": upstream.base_url,
                    "api_key": "listkey-abcdefg",
                },
            )
            assert failed.status_code == 502, failed.text
            detail = failed.json()["detail"]
            assert "HTTP 401" in detail
            assert detail.endswith("。")
            assert "listkey-abcdefg" not in detail
            assert "***" in detail

    asyncio.run(body())


# -------------------------------------------------------------------- workspace


def test_a_provider_change_pushes_the_workspace(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:

            async def change() -> None:
                await asyncio.sleep(0.2)
                await create_provider(client, name="推送用", base_url=upstream.base_url, api_key="")

            changing = asyncio.create_task(change())
            try:
                events = await collect_events(
                    client,
                    "/events",
                    until=lambda seen: "workspace" in event_names(seen),
                    timeout=15.0,
                )
            finally:
                await changing
            assert "workspace" in event_names(events)

    asyncio.run(body())
