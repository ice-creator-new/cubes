"""Files pack: everything stays inside the workspace, reads and writes are capped."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.toolkit import split_result, tool_specs
from app.toolkit.base import ToolContext, ToolInputError
from fake_upstream import call as fake_call, tool_calls
from helpers import action_messages, client_for, get_conversation, get_workspace, run_goal


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def settings_for(workspace_dir: Path, tmp_path: Path) -> Settings:
    # The database lives under data/, so a file beside it in tmp_path is outside Cubes' data
    # and outside the working folder: the escape tests exercise that branch, not protection.
    return Settings(
        database_path=tmp_path / "data" / "cubes.sqlite3", workspace_dir=workspace_dir
    )


async def call_tool(workspace_dir: Path, tmp_path: Path, name: str, args: dict) -> Any:
    settings = settings_for(workspace_dir, tmp_path)
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
        return await spec.run(ctx, args)
    finally:
        await ctx.http.aclose()


async def call_result(
    workspace_dir: Path, tmp_path: Path, name: str, args: dict
) -> tuple[str, dict | None]:
    return split_result(await call_tool(workspace_dir, tmp_path, name, args))


async def call(workspace_dir: Path, tmp_path: Path, name: str, args: dict) -> str:
    text, _ = await call_result(workspace_dir, tmp_path, name, args)
    return text


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        written = await call(
            ws, tmp_path, "file_write", {"path": "notes/计划.md", "content": "第一行\n第二行"}
        )
        assert "notes/计划.md" in written
        assert (ws / "notes" / "计划.md").read_text(encoding="utf-8") == "第一行\n第二行"
        read = await call(ws, tmp_path, "file_read", {"path": "notes/计划.md"})
        assert "第一行" in read and "第二行" in read
        appended = await call(
            ws, tmp_path, "file_write", {"path": "notes/计划.md", "content": "追加", "mode": "append"}
        )
        assert "追加" in appended
        text = (ws / "notes" / "计划.md").read_text(encoding="utf-8")
        assert text.endswith("追加")
        assert "第一行" in text

    run(body())


def test_list_shows_type_and_size(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "a.txt").write_text("12345", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.txt").write_text("x", encoding="utf-8")

    async def body() -> None:
        root = await call(ws, tmp_path, "file_list", {})
        assert "a.txt" in root
        assert "5 B" in root
        assert "sub/" in root
        nested = await call(ws, tmp_path, "file_list", {"path": "sub"})
        assert "b.txt" in nested
        assert "a.txt" not in nested

    run(body())


def test_list_caps_at_two_hundred_entries(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    for index in range(210):
        (ws / f"f{index:03}.txt").write_text("x", encoding="utf-8")

    async def body() -> None:
        listing = await call(ws, tmp_path, "file_list", {})
        assert listing.count("\n") <= 210
        assert "还有 10 项未显示" in listing

    run(body())


def test_read_caps_raw_and_visible_size(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "big.txt").write_text("à" * 200_000, encoding="utf-8")

    async def body() -> None:
        text = await call(ws, tmp_path, "file_read", {"path": "big.txt"})
        assert "已截断" in text
        assert len(text) < 10_000
        wide = await call(
            ws, tmp_path, "file_read", {"path": "big.txt", "max_chars": 5_000}
        )
        assert len(wide) <= 5_200

    run(body())


def test_read_missing_file(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        with pytest.raises(ToolInputError) as caught:
            await call(ws, tmp_path, "file_read", {"path": "missing.txt"})
        assert "不存在" in str(caught.value)

    run(body())


def test_write_caps_size(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        with pytest.raises(ToolInputError) as caught:
            await call(ws, tmp_path, "file_write", {"path": "huge.txt", "content": "a" * (1024 * 1024 + 1)})
        assert "过大" in str(caught.value) or "MB" in str(caught.value)
        assert not (ws / "huge.txt").exists()

    run(body())


def test_paths_cannot_escape_the_workspace(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    async def body() -> None:
        for args in (
            {"path": "../outside.txt"},
            {"path": "../../etc/passwd"},
            {"path": str(tmp_path / "outside.txt")},
            {"path": "/etc/passwd"},
        ):
            with pytest.raises(ToolInputError) as caught:
                await call(ws, tmp_path, "file_read", args)
            assert "允许的文件夹之外" in str(caught.value)
        with pytest.raises(ToolInputError):
            await call(ws, tmp_path, "file_write", {"path": "../escape.txt", "content": "x"})
        assert not (tmp_path / "escape.txt").exists()

    run(body())


def test_symlink_escapes_are_rejected(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    (ws / "link.txt").symlink_to(target)
    (ws / "linkdir").symlink_to(tmp_path, target_is_directory=True)

    async def body() -> None:
        for path in ("link.txt", "linkdir/outside.txt"):
            with pytest.raises(ToolInputError) as caught:
                await call(ws, tmp_path, "file_read", {"path": path})
            assert "允许的文件夹之外" in str(caught.value)
        with pytest.raises(ToolInputError):
            await call(ws, tmp_path, "file_write", {"path": "linkdir/evil.txt", "content": "x"})
        assert not (tmp_path / "evil.txt").exists()

    run(body())


def test_write_is_atomic(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "keep.txt").write_text("old", encoding="utf-8")

    async def body() -> None:
        await call(ws, tmp_path, "file_write", {"path": "keep.txt", "content": "new"})
        assert (ws / "keep.txt").read_text(encoding="utf-8") == "new"
        leftovers = [path.name for path in ws.iterdir() if path.name.startswith(".")]
        assert leftovers == []

    run(body())


def test_absolute_path_inside_the_workspace_is_allowed(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "inner.txt").write_text("inside", encoding="utf-8")

    async def body() -> None:
        text = await call(ws, tmp_path, "file_read", {"path": str(ws / "inner.txt")})
        assert "inside" in text

    run(body())


# ------------------------------------------------------------- action summaries


def test_file_action_summaries_are_relative_and_end_with_the_file_name(tmp_path: Path) -> None:
    """The action row shows the path the way the user reads it, not the absolute prefix."""
    ws = workspace(tmp_path)
    long = "reports/2026/november/week-47/final_copy_of_band_5.json"

    async def body() -> None:
        written = await call_tool(ws, tmp_path, "file_write", {"path": "data/band_5.json", "content": "x"})
        assert written.summary == "写入文件 data/band_5.json"
        read = await call_tool(ws, tmp_path, "file_read", {"path": "data/band_5.json"})
        assert read.summary == "读取文件 data/band_5.json"
        listed = await call_tool(ws, tmp_path, "file_list", {"path": "data"})
        assert listed.summary == "查看文件 data"
        # Still too long: the end survives, because the file name is what identifies it.
        deep = await call_tool(ws, tmp_path, "file_write", {"path": long, "content": "x"})
        assert len(deep.summary) <= 44 + len("写入文件 ")
        assert deep.summary.startswith("写入文件 …")
        assert deep.summary.endswith("band_5.json")
        assert "/Users/" not in deep.summary

    run(body())


# ------------------------------------------------------------- write diff detail


def test_write_detail_for_a_new_file_lists_every_line_as_added(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        text, detail = await call_result(
            ws, tmp_path, "file_write", {"path": "新建.md", "content": "一\n二"}
        )
        target = (ws / "新建.md").resolve()
        assert detail == {
            "kind": "diff",
            "path": str(target),
            "display_path": "新建.md",
            "mode": "create",
            "added": 2,
            "removed": 0,
            "lines": [{"op": "+", "text": "一"}, {"op": "+", "text": "二"}],
            "truncated": False,
        }
        assert "新建.md" in text and "+2 -0" in text

    run(body())


def test_write_detail_overwrite_is_a_context_diff(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "plan.md").write_text("标题\n保留\n旧行\n尾行", encoding="utf-8")

    async def body() -> None:
        text, detail = await call_result(
            ws, tmp_path, "file_write", {"path": "plan.md", "content": "标题\n保留\n新行\n尾行"}
        )
        assert detail is not None
        assert detail["mode"] == "overwrite"
        assert detail["added"] == 1
        assert detail["removed"] == 1
        assert detail["lines"] == [
            {"op": " ", "text": "标题"},
            {"op": " ", "text": "保留"},
            {"op": "-", "text": "旧行"},
            {"op": "+", "text": "新行"},
            {"op": " ", "text": "尾行"},
        ]
        assert detail["truncated"] is False
        assert "+1 -1" in text

    run(body())


def test_write_detail_append_only_shows_appended_lines(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "log.txt").write_text("已有\n", encoding="utf-8")

    async def body() -> None:
        _, detail = await call_result(
            ws, tmp_path, "file_write", {"path": "log.txt", "content": "二\n三", "mode": "append"}
        )
        assert detail is not None
        assert detail["mode"] == "append"
        assert detail["added"] == 2
        assert detail["removed"] == 0
        assert detail["lines"] == [{"op": "+", "text": "二"}, {"op": "+", "text": "三"}]
        assert "note" not in detail

    run(body())


def test_write_detail_truncates_lines_but_keeps_counts_exact(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    old = [f"第 {index} 行" for index in range(300)]
    (ws / "long.txt").write_text("\n".join(old), encoding="utf-8")
    new = [f"改 {index} 行" for index in range(300)]

    async def body() -> None:
        _, detail = await call_result(
            ws, tmp_path, "file_write", {"path": "long.txt", "content": "\n".join(new)}
        )
        assert detail is not None
        assert detail["added"] == 300 and detail["removed"] == 300
        assert len(detail["lines"]) == 200
        assert detail["truncated"] is True

    run(body())


def test_write_detail_clips_a_long_line(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def body() -> None:
        _, detail = await call_result(
            ws, tmp_path, "file_write", {"path": "wide.txt", "content": "x" * 300}
        )
        assert detail is not None
        assert detail["added"] == 1
        assert len(detail["lines"][0]["text"]) == 200
        assert detail["lines"][0]["text"].endswith("…")
        assert detail["truncated"] is True

    run(body())


def test_write_detail_over_a_binary_file_notes_the_unknown_removal(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")

    async def body() -> None:
        _, detail = await call_result(
            ws, tmp_path, "file_write", {"path": "blob.bin", "content": "新内容"}
        )
        assert detail is not None
        assert detail["mode"] == "overwrite"
        assert detail["added"] == 1
        assert detail["removed"] == 0
        assert detail["lines"] == [{"op": "+", "text": "新内容"}]
        assert "不是 UTF-8" in detail["note"]

    run(body())


def test_write_diff_detail_reaches_the_action(make_backend, upstream, tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = make_backend(workspace_dir=ws)
    upstream.script = [
        tool_calls(
            fake_call("call_1", "file_write", {"path": "报告.md", "content": "第一行\n第二行\n"})
        ),
        {"content": "写好了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.patch("/settings/permissions", json={"mode": "full"})
            assert response.status_code == 200, response.text
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct, "写个报告")
            assert run["status"] == "completed", run
            action = action_messages(await get_conversation(client, direct))[0]["action"]
            assert action["summary"] == "写入文件 报告.md"
            target = (ws / "报告.md").resolve()
            assert action["detail"] == {
                "kind": "diff",
                "path": str(target),
                "display_path": "报告.md",
                "mode": "create",
                "added": 2,
                "removed": 0,
                "lines": [{"op": "+", "text": "第一行"}, {"op": "+", "text": "第二行"}],
                "truncated": False,
            }

    asyncio.run(body())
