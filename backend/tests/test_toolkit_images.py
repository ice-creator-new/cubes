"""Images pack: the model sees a real picture file, cropped and shrunk without PIL."""

from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import attachments as attachment_rules
from app.config import Settings
from app.toolkit import tool_specs
from app.toolkit.base import ToolContext, ToolInputError, ToolResult
from app.toolkit.images import MAX_SOURCE_BYTES, image_size
from fake_upstream import call as fake_call, tool_calls
from helpers import action_messages, client_for, get_conversation, get_workspace, run_goal, tool_names
from test_images import enable_vision, image_blocks

SIPS = sys.platform == "darwin" and shutil.which("sips") is not None


def make_png(width: int, height: int) -> bytes:
    """A valid, tiny-to-compress PNG with no image library: black RGB rows."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def make_jpeg(png: bytes, target: Path) -> Path:
    """A real JPEG encoded by the system tool, only used when `sips` exists."""
    source = target.with_suffix(".png")
    source.write_bytes(png)
    subprocess.run(
        ["sips", "-s", "format", "jpeg", str(source), "--out", str(target)],
        check=True,
        capture_output=True,
    )
    return target


class FakeStore:
    """Just enough of the store for one successful call."""

    def __init__(self) -> None:
        self.saved: list[tuple[bytes, str, str]] = []
        self.attached: tuple[str, list[str]] | None = None

    def permission_settings(self) -> dict:
        return {"folders": []}

    def save_attachment(self, data: bytes, mime: str, name: str = "") -> dict:
        self.saved.append((data, mime, name))
        return {"id": f"att_{len(self.saved):012d}", "mime": mime, "size": len(data), "name": name}

    def attach_to_message(self, message_id: str, attachment_ids: list[str]) -> dict | None:
        self.attached = (message_id, attachment_ids)
        return {}


def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def settings_for(workspace_dir: Path, tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "data" / "cubes.sqlite3", workspace_dir=workspace_dir
    )


def spec_for(settings: Settings):
    return next(s for s in tool_specs(settings, "supervisor") if s.name == "view_image")


def context(settings: Settings, store: Any) -> ToolContext:
    return ToolContext(
        store=store,
        settings=settings,
        run_id="r1",
        conversation_id="c1",
        agent={"id": "a1", "name": "主管", "kind": "supervisor"},
        http=httpx.AsyncClient(trust_env=False),
        publish=lambda event, payload: None,
        message_id="m1",
    )


async def call(
    ws: Path, tmp_path: Path, args: dict, store: Any = None
) -> ToolResult:
    settings = settings_for(ws, tmp_path)
    ctx = context(settings, store)
    try:
        value = await spec_for(settings).run(ctx, args)
        assert isinstance(value, ToolResult)
        return value
    finally:
        await ctx.http.aclose()


def test_header_parser_reads_every_supported_container() -> None:
    png = make_png(400, 200)
    assert image_size(png) == (400, 200)

    gif = b"GIF89a" + struct.pack("<HH", 12, 34) + b"\x00" * 10
    assert image_size(gif) == (12, 34)

    # VP8X: three-byte canvas size minus one, after the flags and reserved bytes.
    vp8x = (
        b"RIFF"
        + b"\x00" * 4
        + b"WEBPVP8X"
        + b"\x00" * 4
        + b"\x00" * 4
        + (320 - 1).to_bytes(3, "little")
        + (240 - 1).to_bytes(3, "little")
    )
    assert image_size(vp8x) == (320, 240)

    # VP8L: signature byte then 14 bits of width-1 and 14 bits of height-1.
    bits = (64 - 1) | ((48 - 1) << 14)
    vp8l = b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 4 + b"\x2f" + bits.to_bytes(4, "little")
    assert image_size(vp8l) == (64, 48)

    assert image_size(b"not an image") is None
    assert image_size(b"\x89PNG\r\n\x1a\n") is None


@pytest.mark.skipif(not SIPS, reason="needs macOS sips to produce a JPEG fixture")
def test_header_parser_reads_jpeg(tmp_path: Path) -> None:
    jpeg = make_jpeg(make_png(300, 150), tmp_path / "fixture.jpg")
    assert image_size(jpeg.read_bytes()) == (300, 150)


def test_a_small_png_is_passed_through_and_saved_on_its_action(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    png = make_png(400, 200)
    (ws / "截图.png").write_bytes(png)
    store = FakeStore()

    async def body() -> None:
        result = await call(ws, tmp_path, {"path": "截图.png"}, store)
        assert result.text == "图片 截图.png，400×200，已附在下面。"
        assert result.summary == "查看图片 截图.png"
        assert result.images == ["att_000000000001"]
        assert store.saved == [(png, "image/png", "截图.png")]
        assert store.attached == ("m1", ["att_000000000001"])

    asyncio.run(body())


@pytest.mark.skipif(not SIPS, reason="needs macOS sips for the crop and resize")
def test_a_jpeg_is_viewed_too(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    path = make_jpeg(make_png(300, 150), ws / "照片.jpg")
    store = FakeStore()

    async def body() -> None:
        result = await call(ws, tmp_path, {"path": "照片.jpg"}, store)
        assert result.text == "图片 照片.jpg，300×150，已附在下面。"
        assert store.saved[0][1] == "image/jpeg"

    asyncio.run(body())


@pytest.mark.skipif(not SIPS, reason="needs macOS sips for the crop")
def test_a_crop_box_returns_only_that_region(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "wide.png").write_bytes(make_png(400, 200))
    store = FakeStore()

    async def body() -> None:
        result = await call(
            ws,
            tmp_path,
            {"path": "wide.png", "x": 250, "y": 50, "width": 100, "height": 100},
            store,
        )
        assert result.text == "图片 wide.png，100×100，已附在下面。"
        assert image_size(store.saved[0][0]) == (100, 100)
        assert store.saved[0][1] == "image/png"

    asyncio.run(body())


def test_a_crop_box_must_be_complete_and_inside_the_image(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "wide.png").write_bytes(make_png(400, 200))

    async def body() -> None:
        for args, expected in (
            ({"x": 10}, "同时给出"),
            ({"x": 10, "y": 10, "width": 0, "height": 10}, "必须大于 0"),
            (
                {"x": 350, "y": 0, "width": 100, "height": 100},
                "超出了图片范围",
            ),
            (
                {"x": 0, "y": 0, "width": 400, "height": 200.5},
                "必须是整数",
            ),
        ):
            with pytest.raises(ToolInputError) as caught:
                await call(ws, tmp_path, {"path": "wide.png", **args})
            assert expected in str(caught.value)

    asyncio.run(body())


@pytest.mark.skipif(not SIPS, reason="needs macOS sips for the resize")
def test_a_large_png_is_shrunk_to_the_long_edge(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "big.png").write_bytes(make_png(2000, 1000))
    store = FakeStore()

    async def body() -> None:
        result = await call(ws, tmp_path, {"path": "big.png"}, store)
        assert "1280×640" in result.text
        rendered, mime, _ = store.saved[0]
        assert image_size(rendered) == (1280, 640)
        assert len(rendered) <= attachment_rules.MAX_IMAGE_BYTES
        assert mime in ("image/png", "image/jpeg")

    asyncio.run(body())


def test_a_file_that_is_not_an_image_is_refused(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws / "notes.txt").write_text("hello", encoding="utf-8")

    async def body() -> None:
        with pytest.raises(ToolInputError) as caught:
            await call(ws, tmp_path, {"path": "notes.txt"})
        assert "不是支持的图片" in str(caught.value)
        with pytest.raises(ToolInputError) as missing:
            await call(ws, tmp_path, {"path": "nothing.png"})
        assert "不存在" in str(missing.value)

    asyncio.run(body())


def test_a_source_over_twenty_megabytes_is_refused_before_reading(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    huge = ws / "huge.png"
    with huge.open("wb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")
        stream.truncate(MAX_SOURCE_BYTES + 1)

    async def body() -> None:
        with pytest.raises(ToolInputError) as caught:
            await call(ws, tmp_path, {"path": "huge.png"})
        assert "20 MB" in str(caught.value)

    asyncio.run(body())


def test_view_image_is_gated_exactly_like_file_read(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(make_png(10, 10))

    async def body() -> None:
        with pytest.raises(ToolInputError) as caught:
            await call(ws, tmp_path, {"path": str(outside)})
        assert "允许的文件夹之外" in str(caught.value)

    asyncio.run(body())

    settings = settings_for(ws, tmp_path)
    image_gate = spec_for(settings).gate(settings, None, {"path": str(outside)})
    read_gate = next(
        s for s in tool_specs(settings, "supervisor") if s.name == "file_read"
    ).gate(settings, None, {"path": str(outside)})
    assert image_gate is not None and read_gate is not None
    assert image_gate.prompt == read_gate.prompt
    assert image_gate.grant == read_gate.grant


def test_view_image_is_offered_only_when_the_model_sees_images(make_backend, upstream) -> None:
    upstream.script = [{"content": "好的。"}, {"content": "还是好的。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, direct, "先看看工具")
            await enable_vision(client)
            await run_goal(client, direct, "现在呢")

    asyncio.run(body())
    blind, seeing = upstream.requests
    assert "view_image" not in tool_names(blind)
    assert "view_image" in tool_names(seeing)


def test_the_call_attaches_the_picture_and_the_next_request_carries_it(
    make_backend, upstream, tmp_path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "diagram.png").write_bytes(make_png(400, 200))
    backend = make_backend(workspace_dir=ws)
    upstream.script = [
        tool_calls(fake_call("call_1", "view_image", {"path": "diagram.png"})),
        {"content": "看到了，是一张图。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            direct = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct, "看看这张图")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, direct)
            action = action_messages(detail)[0]
            assert action["action"]["summary"] == "查看图片 diagram.png"
            assert [item["mime"] for item in action["attachments"]] == ["image/png"]

    asyncio.run(body())

    first, second = upstream.requests
    assert "view_image" in tool_names(first)
    assert image_blocks(second), "the model request after the call must carry the image"
    assert image_blocks(second)[0].startswith("data:image/png;base64,")
    text = "\n".join(
        str(message.get("content"))
        for message in second["messages"]
        if message.get("role") == "tool"
    )
    assert "已附在下面" in text


def test_the_pack_hands_the_toolkit_images_to_the_engine(tmp_path: Path) -> None:
    """The adapter is the only bridge: a toolkit ToolResult's ids become the outcome's."""
    from types import SimpleNamespace

    from app.tools import pack

    ws = workspace(tmp_path)
    (ws / "shot.png").write_bytes(make_png(40, 20))
    settings = settings_for(ws, tmp_path)
    store = FakeStore()
    runner = pack._runner(spec_for(settings))
    ctx = SimpleNamespace(
        http=httpx.AsyncClient(trust_env=False),
        store=store,
        settings=settings,
        run=SimpleNamespace(run_id="r1"),
        conversation={"id": "c1"},
        author={"id": "a1", "name": "主管", "kind": "supervisor"},
        publish=lambda event, payload: None,
        source_message_id=lambda: "m1",
        action_message_id="act_1",
    )

    async def body():
        try:
            return await runner(ctx, {"path": "shot.png"})
        finally:
            await ctx.http.aclose()

    outcome = asyncio.run(body())
    assert outcome.images == ["att_000000000001"]
    assert "已附在下面" in outcome.text
    # Regression guard: the new field stays last with a default, so existing calls are unchanged.
    assert ToolResult("看图").images == []
