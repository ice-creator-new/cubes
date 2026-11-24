"""Prompt assembly as pure functions: data in, one string out.

The engine reads the rows, resolves the model and collects the live hints; everything that
decides *what the model is told* lives here. Nothing in this module touches the store, the
clock or the network, so a prompt change is reviewable as a diff of arguments and literals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .text import (
    ASK_OTHERS_RULE,
    ASK_USER_RULE,
    GROUP_ASK_RULE,
    GROUP_COORDINATE_RULE,
    MEMBER_DIRECT_SYSTEM,
    MEMBER_PRIVATE_SYSTEM,
    MEMBER_SYSTEM,
    MEMBER_THREAD_SYSTEM,
    MEMORY_EXTRACT_SYSTEM,
    MEMORY_MORE,
    MEMORY_GROUP_TITLE,
    MEMORY_OWN_TITLE,
    MEMORY_RELATED_TITLE,
    MEMORY_TEAM_TITLE,
    MEMORY_USER_TITLE,
    MOOD_RULE,
    REACT_RULE,
    PRIVATE_NOTIFY_RULE,
    SKILLS_SAFETY_RULE,
    SKILLS_USE_RULE,
    START_TASK_RULE,
    SUBTASK_RULE,
    SUMMARY_BACKGROUND,
    SUMMARY_MERGE,
    SUMMARY_PROMPT,
    SUMMARY_TRANSCRIPT,
    SUPERVISOR_SYSTEM,
    UPDATE_AGENT_RULE,
    UPDATE_SELF_RULE,
    WORKFLOW_SYSTEM,
)

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# How much of one Chinese sentence a directory line keeps.
ABSTRACT_CHARS = 60
# Auto-recall is a hint beside the resident block, not a second block.
RELATED_CHARS = 400
# How much of the finished exchanges the extraction call reads, and how much of it one user
# turn may take: the user's own words are where facts about them live, so they come first.
EXTRACT_TRANSCRIPT_CHARS = 6_000
EXTRACT_USER_CHARS = 600

Summarize = Callable[[str], str]


def abstract(text: str, limit: int = ABSTRACT_CHARS) -> str:
    """One line of text, whitespace collapsed and cut on a character boundary."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}…"


def now_line(moment: datetime) -> str:
    """The local time sentence a prompt states, weekday included."""
    return f"{moment:%Y-%m-%d %H:%M} {WEEKDAYS[moment.weekday()]}"


def _without_mood(system: str) -> str:
    """A prompt without its final mood rule, so more instructions can go before it."""
    return system[: -len(MOOD_RULE)] if system.endswith(MOOD_RULE) else system


def _joined(lines: list[str], empty: str) -> str:
    return "\n".join(lines) if lines else empty


def team_directory(
    supervisor: Mapping[str, Any] | None,
    members: Sequence[Mapping[str, Any]],
    summarize: Summarize = abstract,
) -> str:
    """Every bot the supervisor may address, with the id a tool call must use."""
    lines: list[str] = []
    if supervisor is not None:
        lines.append(f"- 主管：{supervisor['name']}（id {supervisor['id']}）")
    for agent in members:
        lines.append(
            f"- 成员：{agent['name']}（id {agent['id']}）：{summarize(agent['instructions'])}"
        )
    return _joined(lines, "- （团队中暂时只有主管）")


def group_directory(
    groups: Sequence[Mapping[str, Any]],
    name_of: Callable[[str], str],
    summarize: Summarize = abstract,
) -> str:
    """Every group and who is in it, so the supervisor can pick one by id."""
    lines: list[str] = []
    for conversation in groups:
        members = "、".join(f"{name_of(member_id)}（{member_id}）" for member_id in conversation["member_ids"])
        lines.append(
            f"- {conversation['title']}（id {conversation['id']}）："
            f"成员 {members}；目的：{summarize(conversation['purpose'])}"
        )
    return _joined(lines, "- （暂时没有群组）")


def conversation_line(*, kind: str, title: str, purpose: str, peers: Sequence[str] = ()) -> str:
    """One sentence telling the bot which conversation it is speaking in."""
    if kind == "background":
        return (
            f"这是{title}的后台工作区：用户看不到这里，也看不到你的工具调用和"
            "中间步骤。你最后的回复会作为这次定时任务的总结，"
            f"由任务主人发到会话「{purpose}」里给用户看。"
        )
    if kind == "direct":
        return f"这是你与用户的私聊「{title}」；你的最终答复会直接展示给用户。"
    if kind == "private":
        return (
            f"这是你与「{'、'.join(peers)}」的私聊；用户只能看到你们私下聊过，"
            "看不到你们具体说了什么。"
        )
    if kind == "workflow":
        return (
            f"这是工作流「{title}」的运行页面（目的：{purpose}）；"
            "用户在这里看着你一步一步做，每一步、每次工具调用都看得见。"
        )
    return f"这是群组「{title}」（目的：{purpose}），用户在这个群组里下达目标。"


def team_roster(others: Sequence[Mapping[str, Any]]) -> str:
    """One line per other bot, so a member can address a private message."""
    lines = [
        f"- {other['name']}（id {other['id']}）" + ("：主管" if other["kind"] == "supervisor" else "")
        for other in others
    ]
    return _joined(lines, "- （暂时没有其他成员）")


def roster_lines(
    roster: Sequence[Mapping[str, Any]],
    self_id: str,
    summarize: Summarize = abstract,
) -> str:
    """The group taken part in, as one line per member with its duty summarised."""
    lines: list[str] = []
    for member in roster:
        marker = "（你自己）" if member["id"] == self_id else ""
        lines.append(f"- {member['name']}{marker}：{summarize(str(member['instructions']))}")
    return _joined(lines, "- （暂时没有其他成员）")


def task_instruction(task: str, supervisor_name: str = "负责人") -> str:
    """The supervisor handed this member a job: state it after the group history."""
    return (
        f"{supervisor_name}指派给你的任务：{task}\n\n"
        "请基于以上群组历史完成这个任务，直接输出你的结论。"
    )


def memory_sections(
    *,
    about_user: Sequence[str] = (),
    own: Sequence[str] = (),
    team: Sequence[str] = (),
    group: Sequence[str] = (),
    limit: int,
) -> tuple[list[tuple[str, list[str]]], int]:
    """The resident sections that fit, and how many rows did not.

    Rows arrive best first (pinned, then newest). A row that does not fit is dropped
    whole — never cut in half. Headers and the closing note are not counted against
    ``limit``; the fact lines are.
    """
    sections: list[tuple[str, list[str]]] = []
    used = 0
    dropped = 0
    for title, rows in (
        (MEMORY_USER_TITLE, about_user),
        (MEMORY_OWN_TITLE, own),
        (MEMORY_TEAM_TITLE, team),
        (MEMORY_GROUP_TITLE, group),
    ):
        kept: list[str] = []
        for row in rows:
            if used + len(row) + 3 > limit:
                dropped += 1
                continue
            kept.append(row)
            used += len(row) + 3
        if kept:
            sections.append((title, kept))
    return sections, dropped


def memory_block(
    *,
    about_user: Sequence[str] = (),
    own: Sequence[str] = (),
    team: Sequence[str] = (),
    group: Sequence[str] = (),
    related: Sequence[str] = (),
    limit: int,
) -> str:
    """The ``<memory>`` block one bot carries, measured against ``limit`` whole lines.

    ``related`` is a separate, smaller allowance: the rows a search for the current
    message turned up, minus any row the block already shows. The result depends only on
    these arguments, so one memory set yields one block.
    """
    sections, dropped = memory_sections(
        about_user=about_user, own=own, team=team, group=group, limit=limit
    )
    shown = {row for _, rows in sections for row in rows}
    lines: list[str] = []
    for title, rows in sections:
        lines.append(f"{title}：")
        lines.extend(f"- {row}" for row in rows)
    related_lines: list[str] = []
    related_used = 0
    for row in related:
        if row in shown:
            continue
        if related_used + len(row) + 3 > RELATED_CHARS:
            break
        related_lines.append(row)
        related_used += len(row) + 3
    if related_lines:
        lines.append(f"{MEMORY_RELATED_TITLE}：")
        lines.extend(f"- {row}" for row in related_lines)
    if not lines:
        return ""
    if dropped:
        lines.append(MEMORY_MORE.format(count=dropped))
    return "\n\n<memory>\n" + "\n".join(lines) + "\n</memory>"


def skills_block(entries: Sequence[tuple[str, str]]) -> str:
    """The ``<skills>`` block one bot carries, or "" when it has no available skill.

    One line per skill, then the use rule and the safety boundary. Built only from the
    stored rows, so it is byte-stable while skills don't change.
    """
    if not entries:
        return ""
    lines = [f"- {name}：{description}" for name, description in entries]
    return (
        "\n\n<skills>\n"
        + "\n".join(lines)
        + "\n"
        + SKILLS_USE_RULE
        + "\n"
        + SKILLS_SAFETY_RULE
        + "\n</skills>"
    )


def memory_extract_messages(
    *,
    turns: Sequence[tuple[str, Sequence[tuple[str, str]]]],
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The one extraction call: the finished exchanges, then the rows they might duplicate.

    ``turns`` is ``[(user_text, [(bot_name, reply_text), …]), …]`` oldest first. Every user
    line is kept (each capped at :data:`EXTRACT_USER_CHARS`); the replies share what is left
    of :data:`EXTRACT_TRANSCRIPT_CHARS` evenly. Candidates are labelled with their id so an
    update can name the row it replaces.
    """
    users = [f"用户：{abstract(user, EXTRACT_USER_CHARS)}" for user, _ in turns]
    reply_count = sum(len(replies) for _, replies in turns)
    spare = max(0, EXTRACT_TRANSCRIPT_CHARS - sum(len(line) + 1 for line in users))
    share = spare // reply_count if reply_count else 0
    pieces: list[str] = []
    for user_line, (_, replies) in zip(users, turns):
        pieces.append(user_line)
        for name, content in replies:
            if share > len(name) + 1:
                pieces.append(f"{name}：{abstract(content, share - len(name) - 1)}")
    lines = ["这段对话：", *pieces, "", "候选记忆（需要时用 update 或 delete 指向这些 id）："]
    if candidates:
        for row in candidates:
            lines.append(
                f"- {row['id']} [{row['scope']}] {row['topic']}：{abstract(str(row['content']), 200)}"
            )
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("请输出 JSON 数组。")
    return [
        {"role": "system", "content": MEMORY_EXTRACT_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


def mention_instruction(author_name: str, content: str) -> str:
    """This member was mentioned: quote the message it must answer."""
    return f"「{author_name}」@ 了你：{content}\n\n请直接回复这条消息。"


def _asked_by(author: str | None) -> str:
    return "用户" if author is None else f"「{author}」"


def turn_instruction(content: str, author: str | None = None) -> str:
    """A message named this member, or a bot's group question gave it a turn.

    ``author`` is the bot that brought the question here (``ask_group``); None is the user.
    """
    asker = "需要问用户时直接称「你」，不要写 @用户。" if author is None else f"你的发言就是在回答{_asked_by(author)}。"
    return (
        f"{_asked_by(author)}在群里说：{content}\n\n"
        "现在轮到你在群里发言。像在群聊里说话一样自然：前面有同事说过的，可以接着说、同意或反驳，"
        "只补充他们没提到的；不要重复别人说过的内容，也不要客套开场。说完就停，篇幅尽量短。"
        + asker
    )


def opinion_instruction(content: str, author: str | None = None) -> str:
    """Several members answer the same message side by side; this one gives its own view."""
    asker = "需要问用户时直接称「你」，不要写 @用户。" if author is None else f"你的发言就是在回答{_asked_by(author)}。"
    return (
        f"{_asked_by(author)}在群里说：{content}\n\n"
        "几位同事正在同时、各自独立地回答这条消息，你看不到他们的回答，他们也看不到你的。"
        "从你自己的职责出发给出你的看法或方案，不要猜别人会说什么，也不要客套开场。说完就停，篇幅尽量短。"
        + asker
    )


def continue_instruction(previous_steps: int | None = None) -> str:
    """「继续」 in a direct chat resumes what the last run stopped in the middle of."""
    stopped = (
        f"上一轮在第 {previous_steps} 步停下的。"
        if previous_steps
        else "上一轮停在了步数上限。"
    )
    return (
        "（这是「继续」：你在接着上一次没做完的工作。"
        f"{stopped}先看你最后一条回复做到哪里，从那里接着做，已经做完的不要重做；"
        "完成后说明这次完成了什么、还差什么。）"
    )


def turn_limited_notice(name: str, steps: int) -> str:
    """The quiet chat line under a bot whose turn ran into its step budget."""
    return f"「{name}」这一轮用了 {steps} 步，先停在这里。发「继续」接着做。"


def run_limited_notice(reason: str | None, steps: int) -> str:
    """The quiet chat line under a run that ended at a budget.

    ``reason`` is the error the run ended with. It already names what ran out (the sandbox's
    screen steps, the model calls, the tools), so the line repeats it rather than only the
    step count; anything missing or unrecognisable falls back to the count.
    """
    detail = (reason or "").strip()
    # The engine's reasons close with 「任务已结束。」; stopping is what that means here, and the
    # line ends with how to pick the work back up instead.
    for tail in ("，任务已结束。", "，任务已结束", "。", "."):
        if detail.endswith(tail):
            detail = detail[: -len(tail)]
            break
    if not detail:
        return f"这次任务用了 {steps} 步，先停在这里。发「继续」接着做。"
    return f"{detail}，先停在这里。发「继续」接着做。"


def resume_instruction(task: str, rounds: int) -> str:
    """A member picks a group task up again: after a stop, a failure, or the user's follow-up."""
    return (
        f"你在继续你之前在这个群里接下的任务（第 {rounds} 轮）：{task}\n\n"
        "看群里最新的消息（用户可能补充或纠正了要求），在你上次的基础上接着做，"
        "已经做完的部分不用重做，直接给出这一轮的结果。"
    )


def transfer_instruction(task: str, giver: str, note: str = "") -> str:
    """A member handed its whole task to this one with ``group_transfer``."""
    extra = f"\n「{giver}」交接时说：{note}" if note else ""
    return (
        f"「{giver}」把一个任务整个交给了你，现在由你负责到底：{task}{extra}\n\n"
        "基于群里的历史完成它，直接在群里给出结果。"
    )


def thread_task_instruction(request: str, title: str) -> str:
    """The first turn of a task thread: what the group asked for, and what ends it.

    Only used while the thread is still empty; every later turn reads its own history.
    """
    return (
        f"群里交给你的任务：「{title}」\n用户的原话：{request}\n\n"
        "现在开始做。可以先查资料、改文件、跑命令；做完给出要发到群里的那条结论。"
    )


def thread_work_instruction(title: str, content: str) -> str:
    """The first turn of a thread opened by a user's message: what was said, and what ends it.

    ``content`` is the message as the model reads it (:func:`app.agent.history.user_turn_content`),
    so a reply keeps the quote of the message it answers.
    """
    return (
        f"群里交给你的任务：「{title}」。这条是源头：\n\n{content}\n\n"
        "现在开始做。可以先查资料、改文件、跑命令；做完给出要发到群里的那条结论。"
    )


def thread_group_view(title: str, recent: Sequence[str]) -> str:
    """The compact view of the group a task thread belongs to: what was just said there.

    It rides with the turn's live hints (never the system prompt), so it can change with
    every turn without invalidating the provider's prefix cache.
    """
    if not recent:
        return ""
    lines = [f"<group_view>", f"「{title}」群里最近的话（你不必回复这些）："]
    lines.extend(recent)
    lines.append("</group_view>")
    return "\n".join(lines)


def merge_note(title: str, request: str) -> str:
    """A queued task the user folded into the one already running."""
    text = (request or title).strip()
    return (
        f"用户把另一件事并到你现在做的这件里，一起做：「{title}」\n{text}\n\n"
        "把这件事也算进你正在做的结果里，不用另起一份。"
    )


def task_result_line(name: str, title: str) -> str:
    """The group's result when a task's turn ended without words to send."""
    return f"「{name}」把「{title}」做完了，没有别的补充。"


def task_stopped_line(title: str, status: str) -> str:
    """The card's own text when a task did not finish: stopped by the user, or failed."""
    return f"「{title}」已停止" if status == "stopped" else f"「{title}」没能做完"


def group_rules_block(rules: str) -> str:
    """A group's standing rules, as written by the user; "" when the group has none."""
    text = rules.strip()
    if not text:
        return ""
    return f"\n\n<group_rules>\n这个群的规则（用户定的，在这个群里发言都要遵守）：\n{text}\n</group_rules>"


def closing_instruction(content: str, spoke: Sequence[str], author: str | None = None) -> str:
    """The supervisor speaks last after a group discussion (``ask_group``) let members answer."""
    names = "、".join(f"「{name}」" for name in spoke)
    asker = "需要问用户时直接称「你」，不要写 @用户。" if author is None else ""
    return (
        f"{_asked_by(author)}在群里说：{content}\n\n"
        f"{names}已经在群里发过言了。作为主管，你最后再说一句：可以补一个大家没提到的要点、"
        "点出分歧，或者给一个简短的建议。一到三句话，不要逐个复述大家说了什么，不要写总结清单。"
        + asker
    )


def supervisor_system(
    *,
    name: str,
    instructions: str,
    team: str,
    groups: str,
    conversation: str,
    private_peer: str | None = None,
    in_group: bool = False,
    react: bool = False,
    memory: str = "",
    skills: str = "",
    workflow: str = "",
    hints: Sequence[str] = (),
) -> str:
    """The supervisor's system prompt: its duties, the directories, then the live hints.

    Nothing per-turn belongs here: the clock and the live hints go into the newest user turn
    (:func:`runtime_block`), so this string — and the tool list beside it — stays identical
    between turns and the provider's prefix cache can hit. What does belong here is the
    memory block: one whole string, so the prompt is deterministic for a given set of
    memories and is still cacheable.

    The mood rule stays the final instruction, so the memory block, the private rule and the
    dynamic hints all go inside it.
    """
    system = SUPERVISOR_SYSTEM.format(
        name=name,
        instructions=instructions,
        team=team,
        groups=groups,
        conversation=conversation,
    )
    body = _without_mood(system)
    # A group's own chat asks with @; everywhere else the supervisor may take a question out.
    body += GROUP_ASK_RULE if in_group else ASK_OTHERS_RULE
    if in_group:
        body += GROUP_COORDINATE_RULE
    if private_peer is not None:
        body += PRIVATE_NOTIFY_RULE.format(peer=private_peer)
    # A private chat has no way to answer, so a question to the user is only offered elsewhere.
    body += "" if private_peer is not None else ASK_USER_RULE
    body += UPDATE_SELF_RULE + UPDATE_AGENT_RULE + SUBTASK_RULE
    return (
        body
        + skills
        + memory
        + workflow
        + "".join(hints)
        + (REACT_RULE if react else "")
        + MOOD_RULE
    )


def member_system(
    *,
    agent: Mapping[str, Any],
    conversation_kind: str,
    title: str = "",
    purpose: str = "",
    peer: str | None = None,
    roster: Sequence[Mapping[str, Any]] = (),
    team_roster: str,
    groups: str = "",
    memory: str = "",
    skills: str = "",
    workflow: str = "",
    hints: Sequence[str] = (),
) -> str:
    """A member's system prompt for one conversation kind, the mood rule last.

    Every member can DM anyone, so the team directory goes in right before the hints: the
    mood rule must remain the prompt's final instruction. ``memory`` is one whole block.
    ``groups`` lists the groups the member belongs to, so ``ask_group`` knows where it may ask.
    """
    name, instructions = agent["name"], agent["instructions"]
    if conversation_kind == "direct":
        system = MEMBER_DIRECT_SYSTEM.format(name=name, instructions=instructions)
    elif conversation_kind == "workflow":
        system = WORKFLOW_SYSTEM.format(
            name=name, instructions=instructions, title=title, purpose=purpose
        )
    elif conversation_kind == "private":
        system = MEMBER_PRIVATE_SYSTEM.format(
            name=name, instructions=instructions, peer=peer or "另一位成员"
        )
    elif conversation_kind == "thread":
        system = MEMBER_THREAD_SYSTEM.format(
            name=name,
            instructions=instructions,
            title=title,
            purpose=purpose,
            roster=roster_lines(roster, agent["id"]),
        )
    else:
        system = MEMBER_SYSTEM.format(
            name=name,
            instructions=instructions,
            title=title,
            purpose=purpose,
            roster=roster_lines(roster, agent["id"]),
        )
    # A task thread works for a group: the question is asked there with @, so it reads the
    # group's rule and never gets told about ``ask_group`` (which a group slot cannot call).
    in_group = conversation_kind in ("group", "thread")
    rule = GROUP_ASK_RULE if in_group else ASK_OTHERS_RULE
    roster_block = f"\n\n团队通讯录（私信时用名字或 id）：\n{team_roster}"
    if not in_group:
        roster_block += f"\n\n你所在的群组（ask_group 时用名字或 id）：\n{groups or '- （你还不在任何群组里）'}"
    react = conversation_kind in ("direct", "group")
    # A question is only offered where the user can answer it: their chat, a group, or the
    # task thread they opened from a group's card.
    ask = ASK_USER_RULE if conversation_kind in ("direct", "group", "thread") else ""
    return (
        _without_mood(system)
        + rule
        + roster_block
        + ask
        + UPDATE_SELF_RULE
        + SUBTASK_RULE
        + skills
        + memory
        + workflow
        + "".join(hints)
        + (REACT_RULE if react else "")
        + MOOD_RULE
    )


# ---------------------------------------------------------------------- review


REVIEW_SYSTEM = (
    "你是团队主管，负责验收成员完成的任务。只输出一个 JSON 对象："
    '{"verdict":"pass"或"revise","feedback":"一句话反馈"}。'
)


def review_prompt(*, agent_name: str, task: str, original: str, answer: str) -> str:
    """The one review call: the task, the run's user request and the member's answer."""
    return (
        f"你给成员「{agent_name}」布置的任务：\n{task}\n\n"
        f"本次运行的用户原始请求：\n{original or '（无）'}\n\n"
        f"「{agent_name}」的回答：\n{answer}\n\n"
        "判断这个回答是否可以直接交付给用户。直接输出 JSON。"
    )


def member_profile_hint(entries: Sequence[str]) -> str:
    """The supervisor's per-member stats, capped at 12 members and 400 characters."""
    text = "成员档案：" + "；".join(entries)
    if len(text) <= 400:
        return text
    return text[:399] + "…"


def runtime_block(
    *, moment: datetime, timezone: str, hints: Sequence[str] = ()
) -> str:
    """The per-turn facts, appended to the newest user turn instead of the system prompt.

    Everything here changes between turns (the clock) or with live state (a connected
    connector, the sandbox), which is exactly why it must not sit before the history: a
    provider's prefix cache matches bytes from the start, so one changing byte in the system
    prompt costs the whole conversation.
    """
    lines = [hint.strip() for hint in hints if hint and hint.strip()]
    tail = f'<runtime time="{now_line(moment)}" timezone="{timezone}"/>'
    head = "\n".join(lines)
    return f"\n\n{head}\n\n{tail}" if head else f"\n\n{tail}"


def summary_turn(summary: str) -> str:
    """A rolling summary as the user turn that replaces the history it covers."""
    return f"<摘要>\n{summary.strip()}\n</摘要>"


def summary_prompt(*, transcript: str, previous: str | None, background: bool) -> str:
    """The one user message the summariser answers: what to keep, and what it replaces."""
    parts = [SUMMARY_PROMPT]
    if previous:
        parts.append(SUMMARY_MERGE.format(summary=previous.strip()))
    if background:
        parts.append(SUMMARY_BACKGROUND)
    parts.append(f"{SUMMARY_TRANSCRIPT}{transcript}")
    return "".join(parts)


