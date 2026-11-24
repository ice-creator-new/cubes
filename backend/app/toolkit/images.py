"""Pack D (part 2) — images: let the model look at a local picture, not a screenshot of one.

Bots on this Mac used to open an image in Preview, screenshot the desktop and look at that,
because no tool could hand them the file itself. This pack is the direct path: the bytes are
read through the same gate as ``file_read``, cropped and shrunk to what a model can take, and
saved as an attachment so the next model request carries the pixels.

The backend deliberately has no PIL: on macOS the system ``sips`` does the crop and resize,
and everywhere else an image that already fits the limits is passed through untouched.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

from .. import attachments as attachment_rules
from .base import ToolContext, ToolInputError, ToolResult, ToolSpec, text_detail
from .files import _read_gate, _short_path, resolve_path

# A single picture a person could plausibly want looked at. Anything larger is a mistake (a
# raw photo, a disk image, a video still dump) and is refused before it is read into memory.
MAX_SOURCE_BYTES = 20 * 1024 * 1024
# Models see detail well enough at this size and the request stays cheap; Anthropic's own
# computer-use guide uses the same long edge.
MAX_EDGE = 1280
# The macOS helper writes a 1280-wide JPEG far below 5 MB; these are the fallbacks when a
# pathological image still does not fit.
JPEG_QUALITIES = (88, 75, 60)
SIPS_TIMEOUT = 30.0
SUPPORTED = "只支持 PNG、JPEG、WebP、GIF 图片。"

VIEW_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {
            "type": "string",
            "description": "图片路径：相对路径落在工作文件夹里，也可以写完整路径或 ~/…。",
        },
        "x": {"type": "integer", "minimum": 0, "description": "裁剪区域的左边界（像素），可省略。"},
        "y": {"type": "integer", "minimum": 0, "description": "裁剪区域的上边界（像素），可省略。"},
        "width": {"type": "integer", "minimum": 1, "description": "裁剪区域的宽度（像素），可省略。"},
        "height": {"type": "integer", "minimum": 1, "description": "裁剪区域的高度（像素），可省略。"},
    },
    "required": ["path"],
}

_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def image_size(data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from the file's own header, or ``None`` when it cannot be read.

    No decoding and no dependency: each format keeps its dimensions in a fixed corner of the
    header, so a header parser costs a few dozen bytes of work and never allocates the image.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24:
            return None
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) < 10:
            return None
        width, height = struct.unpack("<HH", data[6:10])
        return (width, height) if width and height else None
    if data.startswith(b"\xff\xd8"):
        return _jpeg_size(data)
    if len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG markers to the first start-of-frame, which carries the dimensions."""
    index = 2
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:  # fill byte between markers
            index += 1
            continue
        # Stand-alone markers carry no length section.
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > len(data):
            return None
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if length < 2:
            return None
        if marker in _SOF_MARKERS:
            if index + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height) if width and height else None
        index += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if kind == b"VP8 " and len(data) >= 30:
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if kind == b"VP8L" and len(data) >= 25:
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _crop_box(args: dict, width: int, height: int) -> tuple[int, int, int, int] | None:
    """The validated ``(x, y, width, height)`` crop, or ``None`` when no box was given."""
    keys = ("x", "y", "width", "height")
    values = {key: args.get(key) for key in keys}
    given = [key for key in keys if values[key] is not None]
    if not given:
        return None
    if len(given) != len(keys):
        raise ToolInputError("裁剪区域要同时给出 x、y、width、height 四个整数。")
    numbers: dict[str, int] = {}
    for key in keys:
        value = values[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not float(value).is_integer()
        ):
            raise ToolInputError("裁剪区域的 x、y、width、height 必须是整数。")
        numbers[key] = int(value)
    box = (numbers["x"], numbers["y"], numbers["width"], numbers["height"])
    if box[2] <= 0 or box[3] <= 0:
        raise ToolInputError("裁剪区域的宽度和高度必须大于 0。")
    if box[0] < 0 or box[1] < 0 or box[0] + box[2] > width or box[1] + box[3] > height:
        raise ToolInputError(f"裁剪区域超出了图片范围（图片是 {width}×{height}）。")
    return box


def _suffix(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }[mime]


def _name_for(path: Path, mime: str) -> str:
    """The attachment's name; a converted file gets the extension its bytes actually have."""
    suffix = _suffix(mime)
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        if mime == "image/jpeg" and path.suffix.lower() in (".jpg", ".jpeg"):
            return path.name
        if path.suffix.lower() == suffix:
            return path.name
    return path.stem + suffix


def _sips_available() -> bool:
    return sys.platform == "darwin" and shutil.which("sips") is not None


async def _sips(options: list[str], source: Path, target: Path) -> None:
    """Run one ``sips`` edit, raising the Chinese sentence the model can act on."""
    process = await asyncio.create_subprocess_exec(
        "sips",
        *options,
        str(source),
        "--out",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), SIPS_TIMEOUT)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ToolInputError("处理图片超时了，请换一张小一点的图片。") from exc
    if process.returncode != 0 or not target.exists():
        detail = error.decode("utf-8", errors="replace").strip()
        raise ToolInputError(
            "系统自带的图片工具没能处理这张图片"
            + (f"（{detail[:120]}）。" if detail else "，请换一张试试。")
        )


async def _render(data: bytes, mime: str, crop: tuple[int, int, int, int] | None) -> tuple[bytes, str]:
    """Crop and/or shrink one image to what a model can take, on macOS via ``sips``.

    The original bytes are kept when nothing needs to change. Everything else is written to a
    temporary folder; a conversion that already fits the byte limit is preferred over lossy
    JPEG, and only an image that is still too large is compressed down the quality ladder.
    """
    width, height = image_size(data) or (0, 0)
    needs_resize = max(width, height) > MAX_EDGE
    if crop is None and not needs_resize and len(data) <= attachment_rules.MAX_IMAGE_BYTES:
        return data, mime
    if not _sips_available():
        if crop is not None:
            raise ToolInputError("这台系统上暂时不能裁剪图片，请直接查看整张图片。")
        if needs_resize:
            raise ToolInputError(
                f"图片长边超过 {MAX_EDGE} 像素，这台系统上暂时无法缩小，请先缩小后再看。"
            )
        raise ToolInputError("图片超过 5 MB，这台系统上暂时无法压缩，请先压缩后再看。")

    with tempfile.TemporaryDirectory(prefix="cubes-image-") as folder:
        root = Path(folder)
        current = root / f"source{_suffix(mime)}"
        current.write_bytes(data)
        current_mime = mime

        if crop is not None:
            x, y, crop_width, crop_height = crop
            target = root / f"crop{_suffix(mime if mime != 'image/webp' else 'image/jpeg')}"
            options = [
                "-c",
                str(crop_height),
                str(crop_width),
                "--cropOffset",
                str(y),
                str(x),
            ]
            if mime == "image/webp":
                # sips reads WebP but cannot write it: the first edit makes it a JPEG.
                options += ["-s", "format", "jpeg"]
            await _sips(options, current, target)
            current = target
            current_mime = "image/jpeg" if mime == "image/webp" else mime

        width, height = image_size(current.read_bytes()) or (width, height)
        if max(width, height) > MAX_EDGE:
            target = root / f"resized{_suffix(current_mime if current_mime != 'image/webp' else 'image/jpeg')}"
            options = ["-Z", str(MAX_EDGE)]
            if current_mime == "image/webp":
                options += ["-s", "format", "jpeg"]
            await _sips(options, current, target)
            current = target
            current_mime = "image/jpeg" if current_mime == "image/webp" else current_mime

        rendered = current.read_bytes()
        if len(rendered) <= attachment_rules.MAX_IMAGE_BYTES:
            return rendered, current_mime

        # Still over the attachment limit: re-encode as JPEG, coarser each time, then smaller.
        for edge, quality in [(MAX_EDGE, q) for q in JPEG_QUALITIES] + [(960, 80), (720, 75)]:
            target = root / f"compact-{edge}-{quality}.jpg"
            await _sips(
                [
                    "-Z",
                    str(edge),
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    str(quality),
                ],
                current,
                target,
            )
            compacted = target.read_bytes()
            if len(compacted) <= attachment_rules.MAX_IMAGE_BYTES:
                return compacted, "image/jpeg"
    raise ToolInputError("图片压到 5 MB 以内没有成功，请先裁剪或换一张图片。")


def _shown(ctx: ToolContext, path: Path) -> str:
    return _short_path(ctx, path, limit=40)


def _summary(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


async def run_view_image(ctx: ToolContext, args: dict) -> ToolResult:
    path = resolve_path(ctx, args.get("path"))
    if not path.exists():
        raise ToolInputError(f"文件「{_short_path(ctx, path)}」不存在。")
    if path.is_dir():
        raise ToolInputError(f"「{_short_path(ctx, path)}」是目录，不是图片文件。")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ToolInputError(f"无法读取「{_short_path(ctx, path)}」。") from exc
    if size > MAX_SOURCE_BYTES:
        raise ToolInputError("图片超过 20 MB，请先缩小或裁剪后再看。")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolInputError(f"无法读取「{_short_path(ctx, path)}」。") from exc
    if len(data) > MAX_SOURCE_BYTES:
        raise ToolInputError("图片超过 20 MB，请先缩小或裁剪后再看。")

    mime = attachment_rules.sniff_mime(data)
    if mime is None:
        raise ToolInputError(f"「{path.name}」不是支持的图片（{SUPPORTED}）。")
    dimensions = image_size(data)
    if dimensions is None:
        raise ToolInputError(f"「{path.name}」的图片尺寸读不出来，文件可能已损坏。")
    width, height = dimensions
    crop = _crop_box(args, width, height)

    rendered, rendered_mime = await _render(data, mime, crop)
    final = image_size(rendered)
    if final is None:
        raise ToolInputError("处理后的图片读不出尺寸，请换一张图片。")
    shown_width, shown_height = final
    name = _name_for(path, rendered_mime)

    row = ctx.store.save_attachment(rendered, rendered_mime, name=name)
    attachment_id = str(row["id"])
    # The user sees the picture on the tool's own row; fall back to the reply's message when
    # a caller (a test, a future tool) has no action row.
    message_id = ctx.action_message_id or ctx.message_id
    if message_id:
        ctx.store.attach_to_message(message_id, [attachment_id])

    text = f"图片 {name}，{shown_width}×{shown_height}，已附在下面。"
    return ToolResult(
        text,
        text_detail(text),
        summary=f"查看图片 {_shown(ctx, path)}",
        images=[attachment_id],
    )


def _sees_images(offer: Any, store: Any) -> bool:
    """Offer the tool only to a bot whose current model was marked as seeing images."""
    author = getattr(offer, "author", None)
    if not author:
        return False
    # Imported here: the tools layer imports this package, so a top-level import would cycle.
    from ..tools import resolve

    return resolve.sees_images(store, author)


IMAGE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="view_image",
        label="查看图片",
        description=(
            "查看本地的一张图片，模型会真正看到图片内容。图片可以来自电脑上的文件；"
            "给 x、y、width、height 可以只看图片里的一块区域。"
            "要看网页请用 computer_browser，不要在终端里写 Chrome 调试脚本；"
            "图片文件也直接用它看，不要用 Preview 打开再截桌面。"
        ),
        parameters=VIEW_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_view_image,
        summary=lambda args: f"查看图片 {_summary(args.get('path'), 40)}",
        gate=_read_gate,
        usable=_sees_images,
    ),
)
