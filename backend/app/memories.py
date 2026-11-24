"""Long-term memory: the shadow text search reads, the payload the panel reads, op parsing.

A memory is one self-contained Chinese sentence plus a short topic label; the store keeps
the rows and the FTS index, :mod:`app.prompts` builds the prompt block, and this module
holds everything both sides share: the bigram tokenizer, the vocabulary choices (scopes,
limits), the wire payload and the parser for what the extraction model answers.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

SCOPE_USER = "user"
SCOPE_AGENT = "agent"
SCOPE_TEAM = "team"
# A group's own facts: ``agent_id`` holds the group's conversation id, and only bots speaking
# in that group see them.
SCOPE_GROUP = "group"
SCOPES: tuple[str, ...] = (SCOPE_USER, SCOPE_AGENT, SCOPE_TEAM, SCOPE_GROUP)
# Owner-scoped rows: ``agent_id`` names the bot or the group the row belongs to.
OWNED_SCOPES: tuple[str, ...] = (SCOPE_AGENT, SCOPE_GROUP)
WRITE_SCOPES: tuple[str, ...] = (SCOPE_USER, SCOPE_TEAM)

ORIGIN_USER = "user"
ORIGIN_AGENT = "agent"
ORIGIN_EXTRACT = "extract"

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"

# One fact, one sentence: this bounds a single row the panel shows as one line of prose.
MAX_CONTENT_CHARS = 1_000
MAX_TOPIC_CHARS = 40
# A derived topic keeps the opening of the sentence; no sentence punctuation in a label.
DERIVED_TOPIC_CHARS = 16
# Only the head of a huge message is searched; beyond this the OR gets weaker, not better.
MAX_QUERY_CHARS = 200
# The prompt budget the settings start with, in characters.
DEFAULT_CORE_CHARS = 1_200

_TRAILING = "。！？!?；;，,、…「」\"'）)】]"
_FENCES = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


# ------------------------------------------------------------------ tokenizing


def _pairs(word: str) -> list[str]:
    """Every adjacent pair of one word's characters, or the character when it is alone."""
    if len(word) <= 1:
        return [word]
    return [word[index : index + 2] for index in range(len(word) - 1)]


def bigrams(text: str) -> str:
    """The shadow text FTS5 indexes: every adjacent character pair, words separated.

    FTS5's ``unicode61`` tokenizer treats a whole Chinese sentence as one token, so 「咖啡」
    never matches 「我每天早上喝咖啡」. Indexing pairs instead makes every two-character query
    a token of its own; Latin words keep working because their pairs are indexed the same
    way, and a single-character query falls back to a ``LIKE`` scan in the store.
    """
    pairs: list[str] = []
    for word in str(text).split():
        pairs.extend(_pairs(word))
    return " ".join(pairs)


def fts_query(text: str) -> str | None:
    """The FTS5 ``MATCH`` expression for ``text``; ``None`` when it has nothing to match."""
    head = " ".join(str(text).split())[:MAX_QUERY_CHARS]
    tokens = bigrams(head).split()
    if not tokens:
        return None
    # Quoted tokens: a pair can contain FTS syntax characters, and quotes keep them literal.
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def derive_topic(content: str) -> str:
    """A short label for a memory that arrived without one: the opening of its sentence."""
    head = " ".join(str(content).split())
    cut = head[:DERIVED_TOPIC_CHARS]
    return cut.rstrip(_TRAILING) or head[:MAX_TOPIC_CHARS]


# ------------------------------------------------------------------ what is worth reading

# A user turn made only of these says nothing a memory could keep; the extraction skips it.
# Longest first, so 「好的谢谢」 is consumed as 好的 + 谢谢 rather than stopping at 好.
_ACKNOWLEDGEMENTS = tuple(
    sorted(
        {
            "好", "好的", "好滴", "好嘞", "好吧", "好呀", "行", "行吧", "可以", "嗯", "嗯嗯",
            "哦", "噢", "喔", "对", "对的", "是的", "是", "没问题", "收到", "明白", "了解",
            "知道了", "懂了", "继续", "谢谢", "多谢", "感谢", "辛苦了", "ok", "okay", "k",
            "yes", "yep", "thanks", "thx", "ty", "👍", "🙏", "👌",
        },
        key=len,
        reverse=True,
    )
)
# Particles and addressees carry nothing on their own: 「谢谢你啦」 is still only thanks.
_FILLERS = re.compile(r"[\s\W_啊呀吧呢哈啦嘛了的你您~～]+", re.UNICODE)


def worth_extracting(user_text: str) -> bool:
    """False for a user turn that only acknowledges (「好的，谢谢！」), True otherwise."""
    text = _FILLERS.sub("", str(user_text).lower())
    # The filler pass also removes 了/的 inside words, so the vocabulary is matched the same way.
    while text:
        for word in _ACKNOWLEDGEMENTS:
            token = _FILLERS.sub("", word)
            if token and text.startswith(token):
                text = text[len(token) :]
                break
        else:
            return True
    return False


# ------------------------------------------------------------------ payloads


def memory_payload(memory: Mapping[str, Any]) -> dict:
    """One row as the client contract spells it."""
    return {
        "id": memory["id"],
        "scope": memory["scope"],
        "agent_id": memory["agent_id"],
        "topic": memory["topic"],
        "content": memory["content"],
        "pinned": bool(memory["pinned"]),
        "status": memory["status"],
        "superseded_by": memory["superseded_by"],
        "origin": memory["origin"],
        "author_id": memory["author_id"],
        "source_conversation_id": memory["source_conversation_id"],
        "source_message_ids": list(memory["source_message_ids"]),
        "valid_from": memory["valid_from"],
        "valid_to": memory["valid_to"],
        "created_at": memory["created_at"],
        "updated_at": memory["updated_at"],
    }


def memories_payload(store: Any) -> dict:
    """``GET /memories`` and the ``memories`` SSE event: every row plus the two switches."""
    return {
        "memories": [memory_payload(memory) for memory in store.list_memories()],
        "settings": store.memory_settings(),
    }


# ------------------------------------------------------------------ extraction output


def _ops_array(text: str) -> list[Any] | None:
    """The JSON array in a model reply, even when it wrapped it in prose or a fence."""
    body = _FENCES.sub("", str(text or "")).strip()
    candidates = [body]
    start, end = body.find("["), body.rfind("]")
    if 0 <= start < end:
        candidates.append(body[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, list):
            return value
    return None


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_ops(text: str) -> list[dict] | None:
    """The operations a model answered, validated item by item.

    ``None`` means the reply was not a JSON array at all, so nothing may be applied;
    an unusable item inside a readable array is dropped, and the rest still counts.
    """
    raw = _ops_array(text)
    if raw is None:
        return None
    ops: list[dict] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        kind = _text(item.get("op")).lower()
        if kind == "noop":
            continue
        if kind in ("update", "delete"):
            memory_id = _text(item.get("id"))
            if not memory_id:
                continue
            if kind == "delete":
                ops.append({"op": "delete", "id": memory_id})
                continue
            content = _text(item.get("content"))[:MAX_CONTENT_CHARS]
            if not content:
                continue
            topic = _text(item.get("topic"))[:MAX_TOPIC_CHARS] or derive_topic(content)
            scope = _text(item.get("scope")).lower()
            ops.append(
                {
                    "op": "update",
                    "id": memory_id,
                    "topic": topic,
                    "content": content,
                    "scope": scope if scope in WRITE_SCOPES else SCOPE_USER,
                }
            )
            continue
        if kind != "add":
            continue
        content = _text(item.get("content"))[:MAX_CONTENT_CHARS]
        if not content:
            continue
        topic = _text(item.get("topic"))[:MAX_TOPIC_CHARS] or derive_topic(content)
        scope = _text(item.get("scope")).lower()
        ops.append(
            {
                "op": "add",
                "topic": topic,
                "content": content,
                "scope": scope if scope in WRITE_SCOPES else SCOPE_USER,
            }
        )
    return ops
