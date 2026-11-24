"""Emoji reactions: what counts as a valid emoji, and the reaction-response JSON parser.

Pure helpers with no store or engine imports, so the HTTP routes, the streamed reply
parser and the bot's reaction response all share one definition of "a valid emoji".
"""

from __future__ import annotations

import json
from typing import Any

from .appearance import MOODS

# The keycap bases ``0-9#*`` are only an emoji when immediately followed by U+20E3.
_KEYCAP_BASES = "0123456789#*"


def _emoji_char(ch: str) -> bool:
    """Whether one code point is an emoji code point (or a ZWJ/variation/join character)."""
    cp = ord(ch)
    if cp in (0x00A9, 0x00AE):  # © ®
        return True
    if 0x203C <= cp <= 0x3299:  # the emoji-bearing block, accepted wholesale
        return True
    if 0x1F000 <= cp <= 0x1FAFF:  # the main emoji plane
        return True
    if cp in (0xFE0F, 0x200D, 0x20E3):  # variation selector, ZWJ, combining keycap
        return True
    if 0xE0020 <= cp <= 0xE007F:  # subdivision/flag tag characters
        return True
    return False


def normalize_emoji(raw: str) -> str | None:
    """``raw`` stripped down to a valid emoji, or ``None`` when it is not one.

    A valid emoji is 1–16 characters whose code points are all emoji, plus the ASCII
    keycap bases ``0-9#*`` only when each is immediately followed by U+20E3. Letters,
    CJK, spaces and ``<`` are never an emoji.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > 16:
        return None
    for index, ch in enumerate(text):
        if ch in _KEYCAP_BASES:
            # A keycap base (``1``, ``#``, ``*``) is an emoji only as part of a keycap: the
            # next code point is U+20E3, optionally with a variation selector in between
            # (``1️⃣`` is "1" + FE0F + 20E3, ``1⃣`` is "1" + 20E3).
            if index + 1 < len(text) and ord(text[index + 1]) == 0x20E3:
                continue
            if (
                index + 2 < len(text)
                and ord(text[index + 1]) == 0xFE0F
                and ord(text[index + 2]) == 0x20E3
            ):
                continue
            return None
        if not _emoji_char(ch):
            return None
    return text


def reactable_reason(message: dict, conversation: dict | None) -> str | None:
    """Why ``message`` cannot take a reaction, or ``None`` when it can.

    Only a finished user or agent message in a user-facing conversation takes reactions; a
    streaming reply, an action row and a hidden workspace are all off-limits.
    """
    if message.get("kind") not in ("user", "agent"):
        return "这条消息不能贴表情。"
    if message.get("status") != "complete":
        return "这条消息还没写完，不能贴表情。"
    if conversation is None or conversation.get("kind") == "background":
        return "这条消息不能贴表情。"
    return None


def parse_reaction_response(text: str) -> dict[str, Any] | None:
    """The bot's reaction-response JSON, or ``None`` when it is unreadable.

    ``{"action":"none"}``, ``{"action":"react","emoji":…}`` or
    ``{"action":"reply","text":…,"mood":…}``; an unknown action, a missing/invalid emoji
    or an empty reply text all degrade to ``None`` (treated as "none" by the caller).
    """
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1] if 0 <= start < end else text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if action == "none":
        return {"action": "none"}
    if action == "react":
        emoji = normalize_emoji(data.get("emoji", ""))
        if emoji is None:
            return None
        return {"action": "react", "emoji": emoji}
    if action == "reply":
        reply = str(data.get("text") or "").strip()
        if not reply:
            return None
        mood = data.get("mood")
        if not isinstance(mood, str) or mood not in MOODS:
            mood = None
        # One short, natural line: cap it so a runaway answer never becomes a reply.
        return {"action": "reply", "text": reply[:200], "mood": mood}
    return None
