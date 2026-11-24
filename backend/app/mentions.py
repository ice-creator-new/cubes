"""Parse ``@名字`` mentions against the agents that can actually be addressed.

One pure function so routing, persistence and the tests agree on what counts as a
mention: the marker is ``@`` or its full-width form ``＠``, the name must match a roster
name exactly from the marker, the longest name wins (「原型设计师」 before 「原型」), and
text after the name (punctuation, other words) is not part of the mention.
"""

from __future__ import annotations

from typing import Mapping, Sequence

MENTION_MARKERS = ("@", "＠")


def parse_mentions(text: str, roster: Sequence[Mapping[str, str]]) -> list[str]:
    """Agent ids mentioned in ``text``, in order of appearance and deduplicated."""
    if not text:
        return []
    candidates: list[tuple[str, str]] = []
    for agent in roster:
        agent_id = str(agent.get("id") or "")
        name = str(agent.get("name") or "")
        if agent_id and name:
            candidates.append((name, agent_id))
    if not candidates:
        return []
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    found: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] not in MENTION_MARKERS:
            index += 1
            continue
        start = index + 1
        for name, agent_id in candidates:
            if text.startswith(name, start):
                if agent_id not in found:
                    found.append(agent_id)
                index = start + len(name)
                break
        else:
            index += 1
    return found


def opening_mentions(text: str) -> str:
    """The run of ``@name`` tokens a message opens with, as text; empty when it opens otherwise.

    「@匠师 @军师 看下」 → 「@匠师 @军师」; 「按照 @幕僚长 的方案」 → 「」. A token ends at
    whitespace, a comma or a colon, so 「@幕僚长，你来」 still opens with the supervisor.
    """
    tokens: list[str] = []
    rest = text.lstrip()
    while rest and rest[0] in MENTION_MARKERS:
        end = 1
        while end < len(rest) and not rest[end].isspace() and rest[end] not in "，,:：":
            end += 1
        tokens.append(rest[:end])
        rest = rest[end:].lstrip(" \t\u3000，,:：")
    return " ".join(tokens)


# ``@所有人`` addresses every member of a group at once; the English forms are accepted too.
EVERYONE_NAMES = ("所有人", "全体成员", "everyone", "all")


def mentions_everyone(text: str) -> bool:
    """Whether ``text`` holds ``@所有人`` (or ``@everyone``/``@all``) as a whole mention."""
    if not text:
        return False
    lowered = text.casefold()
    for marker in MENTION_MARKERS:
        for name in EVERYONE_NAMES:
            start = 0
            needle = marker + name
            while (index := lowered.find(needle, start)) >= 0:
                end = index + len(needle)
                # ``@allen`` is a name, not ``@all``: the mention must end at a non-letter.
                if end >= len(lowered) or not lowered[end].isascii() or not lowered[end].isalnum():
                    return True
                start = end
    return False
