"""The conversation's working set: which files a team touched and what state they are in.

A group's bots kept re-reading the same files because each one only saw its own newest few
tool results: a teammate's write was invisible, and an own write from ten turns ago was one
150-character line. This module keeps one small row per file per conversation and renders it
as a hint — the current state of the artifacts, with the newest few small files' content
read straight from disk at build time, so what a bot is told can never be stale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import permissions

logger = logging.getLogger(__name__)

# The hint's size: enough files to cover one work session, content only for the newest few.
HINT_FILES = 20
INLINE_FILES = 3
INLINE_FILE_CHARS = 2_000
INLINE_BUDGET_CHARS = 4_000
HINT_HEADER = "工作集（这个会话里最近动过的文件，最新在前；下面给了内容的文件不用再读，直接改）"
# A path shown to the model is relative to the working folder when it can be, else ``~/…``.
CONTENT_OPEN = "<<<文件内容 {path}"
CONTENT_CLOSE = "文件内容>>>"
# Only source-like text is ever inlined: a screenshot or a bundle is listed, never pasted.
TEXT_SUFFIXES = frozenset(
    {
        ".dart",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".md",
        ".txt",
        ".rst",
        ".css",
        ".scss",
        ".html",
        ".sh",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".sql",
        ".csv",
        ".xml",
        ".ini",
        ".cfg",
        ".env",
        ".gradle",
    }
)
# How many of the newest entries are examined for inlining, so a rebuilt bundle or a
# screenshot does not eat the whole budget while a source file just behind it waits.
INLINE_CANDIDATES = 8

# Shell commands that only read the paths they name. ``sed`` joins ``-i`` as a writer.
READ_COMMANDS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "rg",
        "sed",
        "wc",
        "ls",
        "find",
        "tree",
        "file",
        "stat",
        "diff",
        "cut",
        "sort",
        "uniq",
        "awk",
        "nl",
        "bat",
        "od",
    }
)
# Commands that write a path they name.
WRITE_COMMANDS = frozenset({"tee", "touch", "truncate"})
# A shell command carries at most this many distinct files into the working set.
TOUCHES_PER_COMMAND = 8
# A write guard looks at the newest rows only: the store keeps one row per path up to 200.
LOOKUP_ROWS = 200
# A path token: something with a slash, or a dotted file name. No shell metacharacters.
_PATH_TOKEN = re.compile(r"^[\w./~@+-]*\w(?:\.\w{1,8})?$")
_CD = re.compile(r"^cd\s+(.+)$")
_REDIRECT = re.compile(r"(?:^|\s)\d?>>?\s*([^\s;&|<>]+)")
# A heredoc body that writes / reads files names them as quoted strings.
_QUOTED = re.compile(r"['\"]([^'\"\n]{1,300})['\"]")
_WRITE_CALL = re.compile(r"write_text\(|open\([^)]*['\"][wa]\+?['\"]|\.write\(")
_READ_CALL = re.compile(r"read_text\(|read_bytes\(|open\([^)]*['\"]r")


@dataclass(frozen=True)
class Touch:
    """One file a tool call touched: where, how, and what the call itself knows about it."""

    path: str
    action: str
    lines: int | None = None
    size: int | None = None
    sha: str | None = None
    added: int | None = None
    removed: int | None = None


# ------------------------------------------------------------------ detection


def _arguments(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _count_lines(text: str) -> int:
    return len(text.splitlines())


def _known_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _resolve(cwd: Path, raw: str) -> Path | None:
    """The file a path token names, or ``None`` when it is not an existing file."""
    text = raw.strip().strip("'\"")
    if not text or text.startswith("-") or "://" in text or text.startswith("$"):
        return None
    if not _PATH_TOKEN.match(text):
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    return candidate if _known_file(candidate) else None


def _shell_touches(command: str, base: Path) -> list[Touch]:
    """The existing files a shell command reads or writes, resolved against its own ``cd``."""
    cwd = base
    found: dict[str, Touch] = {}

    def remember(raw: str, action: str) -> None:
        path = _resolve(cwd, raw)
        if path is not None and str(path) not in found:
            found[str(path)] = Touch(str(path), action)

    for raw_line in re.split(r"&&|\|\||;|\n", command):
        line = raw_line.strip()
        if not line:
            continue
        cd = _CD.match(line)
        if cd is not None:
            target = cd.group(1).strip().strip("'\"")
            if target and not target.startswith("-"):
                moved = Path(target).expanduser()
                cwd = moved if moved.is_absolute() else cwd / moved
            continue
        words = _words(line)
        name = words[0] if words else ""
        writes = (
            name in WRITE_COMMANDS
            or (name == "sed" and any(word.startswith(("-i", "--in-place")) for word in words))
            or (name == "perl" and any(word.startswith(("-p", "-i")) for word in words))
        )
        reads = not writes and name in READ_COMMANDS
        if writes or reads:
            action = "write" if writes else "read"
            for word in words[1:]:
                remember(word, action)
        for match in _REDIRECT.finditer(line):
            remember(match.group(1), "write")
        if len(found) >= TOUCHES_PER_COMMAND:
            break
    if "<<" in command:
        # ``python3 - <<'EOF' … p.write_text(…)``: a script that names its files as strings.
        body = command.split("<<", 1)[1]
        action = "write" if _WRITE_CALL.search(body) else ("read" if _READ_CALL.search(body) else "")
        if action:
            for match in _QUOTED.finditer(body):
                remember(match.group(1), action)
    return list(found.values())[:TOUCHES_PER_COMMAND]


def _words(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=False, posix=True)
    except ValueError:
        return line.split()


def touches(
    settings: Any,
    *,
    tool_name: str,
    arguments: str,
    detail: dict | None = None,
) -> list[Touch]:
    """What one finished tool call touched in the working set.

    A ``file_write`` reports its own path, line count and diff counts — the call already
    knows them, so nothing is read back. A ``file_read`` records a read. A shell command is
    parsed for the files its read / write commands name, which is how most real sessions
    edit code (``sed -i``, ``python3 - <<EOF … write_text``); anything unusual is skipped
    rather than guessed.
    """
    args = _arguments(arguments)
    detail = detail if isinstance(detail, dict) else {}
    if tool_name == "file_write":
        path = str(detail.get("path") or args.get("path") or "")
        if not path:
            return []
        content = args.get("content")
        resolved = _resolve(permissions.working_folder(settings), path)
        size = resolved.stat().st_size if resolved is not None else None
        text = content if isinstance(content, str) else None
        # Only an overwrite of a known text body describes the file's current lines and hash;
        # an append or a non-text write leaves them unknown, and the hint re-reads the file.
        mode = str(detail.get("mode") or args.get("mode") or "")
        whole = text if mode in ("create", "overwrite") else None
        return [
            Touch(
                path=str(resolved or Path(path)),
                action="write",
                lines=_count_lines(whole) if whole is not None else None,
                size=size,
                sha=hashlib.sha256(whole.encode("utf-8")).hexdigest() if whole is not None else None,
                added=_int(detail.get("added")),
                removed=_int(detail.get("removed")),
            )
        ]
    if tool_name == "file_read":
        raw = str(args.get("path") or "")
        if not raw:
            return []
        resolved = _resolve(permissions.working_folder(settings), raw)
        if resolved is None:
            return []
        return [Touch(str(resolved), "read", size=resolved.stat().st_size)]
    if tool_name in ("computer_shell", "computer_shell_background"):
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return []
        return _shell_touches(command, permissions.working_folder(settings))
    return []


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def record(
    store: Any,
    settings: Any,
    conversation: dict,
    author: dict,
    *,
    tool_name: str,
    arguments: str,
    detail: dict | None = None,
) -> None:
    """Write one finished call's file touches into the conversation's working set.

    A bookkeeping fault never fails a tool call: the model already has the result, and the
    next hint is only a convenience.
    """
    try:
        found = touches(settings, tool_name=tool_name, arguments=arguments, detail=detail)
    except Exception:  # a malformed command must not break the tool loop
        logger.exception("工作集解析失败：%s", tool_name)
        return
    if not found:
        return
    decided = set(permissions.allowed_folders(store, settings))
    for touch in found:
        # Files outside the allowed folders are still listed (a bot may have read one after
        # the user approved it), but their content is never inlined into a request.
        inside = permissions.inside(Path(touch.path), decided)
        try:
            store.touch_working_file(
                conversation["id"],
                path=touch.path,
                author_id=str(author.get("id") or ""),
                author_name=str(author.get("name") or ""),
                action=touch.action,
                lines=touch.lines,
                size=touch.size,
                sha=touch.sha if inside else None,
                added=touch.added,
                removed=touch.removed,
            )
        except Exception:
            logger.exception("工作集写入失败：%s", touch.path)


# ------------------------------------------------------------------ hint


def moment(updated_at: str) -> float | None:
    """An ISO timestamp as epoch seconds; ``None`` when it cannot be read."""
    try:
        return datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def latest_write(store: Any, conversation_id: str, path: str) -> dict | None:
    """The newest recorded write to one file in this conversation, or ``None``.

    The optimistic write guard asks this who changed a file: the row carries the author and
    the time, which is what a refusal names. One row per path means a later read by someone
    else can hide an earlier write; the guard then has nobody to name and lets the write
    through rather than guess.
    """
    target = str(Path(path))
    try:
        rows = store.list_working_files(conversation_id, limit=LOOKUP_ROWS)
    except Exception:  # a guard must never break a tool call
        return None
    for entry in rows:
        if str(entry.get("path")) == target and entry.get("action") == "write":
            return entry
    return None


def age(updated_at: str, now: datetime) -> str:
    try:
        moment_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        seconds = max(0, int((now - moment_at).total_seconds()))
    except (ValueError, TypeError):
        return "刚刚"
    if seconds < 90:
        return "刚刚"
    if seconds < 3_600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86_400:
        return f"{seconds // 3_600} 小时前"
    return f"{seconds // 86_400} 天前"


def _label(entry: dict) -> str:
    action = "写" if entry.get("action") == "write" else "读"
    parts = [action, str(entry.get("author_name") or "?")]
    return " ".join(parts)


def _name(settings: Any, path: Path) -> str:
    folder = permissions.working_folder(settings)
    try:
        return str(path.relative_to(folder))
    except ValueError:
        return permissions.display(path)


def _entry_line(settings: Any, entry: dict, now: datetime, roots: list[Path]) -> str:
    path = Path(str(entry["path"]))
    write = entry.get("action") == "write"
    line = f"- {_name(settings, path)}  {_label(entry)} {age(str(entry['updated_at']), now)}"
    if write and entry.get("lines"):
        line += f"  {entry['lines']} 行"
    if write and (entry.get("added") is not None or entry.get("removed") is not None):
        line += f" +{entry.get('added') or 0} -{entry.get('removed') or 0}"
    if not _known_file(path):
        line += "（已删除）" if entry.get("action") == "write" else "（已不存在）"
    elif not permissions.inside(path, roots):
        line += "（不在允许的文件夹内，未给出内容）"
    elif entry.get("size") is not None and path.stat().st_size != entry["size"]:
        line += "（之后又被改动过）"
    return line


def _inline(settings: Any, entry: dict, roots: list[Path], budget: int) -> str | None:
    """The file's current text, read now, when it is inside the allowed folders and small."""
    path = Path(str(entry["path"]))
    if not permissions.inside(path, roots) or not _known_file(path):
        return None
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        if path.stat().st_size > INLINE_FILE_CHARS * 4:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip() or "\x00" in text or len(text) > min(INLINE_FILE_CHARS, budget):
        return None
    name = _name(settings, path)
    return f"{CONTENT_OPEN.format(path=name)}\n{text.rstrip()}\n{CONTENT_CLOSE}"


def hint(
    store: Any,
    settings: Any,
    conversation: dict,
    *,
    now: datetime | None = None,
) -> str:
    """The working-set hint for one bot's next turn: state of the artifacts, newest first.

    It rides in the runtime block at the end of the newest user turn, so the system prompt
    stays byte-identical and a provider's prefix cache still hits. Every conversation kind
    has one: a scheduled job editing the same files needs it as much as a group does.
    """
    entries = store.list_working_files(conversation["id"], limit=HINT_FILES)
    if not entries:
        return ""
    moment = now or datetime.now(timezone.utc)
    roots = permissions.allowed_folders(store, settings)
    lines = [HINT_HEADER]
    for entry in entries:
        lines.append(_entry_line(settings, entry, moment, roots))
    blocks: list[str] = []
    remaining = INLINE_BUDGET_CHARS
    for entry in entries[:INLINE_CANDIDATES]:
        if len(blocks) >= INLINE_FILES:
            break
        block = _inline(settings, entry, roots, remaining)
        if block is None:
            continue
        remaining -= len(block)
        blocks.append(block)
        if remaining <= 0:
            break
    if blocks:
        lines.extend(["当前内容：", *blocks])
    return "\n".join(lines)
