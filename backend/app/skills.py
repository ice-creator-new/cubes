"""Agent skills: the SKILL.md format, its validation and folder/zip/GitHub/local imports.

A skill is a folder ``<name>/`` holding ``SKILL.md`` (YAML frontmatter + Markdown body) and
optional ``scripts/``, ``references/``, ``assets/`` files. This module owns the pure parsing
and the import rules; the store (:mod:`app.store.skills`) owns the files on disk and the row.
No YAML dependency: the frontmatter reader understands the small subset real SKILL.md files
use — ``key: value`` scalars, quoted scalars, ``>``/``|`` blocks for description, and a
one-level ``metadata:`` map.
"""

from __future__ import annotations

import io
import os
import re
import stat
import zipfile
from pathlib import Path
from typing import Any

import httpx

SKILL_FILE_NAME = "SKILL.md"
# ``name``: 1–64 chars, lowercase letters/digits and single hyphens between them.
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESC_CHARS = 1_024
MAX_SKILL_BODY_CHARS = 50_000
MAX_SKILL_FILES = 300
MAX_SKILL_TOTAL_BYTES = 10 * 1024 * 1024
MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
SCRIPT_SUFFIXES = (".py", ".sh", ".js", ".ts")

GITHUB_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)(?:/tree/([^/]+)(?:/(.+))?)?/?$"
)

# ``/名称`` in a user's message: at the start or after whitespace, so paths and URLs never match.
SLASH_RE = re.compile(r"(?:^|(?<=\s))/([a-z0-9]+(?:-[a-z0-9]+)*)(?![a-z0-9-])")


def named_skills(text: str, names: set[str]) -> list[str]:
    """The skills ``text`` calls by ``/名称``, in order, each once; unknown names are ignored."""
    found: list[str] = []
    for match in SLASH_RE.finditer(text or ""):
        name = match.group(1)
        if name in names and name not in found:
            found.append(name)
    return found


# Where other agents on this machine keep their skills, as ``(label, folder under HOME)``.
# Each entry is a folder of ``<name>/SKILL.md`` skill folders; one may be a symlink into a
# shared library, which is followed for that skill folder only.
LOCAL_SKILL_ROOTS: tuple[tuple[str, str], ...] = (
    ("Claude Code", ".claude/skills"),
    ("Codex", ".codex/skills"),
    ("Agents", ".agents/skills"),
    ("Cursor", ".cursor/skills"),
    ("Gemini", ".gemini/skills"),
    ("OMP", ".omp/agent/skills"),
)


class SkillInvalid(ValueError):
    """The markdown, archive or URL cannot be read as a skill; the message is for the user."""


class SkillTooLarge(ValueError):
    """The skill exceeds an import cap; the message is for the user."""


class SkillDownloadError(ValueError):
    """The GitHub download failed; the message is for the user."""


# ---------------------------------------------------------------------- validation


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise SkillInvalid("技能缺少 name。")
    if len(name) > MAX_SKILL_NAME_CHARS:
        raise SkillInvalid(f"技能名称不能超过 {MAX_SKILL_NAME_CHARS} 个字符。")
    if not SKILL_NAME_RE.match(name):
        raise SkillInvalid(
            "技能名称只能用小写字母、数字和连字符，不能以连字符开头或结尾，也不能连续出现两个连字符。"
        )
    return name


def validate_description(description: str) -> str:
    if not isinstance(description, str) or not description.strip():
        raise SkillInvalid("技能缺少 description。")
    text = description.strip()
    if len(text) > MAX_SKILL_DESC_CHARS:
        raise SkillInvalid(f"技能描述不能超过 {MAX_SKILL_DESC_CHARS} 个字符。")
    return text


def validate_body(body: str) -> str:
    text = body if isinstance(body, str) else ""
    if len(text) > MAX_SKILL_BODY_CHARS:
        raise SkillInvalid(f"技能正文不能超过 {MAX_SKILL_BODY_CHARS} 个字符。")
    return text


# ---------------------------------------------------------------------- frontmatter


# Keys as real SKILL.md files write them: Claude Code's own fields are hyphenated
# (``allowed-tools``, ``argument-hint``).
_KV_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_-]*)\s*:\s?(.*)$")


def _unquote_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        return (
            inner.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    return value


def _dedent(lines: list[str]) -> list[str]:
    nonempty = [line for line in lines if line.strip()]
    if not nonempty:
        return ["" for _ in lines]
    indent = min(len(line) - len(line.lstrip(" \t")) for line in nonempty)
    return [line[indent:] if line.strip() else "" for line in lines]


def _block_value(lines: list[str], style: str) -> str:
    dedented = _dedent(lines)
    if style == "|":
        return "\n".join(dedented)
    # Folded ``>``: single newlines become spaces, blank lines become paragraph breaks.
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in dedented:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    return "\n".join(" ".join(paragraph) for paragraph in paragraphs)


def _parse_metadata(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _KV_RE.match(line.strip())
        if match is None:
            raise SkillInvalid("metadata 里有无法读取的行：" + line.strip())
        result[match.group(1)] = _unquote_scalar(match.group(2))
    return result


def _parse_frontmatter(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    metadata: dict[str, str] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " \t":
            raise SkillInvalid("frontmatter 里缩进的行没有对应的键：" + line.strip())
        match = _KV_RE.match(line)
        if match is None:
            raise SkillInvalid("frontmatter 里有无法读取的行：" + line.strip())
        key, rest = match.group(1), match.group(2)
        if not rest.strip():
            # ``key:`` — either a nested metadata map or a block scalar on the following lines.
            i += 1
            block: list[str] = []
            while i < n and lines[i].startswith((" ", "\t")):
                block.append(lines[i])
                i += 1
            if key == "metadata":
                metadata = _parse_metadata([b.lstrip(" \t") for b in block])
            else:
                data[key] = "\n".join(_dedent(block))
            continue
        if rest[0] in ">|":
            i += 1
            block = []
            while i < n and lines[i].startswith((" ", "\t")):
                block.append(lines[i])
                i += 1
            data[key] = _block_value(block, rest[0])
            continue
        if key == "metadata":
            i += 1
            continue
        data[key] = _unquote_scalar(rest)
        i += 1
    if metadata:
        data["metadata"] = metadata
    return data


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---\n…\n---\n`` from the body; return ``(raw frontmatter, body)``."""
    if not text.startswith("---"):
        raise SkillInvalid("技能缺少 SKILL.md 的 frontmatter（开头的 --- 块）。")
    lines = text.splitlines()
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise SkillInvalid("技能缺少 SKILL.md 的 frontmatter（结尾的 ---）。")
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return _parse_frontmatter(lines[1:end]), body


def parse_skill_markdown(text: str, *, folder_name: str | None = None) -> dict[str, Any]:
    """Parse and validate one SKILL.md; ``folder_name`` is the import folder to match."""
    frontmatter, body = split_frontmatter(text)
    name = validate_name(str(frontmatter.get("name") or ""))
    description = validate_description(str(frontmatter.get("description") or ""))
    body = validate_body(body)
    if folder_name and name != folder_name:
        raise SkillInvalid(
            f"技能的 name「{name}」和所在文件夹「{folder_name}」不一致。"
        )
    metadata = frontmatter.get("metadata")
    return {
        "name": name,
        "description": description,
        "body": body,
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def render_skill_markdown(
    name: str, description: str, body: str, *, metadata: dict[str, str] | None = None
) -> str:
    """The canonical SKILL.md text for one skill, ready to write back to disk."""
    name = validate_name(name)
    description = validate_description(description)
    body = validate_body(body)
    lines = ["---", f"name: {name}"]
    # Use a literal block whenever a plain scalar could be misread (a leading YAML marker,
    # a newline, or surrounding whitespace).
    if (
        "\n" in description
        or description[:1] in ">|'\"#&*!%{}[]"
        or description.strip() != description
    ):
        lines.append("description: |")
        lines.extend(f"  {line}" for line in description.split("\n"))
    else:
        lines.append(f"description: {description}")
    meta = {
        key: value
        for key, value in (metadata or {}).items()
        if key not in ("name", "description")
    }
    if meta:
        lines.append("metadata:")
        lines.extend(f"  {key}: {value}" for key, value in sorted(meta.items()))
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


def _is_script(path: str) -> bool:
    return path.startswith("scripts/") or path.endswith(SCRIPT_SUFFIXES)


# ---------------------------------------------------------------------- imports


def _check_entry_path(name: str) -> str:
    """A safe relative path for one archive entry, or raise."""
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise SkillInvalid("压缩包里不能有绝对路径：" + name)
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        raise SkillInvalid("压缩包里不能有 .. 路径：" + name)
    return name.replace("\\", "/")


def _is_special(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    if mode == 0:
        return False
    kind = stat.S_IFMT(mode)
    return kind in (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK)


def _strip_single_top(files: dict[str, bytes]) -> tuple[dict[str, bytes], str | None]:
    """Strip one shared top folder; return ``(re-keyed files, that folder's name)``."""
    paths = list(files)
    if not paths or "/" not in paths[0]:
        return files, None
    top = paths[0].split("/", 1)[0]
    if any(not path.startswith(top + "/") for path in paths):
        return files, None
    return {path[len(top) + 1 :]: data for path, data in files.items()}, top


def _assemble(
    files: dict[str, bytes], *, folder_name: str | None, source_url: str | None
) -> dict[str, Any]:
    """Build the import payload from validated ``path -> bytes`` entries."""
    md_paths = [path for path in files if path.rsplit("/", 1)[-1] == SKILL_FILE_NAME]
    if not md_paths:
        raise SkillInvalid("压缩包里没有 SKILL.md。")
    if len(md_paths) > 1:
        raise SkillInvalid("压缩包里有多份 SKILL.md，请先只保留一份：" + "、".join(sorted(md_paths)))
    md_path = md_paths[0]
    skill_dir = md_path.rsplit("/", 1)[0] if "/" in md_path else ""
    # The folder holding SKILL.md names the skill; a skill at the archive root uses the
    # stripped top folder instead.
    name_folder = skill_dir or folder_name
    try:
        text = files[md_path].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillInvalid("SKILL.md 不是 UTF-8 文本。") from exc
    parsed = parse_skill_markdown(text, folder_name=name_folder)
    prefix = (skill_dir + "/") if skill_dir else ""
    skill_files: dict[str, bytes] = {}
    for path, data in files.items():
        if path == md_path:
            continue
        if skill_dir and not path.startswith(prefix):
            continue
        skill_files[path[len(prefix) :]] = data
    return {
        "name": parsed["name"],
        "description": parsed["description"],
        "body": parsed["body"],
        "metadata": parsed["metadata"],
        "files": skill_files,
        "file_count": 1 + len(skill_files),
        "has_scripts": any(_is_script(path) for path in skill_files),
        "source_url": source_url,
    }


def extract_archive(data: bytes) -> dict[str, Any]:
    """Parse one uploaded zip into the import payload; enforce every cap and path rule."""
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise SkillTooLarge("上传的压缩包超过大小上限。")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillInvalid("上传的不是有效的 zip 文件。") from exc
    files: dict[str, bytes] = {}
    total = 0
    try:
        infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise SkillInvalid("上传的 zip 文件损坏。") from exc
    for info in infos:
        name = _check_entry_path(info.filename)
        if info.is_dir() or name.endswith("/"):
            continue
        if _is_special(info):
            raise SkillInvalid("压缩包里不能有链接或设备文件：" + name)
        if info.file_size > MAX_SKILL_FILE_BYTES:
            raise SkillTooLarge("压缩包里单个文件超过 2 MB。")
        total += info.file_size
        if total > MAX_SKILL_TOTAL_BYTES:
            raise SkillTooLarge("压缩包解压后超过 10 MB。")
        files[name] = archive.read(info)
    if len(files) > MAX_SKILL_FILES:
        raise SkillTooLarge(f"压缩包里文件超过 {MAX_SKILL_FILES} 个。")
    scoped, top = _strip_single_top(files)
    return _assemble(scoped, folder_name=top, source_url=None)


def extract_markdown(data: bytes) -> dict[str, Any]:
    """Parse one uploaded ``SKILL.md`` file (``text/markdown``) into the import payload."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillInvalid("SKILL.md 不是 UTF-8 文本。") from exc
    parsed = parse_skill_markdown(text)
    return {
        "name": parsed["name"],
        "description": parsed["description"],
        "body": parsed["body"],
        "metadata": parsed["metadata"],
        "files": {},
        "file_count": 1,
        "has_scripts": False,
        "source_url": None,
    }


def parse_github_url(url: str) -> dict[str, str]:
    match = GITHUB_URL_RE.match(url.strip())
    if match is None:
        raise SkillInvalid(
            "GitHub 地址只能是 https://github.com/<owner>/<repo> 或 "
            "https://github.com/<owner>/<repo>/tree/<分支>/<路径>。"
        )
    return {
        "owner": match.group(1),
        "repo": match.group(2),
        "ref": match.group(3) or "HEAD",
        "path": match.group(4) or "",
    }


async def import_from_github(url: str, client: httpx.AsyncClient | None) -> dict[str, Any]:
    """Download and parse one GitHub skill; ``source_url`` is the original URL."""
    parsed = parse_github_url(url)
    if client is None:
        raise SkillDownloadError("服务未就绪，请稍后重试。")
    download = (
        f"https://codeload.github.com/{parsed['owner']}/{parsed['repo']}/zip/{parsed['ref']}"
    )
    try:
        response = await client.get(download)
    except httpx.HTTPError as exc:
        raise SkillDownloadError("下载 GitHub 压缩包失败。") from exc
    if response.status_code >= 400:
        raise SkillDownloadError("下载 GitHub 压缩包失败（地址或分支可能不对）。")
    data = response.content
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise SkillTooLarge("GitHub 压缩包超过 25 MB。")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillDownloadError("GitHub 返回的压缩包无法读取。") from exc
    files: dict[str, bytes] = {}
    total = 0
    for info in archive.infolist():
        name = _check_entry_path(info.filename)
        if info.is_dir() or name.endswith("/"):
            continue
        if _is_special(info):
            raise SkillInvalid("压缩包里不能有链接或设备文件：" + name)
        if info.file_size > MAX_SKILL_FILE_BYTES:
            raise SkillTooLarge("压缩包里单个文件超过 2 MB。")
        total += info.file_size
        if total > MAX_SKILL_TOTAL_BYTES:
            raise SkillTooLarge("压缩包解压后超过 10 MB。")
        files[name] = archive.read(info)
    if len(files) > MAX_SKILL_FILES:
        raise SkillTooLarge(f"压缩包里文件超过 {MAX_SKILL_FILES} 个。")
    scoped, _top = _strip_single_top(files)
    sub_path = parsed["path"].strip("/")
    if sub_path:
        prefix = sub_path + "/"
        narrowed = {
            path[len(prefix) :]: data for path, data in scoped.items() if path.startswith(prefix)
        }
        if not narrowed:
            raise SkillInvalid(f"GitHub 地址里的路径「{sub_path}」下没有文件。")
        scoped = narrowed
    folder_name = sub_path.rsplit("/", 1)[-1] if sub_path else None
    return _assemble(scoped, folder_name=folder_name, source_url=url.strip())


# ---------------------------------------------------------------------- local folders


def _local_roots(home: Path) -> list[tuple[str, Path]]:
    return [(label, home / relative) for label, relative in LOCAL_SKILL_ROOTS]


def _skill_folders(root: Path) -> list[Path]:
    """The ``<name>/`` folders directly under ``root`` that hold a SKILL.md (hidden ones skipped)."""
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if not entry.name.startswith(".") and entry.is_dir() and (entry / SKILL_FILE_NAME).is_file()
    ]


def scan_local_skills(home: Path) -> list[dict[str, Any]]:
    """Every skill another agent keeps on this machine, for the import picker.

    One row per folder: where it lives, which app it belongs to, its name and description when
    SKILL.md reads, otherwise the reason it cannot be imported. The same skill linked into two
    apps' folders (a shared library) is listed once, under the first app.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, root in _local_roots(home):
        for folder in _skill_folders(root):
            try:
                real = str(folder.resolve())
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            entry: dict[str, Any] = {
                "path": str(folder),
                "app": label,
                "name": folder.name,
                "description": "",
                "error": None,
            }
            try:
                parsed = parse_skill_markdown(
                    (folder / SKILL_FILE_NAME).read_text(encoding="utf-8"), folder_name=folder.name
                )
                entry["name"], entry["description"] = parsed["name"], parsed["description"]
            except (OSError, UnicodeDecodeError):
                entry["error"] = "SKILL.md 读不出来。"
            except SkillInvalid as exc:
                entry["error"] = str(exc)
            found.append(entry)
    return found


def read_local_skill(path: str, home: Path) -> dict[str, Any]:
    """One skill folder ``scan_local_skills`` listed, as the import payload.

    Only a folder directly under one of the known roots is read, so a request cannot point the
    importer anywhere else on disk. Inside it, links and special files are refused (the folder
    itself may be a link into a shared library); the caps match a zip upload.
    """
    folder = Path(path)
    if not any(folder.parent == root for _label, root in _local_roots(home)):
        raise SkillInvalid("只能导入本机技能列表里的文件夹。")
    if folder.name.startswith(".") or not (folder / SKILL_FILE_NAME).is_file():
        raise SkillInvalid("这个文件夹里没有 SKILL.md。")
    base = folder.resolve()
    files: dict[str, bytes] = {}
    total = 0
    for current, dirs, names in os.walk(base, followlinks=False):
        here = Path(current)
        # Tooling leftovers are never part of a skill; pruned so the walk never enters them.
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for directory in dirs:
            if (here / directory).is_symlink():
                raise SkillInvalid("技能文件夹里不能有链接：" + (here / directory).relative_to(base).as_posix())
        for name in names:
            if name == ".DS_Store":
                continue
            target = here / name
            relative = target.relative_to(base).as_posix()
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SkillInvalid("技能文件夹里不能有链接或设备文件：" + relative)
            if info.st_size > MAX_SKILL_FILE_BYTES:
                raise SkillTooLarge(f"技能里单个文件超过 2 MB：{relative}")
            total += info.st_size
            if total > MAX_SKILL_TOTAL_BYTES:
                raise SkillTooLarge("技能文件夹超过 10 MB。")
            files[relative] = target.read_bytes()
            if len(files) > MAX_SKILL_FILES:
                raise SkillTooLarge(f"技能里的文件超过 {MAX_SKILL_FILES} 个。")
    return _assemble(files, folder_name=folder.name, source_url=None)
