"""The invisible dispatcher: who answers one group message, decided by one no-tool call.

Every group has a dispatcher. It is not an agent: it is not in the roster, has no tools,
writes nothing into the chat and never appears as an author. The engine hands it the
group's situation (members, the last lines, every live task) and the user's message; it
answers one strict JSON decision that the engine validates and applies.

Its own words live here so the prompt reads as text, not as code: the system part is a
constant (so the provider's prefix cache stays warm), and everything that changes per
message rides in the user turn as a machine-readable ``<dispatch_context>`` block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

KIND_ANSWER = "answer"
KIND_STEER = "steer"
KIND_RESUME = "resume"
KIND_STATUS = "status"
KIND_INVITE = "invite"
KIND_NONE = "none"
KINDS = (KIND_ANSWER, KIND_STEER, KIND_RESUME, KIND_STATUS, KIND_NONE)
# A kind one intent may name; ``none`` is a whole-message decision, never a slice.
INTENT_KINDS = (KIND_ANSWER, KIND_STEER, KIND_RESUME, KIND_STATUS, KIND_INVITE)

# Who may answer in a routed decision: one message should never turn into a panel.
MAX_SPEAKERS = 3
# One user message may carry several asks; more than this is a plan, not a message.
MAX_INTENTS = 6
# One intent's slice of the user's words, as short as the sentence it came from.
INTENT_TEXT_CHARS = 60
# How long the call may take before the engine falls back to the deterministic route. A
# thinking model spends seconds before its first token; routing is worth the wait.
TIMEOUT_SECONDS = 120.0
# The answer is one small JSON object; a thinking model still spends reasoning out of it.
MAX_TOKENS = 800
# How much of one member's instructions the dispatcher reads; enough to know the role.
INSTRUCTION_CHARS = 200
# How much of one older group line the situation block carries.
LINE_CHARS = 120

DISPATCH_SYSTEM = (
    "你是这个群组的「分发员」。你不发言、没有工具，也从不作为成员出现在聊天里；"
    "你只做一件事：看清群里的情况和用户这条消息，决定接下来该由谁接手。\n"
    "\n"
    "只输出一个 JSON 对象，不要写任何解释或 Markdown：\n"
    '{"kind": "answer|steer|resume|status|none", "speakers": ["成员 id"], '
    '"opinions": false, "task_id": null, "reason": "一句中文", "intents": []}\n'
    "\n"
    "intents 是这个用户消息里分成几件事时用的（大多数消息只有一件事，就不用写）：\n"
    '每项 {"text": "这条里属于它的原话或近原话（不超过 60 字）", '
    '"kind": "answer|steer|resume|status|invite", "speakers": ["成员 id"], '
    '"task_id": null, "invite": ["群外成员的 id"]}。\n'
    "- 一条消息最多拆 6 项，每项最多 3 个发言人；按原话出现的顺序排。\n"
    "- 只有真的有多个互不相同的要求时才拆；一句话里的几层意思仍是一项。\n"
    "- 拆了 intents 时，外面的 kind/speakers 可以省略，系统只按 intents 执行。\n"
    "- 例子：「不要用 flutter 了，改 react；让画师辅助匠师」→ 两项：\n"
    '  {"text": "不要用 flutter 了，改 react", "kind": "answer", "speakers": ["军师 id"]}\n'
    '  {"text": "让画师辅助匠师", "kind": "invite", "invite": ["画师 id"], "speakers": ["画师 id"]}\n'
    "\n"
    "kind 的用法：\n"
    "- answer：应该有人回复。speakers 按发言顺序写 1–3 个成员的 id；"
    "opinions 为 true 表示这几个人同时、各自独立地回答（用户要大家各抒己见时）。\n"
    "- steer：用户是对某个正在进行或还在排队的任务补充、纠正要求。task_id 写那件任务。\n"
    "- resume：用户让某个停下或没做完的任务接着做。task_id 写那件任务。"
    "如果有好几件停下的任务都说得通、看不出是哪一件，不要猜：改用 answer，"
    "让最后发言的那位成员在群里问一句是哪一件。\n"
    "- status：用户在问进度（在干啥、做到哪一步、什么情况、进展如何）。"
    "后台会直接用任务数据发一张进度卡，不叫任何成员。\n"
    "- invite：用户点名、或明显想让一个还不在本群的人来做一件事。"
    "invite 写那个人的 id，speakers 写他进群后接手这项的人（通常就是他自己）。"
    "只有 team_outside 里的人可以邀请；不要编 id，也不要因为谁被引用、被同事提到就邀请。\n"
    "- none：只是「好的」「谢谢」「收到」这类不需要回应的话。\n"
    "\n"
    "分派规则：\n"
    "- 用户明确点名的成员必须回答：开头 @ 的、从 @ 选择器里选的、以及被回复那条消息的作者。"
    "这些名字会写在 addressed 里，一个都不能漏掉，可以再加别人。\n"
    "- 正文中间顺带提到某人（例如「按照 @幕僚长 的方案来做」）通常只是引用，不必让他回答。\n"
    "- 用户点了一个不在群里的人：这是一项 invite，不是回答，也不要漏掉。\n"
    "- 主管只负责组织、协调、监督和团队管理；普通的工作请求交给能做这件事的成员，"
    "与组织无关时不要选主管。\n"
    "- 用户的「继续」「接着」是在让刚才被打断的工作接着做：优先让 interrupted 里那位成员继续，"
    "除非用户明确要换人。\n"
    "- 一件任务只能由一个最合适的人接手；不要为了显得热闹而多叫人。\n"
    "- reason 用一句中文说清为什么这样分，会显示给用户。\n"
    "\n"
    "下面 <dispatch_context> 里是这一刻的事实（JSON）：成员、缺席的成员、被点名的人、"
    "最近几行对话、每一件活着的任务，以及用户这条消息本身。"
)


@dataclass(frozen=True)
class Intent:
    """One slice of a user message, and who should take it.

    ``invite`` names team agents outside the group that the slice needs; ``speakers`` names
    who takes the slice once they are in. A message with one ask still parses as an intent
    when the model wrote the list, but the engine may also route a bare decision the old way.
    """

    text: str
    kind: str = KIND_ANSWER
    speakers: tuple[str, ...] = ()
    task_id: str | None = None
    invite: tuple[str, ...] = ()

    def route(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "speakers": list(self.speakers),
            "task_id": self.task_id,
            "invite": list(self.invite),
        }


@dataclass(frozen=True)
class Decision:
    """One validated routing decision, ready for the engine to apply."""

    kind: str
    speakers: tuple[str, ...] = ()
    opinions: bool = False
    task_id: str | None = None
    reason: str = ""
    by: str = "dispatcher"
    intents: tuple[Intent, ...] = ()

    def route(self) -> dict[str, Any]:
        """The ``messages.route`` payload the client reads under the user's bubble.

        A bare decision keeps the exact shape older clients know; ``intents`` only appears
        when the message really carried several asks.
        """
        route = {
            "speakers": list(self.speakers),
            "kind": self.kind,
            "task_id": self.task_id,
            "reason": self.reason,
            "by": self.by,
        }
        if self.intents:
            route["intents"] = [intent.route() for intent in self.intents]
        return route


def context_block(context: Mapping[str, Any]) -> str:
    """The machine-readable situation the dispatcher reads, as one block."""
    return (
        "<dispatch_context>\n"
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        + "\n</dispatch_context>"
    )


def messages(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The dispatcher's whole request: the stable system part and one situation turn."""
    return [
        {"role": "system", "content": DISPATCH_SYSTEM},
        {
            "role": "user",
            "content": "根据下面的情况给出你的 JSON 决定：\n\n" + context_block(context),
        },
    ]


def _read_json(text: str) -> dict[str, Any] | list[Any] | None:
    """The first JSON object or array in an answer, however the model dressed it.

    Fenced blocks, a bare array of intents, and a sentence around the object are all seen
    often enough to tolerate; only prose with no JSON at all is unreadable.
    """
    chunks = [match.group(1) for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    chunks.append(text)
    decoder = json.JSONDecoder()
    for chunk in chunks:
        for index, character in enumerate(chunk):
            if character not in "{[":
                continue
            try:
                value, _ = decoder.raw_decode(chunk[index:])
            except (TypeError, ValueError):
                continue
            if isinstance(value, (dict, list)):
                return value
    return None


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())[:INTENT_TEXT_CHARS]


def _intent(
    data: Any,
    *,
    allowed: set[str],
    invites: set[str],
    task_ids: set[str],
) -> Intent | None:
    """One intent, or ``None`` when the model named something that does not exist."""
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or KIND_ANSWER).strip().lower()
    if kind not in INTENT_KINDS:
        return None
    speakers: list[str] = []
    if isinstance(data.get("speakers"), list):
        for item in data["speakers"]:
            agent_id = str(item or "").strip()
            if agent_id in allowed and agent_id not in speakers:
                speakers.append(agent_id)
    invite: list[str] = []
    if isinstance(data.get("invite"), list):
        for item in data["invite"]:
            agent_id = str(item or "").strip()
            if agent_id in invites and agent_id not in invite:
                invite.append(agent_id)
    task_id = str(data.get("task_id") or "").strip() or None
    if task_id is not None and task_id not in task_ids:
        task_id = None
    if kind == KIND_ANSWER and not speakers:
        return None
    if kind in (KIND_STEER, KIND_RESUME) and task_id is None:
        return None
    if kind == KIND_INVITE and not invite:
        return None
    # An invite's speakers are who takes the slice after the person is in; if the model only
    # named the invitee, that person is the one.
    if kind == KIND_INVITE and not speakers:
        speakers = list(invite)
    return Intent(
        text=_clean_text(data.get("text")),
        kind=kind,
        speakers=tuple(speakers[:MAX_SPEAKERS]),
        task_id=task_id,
        invite=tuple(invite),
    )


def parse(
    text: str,
    *,
    member_ids: Sequence[str],
    task_ids: Sequence[str],
    invite_ids: Sequence[str] = (),
) -> Decision | None:
    """Strictly read one decision; ``None`` when it is unreadable or names nothing real.

    The answer may be the older single decision or an object/array carrying ``intents``.
    Unknown member ids and task ids are dropped rather than trusted: a hallucinated id must
    never route a message to nobody.
    """
    data = _read_json(text)
    if data is None:
        return None
    allowed = set(member_ids)
    invites = set(invite_ids)
    known_tasks = set(task_ids)
    base: dict[str, Any] = data if isinstance(data, dict) else {}
    raw_intents = data if isinstance(data, list) else base.get("intents")
    if isinstance(raw_intents, list) and raw_intents:
        intents = [
            intent
            for raw in raw_intents
            if (
                intent := _intent(
                    raw, allowed=allowed, invites=invites, task_ids=known_tasks
                )
            )
            is not None
        ][:MAX_INTENTS]
        if intents:
            speakers: list[str] = []
            for intent in intents:
                for agent_id in intent.speakers:
                    if agent_id not in speakers:
                        speakers.append(agent_id)
            kinds = {intent.kind for intent in intents}
            return Decision(
                kind=next(iter(kinds)) if len(kinds) == 1 else KIND_ANSWER,
                speakers=tuple(speakers),
                opinions=bool(base.get("opinions")) and len(speakers) > 1,
                reason=" ".join(str(base.get("reason") or "").split())[:120],
                intents=tuple(intents),
            )
    if not isinstance(base, dict):
        return None
    kind = str(base.get("kind") or "").strip().lower()
    if kind not in KINDS:
        return None
    speakers = []
    raw_speakers = base.get("speakers")
    if isinstance(raw_speakers, list):
        for item in raw_speakers:
            agent_id = str(item or "").strip()
            if agent_id in allowed and agent_id not in speakers:
                speakers.append(agent_id)
    task_id = str(base.get("task_id") or "").strip() or None
    if task_id is not None and task_id not in known_tasks:
        task_id = None
    if kind == KIND_ANSWER and not speakers:
        return None
    if kind in (KIND_STEER, KIND_RESUME) and task_id is None:
        return None
    reason = " ".join(str(base.get("reason") or "").split())[:120]
    return Decision(
        kind=kind,
        speakers=tuple(speakers[:MAX_SPEAKERS]),
        opinions=bool(base.get("opinions")) and len(speakers) > 1,
        task_id=task_id,
        reason=reason,
    )


def normalize(decision: Decision, addressed: Sequence[str]) -> Decision:
    """Apply the one rule the model cannot be trusted with: an explicit address answers.

    Addressed members come first, in the order they were addressed; the model's own picks
    follow, deduplicated, so it can add someone but never drop someone the user named. A
    ``none`` that contradicts an address becomes an answer for the addressed members.
    """
    if decision.intents or decision.kind not in (KIND_ANSWER, KIND_NONE):
        return decision
    order: list[str] = [agent_id for agent_id in addressed if agent_id]
    for agent_id in decision.speakers:
        if agent_id not in order:
            order.append(agent_id)
    if decision.kind == KIND_NONE:
        if not order:
            return decision
        return Decision(
            kind=KIND_ANSWER,
            speakers=tuple(order),
            opinions=decision.opinions,
            reason=decision.reason or "用户点了名，需要有人回答。",
        )
    if not order:
        return decision
    limit = max(MAX_SPEAKERS, len([agent_id for agent_id in addressed if agent_id]))
    return Decision(
        kind=KIND_ANSWER,
        speakers=tuple(order[:limit]),
        opinions=decision.opinions,
        reason=decision.reason,
    )


def reasoning_effort(reasoning: bool, levels: Sequence[str]) -> str | None:
    """The effort a dispatch call asks for, or ``None`` when the model names no such level.

    Routing is one tiny JSON object, so the call should think as little as the model allows.
    Only the openai-chat protocol sends this field; the caller passes it there alone.
    """
    if not reasoning:
        return None
    names = {str(level).strip().lower() for level in levels}
    if "low" in names:
        return "low"
    if "minimal" in names:
        return "minimal"
    return None


def fallback(
    addressed: Sequence[str],
    last_reply_mentions: Sequence[str],
    interrupted: str | None,
    last_speaker: str | None,
    first_member: str | None,
) -> Decision | None:
    """The route used when the dispatcher fails, times out or answers nonsense.

    One tier, in this order: the explicitly addressed members; the members the last bot reply
    @-ed (the conversation is already pointed at them); the bot whose work was just
    interrupted; whoever last spoke; the group's first member. A group message is never
    silently dropped; adding the next tier as well would answer a named member's message
    twice. ``None`` only for a group with no members.
    """
    order: list[str] = []
    for agent_id in addressed:
        if agent_id and agent_id not in order:
            order.append(agent_id)
    if order:
        reason = "分发没有给出可用结果，按点名的成员回答。"
    else:
        mentions = [
            agent_id
            for agent_id in last_reply_mentions
            if agent_id and agent_id not in order
        ]
        if mentions:
            order = mentions[:MAX_SPEAKERS]
            reason = "分发没有给出可用结果，交给上一条回复里被 @ 的成员。"
        elif interrupted:
            order = [interrupted]
            reason = "分发没有给出可用结果，交回刚才被打断的成员。"
        elif last_speaker:
            order = [last_speaker]
            reason = "分发没有给出可用结果，交给刚刚在群里说话的成员。"
        elif first_member:
            order = [first_member]
            reason = "分发没有给出可用结果，交给群里的第一位成员。"
    if not order:
        return None
    return Decision(kind=KIND_ANSWER, speakers=tuple(order), reason=reason, by="fallback")


def everyone(members: Sequence[str], reason: str = "@所有人，每位成员各自回答。") -> Decision:
    """``@所有人``: every member answers side by side, decided without a model call."""
    return Decision(
        kind=KIND_ANSWER,
        speakers=tuple(members),
        opinions=len(members) > 1,
        reason=reason,
        by="mention",
    )
