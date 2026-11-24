"""Tool framework contract: settings parsing, pack availability and OpenAI schemas."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config, permissions
from app.config import Settings, load_settings
from app.toolkit import openai_schemas, tool_specs
from app.toolkit.base import ToolSpec

UTILITY_NAMES = {"current_time", "calculate", "convert_units"}
WEB_NAMES = {"web_search", "fetch_url"}
MEMORY_NAMES = {
    "search_history",
    "read_conversation",
    "note_create",
    "note_update",
    "note_read",
    "note_list",
    "note_delete",
    "task_create",
    "task_update",
    "task_list",
}
FILE_NAMES = {"file_list", "file_read", "file_write"}


def names(specs: list[ToolSpec]) -> list[str]:
    return [spec.name for spec in specs]


def full_settings(tmp_path: Path, **overrides: object) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "database_path": tmp_path / "cubes.sqlite3",
        "workspace_dir": workspace,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_every_pack_is_offered_to_both_roles(tmp_path: Path) -> None:
    settings = full_settings(tmp_path)
    expected = WEB_NAMES | MEMORY_NAMES | UTILITY_NAMES | FILE_NAMES | {"run_code"}
    for role in ("supervisor", "member"):
        assert expected <= set(names(tool_specs(settings, role)))
    assert names(tool_specs(settings, "member")) == names(tool_specs(settings, "supervisor"))


def test_files_and_code_are_always_offered(tmp_path: Path) -> None:
    # The working folder defaults to workspace/ beside the database; the approval mode, not
    # an on/off switch, decides what a call may do without asking.
    without = Settings(database_path=tmp_path / "cubes.sqlite3")
    offered = set(names(tool_specs(without, "supervisor")))
    assert WEB_NAMES | MEMORY_NAMES | UTILITY_NAMES | FILE_NAMES <= offered
    assert "run_code" in offered


def test_specs_match_the_framework_contract(tmp_path: Path) -> None:
    settings = full_settings(tmp_path)
    seen: set[str] = set()
    for spec in tool_specs(settings, "supervisor"):
        assert isinstance(spec, ToolSpec)
        assert re.fullmatch(r"[a-z][a-z0-9_]{2,40}", spec.name), spec.name
        assert spec.name not in seen, f"duplicate tool {spec.name}"
        seen.add(spec.name)
        assert spec.label and len(spec.label) <= 12
        assert re.search(r"[\u4e00-\u9fff]", spec.description)
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        assert isinstance(spec.parameters["properties"], dict)
        assert spec.roles and spec.roles <= {"supervisor", "member"}
        assert callable(spec.run) and callable(spec.summary)
        assert callable(spec.available)


def test_openai_schemas_shape(tmp_path: Path) -> None:
    specs = tool_specs(full_settings(tmp_path), "supervisor")
    schemas = openai_schemas(specs)
    assert len(schemas) == len(specs)
    for schema, spec in zip(schemas, specs):
        assert schema["type"] == "function"
        assert schema["function"]["name"] == spec.name
        assert schema["function"]["description"] == spec.description
        assert schema["function"]["parameters"] is spec.parameters


def test_settings_read_the_new_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("PARALLEL_API_KEY", "pk")
    monkeypatch.setenv("EXA_API_KEY", "ek")
    monkeypatch.setenv("TAVILY_API_KEY", "tk")
    monkeypatch.setenv("BRAVE_API_KEY", "bk")
    monkeypatch.setenv("SEARCH_PROVIDERS", " exa, brave ,nope,exa ")
    monkeypatch.setenv("EXA_MCP_URL", "http://127.0.0.1:9/exa")
    settings = load_settings(dotenv_path=tmp_path / "missing.env")
    assert settings.workspace_dir == workspace
    assert settings.parallel_api_key == "pk"
    assert settings.exa_api_key == "ek"
    assert settings.tavily_api_key == "tk"
    assert settings.brave_api_key == "bk"
    assert settings.search_providers == ("exa", "brave")
    assert settings.exa_mcp_url == "http://127.0.0.1:9/exa"


def test_a_working_folder_that_does_not_exist_yet_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The folder is created on first use, so pointing WORKSPACE_DIR at a new path works.
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    missing = tmp_path / "nope"
    monkeypatch.setenv("WORKSPACE_DIR", str(missing))
    settings = load_settings(dotenv_path=tmp_path / "missing.env")
    assert settings.workspace_dir == missing
    assert not missing.exists()
    assert FILE_NAMES | {"run_code"} <= set(names(tool_specs(settings, "supervisor")))


def test_relative_workspace_resolves_against_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sandbox").mkdir()
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    monkeypatch.setenv("WORKSPACE_DIR", "sandbox")
    settings = load_settings(dotenv_path=tmp_path / "missing.env")
    assert settings.workspace_dir == tmp_path / "sandbox"


def test_defaults_put_the_working_folder_beside_the_database(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "cubes.sqlite3")
    assert settings.workspace_dir is None
    assert permissions.working_folder(settings) == (tmp_path / "workspace").resolve()
    assert settings.search_providers == ("parallel", "exa", "tavily", "brave", "duckduckgo")
    assert settings.parallel_mcp_url.startswith("https://")
