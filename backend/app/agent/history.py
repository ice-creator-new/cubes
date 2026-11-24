"""The model-facing projection of a conversation's stored messages.

Every rule that decides what a bot sees — visibility, mood and image replay, the quote a
reply carries, the cut-off note, the rolling summary, recent tool traces, the screenshot
budget — lives here as a pure function over the rows the store returns. The run loop passes
who is reading and how wide the budget is; it never mixes history policy with streaming or
routing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import attachments as attachment_rules
from .. import prompts
from ..prompts import CUT_OFF_NOTE, INTERRUPTS_NOTE, SCHEDULED_NOTE
from . import budget as budget_rules

# How much of the quoted message the model sees when the user replies to it.
QUOTE_EXCERPT_CHARS = 300
# Screenshot turns kept as pixels, per label; older ones become a short label instead.
COMPUTER_SHOTS_IN_CONTEXT = 2
COMPUTER_SHOT_LABEL = "[沙盒截图，红圈是鼠标位置]"
COMPUTER_SHOT_DROPPED = "[较早的沙盒截图，已省略]（如需查看可重新截图）"
# An old turn's tool calls, condensed to one line each. They are sent as a separate user-side
# note right after the reply rather than appended to it: a model copies the shape of its own
# past replies, and a digest inside them came back verbatim in new answers.
TRACE_DIGEST_HEADER = (
    "[系统备注：你在上一条回复里用过这些工具，结果已省略；上面这些文件的最新状态见本轮运行提示里的"
    "工作集，别的需要时再调用；不要在回复中复述这段备注]\n"
)
# A teammate's turn carries the same note, so a bot knows what was *done*, not only said.
TEAM_DIGEST_PREFIX = "[系统备注：这条回复下方是"
TEAM_DIGEST_HEADER = (
    TEAM_DIGEST_PREFIX
    + "「{name}」这一轮实际执行的工具动作（结果已省略；文件的当前状态见运行"
    "提示里的工作集）。不要在回复中复述这段备注]\n"
)
# The user's actor id in a message's ``reactions`` list, matching :mod:`app.store.reactions`.
USER_ACTOR_ID = "user"


def _reacted_emojis(message: Mapping[str, Any], actor_id: str) -> list[str]:
    """The emoji ``actor_id`` used on ``message``, in the stored order (a bot holds one)."""
    return [
        str(reaction["emoji"])
        for reaction in message.get("reactions") or []
        if any(str(actor["id"]) == actor_id for actor in reaction.get("actors") or [])
    ]
# How much of one message the summariser is shown, how much of the whole transcript, and how
# long one tool-work line may be: a checkpoint has to keep paths and outcomes, not prose.
SUMMARY_MESSAGE_CHARS = 2_000
SUMMARY_TRANSCRIPT_CHARS = 60_000
SUMMARY_DIGEST_CHARS = 300


def quote_excerpt(content: str) -> str:
    """One message as a Markdown quote block, short enough to keep the context cheap."""
    text = content.strip()
    if len(text) > QUOTE_EXCERPT_CHARS:
        text = text[: QUOTE_EXCERPT_CHARS - 1].rstrip() + "…"
    return "\n".join(f"> {line.strip()}" for line in text.splitlines() if line.strip())


def user_turn_content(message: Mapping[str, Any], by_id: Mapping[str, dict]) -> str:
    """A user's turn as the model sees it: a reply carries the message it quotes."""
    prefix = f"{INTERRUPTS_NOTE}\n" if message.get("interrupts") else ""
    if message.get("schedule") and message.get("kind") == "user":
        prefix += SCHEDULED_NOTE.format(title=message["schedule"]["title"]) + "\n"
    quoted = by_id.get(message.get("reply_to_id") or "")
    if quoted is None or quoted["kind"] != "agent" or not quoted["content"].strip():
        return f"{prefix}{message['content']}"
    excerpt = quote_excerpt(str(quoted["content"]))
    if not excerpt:
        return f"{prefix}{message['content']}"
    return (
        f"{prefix}"
        f"用户在回复「{quoted['author_name']}」的这条消息：\n"
        f"{excerpt}\n\n"
        f"{message['content']}"
    )


def recent_attachments(messages: Sequence[dict], *, vision: bool, pdf: bool) -> set[str]:
    """Ids the next request sends as media: the newest images and PDFs within their budgets.

    A model that cannot read one kind still sees its label, so the id stays addressable; it
    just never pays for pixels or a document it would drop.
    """
    images: list[str] = []
    pdfs: list[str] = []
    if not vision and not pdf:
        return set()
    for message in reversed(messages):
        if message["kind"] == "action":
            continue
        for attachment in reversed(message.get("attachments") or []):
            kind = attachment_rules.kind_of(str(attachment.get("mime") or ""))
            if kind == "image":
                if vision and len(images) < attachment_rules.MAX_IMAGES_IN_CONTEXT:
                    if attachment["id"] not in images:
                        images.append(attachment["id"])
            elif kind == "pdf":
                if pdf and len(pdfs) < attachment_rules.MAX_PDFS_IN_CONTEXT:
                    if attachment["id"] not in pdfs:
                        pdfs.append(attachment["id"])
        images_full = not vision or len(images) >= attachment_rules.MAX_IMAGES_IN_CONTEXT
        pdfs_full = not pdf or len(pdfs) >= attachment_rules.MAX_PDFS_IN_CONTEXT
        if images_full and pdfs_full:
            break
    return set(images) | set(pdfs)


TEXT_FILE_OPEN = "<<<文件内容"
TEXT_FILE_CLOSE = "文件内容>>>"


def text_file_part(attachment: dict, *, attachments_dir: Path) -> str:
    """One text file as a labelled, fenced part; an over-long file says how much was cut."""
    label = attachment_rules.attachment_label(attachment, visible=True)
    content = attachment_rules.read_text(attachments_dir, str(attachment["sha256"]))
    if content is None:
        return f"{label}\n（文件内容读取失败）"
    total = len(content)
    body = content[: attachment_rules.TEXT_FILE_CHARS]
    note = ""
    if total > attachment_rules.TEXT_FILE_CHARS:
        note = (
            f"\n…（文件过长，已截断：共 {total} 字符，这里只显示前 "
            f"{attachment_rules.TEXT_FILE_CHARS} 字符）"
        )
    return f"{label}\n{TEXT_FILE_OPEN}\n{body.strip()}\n{TEXT_FILE_CLOSE}{note}"


def with_attachments(
    text: str, attachments: Sequence[dict], shown: set[str], *, attachments_dir: Path
) -> Any:
    """``text`` alone, or content parts: the text, then one part per attachment.

    Text files are always inline — every model reads text. An image or PDF is inlined only
    when its id is in ``shown`` (the request's own budget, gated by what the model can read);
    otherwise its label says the model cannot read it.
    """
    if not attachments:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text.strip() else []
    for attachment in attachments:
        kind = attachment_rules.kind_of(str(attachment.get("mime") or ""))
        if kind == "text":
            parts.append({"type": "text", "text": text_file_part(attachment, attachments_dir=attachments_dir)})
            continue
        visible = attachment["id"] in shown
        parts.append(
            {
                "type": "text",
                "text": attachment_rules.attachment_label(attachment, visible=visible),
            }
        )
        if not visible:
            continue
        url = attachment_rules.data_url(attachments_dir, attachment["sha256"], attachment["mime"])
        if url is None:
            continue
        if kind == "image":
            parts.append({"type": "image_url", "image_url": {"url": url}})
        else:
            parts.append(
                {
                    "type": "file",
                    "file": {
                        "filename": str(attachment.get("name") or "附件.pdf"),
                        "file_data": url,
                    },
                }
            )
    return parts


# ------------------------------------------------------------------ tool traces


def trace_turns(
    messages: Sequence[dict], traces: Sequence[dict]
) -> dict[str, list[dict]]:
    """Every bot's tool calls grouped by the turn that made them, in conversation order.

    A call's action row carries its own ``seq``: consecutive calls from the same author
    belong to one turn, which is either the assistant message right before them or — when
    the model said nothing between two batches — the first action row of the run. The
    grouping keeps all authors: a bot replays its own calls natively, and the others are
    shown as a digest, which is how a teammate's file write becomes visible at all.
    """
    by_seq: dict[int, list[dict]] = {}
    for row in traces:
        by_seq.setdefault(int(row["seq"]), []).append(row)
    turns: dict[str, list[dict]] = {}
    current: list[dict] | None = None
    author: str | None = None
    anchor: str | None = None
    for message in messages:
        if message["kind"] == "action":
            calls = by_seq.get(int(message["seq"])) or []
            if not calls:
                continue
            who = calls[0].get("author_id")
            if author is not None and author == who and anchor is not None:
                if current is None:
                    current = []
                    turns[anchor] = current
                current.extend(calls)
            else:
                anchor = message["id"]
                author = who
                current = list(calls)
                turns[anchor] = current
            continue
        if message["kind"] == "agent" and message.get("author_id"):
            anchor = message["id"]
            author = message["author_id"]
            current = None
            continue
        anchor = author = None
        current = None
    return turns


def own_turns(turns: Mapping[str, list[dict]], self_id: str) -> dict[str, list[dict]]:
    """Only the turns this bot made: the ones it may replay natively."""
    return {
        anchor: calls
        for anchor, calls in turns.items()
        if calls and calls[0].get("author_id") == self_id
    }


def replay_budget(
    turns: Mapping[str, list[dict]], plan: budget_rules.ReplayPlan | None = None
) -> dict[tuple[str, int], str]:
    """The text each replayed call shows: the newest few turns, under one shared budget.

    The newest results are the ones worth keeping, so the chars are spent from the newest
    call backwards; a call that no longer fits says so instead of disappearing, because a
    ``tool_calls`` block is never sent without its results.
    """
    plan = plan or budget_rules.replay_plan(0)
    anchors = list(turns)[-plan.turns :]
    remaining = plan.budget_chars
    shown: dict[tuple[str, int], str] = {}
    for anchor in reversed(anchors):
        calls = turns[anchor]
        for index in range(len(calls) - 1, -1, -1):
            if remaining < 200:
                shown[(anchor, index)] = budget_rules.TRACE_OMITTED
                continue
            text = budget_rules.clip_head_tail(
                str(calls[index].get("result") or ""),
                min(plan.result_chars, remaining),
                marker=budget_rules.TRACE_RESULT_MARKER,
            )
            shown[(anchor, index)] = text
            remaining -= len(text)
    return shown


def digest_body(
    calls: Sequence[dict], plan: budget_rules.ReplayPlan | None = None
) -> list[str]:
    """One line per call, at most the plan's worth; the rest is one honest count."""
    plan = plan or budget_rules.replay_plan(0)
    lines = [budget_rules.digest_line(call, plan.digest_chars) for call in calls[: plan.digest_lines]]
    omitted = len(calls) - len(lines)
    if omitted > 0:
        lines.append(f"- 另有 {omitted} 次工具调用，结果已省略")
    return lines


def digest_lines(
    calls: Sequence[dict], plan: budget_rules.ReplayPlan | None = None
) -> str:
    """One line per past call of a turn that is too old to replay natively."""
    return TRACE_DIGEST_HEADER + "\n".join(digest_body(calls, plan))


def _digest_turn(
    content: str | None,
    calls: Sequence[dict],
    plan: budget_rules.ReplayPlan | None = None,
) -> list[dict[str, Any]]:
    """An old turn: what the bot said, then its tool calls as a note it did not write."""
    turn: list[dict[str, Any]] = []
    if content:
        turn.append({"role": "assistant", "content": content})
    turn.append({"role": "user", "content": digest_lines(calls, plan)})
    return turn


def team_digests(
    turns: Mapping[str, list[dict]],
    names: Mapping[str, str],
    plan: budget_rules.ReplayPlan | None = None,
) -> dict[str, str]:
    """A compact note per teammate turn, newest first until the request's allowance is spent.

    Without this a member's history said「[匠师] 首页改好了」while every file it wrote stayed
    invisible, so the next bot re-read the whole folder to find out what had changed.
    """
    plan = plan or budget_rules.replay_plan(0)
    remaining = plan.team_chars
    notes: dict[str, str] = {}
    for anchor in reversed(list(turns)):
        calls = turns[anchor]
        if not calls:
            continue
        body = digest_body(calls, plan)
        text = "\n".join(body)
        if len(text) > remaining:
            continue
        remaining -= len(text)
        name = names.get(str(calls[0].get("author_id") or ""), "队友")
        notes[anchor] = TEAM_DIGEST_HEADER.format(name=name) + text
    return notes


def _trace_turn(
    content: str | None,
    calls: Sequence[dict],
    replay: Mapping[tuple[str, int], str],
    anchor: str,
    replay_anchors: Sequence[str],
) -> list[dict[str, Any]]:
    """One past turn's tool calls: natively when recent, as one line each when it is old."""
    if anchor not in replay_anchors:
        return _digest_turn(content, calls)
    turn: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": content if content else None,
            "tool_calls": _tool_calls_block(calls),
        }
    ]
    for index, call in enumerate(calls):
        turn.append(
            {
                "role": "tool",
                "tool_call_id": str(call.get("tool_call_id") or ""),
                "content": replay[(anchor, index)],
            }
        )
    return turn


def _tool_calls_block(calls: Sequence[dict]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(call.get("tool_call_id") or ""),
            "type": "function",
            "function": {
                "name": str(call.get("tool_name") or ""),
                "arguments": str(call.get("arguments") or "{}"),
            },
        }
        for call in calls
    ]


# ------------------------------------------------------------------ projection


def to_chat_history(
    messages: list[dict],
    *,
    self_id: str,
    vision: bool,
    pdf: bool,
    attachments_dir: Path,
    traces: Sequence[dict] = (),
    since_seq: int = 0,
    plan: budget_rules.ReplayPlan | None = None,
) -> list[dict[str, Any]]:
    """The conversation as one bot's request sees it: its own turns, others as user turns.

    ``since_seq`` drops everything already folded into a rolling summary, while quotes and
    trace grouping still see the whole conversation. The newest turns of this bot replay
    their tool calls natively — how many, and how much of each result, is ``plan``'s call,
    which the run sizes to the model's window — and every teammate turn carries a one-line
    digest of what it actually did. ``vision`` and ``pdf`` say what the answering model can
    take: images as pixels, PDFs as documents. Text files ride along for every model.
    """
    history: list[dict[str, Any]] = []
    plan = plan or budget_rules.replay_plan(0)
    # One lookup for the whole conversation: a quoted message never costs another query.
    by_id = {message["id"]: message for message in messages}
    shown = recent_attachments(messages, vision=vision, pdf=pdf)
    every_turn = trace_turns(messages, traces)
    kept = {
        anchor: calls
        for anchor, calls in every_turn.items()
        if by_id.get(anchor) is not None
        and by_id[anchor]["seq"] > since_seq
        and calls
    }
    turns = own_turns(kept, self_id)
    replay = replay_budget(turns, plan)
    replay_anchors = list(turns)[-plan.turns :]
    names = {str(message.get("author_id") or ""): str(message["author_name"]) for message in messages}
    teammates = team_digests(
        {anchor: calls for anchor, calls in kept.items() if calls[0].get("author_id") != self_id},
        names,
        plan,
    )
    # A teammate's calls usually land before the reply that concludes the turn (the action
    # rows are stored while it works, its message once it speaks), so the note waits for that
    # reply instead of interrupting the action rows; the position is what keeps it readable.
    deferred: dict[str, list[str]] = {}
    for index, message in enumerate(messages):
        kind = message["kind"]
        if kind in ("notice", "task"):
            # A system note and a task card are for the user's eyes; the model does not need
            # them in context (a card stands for work the thread already shows).
            continue
        if kind == "action":
            calls = turns.get(message["id"])
            if calls and message["seq"] > since_seq:
                history.extend(_trace_turn(None, calls, replay, message["id"], replay_anchors))
                continue
            note = teammates.get(message["id"])
            if note and message["seq"] > since_seq:
                owner = str((kept.get(message["id"]) or [{}])[0].get("author_id") or "")
                following = _next_speaker(messages, index)
                if following is not None and following["kind"] == "agent" and following.get("author_id") == owner:
                    deferred.setdefault(owner, []).append(note)
                else:
                    # This turn ended on a tool call; its work is all the history will see.
                    history.append({"role": "user", "content": note})
            continue
        if message["seq"] <= since_seq:
            continue
        attachments = message.get("attachments") or []
        # A reply cut off mid-way still happened: the model sees what it had said so far.
        # A reply still streaming belongs to another live turn and stays out.
        if message["status"] not in ("complete", "interrupted"):
            continue
        if not message["content"].strip() and not attachments:
            continue
        cut_off = f"\n\n{CUT_OFF_NOTE}" if message["status"] == "interrupted" else ""
        if kind == "user":
            text = user_turn_content(message, by_id)
            reacted = _reacted_emojis(message, self_id)
            if reacted:
                text = f"{text}（你已贴了表情 {''.join(reacted)}）"
            history.append(
                {"role": "user", "content": with_attachments(text, attachments, shown, attachments_dir=attachments_dir)}
            )
        elif message["author_id"] == self_id:
            # Replay the agent's own tags: a history of untagged replies teaches the model
            # to drop them, so its past answers must keep showing the format. Its own
            # Images stay ids; assistant turns carry no media blocks on most services.
            content = message["content"] + cut_off
            if attachments:
                content = f"{content}\n\n{attachment_rules.images_tag([i['id'] for i in attachments])}"
            if message.get("mood"):
                content = f"{content}\n\n<mood>{message['mood']}</mood>"
            calls = turns.get(message["id"]) or []
            if calls and message["id"] in replay_anchors:
                history.extend(
                    _trace_turn(content, calls, replay, message["id"], replay_anchors)
                )
            elif calls:
                history.extend(_digest_turn(content, calls, plan))
            else:
                history.append({"role": "assistant", "content": content})
            # A reply the user reacted to is followed by a short note, like the trace digest.
            user_reacted = _reacted_emojis(message, USER_ACTOR_ID)
            if user_reacted:
                history.append(
                    {
                        "role": "user",
                        "content": (
                            f"[系统备注：用户给你上面这条回复贴了表情 {''.join(user_reacted)}，"
                            "不要在回复中复述这段备注]"
                        ),
                    }
                )
        else:
            text = f"[{message['author_name']}] {message['content']}{cut_off}"
            history.append(
                {"role": "user", "content": with_attachments(text, attachments, shown, attachments_dir=attachments_dir)}
            )
            # What this teammate did, not only what it said: a file it wrote and a command
            # it ran are what the next bot needs before touching the same artifact again.
            notes = [teammates[message["id"]]] if message["id"] in teammates else []
            notes.extend(deferred.pop(str(message.get("author_id") or ""), []))
            for note in notes:
                history.append({"role": "user", "content": note})
    # A deferred note whose reply never reached the request (still streaming, folded away)
    # is still true work; it goes at the end rather than disappearing.
    for notes in deferred.values():
        for note in notes:
            history.append({"role": "user", "content": note})
    return history


def _next_speaker(messages: Sequence[dict], index: int) -> dict | None:
    """The next message that actually says something after ``messages[index]``."""
    for later in messages[index + 1 :]:
        if later["kind"] in ("action", "notice", "task"):
            continue
        if later["status"] not in ("complete", "interrupted"):
            continue
        if not str(later["content"]).strip() and not later.get("attachments"):
            continue
        return later
    return None


def summary_transcript(
    messages: Sequence[dict],
    *,
    traces: Sequence[dict] = (),
    limit: int = SUMMARY_TRANSCRIPT_CHARS,
) -> str:
    """The rows a summariser reads, as plain lines, newest kept when the whole is too long.

    Every author's tool work is kept, one line per call: a summary that folded a coding
    session's file writes and test runs away is exactly why the next bot went looking for
    files that had already been built.
    """
    lines: list[str] = []
    turns = trace_turns(messages, traces)
    for message in messages:
        kind = message["kind"]
        if kind in ("action", "notice", "task"):
            continue
        if not message["content"].strip():
            continue
        who = "用户" if kind == "user" else str(message["author_name"])
        text = " ".join(str(message["content"]).split())
        if len(text) > SUMMARY_MESSAGE_CHARS:
            text = f"{text[: SUMMARY_MESSAGE_CHARS - 1]}…"
        lines.append(f"[{who}] {text}")
        for call in turns.get(message["id"]) or []:
            lines.append(budget_rules.digest_line(call, SUMMARY_DIGEST_CHARS))
    total = 0
    kept: list[str] = []
    for line in reversed(lines):
        total += len(line) + 1
        if total > limit:
            break
        kept.append(line)
    return "\n".join(reversed(kept))


# ------------------------------------------------------------------ request shaping


def visible(messages: Sequence[dict]) -> list[dict]:
    """The rows the model actually reads: finished turns that say something."""
    return [
        message
        for message in messages
        if message["kind"] not in ("action", "notice", "task")
        and message["status"] in ("complete", "interrupted")
        and (str(message["content"]).strip() or message.get("attachments"))
    ]


def keep_boundary(
    messages: Sequence[dict], *, keep: int, since_seq: int = 0
) -> int | None:
    """The last message a compaction may fold away, keeping the newest ``keep`` turns.

    ``None`` means there is nothing to summarise: a conversation shorter than the window it
    must keep verbatim is never compacted, so a short burst of messages after a compaction
    does not trigger another one.
    """
    pending = [message for message in visible(messages) if message["seq"] > since_seq]
    if len(pending) <= keep:
        return None
    boundary = int(pending[-keep - 1]["seq"])
    return boundary if boundary > since_seq else None


def summary_head(messages: Sequence[dict[str, Any]]) -> int:
    """How many leading messages of a request are never trimmed: system, plus the summary."""
    if len(messages) > 1 and messages[0].get("role") == "system":
        second = messages[1]
        if second.get("role") == "user" and text_of(second.get("content")).startswith("<摘要>"):
            return 2
    return 1


def summary_message(summary: str) -> dict[str, Any]:
    """The rolling summary as the user turn that stands in for the history it covers."""
    return {"role": "user", "content": prompts.summary_turn(summary)}


def attach_runtime(
    messages: list[dict[str, Any]], *, moment: Any, timezone: str, hints: Sequence[str] = ()
) -> None:
    """Put this turn's clock and live state at the end of the newest user message.

    Only a turn whose last message is a user turn gets it: mid tool-loop the newest message
    is a tool result, and the clock was already delivered in the request that opened the
    turn — inserting it between a tool call and its result would break the protocol.
    """
    if not messages:
        return
    last = messages[-1]
    if last.get("role") != "user":
        return
    block = prompts.runtime_block(moment=moment, timezone=timezone, hints=hints)
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = content + block
        return
    if isinstance(content, list):
        content.append({"type": "text", "text": block.strip()})


def drop_old_screenshots(
    messages: list[dict[str, Any]], *, keep: Mapping[str, int] | int = COMPUTER_SHOTS_IN_CONTEXT
) -> None:
    """Keep pixels only for the newest few screenshot turns of each kind, across the request.

    ``keep`` is either a mapping of screenshot label to how many turns keep their pixels (a
    tool with its own retention passes its count there) or one count for the plain sandbox
    label. Pixels that are dropped stay in the transcript, which is never rewritten.
    """
    counts = {COMPUTER_SHOT_LABEL: keep} if isinstance(keep, int) else dict(keep)
    seen: dict[str, int] = {}
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list) or not content:
            continue
        first = content[0]
        if not isinstance(first, dict):
            continue
        label = first.get("text")
        if label not in counts:
            continue
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > counts[label]:
            message["content"] = COMPUTER_SHOT_DROPPED


def text_of(content: Any) -> str:
    """A message's text, whether it is a plain string or a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""
