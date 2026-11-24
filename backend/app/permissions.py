"""Who may do what without asking: the approval mode, per-bot overrides and the folders.

Three modes, from the most careful to the least:

- ``ask`` 手动审批: reading inside the allowed folders goes ahead; writing a file, running
  code, using a computer, writing through a connector, changing a schedule and touching a
  place outside the folders each show a card first.
- ``auto`` 自动审批: reading and writing inside the folders, running code and the cloud
  sandbox go ahead; this Mac, places outside the folders, connector writes and schedules
  still ask.
- ``full`` 完全权限: nothing asks, except deleting a member and dissolving a group.

A remembered 「总是允许」 rule still skips its card in every mode. Whatever the mode, the file
tools never reach Cubes' own data directory (it holds the model keys) beyond the working
folder and the bots' own folders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

ASK = "ask"
AUTO = "auto"
FULL = "full"
MODES: tuple[str, ...] = (ASK, AUTO, FULL)
DEFAULT_MODE = ASK
MODE_LABELS = {ASK: "手动审批", AUTO: "自动审批", FULL: "完全权限"}

# What one card guards. A mode lets the cards it grants through without asking; ``TEAM``
# (deleting a member, dissolving a group) is granted by none of them.
READ_OUTSIDE = "read_outside"
WRITE_INSIDE = "write_inside"
WRITE_OUTSIDE = "write_outside"
CODE = "code"
COMPUTER_CLOUD = "computer_cloud"
COMPUTER_HOST = "computer_host"
CONNECTOR_WRITE = "connector_write"
SCHEDULE = "schedule"
TEAM = "team"

GRANTS: dict[str, frozenset[str]] = {
    ASK: frozenset(),
    AUTO: frozenset({WRITE_INSIDE, CODE, COMPUTER_CLOUD}),
    FULL: frozenset(
        {
            READ_OUTSIDE,
            WRITE_INSIDE,
            WRITE_OUTSIDE,
            CODE,
            COMPUTER_CLOUD,
            COMPUTER_HOST,
            CONNECTOR_WRITE,
            SCHEDULE,
        }
    ),
}

MAX_FOLDERS = 20
BOTS_DIRNAME = "bots"


class FolderRefused(ValueError):
    """A folder the user may not open to the bots; the message is the Chinese reason."""


# ------------------------------------------------------------------ modes


def mode_of(store: Any, agent: dict | None) -> str:
    """The mode one bot works in: its own pick, else the workspace's."""
    own = (agent or {}).get("permission_mode")
    if own in MODES:
        return str(own)
    if store is None:
        return DEFAULT_MODE
    return str(store.permission_settings()["mode"])


def grants(store: Any, agent: dict | None, grant: str) -> bool:
    """Whether this bot's mode lets a card guarding ``grant`` through without asking."""
    return bool(grant) and grant in GRANTS[mode_of(store, agent)]


# ------------------------------------------------------------------ folders


def data_dir(settings: Any) -> Path:
    return Path(getattr(settings, "database_path", "data/cubes.sqlite3")).resolve().parent


def working_folder(settings: Any) -> Path:
    """Where relative paths land: ``WORKSPACE_DIR``, else ``workspace/`` beside the database."""
    configured = getattr(settings, "workspace_dir", None)
    folder = Path(configured).resolve() if configured else data_dir(settings) / "workspace"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def allowed_folders(store: Any, settings: Any) -> list[Path]:
    """The working folder first, then every folder the user added."""
    extra = store.permission_settings()["folders"] if store is not None else []
    return [working_folder(settings), *(Path(path) for path in extra)]


def inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def resolve(settings: Any, raw: Any) -> Path:
    """The absolute path a tool means: ``~`` expanded, relative paths under the working folder."""
    folder = working_folder(settings)
    if raw in (None, ""):
        return folder
    if not isinstance(raw, str):
        raise ValueError("路径必须是文本。")
    text = raw.strip()
    if len(text) > 1_000:
        raise ValueError("路径太长了。")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = folder / candidate
    try:
        return candidate.resolve()
    except OSError as exc:
        raise ValueError(f"路径「{text}」无法解析。") from exc


def protected(settings: Any, path: Path) -> bool:
    """Cubes' own data (database, keys, skills) is off limits to the file tools in every mode."""
    data = data_dir(settings)
    return inside(path, [data]) and not inside(path, [working_folder(settings), data / BOTS_DIRNAME])


def clean_folder(settings: Any, raw: str) -> str:
    """One folder the user wants to open to the bots, checked and made absolute."""
    text = str(raw or "").strip()
    if not text:
        raise FolderRefused("文件夹路径不能为空。")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise FolderRefused(f"「{text}」不是完整路径，请写成 /Users/… 或 ~/… 的形式。")
    path = path.resolve()
    if not path.is_dir():
        raise FolderRefused(f"「{display(path)}」不存在或不是文件夹。")
    data = data_dir(settings)
    if inside(data, [path]) or inside(path, [data]):
        raise FolderRefused(
            f"「{display(path)}」包含 Cubes 自己的数据（里面有模型密钥），不能整个放开；可以加它下面的子文件夹。"
        )
    return str(path)


def clean_folders(settings: Any, raw: list[str]) -> list[str]:
    if len(raw) > MAX_FOLDERS:
        raise FolderRefused(f"最多只能加 {MAX_FOLDERS} 个文件夹。")
    primary = working_folder(settings)
    cleaned: list[str] = []
    for item in raw:
        folder = clean_folder(settings, item)
        if Path(folder) != primary and folder not in cleaned:
            cleaned.append(folder)
    return cleaned


def display(path: Path) -> str:
    """A path as the user reads it: under the home folder it starts with ``~``."""
    home = Path.home()
    try:
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        return str(path)


# ------------------------------------------------------------------ prompt


def prompt_hint(store: Any, settings: Any, agent: dict | None) -> str:
    """The line telling one bot where its files go and what it may do without asking."""
    folders = allowed_folders(store, settings)
    primary, extra = folders[0], folders[1:]
    lines = [
        f"文件：相对路径都落在工作文件夹 {primary}；~ 表示用户的主目录 {Path.home()}。",
    ]
    if extra:
        lines.append("用户还允许了这些文件夹：" + "、".join(str(path) for path in extra) + "。")
    mode = mode_of(store, agent)
    if mode == FULL:
        lines.append("你有完全权限：可以直接读写任何位置、运行代码，不用先问。")
    elif mode == AUTO:
        lines.append("在这些文件夹里可以直接读写、运行代码；读写别的位置会先请用户确认。")
    else:
        lines.append("在这些文件夹里可以直接读取；写文件、运行代码和读写别的位置都会先请用户确认，用户可能拒绝。")
    lines.append("用户让你把东西存到某个位置时，就用文件工具写到那里，不要只把内容贴在回复里。")
    return "\n".join(lines)
