"""A reply that stops on a lead-in, before the decision it was leading to.

The model sometimes ends its turn with 「有一件事需要你现在决定：」 or 「需要你确认一下。」
without calling ``ask_user``. The user then has to ask what it meant. This matcher is what
lets the engine notice that one case and give the model a single chance to finish the
thought or open the ask card; it deliberately stays small so it never fires on ordinary
prose that happens to mention a decision.
"""

from __future__ import annotations

import re

# Sentence breaks the matcher cuts on: a lead-in must be in the last sentence.
_SENTENCE_ENDS = re.compile(r"[。！？!?；;\n]")
# Phrases that announce a decision the user has to make. Kept tiny on purpose: each one is a
# phrase a bot writes right before it should have opened the ask card.
DECISION_WORDS = (
    "需要你",
    "请你决定",
    "请选择",
    "确认一下",
    "由你决定",
)


def is_dangling(text: str) -> bool:
    """Whether a finished reply stopped on a lead-in instead of finishing or asking.

    True when the trimmed text ends with ``:``/``：``, or when its last sentence announces a
    decision for the user (see :data:`DECISION_WORDS`). A reply that ends normally (「。」)
    is never dangling unless that last sentence carries one of those phrases.
    """
    trimmed = text.strip()
    if not trimmed:
        return False
    if trimmed[-1] in ":：":
        return True
    sentences = [part for part in _SENTENCE_ENDS.split(trimmed) if part.strip()]
    if not sentences:
        return False
    last = sentences[-1].strip(" \t*_`「」‘’\"'…~～")
    return any(word in last for word in DECISION_WORDS)
