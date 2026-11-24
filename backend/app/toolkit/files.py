"""Pack D — files: the working folder and the allowed folders, capped reads, atomic writes.

Where a call may go is decided by :mod:`app.permissions`: reading inside the allowed folders
never asks, writing and anything outside them go through the approval mode first.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import permissions
from ..agent import working_set
from .base import (
    MAX_RESULT_CHARS,
    Gate,
    ToolContext,
    ToolInputError,
    ToolResult,
    ToolSpec,
    cap_text,
    text_detail,
)

MAX_READ_BYTES = 100 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 200
DEFAULT_READ_CHARS = 8_000
MAX_READ_CHARS = 20_000
TRUNCATED_MARK = "…（已截断）"

# The diff under an action row is a glance, not a patch: few lines, short lines, exact counts.
DIFF_CONTEXT_LINES = 3
MAX_DIFF_LINES = 200
MAX_DIFF_LINE_CHARS = 200

# How recently another run's write still counts as "just changed". A run that never read a
# file may create it, but not take over one a live sibling wrote within this window.
GUARD_SECONDS = 3_600.0


def _fingerprint(path: Path) -> str | None:
    """What a run last saw of a file: its size and bytes' hash, so any rewrite differs."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
        size = path.stat().st_size
    except OSError:
        return None
    return f"{size}:{hashlib.sha256(data).hexdigest()}"


def _seen(ctx: ToolContext, path: Path) -> tuple[str, float] | None:
    """The fingerprint this run last saw for ``path``, if it ever read or wrote it."""
    run = ctx.run
    ledger = getattr(run, "file_ledger", None)
    if not isinstance(ledger, dict):
        return None
    entry = ledger.get(str(path))
    return entry if isinstance(entry, tuple) and len(entry) == 2 else None


def _remember(ctx: ToolContext, path: Path) -> None:
    """This run now knows what the file holds; its next write is not a blind overwrite."""
    run = ctx.run
    ledger = getattr(run, "file_ledger", None)
    if not isinstance(ledger, dict):
        return
    fingerprint = _fingerprint(path)
    if fingerprint is not None:
        ledger[str(path)] = (fingerprint, time.time())


def _stale_refusal(ctx: ToolContext, path: Path) -> str | None:
    """Refuse a write that would silently undo a sibling run's change to the same file.

    The guard is optimistic: this run's own ledger says what it last saw, and the group's
    working set says who wrote the file and when. Both must point at a change by someone
    else for the write to be refused; a file this run has seen unchanged always passes.
    """
    current = _fingerprint(path)
    if current is None or ctx.store is None:
        return None
    entry = working_set.latest_write(ctx.store, ctx.conversation_id, str(path))
    seen = _seen(ctx, path)
    if seen is None:
        # Never read: a fresh sibling write still owns the file, and re-reading is cheap.
        if entry is None:
            return None
        wrote_at = working_set.moment(str(entry.get("updated_at") or ""))
        if wrote_at is None or time.time() - wrote_at > GUARD_SECONDS:
            return None
        author = str(entry.get("author_name") or "别的成员")
        return f"这个文件刚被「{author}」改过，请先 file_read 读最新内容再改。"
    fingerprint, seen_at = seen
    if fingerprint == current or entry is None:
        return None
    wrote_at = working_set.moment(str(entry.get("updated_at") or ""))
    if wrote_at is None or wrote_at <= seen_at:
        return None
    author = str(entry.get("author_name") or "别的成员")
    when = working_set.age(str(entry["updated_at"]), datetime.now(timezone.utc))
    return f"「{author}」{when}改过这个文件，请先 file_read 读最新内容再改。"


LIST_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "description": "相对路径落在工作文件夹里；也可以写完整路径或 ~/…，省略表示工作文件夹。"},
    },
}

READ_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "description": "要读取的文件：相对路径落在工作文件夹里，也可以写完整路径或 ~/…。"},
        "max_chars": {
            "type": "integer",
            "minimum": 200,
            "maximum": MAX_READ_CHARS,
            "description": f"返回字符上限，默认 {DEFAULT_READ_CHARS}。",
        },
    },
    "required": ["path"],
}

WRITE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "description": "要写入的文件：相对路径落在工作文件夹里，也可以写完整路径或 ~/…（例如 ~/Downloads/方案.md）；父目录会自动创建。"},
        "content": {"type": "string", "description": "要写入的文本。"},
        "mode": {
            "type": "string",
            "enum": ["overwrite", "append"],
            "description": "overwrite 覆盖（默认），append 追加。",
        },
    },
    "required": ["path", "content"],
}


def workspace_of(ctx: ToolContext) -> Path:
    """The working folder relative paths land in (it always exists)."""
    return permissions.working_folder(ctx.settings)


def resolve_path(ctx: ToolContext, raw: Any) -> Path:
    """The path a call means, refused when it is Cubes' own data or an unapproved place.

    Inside the working folder and the folders the user allowed, anything goes. Elsewhere the
    call only gets here after the user (or the bot's approval mode) said yes, which the pack
    runner records as ``ctx.allow_outside``. Symlinks are resolved first, so a link inside an
    allowed folder that points out of it counts as outside.
    """
    try:
        resolved = permissions.resolve(ctx.settings, raw)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from exc
    if permissions.protected(ctx.settings, resolved):
        raise ToolInputError(f"「{permissions.display(resolved)}」是 Cubes 自己的数据（里面有模型密钥），不能读写。")
    allowed = permissions.allowed_folders(ctx.store, ctx.settings)
    if not permissions.inside(resolved, allowed) and not ctx.allow_outside:
        raise ToolInputError(f"路径「{permissions.display(resolved)}」在允许的文件夹之外，没有得到你的同意，已拒绝。")
    return resolved


def _relative(ctx: ToolContext, path: Path) -> str:
    workspace = workspace_of(ctx)
    if path == workspace:
        return "."
    if path.is_relative_to(workspace):
        return str(path.relative_to(workspace))
    return permissions.display(path)


def _short_path(ctx: ToolContext, path: Path, limit: int = 44) -> str:
    """A path as an action row shows it: relative inside the working folder, ``~/…`` elsewhere.

    When even that is too long, the end survives — the file name is what the user recognises,
    the absolute prefix is not: ``…/fixtures/band_5.json`` rather than ``/Users/…/fixtures/b``.
    """
    shown = _relative(ctx, path)
    if len(shown) <= limit:
        return shown
    tail = shown[-(limit - 1) :]
    # Start at a path boundary: a half segment after the ellipsis reads as a typo.
    slash = tail.find("/")
    if slash > 0:
        tail = tail[slash:]
    return "…" + tail


def _place(settings: Any, store: Any, raw: Any) -> tuple[Path, bool] | None:
    """Where a call points and whether that is inside the allowed folders; ``None`` if unreadable."""
    try:
        path = permissions.resolve(settings, raw)
    except ValueError:
        return None
    if permissions.protected(settings, path):
        return None
    return path, permissions.inside(path, permissions.allowed_folders(store, settings))


def _read_gate(settings: Any, store: Any, args: dict) -> Gate | None:
    place = _place(settings, store, args.get("path"))
    if place is None or place[1]:
        return None
    shown = permissions.display(place[0])
    return Gate(
        grant=permissions.READ_OUTSIDE,
        rule="files:read_outside",
        prompt=f"想读取允许的文件夹之外的「{shown}」。",
        summary=f"想读取「{shown}」",
        resume=f"正在读取「{shown}」",
    )


def _write_gate(settings: Any, store: Any, args: dict) -> Gate | None:
    place = _place(settings, store, args.get("path"))
    if place is None:
        return None
    path, inside = place
    shown = permissions.display(path)
    content = args.get("content")
    size = f"{len(content)} 个字符" if isinstance(content, str) else "内容"
    verb = "追加到" if args.get("mode") == "append" else ("覆盖" if path.exists() else "新建")
    if inside:
        return Gate(
            grant=permissions.WRITE_INSIDE,
            rule="files:write_inside",
            prompt=f"想{verb}文件「{shown}」（{size}）。",
            summary=f"想写入「{shown}」",
            resume=f"正在写入「{shown}」",
        )
    return Gate(
        grant=permissions.WRITE_OUTSIDE,
        rule="files:write_outside",
        prompt=f"想在允许的文件夹之外{verb}文件「{shown}」（{size}）。",
        summary=f"想写入「{shown}」",
        resume=f"正在写入「{shown}」",
    )


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"
    return f"{size:.0f} B"  # pragma: no cover - unreachable


async def run_file_list(ctx: ToolContext, args: dict) -> str:
    directory = resolve_path(ctx, args.get("path"))
    if not directory.exists():
        raise ToolInputError(f"路径「{_relative(ctx, directory)}」不存在。")
    if not directory.is_dir():
        raise ToolInputError(f"「{_relative(ctx, directory)}」不是目录。")
    try:
        entries = sorted(
            directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        )
    except OSError as exc:
        raise ToolInputError(f"无法读取目录「{_relative(ctx, directory)}」。") from exc
    lines = [f"目录：{_relative(ctx, directory)}"]
    for entry in entries[:MAX_LIST_ENTRIES]:
        if entry.is_dir():
            lines.append(f"- {entry.name}/ 目录")
        else:
            try:
                size = entry.stat().st_size
            except OSError:  # pragma: no cover - vanished between list and stat
                size = -1
            lines.append(f"- {entry.name} {_human_size(size) if size >= 0 else '大小未知'}")
    hidden = len(entries) - MAX_LIST_ENTRIES
    if hidden > 0:
        lines.append(f"还有 {hidden} 项未显示。")
    if len(lines) == 1:
        lines.append("（空目录）")
    text = cap_text("\n".join(lines), MAX_RESULT_CHARS)
    return ToolResult(
        text,
        text_detail(text),
        summary=f"查看文件 {_short_path(ctx, directory)}",
    )


async def run_file_read(ctx: ToolContext, args: dict) -> str:
    path = resolve_path(ctx, args.get("path"))
    if not path.exists():
        raise ToolInputError(f"文件「{_relative(ctx, path)}」不存在。")
    if path.is_dir():
        raise ToolInputError(f"「{_relative(ctx, path)}」是目录，不是文件。")
    raw = args.get("max_chars")
    if raw is None:
        max_chars = DEFAULT_READ_CHARS
    elif isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolInputError("正文字符上限必须是整数。")
    else:
        max_chars = int(raw)
        if max_chars < 200 or max_chars > MAX_READ_CHARS:
            raise ToolInputError(f"正文字符上限必须在 200 到 {MAX_READ_CHARS} 之间。")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        raise ToolInputError(f"无法读取「{_relative(ctx, path)}」。") from exc
    clipped = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATED_MARK
    elif clipped:
        text = text + "\n…（文件较大，只读取了前 100 KB）"
    # This run now knows the file's state: a later write it makes is not a blind overwrite.
    _remember(ctx, path)
    body = cap_text(text, MAX_RESULT_CHARS)
    return ToolResult(
        body, text_detail(body), summary=f"读取文件 {_short_path(ctx, path)}"
    )


def _diff_counts(old: list[str], new: list[str]) -> tuple[int, int]:
    """``(added, removed)`` over the whole line diff, independent of what is shown."""
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _unified_rows(old: list[str], new: list[str]) -> list[tuple[str, str]]:
    """Unified-diff rows with three context lines around every change (no ``@@`` headers)."""
    rows: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for group in matcher.get_grouped_opcodes(DIFF_CONTEXT_LINES):
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows.extend((" ", line) for line in old[i1:i2])
            elif tag == "replace":
                rows.extend(("-", line) for line in old[i1:i2])
                rows.extend(("+", line) for line in new[j1:j2])
            elif tag == "delete":
                rows.extend(("-", line) for line in old[i1:i2])
            else:
                rows.extend(("+", line) for line in new[j1:j2])
    return rows


def _capped_rows(raw: list[tuple[str, str]]) -> tuple[list[dict], bool]:
    """The rows that fit an action row, and whether anything had to be dropped."""
    truncated = len(raw) > MAX_DIFF_LINES
    lines: list[dict] = []
    for op, text in raw[:MAX_DIFF_LINES]:
        if len(text) > MAX_DIFF_LINE_CHARS:
            text = text[: MAX_DIFF_LINE_CHARS - 1] + "…"
            truncated = True
        lines.append({"op": op, "text": text})
    return lines, truncated


def _write_detail(
    path: Path,
    mode: str,
    content: str,
    old_text: str | None,
    note: str | None,
    shown: str,
) -> tuple[dict, int, int]:
    """The ``diff`` detail under a write, plus the exact ``(added, removed)`` line counts."""
    if mode == "overwrite" and old_text is not None:
        old_lines = old_text.splitlines()
        new_lines = content.splitlines()
        added, removed = _diff_counts(old_lines, new_lines)
        rows = _unified_rows(old_lines, new_lines)
    else:
        # A new file, an append, or an old file whose bytes are not text: every written line is
        # an addition; only an overwrite could have told us how many lines it replaced.
        rows = [("+", line) for line in content.splitlines()]
        added, removed = len(rows), 0
    lines, truncated = _capped_rows(rows)
    detail: dict = {
        "kind": "diff",
        "path": str(path),
        "display_path": shown,
        "mode": mode,
        "added": added,
        "removed": removed,
        "lines": lines,
        "truncated": truncated,
    }
    if note is not None and mode == "overwrite":
        detail["note"] = note
    return detail, added, removed


async def run_file_write(ctx: ToolContext, args: dict) -> str:
    path = resolve_path(ctx, args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolInputError("内容必须是文本。")
    mode = args.get("mode", "overwrite")
    if mode not in ("overwrite", "append"):
        raise ToolInputError("写入方式只能是 overwrite 或 append。")
    if path.exists() and path.is_dir():
        raise ToolInputError(f"「{_relative(ctx, path)}」是目录，不能写入。")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolInputError("内容过大：超过 1 MB，已拒绝写入。")

    existed = path.exists()
    old_text: str | None = None
    note: str | None = None
    old_bytes = b""
    if existed:
        try:
            with path.open("rb") as handle:
                old_bytes = handle.read(MAX_WRITE_BYTES + 1)
        except OSError as exc:
            raise ToolInputError(f"无法读取「{_relative(ctx, path)}」。") from exc
        if len(old_bytes) > MAX_WRITE_BYTES:
            old_bytes = old_bytes[:MAX_WRITE_BYTES]
            note = "原文件超过 1 MB，差异只比较了开头部分。"
        refusal = _stale_refusal(ctx, path)
        if refusal is not None:
            raise ToolInputError(refusal)
        try:
            old_text = old_bytes.decode("utf-8")
        except UnicodeDecodeError:
            old_text = None
            note = "原文件不是 UTF-8 文本，无法统计删除的行。"

    text = content
    if mode == "append" and existed:
        if len(old_bytes) + len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolInputError("追加后超过 1 MB，已拒绝写入。")
        text = old_bytes.decode("utf-8", errors="replace") + content
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".cubes-", dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ToolInputError(f"无法写入「{_relative(ctx, path)}」。") from exc
    _remember(ctx, path)

    # An overwrite of a file that did not exist is a creation for the reader's purposes.
    shown_mode = "append" if mode == "append" else ("overwrite" if existed else "create")
    shown = _short_path(ctx, path)
    detail, added, removed = _write_detail(path, shown_mode, content, old_text, note, shown)
    action = "追加" if mode == "append" else "写入"
    body = (
        f"已{action}「{_relative(ctx, path)}」"
        f"（{len(text)} 个字符，+{added} -{removed}）。"
    )
    return ToolResult(body, detail, summary=f"{action}文件 {shown}")


def _summary(value: Any, limit: int) -> str:
    """A one-line argument for an action row; a long one keeps its end, i.e. the file name.

    Only used when a call never got as far as resolving its path (the runtime summary in
    :class:`ToolResult` covers the real cases), so the raw argument is all there is.
    """
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


FILE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="file_list",
        label="查看文件",
        description="列出一个文件夹里的文件和子文件夹，含类型和大小。",
        parameters=LIST_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_file_list,
        summary=lambda args: f"查看文件 {_summary(args.get('path') or '工作文件夹', 40)}",
        gate=_read_gate,
    ),
    ToolSpec(
        name="file_read",
        label="读取文件",
        description="读取一个文本文件。单个文件最多读取前 100 KB。",
        parameters=READ_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_file_read,
        summary=lambda args: f"读取文件 {_summary(args.get('path'), 40)}",
        gate=_read_gate,
    ),
    ToolSpec(
        name="file_write",
        label="写入文件",
        description=(
            "写一个文本文件，父目录会自动创建，写入是原子替换。"
            "用户让你把文档、代码或数据存到某处时用它；正文上限 1 MB。"
        ),
        parameters=WRITE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_file_write,
        summary=lambda args: f"写入文件 {_summary(args.get('path'), 40)}",
        gate=_write_gate,
    ),
)
