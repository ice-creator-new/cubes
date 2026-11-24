"""Claude Code and Codex as a bot's engine, run against a fake CLI that speaks their JSON.

The event shapes here are the ones the installed CLIs really printed (see
``app.cli_engines``); the scripts below replay them, so the adapter is tested without
spending anyone's subscription quota.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from helpers import (
    client_for,
    get_conversation,
    start_run,
    store_of,
    wait_for_message,
    wait_for_run,
)

from app import cli_engines, providers
from app.errors import StreamAborted, UpstreamError
from app.providers import ResolvedModel

# A CLI that behaves like the real one, with a mode switch for the failure cases.
FAKE_CLI = '''#!{python}
import json, os, sys, time

mode = os.environ.get("FAKE_CLI_MODE", "ok")
# Unit tests name a phrase that must arrive in the system prompt; the app's own run has no such marker.
need = os.environ.get("FAKE_CLI_EXPECT_SYSTEM", "")
argv = sys.argv[1:]

if "--version" in argv:
    print("9.9.9 (Fake CLI)")
    sys.exit(0)
if "auth" in argv and "status" in argv:
    print(json.dumps({{"loggedIn": True, "authMethod": "oauth_token"}}))
    sys.exit(0)
if "login" in argv and "status" in argv:
    sys.exit(0)

raw = sys.stdin.read()
# Claude Code reads one stream-json user line; the fake keeps the plain text for the
# greeting checks and the content blocks so tests can see images and documents.
prompt = raw
blocks = []
if "--input-format" in argv and "stream-json" in argv:
    try:
        payload = json.loads(raw.strip().splitlines()[0])
        blocks = payload["message"]["content"]
        prompt = "\\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    except Exception:
        blocks = []
images = []
for argument in argv:
    if argument.startswith("--image="):
        path = argument.split("=", 1)[1]
        images.append({{"path": path, "exists": os.path.isfile(path),
            "size": os.path.getsize(path) if os.path.isfile(path) else 0}})
folder = os.path.join(os.getcwd(), "附件")
files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
if os.environ.get("FAKE_CLI_LOG"):
    with open(os.path.join(os.environ["FAKE_CLI_LOG"], "calls.jsonl"), "a") as handle:
        handle.write(json.dumps({{"argv": argv, "prompt": prompt, "blocks": blocks,
            "images": images, "files": files}}, ensure_ascii=False) + "\\n")
sys.stderr.write("prompt-chars=%d\\n" % len(prompt))

if mode == "missing-login":
    sys.stderr.write("Error: not logged in. Run the login command first.\\n")
    sys.exit(1)
if mode == "resume-gone" and "--resume" in argv:
    sys.stderr.write("Error: No conversation found with session ID s-gone.\\n")
    sys.exit(1)
if mode == "fail":
    sys.stderr.write("Error: rate limit exceeded for this account\\n")
    sys.exit(3)
if mode == "slow":
    with open(os.environ["FAKE_PID_FILE"], "w") as handle:
        handle.write(str(os.getpid()))
    print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
        "delta": {{"type": "text_delta", "text": "开始"}}}}}}), flush=True)
    time.sleep(60)
    sys.exit(0)

if "{codex}":
    greeting = "来自 Codex" if not need or need in prompt else "没有身份"
    print(json.dumps({{"type": "thread.started", "thread_id": "t-1"}}))
    print(json.dumps({{"type": "turn.started"}}))
    print(json.dumps({{"type": "item.completed",
        "item": {{"id": "item_0", "type": "agent_message", "text": greeting}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 11,
        "cached_input_tokens": 4, "output_tokens": 6, "reasoning_output_tokens": 0}}}}))
    sys.exit(0)

print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s-1"}}))
appended = ""
if "--append-system-prompt" in argv:
    appended = argv[argv.index("--append-system-prompt") + 1]
greeting = "你好，世界" if "打个招呼" in prompt and (not need or need in appended) else "没有收到对话"
print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
    "index": 0, "delta": {{"type": "text_delta", "text": greeting[:2]}}}}}}), flush=True)
print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
    "index": 0, "delta": {{"type": "text_delta", "text": greeting[2:]}}}}}}), flush=True)
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "text", "text": greeting}}],
    "usage": {{"input_tokens": 0, "output_tokens": 0}}}}}}))
print(json.dumps({{"type": "result", "subtype": "success", "stop_reason": "end_turn",
    "usage": {{"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 7,
    "cache_creation_input_tokens": 3}}}}))
'''


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake as ``claude`` and ``codex``, selected by the environment.

    Both CLIs' config folders are pointed at the test's own tmp directory, so the model
    catalogs come from known files instead of whatever this machine has configured.
    """
    monkeypatch.delenv("FAKE_CLI_MODE", raising=False)
    monkeypatch.delenv("FAKE_CLI_EXPECT_SYSTEM", raising=False)
    monkeypatch.delenv("FAKE_CLI_LOG", raising=False)
    cli_engines._STATUS_CACHE.clear()
    cli_engines._CATALOG_CACHE.clear()
    claude_dir = tmp_path / "claude-home"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "model": "sonnet[1m]",
                "modelPicker": {
                    "options": [
                        {
                            "label": "claude-sonnet-5.5 · Kiro",
                            "model": "kiro/claude-sonnet-5-5[1m]",
                            "description": "via magpie",
                        }
                    ]
                },
                "env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "mify/deepseek-flash[1m]"},
            }
        ),
        encoding="utf-8",
    )
    codex_dir = tmp_path / "codex-home"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    (codex_dir / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5-codex",
                        "display_name": "GPT-5 Codex",
                        "visibility": "list",
                        "default_reasoning_level": "medium",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "medium"},
                            {"effort": "high"},
                        ],
                    },
                    {"slug": "internal-only", "visibility": "hide"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    paths: dict[str, Path] = {}
    for name, codex in (("claude", False), ("codex", True)):
        script = tmp_path / name
        script.write_text(
            FAKE_CLI.format(python=sys.executable, codex="codex" if codex else ""),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        paths[name] = script
    monkeypatch.setenv("CUBES_CLAUDE_BIN", str(paths["claude"]))
    monkeypatch.setenv("CUBES_CODEX_BIN", str(paths["codex"]))
    return paths["claude"]


def resolved(protocol: str, workdir: Path, model: str = cli_engines.CLI_DEFAULT_MODEL) -> ResolvedModel:
    return ResolvedModel(
        protocol=protocol,
        base_url="",
        api_key="",
        model=model,
        provider_id="p-1",
        provider_name="本机订阅",
        workdir=str(workdir),
    )


def replace_resolved(workdir: Path, model: str, *, effort: str = "") -> ResolvedModel:
    return ResolvedModel(
        protocol="claude-code",
        base_url="",
        api_key="",
        model=model,
        provider_id="p-1",
        provider_name="本机订阅",
        workdir=str(workdir),
        effort=effort,
    )


def messages() -> list[dict]:
    return [
        {"role": "system", "content": "你是一只叫小方的方块机器人。"},
        {"role": "user", "content": "打个招呼"},
    ]


def collect(stream: cli_engines.CLIStream, **kwargs) -> list[str]:
    async def body() -> list[str]:
        return [chunk async for chunk in stream.chunks(**kwargs)]

    return asyncio.run(body())


def test_claude_code_streams_its_text_and_usage(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLI_EXPECT_SYSTEM", "小方")
    stream = cli_engines.CLIStream("claude-code", resolved("claude-code", tmp_path), messages=messages())
    pieces = collect(stream)
    stream.validate(allow_tool_calls=True)

    assert "".join(pieces) == "你好，世界"
    assert stream.tool_calls == []
    assert stream.result.saw_done is True
    assert stream.result.usage.input_tokens == 15
    assert stream.result.usage.output_tokens == 2
    assert stream.result.usage.cached_tokens == 7


def test_codex_streams_agent_messages_and_usage(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLI_EXPECT_SYSTEM", "小方")
    stream = cli_engines.CLIStream("codex", resolved("codex", tmp_path), messages=messages())
    pieces = collect(stream)
    stream.validate(allow_tool_calls=True)

    assert pieces == ["来自 Codex"]
    assert stream.result.usage.input_tokens == 11
    assert stream.result.usage.output_tokens == 6
    assert stream.result.usage.cached_tokens == 4


def test_a_non_zero_exit_becomes_an_honest_chinese_error(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "fail")
    stream = cli_engines.CLIStream("claude-code", resolved("claude-code", tmp_path), messages=messages())
    pieces = collect(stream)

    assert pieces == []
    with pytest.raises(UpstreamError) as caught:
        stream.validate(allow_tool_calls=True)
    assert "限流" in str(caught.value)


def test_a_missing_login_names_the_command_to_run(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "missing-login")
    stream = cli_engines.CLIStream("claude-code", resolved("claude-code", tmp_path), messages=messages())
    collect(stream)

    with pytest.raises(UpstreamError) as caught:
        stream.validate(allow_tool_calls=True)
    assert "claude" in str(caught.value)
    assert "登录" in str(caught.value)


def test_a_missing_command_is_reported_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUBES_CLAUDE_BIN", str(tmp_path / "nope"))
    stream = cli_engines.CLIStream("claude-code", resolved("claude-code", tmp_path), messages=messages())

    with pytest.raises(UpstreamError) as caught:
        collect(stream)
    assert "没有找到 claude 命令" in str(caught.value)


def test_a_stop_kills_the_whole_process(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_CLI_MODE", "slow")
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    stream = cli_engines.CLIStream("claude-code", resolved("claude-code", tmp_path), messages=messages())
    seen = {"text": ""}
    first = True

    async def body() -> None:
        nonlocal first
        try:
            async for chunk in stream.chunks(should_stop=lambda: not first):
                seen["text"] += chunk
                first = False
        finally:
            await stream.aclose()

    with pytest.raises(StreamAborted):
        asyncio.run(body())

    assert seen["text"] == "开始"
    pid = int(pid_file.read_text())
    deadline = asyncio.run(_wait_gone(pid))
    assert deadline, "the CLI process outlived the stop request"


async def _wait_gone(pid: int) -> bool:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


def test_the_status_probe_reads_installation_and_login(fake_cli: Path) -> None:
    status = asyncio.run(cli_engines.engine_status("claude-code"))

    assert status.installed is True
    assert status.ready is True
    assert status.version.startswith("9.9.9")
    assert "登录" in status.detail


def test_a_cli_provider_is_created_without_address_or_key(make_backend, fake_cli: Path) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={
                    "name": "Claude 订阅",
                    "protocol": "claude-code",
                    "base_url": "",
                    "models": [],
                },
            )
            assert created.status_code == 201, created.text
            provider = created.json()
            assert provider["protocol"] == "claude-code"
            assert provider["base_url"] == ""
            # 「默认」 first, then the real models this machine's claude reports.
            assert provider["models"][:6] == [
                "默认",
                "opus",
                "sonnet",
                "haiku",
                "fable",
                "kiro/claude-sonnet-5-5[1m]",
            ]
            assert "sonnet[1m]" in provider["models"]

            state = (await client.get("/providers")).json()
            kinds = {engine["kind"]: engine for engine in state["engines"]}
            assert kinds["claude-code"]["ready"] is True
            assert kinds["codex"]["ready"] is True
            row = next(item for item in state["providers"] if item["id"] == provider["id"])
            details = {item["model"]: item for item in row["model_details"]}
            assert details["kiro/claude-sonnet-5-5[1m]"]["label"] == "claude-sonnet-5.5 · Kiro"
            assert details["sonnet"]["efforts"] == list(cli_engines.CLAUDE_EFFORTS)
            assert "mify/deepseek-flash[1m]" in details["sonnet"]["description"]

    asyncio.run(body())


def test_a_bot_answers_with_the_real_cli_engine(make_backend, fake_cli: Path) -> None:
    backend = make_backend()
    store = store_of(backend)
    chat = store.ensure_direct_conversation((store.supervisor())["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": ["默认"]},
            )
            assert created.status_code == 201, created.text
            provider = created.json()
            chosen = await client.put(
                "/providers/default", json={"provider_id": provider["id"], "model": "默认"}
            )
            assert chosen.status_code == 200, chosen.text

            run = await start_run(client, chat["id"], "打个招呼")
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", json.dumps(finished, ensure_ascii=False)

            detail = await get_conversation(client, chat["id"])
            replies = [row for row in detail["messages"] if row["kind"] == "agent"]
            assert replies and replies[-1]["content"] == "你好，世界"
            # The engine's own tool loop is untouched: a CLI turn has no tool calls.
            assert replies[-1]["status"] == "complete"

    asyncio.run(body())


def test_stopping_a_run_kills_its_cli_process(
    make_backend, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "slow")
    pid_file = tmp_path / "engine-pid"
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    backend = make_backend()
    store = store_of(backend)
    chat = store.ensure_direct_conversation((store.supervisor())["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": ["默认"]},
            )
            provider = created.json()
            await client.put(
                "/providers/default", json={"provider_id": provider["id"], "model": "默认"}
            )
            run = await start_run(client, chat["id"], "慢慢想")
            await wait_for_message(client, chat["id"], status="streaming")
            stopped = await client.post(f"/runs/{run['id']}/stop", json={})
            assert stopped.status_code == 200, stopped.text
            await wait_for_run(client, run["id"], statuses=("stopped",))

    asyncio.run(body())

    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    assert asyncio.run(_wait_gone(pid)), "the CLI process survived the stop request"


# ------------------------------------------------------------------ model catalog


def test_codex_catalog_keeps_only_listed_models_and_marks_the_current_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model_provider = "magpie"\nmodel = "gpt-5-codex"\nmodel_reasoning_effort = "high"\n',
        encoding="utf-8",
    )
    script = tmp_path / "codex"
    script.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"models": [
    {{"slug": "gpt-5-codex", "display_name": "GPT-5 Codex", "visibility": "list",
      "default_reasoning_level": "medium",
      "supported_reasoning_levels": [{{"effort": "low"}}, {{"effort": "high"}}]}},
    {{"slug": "gpt-5-codex-mini", "display_name": "GPT-5 Codex Mini", "visibility": "list"}},
    {{"slug": "internal", "display_name": "Internal", "visibility": "hide"}}
]}}))
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("CUBES_CODEX_BIN", str(script))
    monkeypatch.setenv("CODEX_HOME", str(home))
    cli_engines._CATALOG_CACHE.clear()

    options = asyncio.run(cli_engines.model_catalog("codex"))

    assert [option.name for option in options] == ["gpt-5-codex", "gpt-5-codex-mini"]
    first = options[0]
    assert first.label == "GPT-5 Codex"
    assert first.efforts == ("low", "high")
    assert first.default_effort == "medium"
    assert first.current is True
    assert options[1].current is False


def test_codex_catalog_falls_back_to_its_cache_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex-cache-home"
    home.mkdir()
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "cached-model",
                        "display_name": "Cached",
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "max"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CUBES_CODEX_BIN", str(tmp_path / "missing"))
    cli_engines._CATALOG_CACHE.clear()

    options = asyncio.run(cli_engines.model_catalog("codex"))

    assert [option.name for option in options] == ["cached-model"]
    assert options[0].efforts == ("max",)


def test_claude_catalog_reads_aliases_picker_and_env_mapping(fake_cli: Path) -> None:
    options = asyncio.run(cli_engines.model_catalog("claude-code"))
    by_name = {option.name: option for option in options}

    # Aliases first, then the picker, then the model the CLI is set to when it is not listed.
    assert [option.name for option in options][:5] == [
        "opus",
        "sonnet",
        "haiku",
        "fable",
        "kiro/claude-sonnet-5-5[1m]",
    ]
    assert by_name["sonnet"].efforts == cli_engines.CLAUDE_EFFORTS
    assert "mify/deepseek-flash[1m]" in by_name["sonnet"].description
    # ``[1m]`` is part of the model string, so it is kept verbatim.
    assert by_name["kiro/claude-sonnet-5-5[1m]"].label == "claude-sonnet-5.5 · Kiro"
    assert by_name["sonnet[1m]"].current is True


# ------------------------------------------------------------------------- effort


def test_effort_reaches_the_command_line(tmp_path: Path) -> None:
    resolved = replace_resolved(tmp_path, "kiro/claude-sonnet-5-5", effort="high")

    claude = cli_engines.argv_for("claude-code", resolved, "claude", "")
    codex = cli_engines.argv_for("codex", resolved, "codex", "")

    assert claude[claude.index("--effort") + 1] == "high"
    assert codex[codex.index("-c") + 1] == 'model_reasoning_effort="high"'
    # No effort means no flag at all: the CLI keeps its own setting.
    plain = cli_engines.argv_for("claude-code", replace_resolved(tmp_path, "sonnet"), "claude", "")
    assert "--effort" not in plain


def test_argv_follows_the_bot_approval_mode(tmp_path: Path) -> None:
    ws = str(tmp_path / "ws")
    extra = [str(tmp_path / "shared"), str(tmp_path / "extra")]
    base = replace_resolved(tmp_path / "ws", "sonnet")
    ask = replace(base, permission_mode="ask", folders=(ws, *extra), workdir=ws)
    auto = replace(ask, permission_mode="auto")
    full = replace(ask, permission_mode="full")

    # ask: today's behaviour, nothing widened.
    claude = cli_engines.argv_for("claude-code", ask, "claude", "")
    assert claude[claude.index("--permission-mode") + 1] == "acceptEdits"
    assert "--add-dir" not in claude
    codex = cli_engines.argv_for("codex", ask, "codex", "")
    assert "--sandbox" not in codex
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex

    # auto: every allowed folder beyond the CLI's own working folder.
    claude = cli_engines.argv_for("claude-code", auto, "claude", "")
    assert claude[claude.index("--permission-mode") + 1] == "acceptEdits"
    assert claude[claude.index("--add-dir") + 1 : claude.index("--add-dir") + 3] == extra
    assert ws not in claude
    codex = cli_engines.argv_for("codex", auto, "codex", "")
    assert codex[codex.index("--sandbox") + 1] == "workspace-write"
    assert [codex[index + 1] for index, item in enumerate(codex) if item == "--add-dir"] == extra
    # ``codex exec resume`` has no --sandbox or --add-dir: the same answer goes through -c.
    resumed = cli_engines.argv_for(
        "codex", auto, "codex", "", session=cli_engines.CLISession(id="t-1", resume=True)
    )
    assert "resume" in resumed
    assert "--sandbox" not in resumed and "--add-dir" not in resumed
    assert 'sandbox_mode="workspace-write"' in resumed
    assert (
        'sandbox_workspace_write.writable_roots=["' + extra[0] + '", "' + extra[1] + '"]'
        in resumed
    )

    # full: the CLI's own bypass, with the companion flag Claude Code requires for the mode.
    claude = cli_engines.argv_for("claude-code", full, "claude", "")
    assert claude[claude.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--allow-dangerously-skip-permissions" in claude
    codex = cli_engines.argv_for("codex", full, "codex", "")
    assert "--dangerously-bypass-approvals-and-sandbox" in codex


def test_effort_validation_follows_the_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_engines._CATALOG_CACHE["codex"] = (
        time.monotonic(),
        [cli_engines.ModelOption("cached-only", "Cached", efforts=("low", "medium"))],
    )
    try:
        assert cli_engines.effort_allowed("codex", "cached-only", "medium") is True
        assert cli_engines.effort_allowed("codex", "cached-only", "ultra") is False
        # A model the catalog does not describe accepts what the CLI itself knows.
        assert cli_engines.effort_allowed("claude-code", "默认", "xhigh") is True
        assert cli_engines.effort_allowed("claude-code", "默认", "turbo") is False
    finally:
        cli_engines._CATALOG_CACHE.pop("codex", None)


def test_an_invalid_effort_is_refused(make_backend, fake_cli: Path) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": []},
            )
            assert created.status_code == 201, created.text
            provider = created.json()

            refused = await client.patch(
                f"/agents/{supervisor['id']}",
                json={
                    "model_ref": {
                        "provider_id": provider["id"],
                        "model": "sonnet",
                        "effort": "turbo",
                    }
                },
            )
            assert refused.status_code == 422, refused.text
            assert "思考强度" in refused.json()["detail"]

            accepted = await client.patch(
                f"/agents/{supervisor['id']}",
                json={
                    "model_ref": {
                        "provider_id": provider["id"],
                        "model": "sonnet",
                        "effort": "high",
                    }
                },
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["model_ref"]["effort"] == "high"

    asyncio.run(body())
    stored = store.get_agent(supervisor["id"])
    assert stored["model_ref"]["effort"] == "high"


# ------------------------------------------------------------------- CLI sessions


def _calls(log_dir: Path) -> list[dict]:
    path = log_dir / "calls.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _cli_chat(backend):
    store = store_of(backend)
    chat = store.ensure_direct_conversation((store.supervisor())["id"])
    return store, chat


def test_the_second_turn_resumes_with_only_the_new_message(
    make_backend, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_dir = tmp_path / "cli-log"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    backend = make_backend()
    store, chat = _cli_chat(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": ["默认"]},
            )
            provider = created.json()
            await client.put("/providers/default", json={"provider_id": provider["id"], "model": "默认"})
            for text in ("第一句", "第二句"):
                run = await start_run(client, chat["id"], text)
                finished = await wait_for_run(client, run["id"])
                assert finished["status"] == "completed", json.dumps(finished, ensure_ascii=False)

    asyncio.run(body())
    calls = _calls(log_dir)
    assert len(calls) == 2
    first, second = calls

    assert "--session-id" in first["argv"]
    assert "--resume" not in first["argv"]
    assert "第一句" in first["prompt"]

    assert "--resume" in second["argv"]
    assert "--session-id" not in second["argv"]
    assert second["argv"][second["argv"].index("--resume") + 1] == "s-1"
    # Only what the CLI session has not seen: the user's new message, not the old one.
    assert "第二句" in second["prompt"]
    assert "第一句" not in second["prompt"]


def test_a_rewind_starts_a_fresh_session(
    make_backend, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_dir = tmp_path / "cli-log-rewind"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    backend = make_backend()
    store, chat = _cli_chat(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": ["默认"]},
            )
            provider = created.json()
            await client.put("/providers/default", json={"provider_id": provider["id"], "model": "默认"})
            for text in ("第一句", "第二句"):
                run = await start_run(client, chat["id"], text)
                await wait_for_run(client, run["id"])
            detail = await get_conversation(client, chat["id"])
            first_user = next(row for row in detail["messages"] if row["kind"] == "user")
            rewound = await client.post(f"/conversations/{chat['id']}/messages/{first_user['id']}/rewind")
            assert rewound.status_code == 200, rewound.text
            run = await start_run(client, chat["id"], "第三句")
            await wait_for_run(client, run["id"])

    asyncio.run(body())
    calls = _calls(log_dir)
    assert len(calls) == 3
    third = calls[2]
    assert "--resume" not in third["argv"]
    assert "--session-id" in third["argv"]
    # Everything the fresh session needs to know is sent again.
    assert "第一句" in third["prompt"]
    assert "第三句" in third["prompt"]


def test_a_resume_the_cli_cannot_find_falls_back_to_the_full_history(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "cli-log-fallback"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    monkeypatch.setenv("FAKE_CLI_MODE", "resume-gone")
    history = [
        {"role": "system", "content": "你是一只叫小方的方块机器人。"},
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "你好，世界"},
        {"role": "user", "content": "打个招呼"},
    ]
    stream = cli_engines.CLIStream(
        "claude-code",
        resolved("claude-code", tmp_path),
        messages=history,
        session=cli_engines.CLISession("s-gone", resume=True),
        prompt_messages=[{"role": "user", "content": "打个招呼"}],
    )
    pieces = collect(stream)
    stream.validate(allow_tool_calls=True)

    assert "".join(pieces) == "你好，世界"
    calls = _calls(log_dir)
    assert "--resume" in calls[0]["argv"]
    assert calls[1]["argv"][calls[1]["argv"].index("--session-id") + 1] != "s-gone"
    assert "第一句" in calls[1]["prompt"]


def test_a_steer_to_a_cli_bot_waits_instead(
    make_backend, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CLI drives its own session: there is no mid-turn seam, so the send queues.

    The team default is 插进这一轮 here, so only the engine's own limit can turn it into a
    queued send.
    """
    monkeypatch.setenv("FAKE_CLI_MODE", "slow")
    monkeypatch.setenv("FAKE_PID_FILE", str(tmp_path / "engine-pid"))
    backend = make_backend()
    store = store_of(backend)
    chat = store.ensure_direct_conversation((store.supervisor())["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": ["默认"]},
            )
            provider = created.json()
            await client.put(
                "/providers/default", json={"provider_id": provider["id"], "model": "默认"}
            )
            run = await start_run(client, chat["id"], "慢慢想")
            await wait_for_message(client, chat["id"], status="streaming")

            waiting = await client.post(
                f"/conversations/{chat['id']}/runs", json={"content": "加一句"}
            )
            assert waiting.status_code == 202, waiting.text
            assert waiting.json()["status"] == "queued"

            await client.delete(f"/runs/{waiting.json()['id']}")
            await client.post(f"/runs/{run['id']}/stop", json={})
            await wait_for_run(client, run["id"], statuses=("stopped",))

    asyncio.run(body())


# ----------------------------------------------------------------------- media


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


def media_messages() -> list[dict]:
    """One turn carrying an image and a PDF the way ``history.with_attachments`` builds it."""
    return [
        {"role": "system", "content": "你是一只叫小方的方块机器人。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "用户：看看这两份东西"},
                {"type": "text", "text": "[图片 att_aaaaaaaaaaaa]"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
                },
                {"type": "text", "text": "[PDF 报告.pdf（att_bbbbbbbbbbbb）]"},
                {
                    "type": "file",
                    "file": {
                        "filename": "报告.pdf",
                        "file_data": "data:application/pdf;base64," + base64.b64encode(PDF).decode(),
                    },
                },
            ],
        },
    ]


def test_claude_receives_images_and_pdfs_as_stream_blocks(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "cli-media-log"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    stream = cli_engines.CLIStream("claude-code", resolved("claude-code", tmp_path), messages=media_messages())
    collect(stream)
    stream.validate(allow_tool_calls=True)

    call = _calls(log_dir)[0]
    # One stream-json user line on stdin, with the media in place.
    assert "--input-format" in call["argv"]
    assert call["argv"][call["argv"].index("--input-format") + 1] == "stream-json"
    blocks = call["blocks"]
    images = [block for block in blocks if block["type"] == "image"]
    assert images == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(PNG).decode(),
            },
        }
    ]
    documents = [block for block in blocks if block["type"] == "document"]
    assert documents == [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(PDF).decode(),
            },
        }
    ]
    # The labels stay in the text, so the model can still name an attachment by id.
    assert "[图片 att_aaaaaaaaaaaa]" in call["prompt"]
    assert "att_bbbbbbbbbbbb" in call["prompt"]


def test_codex_gets_images_as_files_and_pdfs_in_its_folder(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "cli-codex-media-log"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    workdir = tmp_path / "bot"
    workdir.mkdir()
    stream = cli_engines.CLIStream("codex", resolved("codex", workdir), messages=media_messages())
    pieces = collect(stream)
    stream.validate(allow_tool_calls=True)
    assert pieces == ["来自 Codex"]

    call = _calls(log_dir)[0]
    assert len(call["images"]) == 1
    staged = call["images"][0]
    # The file existed while the CLI ran, with the real bytes and an extension it knows.
    assert staged["exists"] is True
    assert staged["size"] == len(PNG)
    assert Path(staged["path"]).suffix == ".png"
    assert Path(staged["path"]).parent != workdir
    # The PDF is in the bot's own folder, and the prompt names that relative path.
    assert call["files"] == ["报告.pdf"]
    assert "附件/报告.pdf" in call["prompt"]
    assert (workdir / "附件" / "报告.pdf").read_bytes() == PDF
    # The staged images are gone once the turn has ended.
    assert not Path(staged["path"]).exists()


def test_codex_never_overwrites_a_pdf_with_the_same_name(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "cli-codex-dedupe"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    workdir = tmp_path / "bot-dedupe"
    workdir.mkdir()
    second = PDF.replace(b"1 0 obj", b"2 0 obj")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "用户：两份同名的报告"},
                {"type": "file", "file": {"filename": "报告.pdf", "file_data": "data:application/pdf;base64," + base64.b64encode(PDF).decode()}},
                {"type": "file", "file": {"filename": "报告.pdf", "file_data": "data:application/pdf;base64," + base64.b64encode(second).decode()}},
            ],
        }
    ]
    stream = cli_engines.CLIStream("codex", resolved("codex", workdir), messages=messages)
    collect(stream)

    call = _calls(log_dir)[0]
    assert call["files"] == ["报告 (2).pdf", "报告.pdf"] or call["files"] == ["报告.pdf", "报告 (2).pdf"]
    assert (workdir / "附件" / "报告.pdf").read_bytes() == PDF
    assert (workdir / "附件" / "报告 (2).pdf").read_bytes() == second


def test_cli_protocols_report_their_media_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_engines._CATALOG_CACHE["codex"] = (
        time.monotonic(),
        [
            cli_engines.ModelOption("gpt-5-codex", "GPT-5 Codex", input_modalities=("text", "image")),
            cli_engines.ModelOption("text-only", "Text Only", input_modalities=("text",)),
            cli_engines.ModelOption("unknown", "Unknown"),
        ],
    )
    try:
        def caps(protocol: str, model: str):
            return providers.model_caps(
                {"protocol": protocol, "models": [model], "model_overrides": {}, "model_caps": {}},
                model,
            )

        claude = caps("claude-code", "默认")
        assert (claude.images, claude.pdf) == (True, True)
        assert claude.sources["pdf"] == "provider"
        codex = caps("codex", "gpt-5-codex")
        assert (codex.images, codex.pdf) == (True, True)
        assert caps("codex", "text-only").images is False
        # An entry that says nothing keeps the CLI's usual ability.
        assert caps("codex", "unknown").images is True
    finally:
        cli_engines._CATALOG_CACHE.pop("codex", None)


def test_a_claude_bot_really_receives_uploaded_images_and_pdfs(
    make_backend, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_dir = tmp_path / "cli-upload-log"
    log_dir.mkdir()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    backend = make_backend()
    store, chat = _cli_chat(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={"name": "Claude 订阅", "protocol": "claude-code", "base_url": "", "models": ["默认"]},
            )
            provider = created.json()
            await client.put("/providers/default", json={"provider_id": provider["id"], "model": "默认"})
            image = (await client.post("/attachments", content=PNG, headers={"Content-Type": "image/png"})).json()
            pdf = (
                await client.post(
                    "/attachments",
                    content=PDF,
                    headers={"Content-Type": "application/pdf", "X-Attachment-Name": "%E6%8A%A5%E5%91%8A.pdf"},
                )
            ).json()
            started = await client.post(
                f"/conversations/{chat['id']}/runs",
                json={"content": "看看这两份", "attachment_ids": [image["id"], pdf["id"]]},
            )
            assert started.status_code == 202, started.text
            finished = await wait_for_run(client, started.json()["id"])
            assert finished["status"] == "completed", json.dumps(finished, ensure_ascii=False)

    asyncio.run(body())
    call = _calls(log_dir)[0]
    kinds = [block["type"] for block in call["blocks"]]
    assert kinds.count("image") == 1
    assert kinds.count("document") == 1
    assert "报告.pdf" in call["prompt"]
