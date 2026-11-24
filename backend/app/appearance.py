"""Appearance catalog, stored-appearance validation and the streamed mood tag parser.

The catalog is the single source of truth for the shared contract: the web client keeps a
matching copy and a test pins the two lists together. Stored appearance JSON is read
defensively — a corrupt row or an unknown value degrades to ``None`` (clients then derive
an appearance from ``avatar_seed``) instead of breaking a workspace payload.

Stored are the silhouette, the body colour and an optional eye colour. The body is a catalog
colour or any ``#rrggbb``; the eye is any ``#rrggbb``, or absent for the automatic
high-contrast eye. Faces are automatic: the client derives them from the reply's mood tag
and from what the bot is doing, so an appearance has no expression. Old rows and old
clients may still carry the retired keys; both are accepted and then dropped (see
``LEGACY_SHAPES`` and ``EXPRESSIONS``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .attachments import split_images_tag

logger = logging.getLogger(__name__)

SHAPES = (
    "round",
    "blob",
    "squircle",
    "sparkle",
    "clover",
    "teardrop",
    "flower",
    "peak",
    "capsule",
    "arch",
    "pentagon",
    "star",
    "hexagon",
    "pebble",
    "heart",
    "egg",
    "diamond",
    "octagon",
    "cat",
    "bear",
    "ghost",
)
# Silhouettes retired with the fourteen-shape set, mapped to the shape that replaced them.
# Read at parse time, so a stored row from an older build keeps rendering without a rewrite.
LEGACY_SHAPES = {"bean": "blob", "cloud": "clover", "drop": "teardrop", "lean": "arch"}
COLORS = (
    "ink",
    "cream",
    "clay",
    "coral",
    "tangerine",
    "honey",
    "moss",
    "jade",
    "sky",
    "iris",
    "plum",
    "slate",
)
EXPRESSIONS = (
    "neutral",
    "happy",
    "proud",
    "curious",
    "confused",
    "sorry",
    "surprised",
    "focused",
    "sleepy",
    "wink",
)
# Retired user-selectable expressions. Faces are automatic now; the key is kept only so a
# ``PATCH`` from an older client is still validated and then dropped, and so a stored row
# written by an older build parses.
# What the model may tag at the end of a reply.
MOODS = ("neutral", "happy", "proud", "curious", "confused", "sorry", "surprised", "focused")

MOOD_OPEN = "<mood>"
MOOD_CLOSE = "</mood>"
# A reply may pass images on with ``<images>att_… att_…</images>`` just before its mood tag.
IMAGES_OPEN = "<images>"
# Some models close a turn with ``<answer>done</answer>``: a marker for their own harness that
# is never part of the reply. It is held and dropped like the other trailing tags.
ANSWER_OPEN = "<answer>"
ANSWER_CLOSE = "</answer>"
# A reply may react to the message it answers with ``<react>emoji</react>``, like the mood.
REACT_OPEN = "<react>"
REACT_CLOSE = "</react>"
# Longest plausible tags: ``<mood>`` + value + ``</mood>``, ``<images>`` + four ids +
# ``</images>``, a short ``<answer>…</answer>``, and ``<react>`` + emoji + ``</react>``.
# Past their sum a held tail is ordinary text.
MAX_MOOD_CHARS = 64
MAX_IMAGES_CHARS = 96
MAX_ANSWER_CHARS = 48
MAX_REACT_CHARS = 64
# Every name a trailing tag may carry. Models also write the mood itself as the tag
# (``<neutral></neutral>``, ``<curious>curious</curious>``, ``< neutral >…< /neutral >``).
TAG_NAMES = ("mood", "images", "answer", "react", *MOODS)
# The start of a tag as models write it: spaces anywhere, ``/`` before or after the name.
_TAG_START = re.compile(r"<\s*/?\s*([A-Za-z]*)\s*/?\s*(>?)")
# One trailing mood tag in any of the shapes models produce; matched against a reply's end.
# ``lone`` is an opening tag left unclosed (``<focused>`` as the last thing in the reply).
_MOOD_TAIL = re.compile(
    r"(?:<\s*(?P<name>[A-Za-z]+)\s*>(?P<value>[^<\n]*)<\s*/\s*(?P=name)\s*>"
    r"|<\s*(?P<bare>[A-Za-z]+)\s*/\s*>"
    r"|<\s*(?P<lone>[A-Za-z]+)\s*>)\s*\Z",
    re.IGNORECASE,
)
# A reaction tag: ``<react>👍</react>``. Models put it at the end as asked, or mid-reply
# right after the sentence it reacts to; it is never reply content, so every copy is peeled
# wherever it is. The emoji is validated later (an invalid one still vanishes from the reply).
_REACT_TAG = re.compile(
    r"[ \t]*<\s*react\s*>(?P<value>[^<\n]{0,32})<\s*/\s*react\s*>[ \t]*(?:\n(?=\n))?",
    re.IGNORECASE,
)
# The tag written as plain text on its own last line: ``@mood focused``, ``mood: focused``,
# ``Mood：happy``. Only a known mood value counts, so a sentence ending in "mood" is safe.
_MOOD_LINE = re.compile(
    r"(?:\A|\n)[ \t]*@?mood[ \t]*[:：=]?[ \t]*(?P<value>[A-Za-z]+)[ \t]*\Z",
    re.IGNORECASE,
)
# A last line still being streamed that could grow into that plain-text form.
_MOOD_LINE_PREFIX = re.compile(
    r"[ \t]*@?(?:m|mo|moo|mood(?:[ \t]*[:：=]?[ \t]*[A-Za-z]{0,12}[ \t]*)?)",
    re.IGNORECASE,
)
# What older builds appended to a bot's own past replies when replaying them: the model
# learned the shape and wrote the block into new answers. It is never reply content, so any
# copy is dropped, wherever in the text it landed, and the gap it leaves is closed up.
_ECHOED_DIGEST = re.compile(r"\s*<此前用过的工具>.*?</此前用过的工具>\s*", re.DOTALL)


def strip_echoed_digest(text: str) -> str:
    """``text`` without any ``<此前用过的工具>…</此前用过的工具>`` block a model copied in."""
    if "<此前用过的工具>" not in text:
        return text
    return _ECHOED_DIGEST.sub("\n\n", text).strip()


def normalize_shape(shape: str) -> str:
    """A logged shape id, with a retired id mapped to the shape that replaced it."""
    return LEGACY_SHAPES.get(shape, shape)


_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def hex_color(value: str) -> str | None:
    """``value`` as a lowercase ``#rrggbb``, or ``None`` when it is not one."""
    return value.lower() if _HEX.fullmatch(value) else None


def body_color(value: str) -> str | None:
    """A catalog colour as is, a free colour as lowercase ``#rrggbb``, otherwise ``None``."""
    return value if value in COLORS else hex_color(value)


def parse_appearance(raw: str | None) -> dict[str, str] | None:
    """The appearance stored in ``raw``, or ``None`` when it is missing or unusable.

    A retired shape id is mapped, and a stored ``expression`` from an older build is
    ignored. An unusable eye colour is dropped on its own: the body still renders, with
    the automatic eye.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("外观数据不是合法 JSON，已按默认外观处理。")
        return None
    if not isinstance(data, dict):
        logger.warning("外观数据不是对象，已按默认外观处理。")
        return None
    shape = data.get("shape")
    color = data.get("color")
    if not isinstance(shape, str) or not isinstance(color, str):
        logger.warning("外观数据含有未知取值，已按默认外观处理。")
        return None
    shape = normalize_shape(shape)
    color = body_color(color)
    if shape not in SHAPES or color is None:
        logger.warning("外观数据含有未知取值，已按默认外观处理。")
        return None
    parsed = {"shape": shape, "color": color}
    eye = data.get("eye")
    if isinstance(eye, str) and (eye := hex_color(eye)):
        parsed["eye"] = eye
    return parsed


def appearance_json(appearance: dict[str, Any] | None) -> str | None:
    """Serialize an appearance for storage; ``None`` clears it."""
    if appearance is None:
        return None
    stored: dict[str, str] = {
        "shape": normalize_shape(str(appearance["shape"])),
        "color": appearance["color"],
    }
    if appearance.get("eye"):
        stored["eye"] = appearance["eye"]
    return json.dumps(stored, ensure_ascii=False)


class MoodStream:
    """Splits streamed reply text from its trailing ``<images>`` and ``<mood>`` tags.

    The tags are only meaningful at the very end of a reply, so the parser withholds a tail
    that could become one: ``push()`` returns only text that can never turn out to be part
    of a tag, ``visible`` is everything already released (what clients received), and
    ``finish()`` resolves the withheld tail into the final body plus the mood value. The
    image ids found along the way are on ``images`` after ``finish()``.
    """

    def __init__(self) -> None:
        self._released: list[str] = []
        self._hold = ""
        self.images: list[str] = []
        self.reaction: str | None = None
        # The whole reply came inside ``<answer>…</answer>`` (MiMo does this): both go.
        self._wrapped = False
        # A react tag was cut out mid-reply: the blank lines around it are closed up at the end.
        self._cut = False

    def push(self, chunk: str) -> str:
        """Add a streamed chunk; return the text that is safe to send right now."""
        if not chunk:
            return ""
        self._hold += chunk
        if not self._hold:
            return ""
        if not self._wrapped and not self.visible.strip():
            opening = self._hold.lstrip()
            if opening.startswith(ANSWER_OPEN):
                self._hold = opening[len(ANSWER_OPEN) :].lstrip()
                self._wrapped = True
                if not self._hold:
                    return ""
        # Hold from the *earliest* ``<`` that could still become a tag: a later ``<`` may be
        # the closing marker of a tag whose opening has to stay withheld too.
        index = self._hold.find("<")
        while index >= 0:
            tail = self._hold[index:]
            # A finished ``<react>…</react>`` mid-reply is cut out and the text after it goes on.
            whole = _REACT_TAG.match(tail)
            if whole is not None and tail[whole.end() :].strip() and self._cuttable(index):
                self.reaction = whole.group("value").strip() or self.reaction
                head, rest = self._hold[:index], tail[whole.end() :]
                # Inline (`好的 <react>👍</react> 马上办`): one space stays between the words.
                if head.endswith((" ", "\t")) and not rest[:1].isspace():
                    rest = " " + rest
                self._hold = head.rstrip(" \t") + rest
                self._cut = True
                index = self._hold.find("<", index)
                continue
            if _may_be_tag(tail):
                head, self._hold = self._hold[:index], tail
                self._released.append(head)
                return head
            index = self._hold.find("<", index + 1)
        # A last line that may still become ``@mood focused`` waits for the next chunk (or the
        # end); a newline after it proves it was ordinary text.
        cut = self._hold.rfind("\n") + 1
        line = self._hold[cut:]
        if line and len(line) <= MAX_MOOD_CHARS and _MOOD_LINE_PREFIX.fullmatch(line):
            released, self._hold = self._hold[:cut], line
        else:
            released, self._hold = self._hold, ""
        self._released.append(released)
        return released

    @property
    def visible(self) -> str:
        """Text already confirmed as reply body, i.e. what every client has seen."""
        return "".join(self._released)

    def _cuttable(self, index: int) -> bool:
        """Whether a react tag at ``index`` of the hold is reply markup, not a quoted sample.

        A tag written inside a fenced block or an inline code span is prose about the tag
        (someone asking how it works), so the stream leaves it alone and so does the end.
        """
        text = self.visible + self._hold
        return _outside_code(code_spans(text), len(self.visible) + index)

    def finish(self) -> tuple[str, str | None]:
        """The full reply without the tags, plus the mood value when it is valid."""
        if self._hold:
            self._released.append(self._hold)
            self._hold = ""
        earlier = self.reaction
        body, mood, self.images, self.reaction = split_trailing_tags(
            strip_echoed_digest("".join(self._released)), wrapped=self._wrapped
        )
        # A tag already cut out mid-stream counts when the end carried none of its own. The
        # text before it may already be on screen, so the gap it left is closed in the stored body.
        self.reaction = self.reaction or earlier
        if self._cut:
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
        return body, mood


def split_trailing_tags(
    text: str, *, wrapped: bool = False
) -> tuple[str, str | None, list[str], str | None]:
    """Peel every trailing tag off a reply: ``(body, mood, image ids, reaction emoji)``.

    The tags may come in any order, so they are peeled off the end until none is left; the
    last mood written wins, and so does the last reaction. ``wrapped`` is a reply that opened
    with ``<answer>``: its closing ``</answer>`` goes too.
    """
    mood: str | None = None
    images: list[str] = []
    reaction: str | None = None
    while True:
        before = text
        trimmed = text.rstrip()
        if wrapped and trimmed.endswith(ANSWER_CLOSE):
            text = trimmed[: -len(ANSWER_CLOSE)].rstrip()
        text = split_answer_marker(text)
        text, found = split_mood_tag(text)
        mood = mood or found
        if not images:
            text, images = split_images_tag(text)
        text, reacted = split_react_tag(text)
        if reaction is None and reacted is not None:
            reaction = reacted
        if text == before:
            return text, mood, images, reaction


def _may_be_tag(tail: str) -> bool:
    """Whether ``tail`` (starting at a ``<``) could still become a trailing tag.

    The trailing tags may come in any order and follow one another, so a tail that starts as
    one of them stays held for as long as it fits the length budget of all three. A tag
    still being typed is held while its name could grow into a known one.
    """
    if len(tail) > MAX_IMAGES_CHARS + MAX_MOOD_CHARS + MAX_ANSWER_CHARS + MAX_REACT_CHARS:
        return False
    match = _TAG_START.match(tail)
    if match is None:  # pragma: no cover - the pattern matches any "<"
        return False
    name = match.group(1).lower()
    if match.group(2):
        return name in TAG_NAMES
    if match.end() < len(tail):
        return False
    return any(known.startswith(name) for known in TAG_NAMES)


def code_spans(text: str) -> list[tuple[int, int]]:
    """Ranges covered by a fenced block or an inline ``code`` span, in order."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    fence = re.compile(r"```")
    while True:
        opening = fence.search(text, cursor)
        if opening is None:
            break
        closing = fence.search(text, opening.end())
        end = closing.end() if closing is not None else len(text)
        spans.append((opening.start(), end))
        cursor = end
    # Inline spans only outside the fences already found.
    inline = re.compile(r"`[^`\n]+`")
    for match in inline.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        spans.append((match.start(), match.end()))
    return sorted(spans)


def _outside_code(spans: list[tuple[int, int]], index: int) -> bool:
    return not any(start <= index < end for start, end in spans)


def split_react_tag(text: str) -> tuple[str, str | None]:
    """Strip every ``<react>…</react>`` tag; return ``(body, the last emoji or None)``.

    Whatever is inside a tag is returned as the raw emoji — even an invalid one — so the
    tag always disappears from the reply and the caller decides whether to apply it. The
    gap a mid-reply tag leaves is closed up, so no empty line is left where it stood. A tag
    written inside a code span or a fenced block is prose *about* the tag, so it stays.
    """
    spans = code_spans(text)
    found = [match for match in _REACT_TAG.finditer(text) if _outside_code(spans, match.start())]
    if not found:
        return text, None
    pieces: list[str] = []
    cursor = 0
    for match in found:
        pieces.append(text[cursor : match.start()])
        cursor = match.end()
    pieces.append(text[cursor:])
    body = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()
    return body, found[-1].group("value").strip()


def split_answer_marker(text: str) -> str:
    """Strip one trailing ``<answer>…</answer>`` marker; anything else is returned unchanged."""
    trimmed = text.rstrip()
    if not trimmed.endswith(ANSWER_CLOSE):
        return text
    start = trimmed.rfind(ANSWER_OPEN, 0, len(trimmed) - len(ANSWER_CLOSE))
    if start < 0 or len(trimmed) - start > MAX_ANSWER_CHARS:
        return text
    return trimmed[:start].rstrip()


def split_mood_tag(text: str) -> tuple[str, str | None]:
    """Strip one trailing mood tag from ``text``; return ``(body, mood or None)``.

    A model that forgot the angle brackets writes ``@mood focused`` or ``mood: focused`` as
    its last line; that line goes too when it names a known mood.

    ``<mood>happy</mood>`` is the asked-for form. A tag named after the mood itself —
    ``<happy></happy>``, ``<happy>happy</happy>``, ``<happy/>``, with or without spaces —
    means the same, and so does a lone unclosed ``<happy>`` ending the reply. ``<mood>``
    with an unknown value is still stripped, without a mood. A lone tag that is not a mood
    (``List<int>`` at the end of a sentence) is left alone.
    """
    trimmed = text.rstrip()
    window = trimmed[-MAX_MOOD_CHARS:]
    match = _MOOD_TAIL.search(window)
    if match is None:
        return _split_mood_line(trimmed, text)
    lone = match.group("lone")
    if lone is not None:
        if lone.lower() not in MOODS:
            return text, None
        mood: str | None = lone.lower()
    else:
        name = (match.group("name") or match.group("bare")).lower()
        value = (match.group("value") or "").strip().lower()
        if name == "mood":
            mood = value if value in MOODS else None
        elif name in MOODS and value in ("", name):
            mood = name
        else:
            return text, None
    body = trimmed[: len(trimmed) - len(window) + match.start()].rstrip()
    return body, mood


def _split_mood_line(trimmed: str, text: str) -> tuple[str, str | None]:
    """The plain-text form (``@mood focused``) on the reply's last line, when it names a mood."""
    window = trimmed[-MAX_MOOD_CHARS:]
    match = _MOOD_LINE.search(window)
    if match is None or match.group("value").lower() not in MOODS:
        return text, None
    # A window that cut a line in half has no line start to anchor on: only a whole line counts.
    start = len(trimmed) - len(window) + match.start()
    if match.group(0)[:1] != "\n" and start != 0:
        return text, None
    return trimmed[:start].rstrip(), match.group("value").lower()
