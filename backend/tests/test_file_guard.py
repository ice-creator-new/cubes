"""Two runs writing one file: a stale overwrite is refused, with the author named.

The guard is optimistic: each run keeps a ledger of the fingerprints it has seen, and the
group's working set says who wrote the file and when. A refused write tells the model to read
the file again, which is the one cheap fix that makes the conflict real rather than silent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.store.db import now_iso
from app.toolkit import split_result
from app.toolkit.base import ToolContext, ToolInputError
from app.toolkit.files import run_file_read, run_file_write


class FakeStore:
    """Only the two reads the guard makes: allowed folders and the working set's writes."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def permission_settings(self) -> dict:
        return {"folders": []}

    def list_working_files(self, conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.rows[:limit]


def settings_for(workspace_dir: Path, tmp_path: Path) -> Settings:
    return Settings(database_path=tmp_path / "data" / "cubes.sqlite3", workspace_dir=workspace_dir)


def write_row(path: Path, author: str, *, minutes_ago: float = 0, author_id: str = "a1") -> dict:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "path": str(path),
        "author_id": author_id,
        "author_name": author,
        "action": "write",
        "updated_at": moment.isoformat().replace("+00:00", "Z"),
    }


def context(settings: Settings, store: FakeStore) -> ToolContext:
    return ToolContext(
        store=store,
        settings=settings,
        run_id="run-b",
        conversation_id="group-1",
        agent={"id": "a2", "name": "助理", "kind": "member"},
        http=httpx.AsyncClient(trust_env=False),
        publish=lambda event, payload: None,
        run=SimpleNamespace(file_ledger={}),
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_stale_overwrite_is_refused_and_names_the_author(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "方案.md"
    target.write_text("第一版", encoding="utf-8")
    settings = settings_for(workspace, tmp_path)
    store = FakeStore()

    async def body() -> None:
        ctx = context(settings, store)
        try:
            # This run reads the first version, so its ledger has that fingerprint.
            await run_file_read(ctx, {"path": "方案.md"})
            target.write_text("甲改过的一版", encoding="utf-8")
            store.rows = [write_row(target.resolve(), "匠师")]
            with pytest.raises(ToolInputError) as caught:
                await run_file_write(ctx, {"path": "方案.md", "content": "我要覆盖"})
            assert "「匠师」" in str(caught.value)
            assert "file_read" in str(caught.value)
            assert target.read_text(encoding="utf-8") == "甲改过的一版"
        finally:
            await ctx.http.aclose()

    run(body())


def test_reading_the_new_version_then_writing_passes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "方案.md"
    target.write_text("第一版", encoding="utf-8")
    settings = settings_for(workspace, tmp_path)
    store = FakeStore()

    async def body() -> None:
        ctx = context(settings, store)
        try:
            await run_file_read(ctx, {"path": "方案.md"})
            target.write_text("甲改过的一版", encoding="utf-8")
            store.rows = [write_row(target.resolve(), "匠师")]
            with pytest.raises(ToolInputError):
                await run_file_write(ctx, {"path": "方案.md", "content": "我要覆盖"})
            # Reading again is the fix the refusal asked for.
            await run_file_read(ctx, {"path": "方案.md"})
            text, _detail = split_result(
                await run_file_write(ctx, {"path": "方案.md", "content": "新的一版"})
            )
            assert "已写入" in text
            assert target.read_text(encoding="utf-8") == "新的一版"
        finally:
            await ctx.http.aclose()

    run(body())


def test_a_run_that_never_read_may_not_take_a_fresh_sibling_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "报告.md"
    target.write_text("甲写的", encoding="utf-8")
    settings = settings_for(workspace, tmp_path)
    store = FakeStore([write_row(target.resolve(), "匠师")])

    async def body() -> None:
        ctx = context(settings, store)
        try:
            with pytest.raises(ToolInputError) as caught:
                await run_file_write(ctx, {"path": "报告.md", "content": "抢过来"})
            assert "这个文件刚被「匠师」改过" in str(caught.value)
            # A new file is always the writer's own: creating it asks nothing.
            text, _detail = split_result(
                await run_file_write(ctx, {"path": "新文件.md", "content": "第一次写"})
            )
            assert "已写入" in text
            assert (workspace / "新文件.md").read_text(encoding="utf-8") == "第一次写"
        finally:
            await ctx.http.aclose()

    run(body())


def test_an_old_write_does_not_block_a_run_that_never_read_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "旧稿.md"
    target.write_text("很久以前的稿子", encoding="utf-8")
    settings = settings_for(workspace, tmp_path)
    store = FakeStore([write_row(target.resolve(), "匠师", minutes_ago=120)])

    async def body() -> None:
        ctx = context(settings, store)
        try:
            text, _detail = split_result(
                await run_file_write(ctx, {"path": "旧稿.md", "content": "重新写完"})
            )
            assert "已写入" in text
            assert target.read_text(encoding="utf-8") == "重新写完"
        finally:
            await ctx.http.aclose()

    run(body())


def test_an_unchanged_file_needs_no_working_set_row(tmp_path: Path) -> None:
    """An own write later in the same run is not a conflict: the ledger matches the file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "日志.txt"
    target.write_text("第一行\n", encoding="utf-8")
    settings = settings_for(workspace, tmp_path)
    store = FakeStore()

    async def body() -> None:
        ctx = context(settings, store)
        try:
            await run_file_read(ctx, {"path": "日志.txt"})
            await run_file_write(ctx, {"path": "日志.txt", "content": "第一行\n第二行\n"})
            text, _detail = split_result(
                await run_file_write(ctx, {"path": "日志.txt", "content": "追加\n", "mode": "append"})
            )
            assert "已追加" in text
            assert target.read_text(encoding="utf-8") == "第一行\n第二行\n追加\n"
        finally:
            await ctx.http.aclose()

    run(body())


def test_now_iso_is_readable_by_the_guard() -> None:
    # The guard reads working-set timestamps with the same parser the hint's ages use.
    from app.agent import working_set

    assert working_set.moment(now_iso()) is not None
