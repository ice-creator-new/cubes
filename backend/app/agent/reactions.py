"""Reactions in the loop: a reply's own ``<react>`` tag, and the bot's reaction response.

Two behaviours share one module. A reply may carry a trailing ``<react>emoji</react>`` tag
that lands on the message it answers (for free, while it streams). Separately, when the
*user* reacts to a bot's message, that bot takes one small, debounced, no-tool call and
decides to ignore, react back or say one line. Both publish the ``reactions`` event and
obey the same "at most 20 distinct emoji" rule.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .. import models, reactions as reaction_rules, schedules, usage as usage_rules
from ..appearance import MOODS
from .run_state import ActiveRun

logger = logging.getLogger(__name__)

REACTION_PURPOSE = "reaction"
REACTION_MAX_TOKENS = 200
# How many recent lines and how long each may be in the reaction-response call.
RESPONSE_RECENT_ROWS = 8
RESPONSE_LINE_CHARS = 120


class ReactionResponses:
    """The reply's ``<react>`` tag, and the debounced reaction response."""

    # ---------------------------------------------------------- reply's react tag

    def _publish_reactions(self, conversation_id: str, message_id: str) -> None:
        self._publish(
            "reactions",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "reactions": self._store.reactions_payload(message_id),
            },
        )

    def _apply_reply_reaction(
        self, active: ActiveRun, message: dict, reaction: str | None
    ) -> bool:
        """Apply a reply's trailing ``<react>`` tag to the user message it answers; True if applied.

        A reply normally points at the user message it answers. A member the supervisor
        consulted (``ask_agent``) points at the supervisor's tool row instead; its reaction
        belongs on the user message that started the run. An invalid emoji, no user message
        to land on, or a hidden conversation all mean nothing is applied (``False``).
        """
        # A workflow step answers the plan, not a user message: the newest line in the owner's
        # chat is unrelated to it, so there is nothing to react to.
        if reaction is None or active.workflow_run_id:
            return False
        emoji = reaction_rules.normalize_emoji(reaction)
        if emoji is None:
            return False
        conversation = self._store.get_conversation(message["conversation_id"])
        if conversation is None or conversation["kind"] not in ("direct", "group"):
            return False
        target = self._reaction_target(active, conversation, message)
        if target is None:
            return False
        target_id = target["id"]
        self._store.add_reaction(
            target_id,
            emoji,
            actor_id=message.get("author_id") or "",
            actor_name=message.get("author_name") or "",
        )
        self._publish_reactions(conversation["id"], target_id)
        return True

    def _reaction_target(self, active: ActiveRun, conversation: dict, message: dict) -> dict | None:
        """The user message a reply's reaction lands on, or ``None``."""
        target_id = message.get("reply_to_id")
        target = self._store.get_message(target_id) if target_id else None
        if target is not None and target["conversation_id"] != conversation["id"]:
            return None
        if target is not None and target["kind"] == "user":
            return target
        if target is not None and target["kind"] != "action":
            return None
        # No target, or the supervisor's consult row: the run's own trigger is what the user asked.
        trigger = self._trigger_message(active, conversation)
        if trigger is None or trigger["kind"] != "user":
            return None
        return trigger

    def _remove_reaction_only_reply(self, active: ActiveRun, message: dict) -> None:
        """A reply that was only a reaction: drop its streaming row and tell every client."""
        message_id = message["id"]
        active.streaming_ids.discard(message_id)
        active.replies = [
            (agent_id, mid) for agent_id, mid in active.replies if mid != message_id
        ]
        self._store.delete_message(message_id)
        self._publish(
            "message_removed",
            {"conversation_id": message["conversation_id"], "message_id": message_id},
        )

    # ------------------------------------------------------- response scheduling

    def schedule_reaction_response(self, message: dict) -> None:
        """Queue a debounced response for the agent that authored ``message``.

        Only a user's reaction on an agent's message in a user-facing conversation triggers
        it; the debounce merges several quick reactions into one call.
        """
        conversation = self._store.get_conversation(message["conversation_id"])
        if conversation is None or conversation["kind"] not in ("direct", "group"):
            return
        author_id = message.get("author_id")
        if not author_id or author_id == "user":
            return
        if self._store.get_agent(author_id) is None:
            return
        key = (conversation["id"], author_id)
        pending = self._reaction_pending.setdefault(key, [])
        if message["id"] not in pending:
            pending.append(message["id"])
        self._arm_reaction_response(key)

    def cancel_reaction_response(self, message: dict) -> None:
        """Cancel a still-pending response when the removal left nothing new to respond to."""
        author_id = message.get("author_id")
        if not author_id or author_id == "user":
            return
        key = (message["conversation_id"], author_id)
        pending = self._reaction_pending.get(key)
        if pending is None or message["id"] not in pending:
            return
        if any(reaction["mine"] for reaction in self._store.reactions_payload(message["id"])):
            return
        pending.remove(message["id"])
        if not pending:
            timer = self._reaction_timers.pop(key, None)
            if timer is not None:
                timer.cancel()
            self._reaction_pending.pop(key, None)

    def _arm_reaction_response(self, key: tuple[str, str]) -> None:
        timer = self._reaction_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        loop = asyncio.get_running_loop()
        self._reaction_timers[key] = loop.call_later(
            self._settings.reaction_debounce_seconds, self._start_reaction_response, key
        )

    def _start_reaction_response(self, key: tuple[str, str]) -> None:
        self._reaction_timers.pop(key, None)
        conversation_id, agent_id = key
        message_ids = self._reaction_pending.pop(key, [])
        if not message_ids:
            return
        if self.is_running(conversation_id):
            return  # a run owns this conversation now; its own turns will do the talking
        agent = self._store.get_agent(agent_id)
        if agent is None or self._over_token_budget(agent):
            return
        task = asyncio.create_task(
            self._respond_to_reactions(conversation_id, agent, message_ids)
        )
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    def _over_token_budget(self, agent: dict) -> bool:
        budget = agent.get("token_budget")
        if not isinstance(budget, dict):
            return False
        limit = budget.get("limit")
        period = budget.get("period")
        if not isinstance(limit, int) or period not in usage_rules.PERIODS:
            return False
        zone = schedules.zone_of(self._settings.timezone)
        used = self._store.budget_used(agent["id"], period, datetime.now(timezone.utc), zone)
        return used >= limit

    # ------------------------------------------------------------ the one call

    async def _respond_to_reactions(
        self, conversation_id: str, agent: dict, message_ids: list[str]
    ) -> None:
        """One small no-tool call decides how the agent answers the user's reactions."""
        try:
            conversation = self._store.get_conversation(conversation_id)
            if conversation is None or conversation["kind"] not in ("direct", "group"):
                return
            reacted: list[tuple[dict, list[str]]] = []
            for message_id in message_ids:
                message = self._store.get_message(message_id)
                if message is None or message["conversation_id"] != conversation_id:
                    continue
                emojis = [
                    reaction["emoji"]
                    for reaction in self._store.reactions_payload(message_id)
                    if reaction["mine"]
                ]
                if emojis:
                    reacted.append((message, emojis))
            if not reacted:
                return
            messages = self._reaction_response_messages(conversation, agent, reacted)
            stream = self._completion(
                messages,
                tools=None,
                author=agent,
                purpose=REACTION_PURPOSE,
                max_tokens=REACTION_MAX_TOKENS,
            )
            async for _ in stream.chunks():
                pass
            stream.validate(allow_tool_calls=False)
            usage = getattr(getattr(stream, "result", None), "usage", None)
            if usage is not None and usage.total:
                self._store.record_usage(
                    agent_id=agent["id"],
                    conversation_id=conversation_id,
                    run_id=None,
                    purpose=REACTION_PURPOSE,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                )
            result = reaction_rules.parse_reaction_response(stream.result.content)
            if result is None:
                logger.warning("reaction response was unreadable: %r", stream.result.content)
                return
            self._apply_reaction_response(conversation_id, agent, reacted, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bad response never disturbs the conversation
            logger.warning("reaction response failed for %s: %s", conversation_id, exc)

    def _apply_reaction_response(
        self,
        conversation_id: str,
        agent: dict,
        reacted: list[tuple[dict, list[str]]],
        result: dict[str, Any],
    ) -> None:
        target = reacted[0][0]
        action = result["action"]
        if action == "none":
            return
        if action == "react":
            self._store.add_reaction(
                target["id"], result["emoji"], actor_id=agent["id"], actor_name=agent["name"]
            )
            self._publish_reactions(conversation_id, target["id"])
            return
        message = self._store.append_message(
            conversation_id=conversation_id,
            author_id=agent["id"],
            author_name=agent["name"],
            avatar_seed=agent["avatar_seed"],
            kind="agent",
            content=result["text"],
            status="complete",
            reply_to_id=target["id"],
            mood=result["mood"],
        )
        self._publish("message_start", models.message_payload(message))
        self._publish("message_end", models.message_payload(message))

    def _reaction_response_messages(
        self,
        conversation: dict,
        agent: dict,
        reacted: list[tuple[dict, list[str]]],
    ) -> list[dict[str, Any]]:
        instructions = str(agent["instructions"] or "").strip()
        first_line = instructions.splitlines()[0] if instructions else ""
        system = (
            f"你是「{agent['name']}」。{first_line}\n"
            "用户在对话里给你的某条回复贴了表情。你快速决定要不要回应，只输出一个 JSON 对象，"
            "不要输出任何别的文字：\n"
            '{"action":"none"} —— 不回应（多数时候选这个）；\n'
            '{"action":"react","emoji":"😊"} —— 给那条消息贴一个表情回应，只写一个 emoji；\n'
            '{"action":"reply","text":"一句话","mood":"happy"} —— 说一句很短的、自然的话回应'
            "（text 不超过 200 字，不要 @，不要提工具，mood 取 "
            + "、".join(MOODS)
            + " 之一）。"
        )
        recent = [
            f"[{row['author_name']}] {' '.join(str(row['content']).split())[:RESPONSE_LINE_CHARS]}"
            for row in self._store.list_messages(conversation["id"])[-RESPONSE_RECENT_ROWS:]
            if row["kind"] in ("user", "agent") and str(row["content"]).strip()
        ]
        reacted_lines = [
            f"你的这条回复：{' '.join(str(message['content']).split())[:RESPONSE_LINE_CHARS]}\n"
            f"用户贴了：{''.join(emojis)}"
            for message, emojis in reacted
        ]
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "\n".join([*recent, "", *reacted_lines, "", "直接输出 JSON。"]),
            },
        ]
