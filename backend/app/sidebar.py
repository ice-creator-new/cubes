"""Sidebar layout validation and shaping, shared by the store and the HTTP routes.

A layout write replaces the whole layout, so every write is validated as a unit: the
conversations and folders it mentions must exist, each conversation may appear once, and
``folder_ids`` must be a permutation of the folders that exist. Ordering comes from the
request arrays — pinned entries are numbered in place, then each folder's entries — so two
windows writing at once can never interleave positions.
"""

from __future__ import annotations

from typing import Iterable, Sequence


class SidebarInvalid(ValueError):
    """A layout that cannot be applied; the message is a Chinese sentence ending in 。"""


class FolderNameTaken(RuntimeError):
    """A folder name collides with an existing folder."""


def folder_name_key(name: str) -> str:
    """Comparison key for folder names: trimmed and case-insensitive."""
    return name.strip().casefold()


def shape_layout(
    *,
    items: Sequence[dict],
    folder_ids: Sequence[str],
    conversation_ids: Iterable[str],
    known_folder_ids: Iterable[str],
) -> list[dict]:
    """Validate one full layout and renumber it; return the rows to persist in order."""
    conversations = set(conversation_ids)
    folders = set(known_folder_ids)

    if len(set(folder_ids)) != len(folder_ids) or set(folder_ids) != folders:
        raise SidebarInvalid("分组 ID 必须是现有分组的一个排列。")

    shaped: list[dict] = []
    seen: set[str] = set()
    pin_position = 0
    positions: dict[str | None, int] = {}
    for item in items:
        conversation_id = str(item.get("conversation_id") or "").strip()
        if not conversation_id:
            raise SidebarInvalid("侧栏条目缺少会话 ID。")
        if conversation_id in seen:
            raise SidebarInvalid(f"会话「{conversation_id}」在侧栏里出现了不止一次。")
        if conversation_id not in conversations:
            raise SidebarInvalid(f"会话「{conversation_id}」不存在。")
        seen.add(conversation_id)
        folder_id = item.get("folder_id")
        folder_id = None if folder_id is None else str(folder_id).strip()
        if folder_id is not None and folder_id not in folders:
            raise SidebarInvalid(f"分组「{folder_id}」不存在。")
        pinned = bool(item.get("pinned"))
        row = {
            "conversation_id": conversation_id,
            "pinned": pinned,
            "pin_position": 0,
            "folder_id": folder_id,
            "position": positions.get(folder_id, 0),
        }
        if pinned:
            row["pin_position"] = pin_position
            pin_position += 1
        positions[folder_id] = row["position"] + 1
        shaped.append(row)
    return shaped
