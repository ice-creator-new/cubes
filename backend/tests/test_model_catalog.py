"""Model capabilities: parsing provider answers, the models.dev catalog, and resolution."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app import model_catalog, providers
from app.providers import ModelCaps
from app.store import Store

MAGPIE_ENTRY = {
    "id": "mify/moonshot/kimi-k3.1-pro",
    "object": "model",
    "modalities": {"input": ["text", "image", "video"]},
    "context_window": 1048576,
    "max_input_tokens": 1048576,
    "max_output_tokens": 1048576,
    "reasoning": True,
    "reasoning_levels": ["low", "high", "max"],
}

OPENROUTER_ENTRY = {
    "id": "anthropic/claude-sonnet-4.5",
    "architecture": {"input_modalities": ["text", "image", "file"], "modality": "text+image->text"},
    "context_length": 200000,
    "top_provider": {"context_length": 200000, "max_completion_tokens": 32000},
    "supported_parameters": ["tools", "reasoning"],
}

ANTHROPIC_ENTRY = {
    "id": "claude-test",
    "type": "model",
    "max_input_tokens": 180000,
    "capabilities": {"image_input": {"supported": True}, "pdf_input": {"supported": False}},
}


def caps(*entries: dict) -> dict[str, dict]:
    return model_catalog.parse_model_list({"data": list(entries)})


# ------------------------------------------------------------------ provider shapes


def test_an_openai_list_names_models_and_declares_nothing() -> None:
    parsed = model_catalog.parse_model_list(
        {"object": "list", "data": [{"id": "gpt-x", "object": "model", "owned_by": "openai"}]}
    )
    assert parsed == {"gpt-x": {}}


def test_openrouter_modalities_limits_and_reasoning_are_read() -> None:
    parsed = caps(OPENROUTER_ENTRY)
    assert parsed["anthropic/claude-sonnet-4.5"] == {
        "images": True,
        "video": False,
        "audio": False,
        "pdf": True,
        "context_window": 200000,
        "max_output": 32000,
        "reasoning": True,
    }


def test_a_magpie_or_litellm_list_carries_every_modality() -> None:
    parsed = caps(MAGPIE_ENTRY)
    assert parsed["mify/moonshot/kimi-k3.1-pro"] == {
        "images": True,
        "video": True,
        "audio": False,
        "pdf": False,
        "context_window": 1048576,
        "max_output": 1048576,
        "reasoning": True,
        "reasoning_levels": ["low", "high", "max"],
    }


def test_anthropic_capabilities_and_input_limit_are_read() -> None:
    parsed = caps(ANTHROPIC_ENTRY)
    assert parsed["claude-test"] == {
        "images": True,
        "pdf": False,
        "context_window": 180000,
    }


def test_ollama_tag_lists_and_bare_strings_still_name_their_models() -> None:
    assert list(model_catalog.parse_model_list({"models": [{"name": "llama3:8b"}]})) == ["llama3:8b"]
    assert list(model_catalog.parse_model_list(["a", "b"])) == []


# ------------------------------------------------------------------ catalog matching


def catalog() -> model_catalog.Catalog:
    return model_catalog.build_catalog(
        [
            {
                "id": "claude-sonnet-4-5",
                "provider": "anthropic",
                "input": ["text", "image"],
                "context": 200000,
                "output": 64000,
                "reasoning": True,
            },
            {
                "id": "deepseek-ai/DeepSeek-V3",
                "provider": "deepseek",
                "input": ["text"],
                "context": 128000,
                "output": 8192,
                "reasoning": False,
            },
        ]
    )


def test_matching_tries_the_exact_id_then_the_last_segment_then_the_name() -> None:
    entries = catalog()
    assert entries.lookup("claude-sonnet-4-5")["provider"] == "anthropic"
    # After the last '/', so a gateway's prefixed id still matches.
    assert entries.lookup("openrouter/anthropic/claude-sonnet-4-5")["provider"] == "anthropic"
    # A case-insensitive exact hit keeps the provider whose id already carries a slash.
    assert entries.lookup("deepseek-ai/deepseek-v3")["provider"] == "deepseek"


def test_normalized_names_strip_dates_tags_and_are_still_exact() -> None:
    entries = catalog()
    assert entries.lookup("claude-sonnet-4-5-20250929")["context"] == 200000
    assert entries.lookup("claude-sonnet-4-5:free")["context"] == 200000
    assert entries.lookup("DeepSeek-V3:latest")["context"] == 128000


def test_matching_is_conservative_and_never_guesses() -> None:
    entries = catalog()
    assert entries.lookup("claude-sonnet-3") is None
    assert entries.lookup("claude-sonnet-4") is None
    assert entries.lookup("deepseek-v3.1") is None
    assert entries.lookup("") is None


def test_the_bundled_snapshot_loads() -> None:
    entries = model_catalog.catalogs_from_snapshot(
        json.loads(model_catalog.SNAPSHOT_PATH.read_text(encoding="utf-8"))
    )
    assert len(entries.entries) > 1000
    assert entries.lookup("gpt-4o-mini") is not None


# ------------------------------------------------------------------ resolution order


def settings_for(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        database_path=tmp_path / "cubes.sqlite3",
        context_window_default=128000,
        context_output_default=8192,
    )


def provider(**patch) -> dict:
    raw = {
        "id": "p1",
        "name": "服务",
        "protocol": "openai-chat",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "",
        "models": ["claude-sonnet-4-5"],
        "model_caps": {},
        "model_overrides": {},
    }
    raw.update(patch)
    return raw


def test_each_field_takes_the_first_layer_that_knows_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        providers.model_catalog, "load", lambda _settings: catalog()
    )
    settings = settings_for(tmp_path)
    resolved = providers.model_caps(
        provider(), "claude-sonnet-4-5", settings
    )
    assert (resolved.images, resolved.context_window, resolved.max_output) == (True, 200000, 64000)
    assert resolved.reasoning is True
    assert resolved.sources["images"] == "catalog"
    assert resolved.sources["context_window"] == "catalog"
    # The catalog's input list is a declaration: no video means it cannot take video.
    assert resolved.sources["video"] == "catalog"
    assert resolved.video is False


def test_the_provider_answer_wins_over_the_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        providers.model_catalog, "load", lambda _settings: catalog()
    )
    resolved = providers.model_caps(
        provider(model_caps={"claude-sonnet-4-5": {"images": False, "context_window": 1000000}}),
        "claude-sonnet-4-5",
        settings_for(tmp_path),
    )
    assert resolved.images is False
    assert resolved.context_window == 1000000
    assert resolved.sources["images"] == "provider"
    assert resolved.max_output == 64000


def test_a_user_override_wins_and_star_applies_to_the_rest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        providers.model_catalog, "load", lambda _settings: catalog()
    )
    settings = settings_for(tmp_path)
    resolved = providers.model_caps(
        provider(
            model_caps={"claude-sonnet-4-5": {"context_window": 1000000}},
            model_overrides={
                "claude-sonnet-4-5": {"images": False, "context": 50000},
                "*": {"output": 4096},
            },
        ),
        "claude-sonnet-4-5",
        settings,
    )
    assert resolved.images is False
    assert (resolved.context_window, resolved.max_output) == (50000, 4096)
    assert resolved.sources["images"] == "user"
    assert resolved.sources["max_output"] == "user"


def test_unknown_models_fall_back_to_the_configured_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(providers.model_catalog, "load", lambda _settings: model_catalog.Catalog())
    resolved = providers.model_caps(provider(models=["nobody"]), "nobody", settings_for(tmp_path))
    assert (resolved.context_window, resolved.max_output) == (128000, 8192)
    assert resolved.images is False
    assert set(resolved.sources.values()) == {"default"}


def test_cleaning_overrides_keeps_only_valid_answers() -> None:
    assert providers.clean_model_overrides(
        {
            "m1": {"images": True, "context": 64000, "output": None},
            "m2": {"images": None},
            "*": {"images": False},
        }
    ) == {"m1": {"images": True, "context": 64000}, "*": {"images": False}}
    with pytest.raises(providers.ProviderInvalid):
        providers.clean_model_overrides({"m1": {"context": 0}})
    with pytest.raises(providers.ProviderInvalid):
        providers.clean_model_overrides({"m1": {"output": -5}})


# ------------------------------------------------------------------ detection + store


def test_fetch_models_reads_a_magpie_answer_into_caps() -> None:
    payload = {
        "data": [
            MAGPIE_ENTRY,
            {
                "id": "mify/deepseek/deepseek-v4-pro",
                "modalities": {"input": ["text"]},
                "context_window": 1000000,
                "max_output_tokens": 384000,
            },
        ]
    }

    async def body() -> tuple[list[str], dict[str, dict]]:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            return await providers.fetch_models(
                client, protocol="openai-chat", base_url="http://127.0.0.1:3425/v1", api_key="magpie"
            )

    import asyncio

    ids, found = asyncio.run(body())
    assert ids == ["mify/moonshot/kimi-k3.1-pro", "mify/deepseek/deepseek-v4-pro"]
    assert found["mify/moonshot/kimi-k3.1-pro"]["images"] is True
    assert found["mify/deepseek/deepseek-v4-pro"] == {
        "images": False,
        "video": False,
        "audio": False,
        "pdf": False,
        "context_window": 1000000,
        "max_output": 384000,
    }


def test_migrating_the_old_vision_and_limit_columns(tmp_path) -> None:
    path = tmp_path / "cubes.sqlite3"
    store = Store(path, env_base_url="", env_api_key="", env_model="")
    store.initialize()
    created = store.create_provider(
        name="老服务", protocol="openai-chat", base_url="http://x/v1", api_key="", models=["m1", "m2"]
    )
    # A database written by the previous build: both choices live in the old columns.
    conn = store._ensure()
    conn.execute(
        "UPDATE providers SET vision_models = ?, model_limits = ? WHERE id = ?",
        (json.dumps(["m1"]), json.dumps({"m1": {"context": 32000, "output": 2048}}), created["id"]),
    )
    conn.execute("UPDATE meta SET value = '22' WHERE key = 'schema_version'")
    conn.commit()
    store.close()

    reopened = Store(path, env_base_url="", env_api_key="", env_model="")
    reopened.initialize()
    migrated = reopened.get_provider(created["id"])
    assert migrated is not None
    assert migrated["model_overrides"] == {
        "m1": {"images": True, "context": 32000, "output": 2048}
    }
    row = reopened._ensure().execute(
        "SELECT vision_models, model_limits FROM providers WHERE id = ?", (created["id"],)
    ).fetchone()
    assert json.loads(row["vision_models"]) == []
    assert json.loads(row["model_limits"]) == {}

    # The resolved model follows those choices, exactly as the user's own answers do.
    agent = {"model_ref": {"provider_id": created["id"], "model": "m1"}}
    resolved = providers.resolve_model(reopened, agent)
    assert resolved.vision is True
    assert (resolved.context_window, resolved.max_output) == (32000, 2048)
    # A model outside the list is untouched: the catalog or the defaults still answer.
    assert providers.model_caps(migrated, "m2").sources["images"] == "default"
    reopened.close()
