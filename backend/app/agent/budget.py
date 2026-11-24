"""Token accounting and request fitting for one model call.

Nothing here touches the store, the clock or the network: it is the arithmetic a request's
size is judged by. The engine measures a request before sending it, compacts the
conversation when the estimate crosses the threshold, and this module is the only place
that decides what "too big" means — so the tests and the run loop agree by construction.

A provider that reports ``usage`` is the source of truth for what a call cost; this estimate
only decides *before* a call whether the next one would fit, so it may be approximate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

# One image is charged flat, whatever its resolution: a screenshot's pixels cost far more
# than its label, and a vision-heavy turn must still trip the budget.
IMAGE_TOKENS = 1_500
# A PDF is charged by its size: the base64 payload is 4/3 of the file, and a page of text
# costs roughly one token per 64 bytes. Clamped, so a scanned 20 MB PDF cannot eat a whole
# window by itself and a one-page note still costs something.
PDF_BYTES_PER_TOKEN = 64
PDF_MIN_TOKENS = 1_500
PDF_MAX_TOKENS = 60_000
# Characters per token for non-CJK text, measured on this workspace's own traffic.
ASCII_CHARS_PER_TOKEN = 3.6
# Everything from CJK radicals up (Han, kana, hangul, fullwidth punctuation) costs one token
# per character.
CJK_START = 0x2E80
# Framing every protocol adds around one message (role, separators).
MESSAGE_OVERHEAD_TOKENS = 4

# Room kept free for the model's own answer, and for the drift between an estimate and the
# provider's real count (tool schema rendering, tokeniser quirks).
OUTPUT_HEADROOM_TOKENS = 20_000
SAFETY_TOKENS = 8_000
# A request is compacted once it passes this share of the usable budget. The answer's room
# and the estimate's drift are already taken out of "usable", so this only has to leave space
# for the turn about to be added; 70 folded a 128K conversation at barely half its window.
COMPACT_PERCENT = 90
# Never plan a request smaller than this, even for an absurdly small declared window.
MIN_USABLE_TOKENS = 2_000

# Tool traces replayed natively: the newest few turns, and the chars they may spend. These
# are the floors a small window gets; a wide window replays far more (see
# :func:`replay_plan`), because 3 turns and 8K chars of a 1M-token coding session is why a
# bot kept re-reading files it had itself written a few turns ago.
TRACE_TURNS = 3
TRACE_RESULT_CHARS = 1_800
TRACE_BUDGET_CHARS = 8_000
# Older turns keep one line per call, at most this many.
TRACE_DIGEST_CHARS = 150
TRACE_DIGEST_LINES = 6
TRACE_OMITTED = "[结果已省略，如需重新获取请再调用一次该工具]"
# How wide a window must be for one more replayed turn, and the caps of the scaling.
TURN_TOKENS = 80_000
MAX_TRACE_TURNS = 12
MAX_TRACE_RESULT_CHARS = 8_000
MAX_TRACE_DIGEST_LINES = 24
# A teammate's tool work in the history is bounded as a whole, spent newest first.
TEAM_DIGEST_FLOOR_CHARS = 2_000
TEAM_DIGEST_CEILING_CHARS = 12_000

# The summariser writes a checkpoint, not an essay; the prompt caps it at 3000 characters.
# A coding session's summary has to carry file paths and artifact state, not just talk.
SUMMARY_MAX_TOKENS = 4_000

# The live result a tool returns to the model in the same turn is cut the same way, head and
# tail, so a shell command's error is never the part that gets dropped.
LIVE_RESULT_CHARS = 12_000
LIVE_RESULT_MARKER = "\n…（结果过长，已截断，共 {total} 字符）\n"
TRACE_RESULT_MARKER = "\n…（工具结果已截断，共 {total} 字符）\n"


def estimate_tokens(text: str) -> int:
    """Approximate tokens in one string: one per CJK character, one per 3.6 others."""
    if not text:
        return 0
    wide = 0
    narrow = 0
    for character in text:
        if ord(character) >= CJK_START:
            wide += 1
        else:
            narrow += 1
    return wide + int(narrow / ASCII_CHARS_PER_TOKEN + 0.5)


def pdf_tokens(data_url: str) -> int:
    """The tokens one inline PDF is charged, from its base64 payload length."""
    payload = data_url.partition(",")[2]
    size = len(payload) * 3 // 4
    return max(PDF_MIN_TOKENS, min(PDF_MAX_TOKENS, size // PDF_BYTES_PER_TOKEN))


def estimate_content(content: Any) -> int:
    """Tokens in one message's content: text, a PDF's documents and a flat image charge.

    Text files arrive as text blocks, so they are counted exactly like any other text.
    """
    if isinstance(content, str):
        return estimate_tokens(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if isinstance(part, str):
            total += estimate_tokens(part)
        elif isinstance(part, dict):
            if part.get("type") == "image_url":
                total += IMAGE_TOKENS
            elif part.get("type") == "file":
                file = part.get("file") if isinstance(part.get("file"), dict) else {}
                total += pdf_tokens(str(file.get("file_data") or ""))
            else:
                total += estimate_tokens(str(part.get("text") or ""))
    return total


def estimate_messages(messages: Sequence[dict[str, Any]]) -> int:
    """Tokens one message list costs, tool calls and results included."""
    total = 0
    for message in messages:
        total += MESSAGE_OVERHEAD_TOKENS + estimate_content(message.get("content"))
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                total += estimate_tokens(str(function.get("name") or ""))
                total += estimate_tokens(str(function.get("arguments") or ""))
    return total


def estimate_tools(tools: Sequence[dict[str, Any]] | None) -> int:
    """Tokens the tool schemas add to every request; they never change within a run."""
    if not tools:
        return 0
    return estimate_tokens(json.dumps(list(tools), ensure_ascii=False))


def estimate_request(
    messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] | None = None
) -> int:
    """Tokens one whole request costs: its messages plus the tool list it carries."""
    return estimate_messages(messages) + estimate_tools(tools)


def usable_budget(context_window: int, max_output: int) -> int:
    """How many tokens a request may spend: window minus the answer, minus a safety margin."""
    headroom = min(max(0, max_output), OUTPUT_HEADROOM_TOKENS)
    return max(context_window - headroom - SAFETY_TOKENS, MIN_USABLE_TOKENS)


def compact_threshold(usable: int) -> int:
    """The estimate above which the conversation is compacted before the next call."""
    return max(1, usable * COMPACT_PERCENT // 100)


@dataclass(frozen=True)
class ReplayPlan:
    """How much of a bot's own tool work its request replays, sized to its window.

    One small window and one huge one must not pay the same: a 1M-token model can afford to
    see its last dozen tool turns in full, and a 32K model cannot. Everything here is a
    function of the window only, so the same conversation always plans the same request.
    """

    turns: int = TRACE_TURNS
    budget_chars: int = TRACE_BUDGET_CHARS
    result_chars: int = TRACE_RESULT_CHARS
    digest_chars: int = TRACE_DIGEST_CHARS
    digest_lines: int = TRACE_DIGEST_LINES
    team_chars: int = TEAM_DIGEST_FLOOR_CHARS


def replay_plan(context_window: int) -> ReplayPlan:
    """The replay allowances for one model's declared window."""
    window = max(0, int(context_window or 0))
    share = min(max(window // 16, TRACE_BUDGET_CHARS), 60_000)
    turns = min(max(window // TURN_TOKENS, TRACE_TURNS), MAX_TRACE_TURNS)
    return ReplayPlan(
        turns=turns,
        budget_chars=share,
        result_chars=min(max(share // 3, TRACE_RESULT_CHARS), MAX_TRACE_RESULT_CHARS),
        digest_chars=min(max(share // 40, TRACE_DIGEST_CHARS), 400),
        digest_lines=min(max(turns * 2, TRACE_DIGEST_LINES), MAX_TRACE_DIGEST_LINES),
        team_chars=min(max(share // 4, TEAM_DIGEST_FLOOR_CHARS), TEAM_DIGEST_CEILING_CHARS),
    )


def keep_turns(context_window: int, *, private: bool) -> int:
    """Turns a compaction leaves verbatim: a wide window keeps more of a real work session."""
    window = max(0, int(context_window or 0))
    base = 4 if private else 6
    ceiling = 12 if private else 24
    return min(max(window // TURN_TOKENS + base, base), ceiling)


def clip_head_tail(text: str, total: int, *, marker: str) -> str:
    """``text`` if it fits, else its head and tail around a marker naming the real length.

    The tail matters: a command's error, a stack trace and a web page's footer all live at
    the end, and the old head-only cut threw exactly that away.
    """
    if total <= 0:
        return ""
    if len(text) <= total:
        return text
    shown = marker.format(total=len(text))
    room = max(0, total - len(shown))
    head = room * 2 // 3
    tail = room - head
    body = text[:head] + shown + (text[-tail:] if tail else "")
    return body


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def _argument_hint(arguments: Any) -> str:
    """The most telling argument a call carried: a query, a url, a command, a name."""
    parsed: Any = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return _one_line(arguments, 40)
    if not isinstance(parsed, dict):
        return _one_line(str(parsed or ""), 40)
    for key in ("query", "url", "command", "text", "name", "title", "path", "selector"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value, 40)
    for value in parsed.values():
        if isinstance(value, str) and value.strip():
            return _one_line(value, 40)
    return ""


def _result_hint(result: str) -> str:
    """The first meaningful sentence of a result, JSON unwrapped, for a one-line digest."""
    text = result
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("summary", "error", "message", "result", "text", "detail"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        else:
            text = json.dumps(parsed, ensure_ascii=False)
    return _one_line(text, 90)


def digest_line(call: dict[str, Any], limit: int = TRACE_DIGEST_CHARS) -> str:
    """One past tool call as a single line: what was asked, and how it ended."""
    name = str(call.get("tool_name") or "")
    hint = _argument_hint(call.get("arguments"))
    ended = _result_hint(str(call.get("result") or ""))
    if not ended:
        ended = "失败" if call.get("status") == "error" else "完成"
    line = f"- {name}({hint}) → {ended}" if hint else f"- {name} → {ended}"
    return _one_line(line, limit)


def _drop_exchange(messages: list[dict[str, Any]], head: int) -> bool:
    """Drop the oldest whole exchange: a tool turn keeps its results, or goes with them."""
    if len(messages) <= head + 1:
        return False
    count = 1
    first = messages[head]
    if first.get("tool_calls") or first.get("role") == "tool":
        while (
            head + count < len(messages)
            and messages[head + count].get("role") == "tool"
        ):
            count += 1
    del messages[head : head + count]
    return True


def fit(
    messages: list[dict[str, Any]],
    *,
    tools: Sequence[dict[str, Any]] | None,
    budget: int,
    head: int = 1,
) -> int:
    """Drop the oldest messages until the request fits; returns the final estimate.

    ``head`` is how many leading messages are untouchable (the system prompt, and the
    summary turn when there is one), and the newest message is never dropped: it is what
    the model is being asked about. Tool calls are always dropped together with their
    results, so the request stays protocol-valid.
    """
    total = estimate_request(messages, tools)
    while total > budget and _drop_exchange(messages, head):
        total = estimate_request(messages, tools)
    # A cut can leave a tool result whose call was dropped: the run of results goes too.
    while len(messages) > head + 1 and messages[head].get("role") == "tool":
        del messages[head]
        total = estimate_request(messages, tools)
    return total
