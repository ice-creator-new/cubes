"""Approvals: one primitive for every card a tool parks on, and the unattended policy.

A tool that changes the team, the schedules, a connector's data or the sandbox first shows a
card and waits for the user's yes or no. The store owns the action state machine; this
module owns the words and the steps around it, so all six card sites behave and read the
same way. It also decides what a run nobody watches may do: a scheduled run works in a
hidden workspace where no card can ever be answered.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import approval_rules
from . import permissions
from .errors import ToolError
from .models import message_payload
from .tools.names import DELETE_AGENT, DISSOLVE_GROUP

# No one is looking at a scheduled run's hidden workspace, so no card there can be answered.
UNATTENDED_REFUSAL = "定时任务在后台运行，没有人能确认这一步，本次没有执行。"
# Management tools that would park on a confirmation card: refused outright when unattended.
UNATTENDED_TOOLS: tuple[str, ...] = (DELETE_AGENT, DISSOLVE_GROUP)
# Managing schedules from inside a run a schedule started would recurse; it is refused too.
SCHEDULE_UNATTENDED_REFUSAL = "这是定时任务发起的运行，不能再管理定时任务，本次调用未执行。"
# The action row vanished between creating it and asking: only a bug or a race can do this.
EXPIRED = "确认请求已失效，本次调用未执行。"


@dataclass(frozen=True)
class Card:
    """One confirmation card: what it asks, its two buttons, and how the row reads.

    ``resume_summary`` and ``resume_step`` describe the running state a yes puts the row
    back into; when they are ``None`` the action stays parked for the caller to finish,
    which is what a decision that completes the work itself needs.
    """

    prompt: str
    confirm_label: str
    deny_label: str
    summary: str
    step: str
    resume_summary: str | None = None
    resume_step: str | None = None
    # The permission a mode may already give for this card (``None``: always ask).
    grant: str | None = None
    # What a 「总是允许」 on this card remembers; ``None`` is the tool's own name.
    rule: str | None = None

    @property
    def confirmation(self) -> dict:
        """The stored shape the client renders as the card's buttons."""
        shown = {
            "prompt": self.prompt,
            "confirm_label": self.confirm_label,
            "deny_label": self.deny_label,
        }
        if self.rule:
            shown["rule"] = self.rule
        return shown


def refusal(ctx, handler, *, scheduled: bool) -> str | None:
    """Why this handler may not run here, or ``None`` when it may.

    A scheduled run answers no cards, so a destructive tool is refused there — unless the
    user wrote a 「总是允许」 rule for that tool and bot, which is exactly the promise that
    lets it run unattended.
    """
    if not scheduled:
        return None
    if approval_rules.rule_covers(
        ctx.store, tool_name=handler.name, agent_id=ctx.author["id"]
    ):
        return None
    return handler.unattended_refusal


def allowed_without_card(ctx, card: Card, tool_name: str) -> bool:
    """The bot's approval mode gives this card's permission, or a remembered rule covers it."""
    if card.grant and permissions.grants(ctx.store, ctx.author, card.grant):
        return True
    return skips(ctx, card.rule or tool_name)


def skips(ctx, tool_name: str) -> bool:
    """Whether this run's next card-worthy call may go ahead without showing a card."""
    return approval_rules.rule_covers(
        ctx.store, tool_name=tool_name, agent_id=ctx.author["id"]
    )


async def ask(ctx, card: Card, *, action: dict | None = None) -> str:
    """Park this call's action row on ``card`` and wait for the user's answer.

    A yes resumes the row (saying what is now running, when the site says so); a no, a stop
    or a timeout leaves it parked, because only the caller knows what that means for the
    tool's own result. The decision comes back as ``approve``, ``deny``, ``stop`` or
    ``timeout``.
    """
    row = ctx.action if action is None else action
    if allowed_without_card(ctx, card, str(row.get("tool_name") or "")):
        # The mode gives it, or a 「总是允许」 rule covers it: no card is shown, and the row
        # goes straight on with the phrase the site uses once the work starts.
        if card.resume_summary is not None:
            resumed = ctx.store.resume_action(row["id"], summary=card.resume_summary)
            if resumed is not None:
                ctx.publish_message("message_end", resumed[1])
            if card.resume_step is not None:
                ctx.set_step(card.resume_step)
        return "approve"
    marked = ctx.store.mark_action_awaiting(
        row["id"], summary=card.summary, confirmation=card.confirmation
    )
    if marked is None:  # pragma: no cover - the action was created moments ago
        raise ToolError(EXPIRED)
    ctx.publish_message("message_end", marked[1])
    ctx.set_step(card.step)
    ctx.publish("decisions", _decisions(ctx))
    ctx.refresh_task()
    decision = await ctx.await_decision(row["id"])
    ctx.refresh_task()
    if decision == "approve" and card.resume_summary is not None:
        resumed = ctx.store.resume_action(row["id"], summary=card.resume_summary)
        if resumed is not None:
            ctx.publish_message("message_end", resumed[1])
        if card.resume_step is not None:
            ctx.set_step(card.resume_step)
    # A yes puts the card back to running, so it is no longer waiting. Publish that now:
    # the later finish only republishes when the row is still awaiting, and this one is not.
    ctx.publish("decisions", _decisions(ctx))
    return decision

def _decisions(ctx) -> list[dict]:
    from .decisions import collect_decisions

    return collect_decisions(ctx.store, confirm_timeout_seconds=ctx.settings.confirm_timeout_seconds)



async def ask_in(
    ctx,
    card: Card,
    *,
    conversation_id: str,
    author: dict,
    tool_name: str,
    tool_call_id: str,
    arguments: str = "{}",
) -> tuple[dict, str]:
    """Show ``card`` in another conversation of this workspace and wait for its answer.

    A scheduled run's own workspace is invisible, so its sandbox question is asked where
    the user reads that schedule's results. The caller completes the action: a yes usually
    means something to remember beyond this call.
    """
    skip = allowed_without_card(ctx, card, tool_name)
    row, message = ctx.store.create_action(
        run_id=ctx.run.run_id,
        conversation_id=conversation_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments=arguments,
        summary=card.summary,
        author_id=author["id"],
        author_name=author["name"],
        avatar_seed=author["avatar_seed"],
        status="running" if skip else "awaiting",
        confirmation=None if skip else card.confirmation,
    )
    # The target is a conversation the user reads: its card is published like any other.
    # A skipped card still leaves its action row, so the caller's finish step has a target.
    ctx.publish("message_start", message_payload(message))
    if skip:
        return row, "approve"
    ctx.set_step(card.step)
    ctx.publish("decisions", _decisions(ctx))
    ctx.refresh_task()
    row_decision = await ctx.await_decision(row["id"])
    ctx.refresh_task()
    return row, row_decision
