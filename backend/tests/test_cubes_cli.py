"""The headless CLI and its MCP server against a real backend and the fake upstream."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import cubes_cli
from fake_upstream import call, tool_calls
from helpers import store_of

BACKEND_DIR = Path(__file__).resolve().parents[1]


def configure(monkeypatch, backend) -> None:
    """Point the CLI at this backend and ignore the developer's own cli.json."""
    monkeypatch.setenv("CUBES_URL", backend.url)
    monkeypatch.setenv("CUBES_CLI_CONFIG", "")
    monkeypatch.delenv("CUBES_TOKEN_FILE", raising=False)


def tool_text(result) -> str:
    """The text a tool returned, whichever block shape the SDK used."""
    parts = [
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    assert parts, result
    return "".join(parts)


# ----------------------------------------------------------------------------- CLI


def test_send_wait_returns_the_reply(make_backend, upstream, monkeypatch, capsys) -> None:
    backend = make_backend()
    configure(monkeypatch, backend)

    code = cubes_cli.main(["send", "主管", "你好", "--wait", "--timeout", "30"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "已交给「主管」" in captured.out
    assert "回复正文" in captured.out


def test_send_to_a_group_by_name(make_backend, upstream, monkeypatch, capsys) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="研究", kind="member")
    store.create_conversation(
        kind="group",
        title="内容组",
        purpose="写稿",
        member_ids=[store.supervisor()["id"], member["id"]],
    )
    configure(monkeypatch, backend)

    code = cubes_cli.main(["send", "内容组", "你好", "--wait", "--timeout", "30"])

    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert "已交给「内容组」" in captured.out
    assert "回复正文" in captured.out


def test_send_wait_json_is_one_document(make_backend, upstream, monkeypatch, capsys) -> None:
    backend = make_backend()
    configure(monkeypatch, backend)

    code = cubes_cli.main(
        ["send", "主管", "你好", "--wait", "--timeout", "30", "--json"]
    )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    document = json.loads(captured.out)  # the whole stdout is exactly one JSON document
    assert document["target"]["name"] == "主管"
    assert document["target"]["kind"] == "bot"
    assert document["target"]["id"]
    assert document["run"]["status"] == "completed"
    assert document["run"]["id"]
    assert document["reply"]["author_name"] == "主管"
    assert document["reply"]["content"] == "回复正文"
    assert document["waiting"] is None


def test_send_wait_parked_on_a_card_exits_3(
    make_backend, upstream, monkeypatch, capsys
) -> None:
    backend = make_backend()
    member = store_of(backend).create_agent(
        name="甲", instructions="研究", kind="member"
    )
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "delete_agent",
                {"agent_id": member["id"], "reason": "它已经完成任务"},
            )
        ),
        {"content": "已按你的决定处理"},
    ]
    configure(monkeypatch, backend)

    code = cubes_cli.main(["send", "主管", "把甲删掉", "--wait", "--timeout", "30"])

    captured = capsys.readouterr()
    assert code == 3, captured.out + captured.err
    assert "删除成员「甲」" in captured.out
    assert "等你" in captured.out
    # Nothing was decided for the user.
    assert store_of(backend).get_agent(member["id"]) is not None


def test_send_wait_times_out_exit_4(make_backend, upstream, monkeypatch, capsys) -> None:
    backend = make_backend()
    upstream.chunk_delay = 0.3
    configure(monkeypatch, backend)

    code = cubes_cli.main(["send", "主管", "慢慢来", "--wait", "--timeout", "0.2"])

    captured = capsys.readouterr()
    assert code == 4, captured.out + captured.err
    assert "任务还在跑" in captured.out


def test_unreachable_backend_exits_5(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CUBES_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CUBES_CLI_CONFIG", "")
    monkeypatch.delenv("CUBES_TOKEN_FILE", raising=False)

    code = cubes_cli.main(["status"])

    captured = capsys.readouterr()
    assert code == 5
    assert captured.out == ""
    assert "连不上后端" in captured.err


def test_console_wrapper_sends_and_waits(make_backend, upstream) -> None:
    """The documented entry point: `cubes send 主管 "你好" --wait` prints the reply."""
    backend = make_backend()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CUBES_TOKEN_FILE", "CUBES_URL", "CUBES_CLI_CONFIG")
    }
    env["CUBES_URL"] = backend.url
    env["CUBES_CLI_CONFIG"] = ""

    completed = subprocess.run(
        [
            str(BACKEND_DIR / "scripts" / "cubes"),
            "send",
            "主管",
            "你好",
            "--wait",
            "--timeout",
            "30",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert "已交给「主管」" in completed.stdout
    assert "回复正文" in completed.stdout


def test_bots_and_result_commands(make_backend, upstream, monkeypatch, capsys) -> None:
    backend = make_backend()
    store_of(backend).create_agent(name="甲", instructions="研究", kind="member")
    configure(monkeypatch, backend)

    assert cubes_cli.main(["bots"]) == 0
    listed = capsys.readouterr().out
    assert "主管" in listed
    assert "甲" in listed

    assert cubes_cli.main(["send", "主管", "你好", "--wait", "--timeout", "30", "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)["run"]["id"]

    assert cubes_cli.main(["result", run_id, "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["run"]["status"] == "completed"
    assert document["reply"]["content"] == "回复正文"

    assert cubes_cli.main(["runs", "--json"]) == 0
    runs = json.loads(capsys.readouterr().out)["runs"]
    assert any(run["id"] == run_id for run in runs)


# ----------------------------------------------------------------------------- MCP


def test_mcp_server_exposes_four_tools_and_runs_a_task(
    make_backend, upstream, monkeypatch
) -> None:
    from mcp import Client

    backend = make_backend()
    configure(monkeypatch, backend)

    async def body() -> None:
        server = cubes_cli.build_mcp_server()
        async with Client(server) as client:
            listing = await client.list_tools()
            assert sorted(tool.name for tool in listing.tools) == [
                "get_result",
                "list_bots",
                "send_task",
                "wait_run",
            ]

            bots = json.loads(tool_text(await client.call_tool("list_bots", {})))
            assert any(bot["name"] == "主管" for bot in bots["bots"])

            sent = json.loads(
                tool_text(
                    await client.call_tool("send_task", {"target": "主管", "task": "你好"})
                )
            )
            assert sent["ok"] is True
            run_id = sent["run_id"]

            waited = json.loads(
                tool_text(
                    await client.call_tool("wait_run", {"run_id": run_id, "timeout": 30})
                )
            )
            assert waited["status"] == "completed"
            assert waited["waiting_on_user"] is False
            assert waited["reply"]["content"] == "回复正文"

            got = json.loads(tool_text(await client.call_tool("get_result", {"run_id": run_id})))
            assert got["run"]["status"] == "completed"
            assert got["reply"]["content"] == "回复正文"

    asyncio.run(body())


def test_mcp_stdio_entrypoint_serves_the_four_tools() -> None:
    """`cubes mcp` is the command Claude Desktop runs; it must speak MCP on stdio."""
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    server = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(cubes_cli.__file__).resolve()), "mcp"],
    )

    async def body() -> None:
        async with Client(server) as client:
            listing = await client.list_tools()
            assert sorted(tool.name for tool in listing.tools) == [
                "get_result",
                "list_bots",
                "send_task",
                "wait_run",
            ]

    asyncio.run(body())


def test_token_file_authenticates_against_a_guarded_backend(
    make_backend, upstream, monkeypatch, capsys, tmp_path
) -> None:
    backend = make_backend(auth_token="test-token")
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n", encoding="utf-8")
    monkeypatch.setenv("CUBES_URL", backend.url)
    monkeypatch.setenv("CUBES_CLI_CONFIG", "")
    monkeypatch.setenv("CUBES_TOKEN_FILE", str(token_file))

    code = cubes_cli.main(["send", "主管", "你好", "--wait", "--timeout", "30"])

    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert "回复正文" in captured.out


def test_wrong_token_is_reported_not_retried(
    make_backend, monkeypatch, capsys, tmp_path
) -> None:
    backend = make_backend(auth_token="test-token")
    token_file = tmp_path / "token"
    token_file.write_text("wrong\n", encoding="utf-8")
    monkeypatch.setenv("CUBES_URL", backend.url)
    monkeypatch.setenv("CUBES_CLI_CONFIG", "")
    monkeypatch.setenv("CUBES_TOKEN_FILE", str(token_file))

    code = cubes_cli.main(["status"])

    captured = capsys.readouterr()
    assert code == 1
    assert "401" in captured.err
