"""Code pack: isolated subprocess, minimal environment, capped output and a hard timeout."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.toolkit import split_result, tool_specs
from app.toolkit.base import ToolContext, ToolInputError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def settings_for(ws: Path, tmp_path: Path) -> Settings:
    return Settings(database_path=tmp_path / "cubes.sqlite3", workspace_dir=ws)


async def call(ws: Path, tmp_path: Path, args: dict, name: str = "run_code") -> str:
    settings = settings_for(ws, tmp_path)
    spec = next(s for s in tool_specs(settings, "supervisor") if s.name == name)
    ctx = ToolContext(
        store=None,  # type: ignore[arg-type]
        settings=settings,
        run_id="r1",
        conversation_id="c1",
        agent={"id": "a1", "name": "主管", "kind": "supervisor"},
        http=httpx.AsyncClient(trust_env=False),
        publish=lambda event, payload: None,
    )
    try:
        text, _ = split_result(await spec.run(ctx, args))
        return text
    finally:
        await ctx.http.aclose()


def test_run_code_never_sees_backend_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = workspace(tmp_path)
    monkeypatch.setenv("MODEL_API_KEY", "super-secret")
    monkeypatch.setenv("PARALLEL_API_KEY", "another-secret")

    async def body() -> None:
        out = await call(
            ws,
            tmp_path,
            {
                "language": "python",
                "code": (
                    "import os\n"
                    "print('MODEL_API_KEY' in os.environ)\n"
                    "print('PARALLEL_API_KEY' in os.environ)\n"
                    "print(os.getcwd())\n"
                    "print(os.environ.get('HOME'))\n"
                    "print(sorted(os.environ)[:20])\n"
                ),
            },
        )
        assert "False\nFalse" in out
        assert "退出码：0" in out
        assert str(ws) in out

    run(body())


def test_run_code_uses_the_workspace_as_cwd(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "data.txt").write_text("你好", encoding="utf-8")

    async def body() -> None:
        out = await call(
            ws,
            tmp_path,
            {"language": "python", "code": "print(open('data.txt', encoding='utf-8').read())"},
        )
        assert "你好" in out

    run(body())


def test_run_code_reports_the_exit_code_and_stderr(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        out = await call(
            ws,
            tmp_path,
            {"language": "python", "code": "import sys\nprint('问题', file=sys.stderr)\nsys.exit(3)"},
        )
        assert "退出码：3" in out
        assert "问题" in out

    run(body())


def test_shell_runs(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        out = await call(ws, tmp_path, {"language": "shell", "code": "echo 你好 $((1 + 2))"})
        assert "你好 3" in out
        assert "退出码：0" in out

    run(body())


def test_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        started = time.monotonic()
        out = await call(
            ws,
            tmp_path,
            {
                "language": "python",
                "code": "import time\nprint('start', flush=True)\ntime.sleep(30)",
                "timeout_seconds": 1,
            },
        )
        assert time.monotonic() - started < 10
        assert "超时" in out

    run(body())


def test_output_is_capped(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        out = await call(
            ws, tmp_path, {"language": "python", "code": "print('a' * 40000)"}
        )
        assert len(out) < 20_000
        assert "已截断" in out

    run(body())


def test_run_code_validates_arguments(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        for args in (
            {"language": "ruby", "code": "puts 1"},
            {"language": "python", "code": "   "},
            {"language": "python", "code": "print(1)", "timeout_seconds": 0},
        ):
            with pytest.raises(ToolInputError) as caught:
                await call(ws, tmp_path, args)
            assert str(caught.value).endswith("。")

    run(body())


def test_run_code_is_offered_with_the_default_working_folder(tmp_path: Path) -> None:
    # No workspace switch: the folder defaults beside the database and the approval mode
    # decides whether a run needs the user's yes first.
    settings = Settings(database_path=tmp_path / "cubes.sqlite3")
    offered = [spec.name for spec in tool_specs(settings, "supervisor")]
    assert "run_code" in offered
    assert "file_read" in offered
