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
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

KIND_ANSWER = "answer"
KIND_STEER = "steer"
KIND_RESUME = "resume"
KIND_STATUS = "status"
KIND_NONE = "none"
KINDS = (KIND_ANSWER, KIND_STEER, KIND_RESUME, KIND_STATUS, KIND_NONE)

# Who may answer in a routed decision: one message should never turn into a panel.
MAX_SPEAKERS = 3
# How long the call may take before the engine falls back to the deterministic route.
TIMEOUT_SECONDS = 6.0
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
    '"opinions": false, "task_id": null, "reason": "一句中文"}\n'
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
    "- none：只是「好的」「谢谢」「收到」这类不需要回应的话。\n"
    "\n"
    "分派规则：\n"
    "- 用户明确点名的成员必须回答：开头 @ 的、从 @ 选择器里选的、以及被回复那条消息的作者。"
    "这些名字会写在 addressed 里，一个都不能漏掉，可以再加别人。\n"
    "- 正文中间顺带提到某人（例如「按照 @幕僚长 的方案来做」）通常只是引用，不必让他回答。\n"
    "- 主管只负责组织、协调、监督和团队管理；普通的工作请求交给能做这件事的成员，"
    "与组织无关时不要选主管。\n"
    "- 一件任务只能由一个最合适的人接手；不要为了显得热闹而多叫人。\n"
    "- reason 用一句中文说清为什么这样分，会显示给用户。\n"
    "\n"
    "下面 <dispatch_context> 里是这一刻的事实（JSON）：成员、缺席的成员、被点名的人、"
    "最近几行对话、每一件活着的任务，以及用户这条消息本身。"
)


@dataclass(frozen=True)
class Decision:
    """One validated routing decision, ready for the engine to apply."""

    kind: str
    speakers: tuple[str, ...] = ()
    opinions: bool = False
    task_id: str | None = None
    reason: str = ""
    by: str = "dispatcher"

    def route(self) -> dict[str, Any]:
        """The ``messages.route`` payload the client reads under the user's bubble."""
        return {
            "speakers": list(self.speakers),
            "kind": self.kind,
            "task_id": self.task_id,
            "reason": self.reason,
            "by": self.by,
        }


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


def parse(
    text: str,
    *,
    member_ids: Sequence[str],
    task_ids: Sequence[str],
) -> Decision | None:
    """Strictly read one decision; ``None`` when it is unreadable or names nothing real.

    Models fence the object or say a word around it, so the first ``{`` … last ``}`` span
    is what is parsed. Unknown member ids and task ids are dropped rather than trusted: a
    hallucinated id must never route a message to nobody.
    """
    start, end = text.find("{"), text.rfind("}")
    if not 0 <= start < end:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        return None
    allowed = set(member_ids)
    speakers: list[str] = []
    raw_speakers = data.get("speakers")
    if isinstance(raw_speakers, list):
        for item in raw_speakers:
            agent_id = str(item or "").strip()
            if agent_id in allowed and agent_id not in speakers:
                speakers.append(agent_id)
    task_id = str(data.get("task_id") or "").strip() or None
    if task_id is not None and task_id not in task_ids:
        task_id = None
    if kind == KIND_ANSWER and not speakers:
        return None
    if kind in (KIND_STEER, KIND_RESUME) and task_id is None:
        return None
    reason = " ".join(str(data.get("reason") or "").split())[:120]
    return Decision(
        kind=kind,
        speakers=tuple(speakers[:MAX_SPEAKERS]),
        opinions=bool(data.get("opinions")) and len(speakers) > 1,
        task_id=task_id,
        reason=reason,
    )


def normalize(decision: Decision, addressed: Sequence[str]) -> Decision:
    """Apply the one rule the model cannot be trusted with: an explicit address answers.

    Addressed members come first, in the order they were addressed; the model's own picks
    follow, deduplicated, so it can add someone but never drop someone the user named. A
    ``none`` that contradicts an address becomes an answer for the addressed members.
    """
    if decision.kind not in (KIND_ANSWER, KIND_NONE):
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


def fallback(
    addressed: Sequence[str],
    last_speaker: str | None,
    first_member: str | None,
) -> Decision | None:
    """The route used when the dispatcher fails, times out or answers nonsense.

    One tier, in this order: the explicitly addressed members, else whoever last spoke in
    the group, else its first member. A group message is never silently dropped; adding
    the next tier as well would answer a named member's message twice. ``None`` only for a
    group with no members.
    """
    order: list[str] = []
    for agent_id in addressed:
        if agent_id and agent_id not in order:
            order.append(agent_id)
    if order:
        reason = "分发没有给出可用结果，按点名的成员回答。"
    else:
        for agent_id in (last_speaker, first_member):
            if agent_id:
                order.append(agent_id)
                break
        reason = "分发没有给出可用结果，交给刚刚在群里说话的成员。"
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
