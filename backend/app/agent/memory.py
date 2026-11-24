"""Long-term memory in the loop: the block a reply starts with, and its extraction.

The block is injected into every prompt (except a private bot chat). Extraction is batched
per conversation: a finished turn worth reading is parked in ``memory_pending``, and one
model call reads every parked turn once the conversation has been quiet for
``memory_idle_seconds`` — or straight away once ``memory_batch_turns`` are waiting. A turn
is not parked at all when the user only acknowledged (「好的，谢谢」) or when the bot
already wrote memory itself with ``remember``/``forget``. None of it reaches the user.
"""

from __future__ import annotations

import asyncio
import logging

from .. import memories as memory_rules
from .. import prompts
from .run_state import ActiveRun

logger = logging.getLogger(__name__)

# Auto-recall: how many rows a search for the current message may add to the block.
RECALL_LIMIT = 3
# How many existing rows the extraction call may see as update/delete candidates.
EXTRACT_CANDIDATES = 12
# What the extraction call names itself in ``X-Cubes-Purpose``; a gateway ignores it.
EXTRACT_PURPOSE = "memory-extract"
# A turn in which the bot already did this needs no second pass over it.
MEMORY_WRITE_TOOLS = ("remember", "forget")


class MemoryExtraction:
    """Memory read into the prompt, memory written from a finished exchange."""

    def _memory_block(self, conversation: dict, author: dict, trigger: str) -> str:
        """The ``<memory>`` block for one bot's next prompt, or "" when memory is off.

        A private bot chat gets nothing: what the two bots say there must never leak into
        the workspace's memories, so the block is not injected either. Otherwise the
        resident rows are the three scopes this bot may see, and a search of the current
        message adds a few related rows the block did not already show.
        """
        if conversation["kind"] == "private":
            return ""
        settings = self._store.memory_settings()
        if settings["paused"]:
            return ""
        group_id = conversation["id"] if conversation["kind"] == "group" else None
        rows = self._store.visible_memories(agent_id=author["id"], group_id=group_id)
        by_scope: dict[str, list[str]] = {"user": [], "agent": [], "team": [], "group": []}
        for row in rows:
            by_scope.setdefault(str(row["scope"]), []).append(str(row["content"]))
        limit = int(settings["core_chars"])
        sections, _ = prompts.memory_sections(
            about_user=by_scope["user"],
            own=by_scope["agent"],
            team=by_scope["team"],
            group=by_scope["group"],
            limit=limit,
        )
        shown = {row for _, kept in sections for row in kept}
        related: list[str] = []
        if trigger.strip():
            found = self._store.search_memories(
                trigger,
                agent_id=author["id"],
                group_id=group_id,
                limit=RECALL_LIMIT + len(shown),
            )
            related = [
                str(row["content"])
                for row in found
                if str(row["content"]) not in shown
            ][:RECALL_LIMIT]
        return prompts.memory_block(
            about_user=by_scope["user"],
            own=by_scope["agent"],
            team=by_scope["team"],
            group=by_scope["group"],
            related=related,
            limit=limit,
        )
    def _schedule_memory_extraction(self, active: ActiveRun, status: str) -> None:
        """Park a finished turn for its conversation's next batched extraction.

        Only a completed, user-started run in a visible conversation, while memory is on and
        auto extraction is left on — and only a turn with something to read: a bare
        acknowledgement, or a turn whose bot already wrote memory itself, is skipped. The run
        is claimed in the database as it is parked, so a restart never reads it twice.
        """
        if status != "completed" or active.workflow_run_id:
            return
        conversation = self._store.get_conversation(active.conversation_id)
        if conversation is None or conversation["kind"] not in ("direct", "group"):
            return
        settings = self._store.memory_settings()
        if settings["paused"] or not settings["auto_extract"]:
            return
        trigger = self._trigger_message(active, conversation)
        if trigger is None or trigger["kind"] != "user":
            return
        if not memory_rules.worth_extracting(str(trigger["content"] or "")):
            return
        reply_ids = [message_id for _, message_id in active.replies]
        if not reply_ids:
            return
        if self._store.used_tools(active.run_id, MEMORY_WRITE_TOOLS):
            return
        waiting = self._store.queue_memory_turn(
            active.run_id,
            conversation_id=conversation["id"],
            agent_id=active.replies[-1][0],
            trigger_message_id=trigger["id"],
            reply_ids=reply_ids,
        )
        if waiting is None:
            return
        full = waiting >= self._settings.memory_batch_turns
        self._arm_memory_flush(conversation["id"], delay=0.0 if full else self._settings.memory_idle_seconds)

    def _arm_memory_flush(self, conversation_id: str, *, delay: float) -> None:
        """(Re)start the quiet-period timer of one conversation; a new turn pushes it back."""
        timer = self._memory_timers.pop(conversation_id, None)
        if timer is not None:
            timer.cancel()
        loop = asyncio.get_running_loop()
        self._memory_timers[conversation_id] = loop.call_later(
            delay, self._start_memory_flush, conversation_id
        )

    def _start_memory_flush(self, conversation_id: str) -> None:
        self._memory_timers.pop(conversation_id, None)
        # A reply still running here will park another turn when it ends; read them together.
        if conversation_id in self._runs:
            self._arm_memory_flush(conversation_id, delay=self._settings.memory_idle_seconds)
            return
        task = asyncio.create_task(self._flush_memory_turns(conversation_id))
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    def _resume_memory_flushes(self) -> None:
        """At startup: turns parked before a restart get their quiet period again."""
        for conversation_id in self._store.memory_pending_conversations():
            self._arm_memory_flush(conversation_id, delay=self._settings.memory_idle_seconds)

    async def _flush_memory_turns(self, conversation_id: str) -> None:
        """One model call reads every parked turn of a conversation into add/update/delete ops.

        Nothing here may reach the user: a failure is logged, kept for the panel's shadow
        and otherwise dropped, because the answers they came for are already delivered. The
        parked turns are cleared either way, so a broken upstream never builds a backlog.
        """
        pending = self._store.pending_memory_turns(conversation_id)
        if not pending:
            return
        run_ids = [turn["run_id"] for turn in pending]
        cancelled = False
        try:
            settings = self._store.memory_settings()
            conversation = self._store.get_conversation(conversation_id)
            if settings["paused"] or not settings["auto_extract"] or conversation is None:
                return
            # The bot that answered last speaks for the batch: its model runs the call and its
            # scopes decide which rows are candidates.
            responder = self._store.get_agent(pending[-1]["agent_id"])
            if responder is None:
                return
            turns: list[tuple[str, list[tuple[str, str]]]] = []
            last_reply: str | None = None
            for turn in pending:
                trigger = self._store.get_message(turn["trigger_message_id"])
                if trigger is None:
                    continue
                replies: list[tuple[str, str]] = []
                for message_id in turn["reply_ids"]:
                    message = self._store.get_message(message_id)
                    if message is None or message["kind"] != "agent":
                        continue
                    content = str(message["content"] or "").strip()
                    if content:
                        replies.append((str(message["author_name"]), content))
                        last_reply = message_id
                if replies:
                    turns.append((str(trigger["content"] or ""), replies))
            if not turns or last_reply is None:
                return
            # Matches come first, then the newest of everything else this bot may see: the
            # same fact under a different topic (住在杭州 / 居住城市) is only caught if
            # the extraction call can read the row it would duplicate.
            query = " ".join(text for user, replies in turns for text in (user, *(r for _, r in replies)))
            matches = self._store.search_memories(
                query, agent_id=str(responder["id"]), limit=EXTRACT_CANDIDATES
            )
            seen = {row["id"] for row in matches}
            candidates = [
                *matches,
                *(
                    row
                    for row in self._store.visible_memories(agent_id=str(responder["id"]))
                    if row["id"] not in seen
                ),
            ][:EXTRACT_CANDIDATES]
            messages = prompts.memory_extract_messages(turns=turns, candidates=candidates)
            stream = self._completion(messages, tools=None, author=responder, purpose=EXTRACT_PURPOSE)
            async for _ in stream.chunks():
                pass
            stream.validate(allow_tool_calls=False)
            # A housekeeping call still counts against its bot's budget, though it is never
            # gated: this row attributes the extraction to the agent that answered last.
            usage = getattr(getattr(stream, "result", None), "usage", None)
            if usage is not None and usage.total:
                self._store.record_usage(
                    agent_id=str(responder["id"]),
                    conversation_id=conversation_id,
                    run_id=None,
                    purpose=EXTRACT_PURPOSE,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                )
            ops = memory_rules.parse_ops(stream.result.content)
            if ops is None:
                raise ValueError("抽取结果不是 JSON 数组。")
            # The batch's last reply is where the 「记住了 N 条」 chip appears: one chip per
            # batch, under the reply the user just read, rather than one under every turn.
            self._store.apply_memory_ops(
                ops,
                author_id=str(responder["id"]),
                source_conversation_id=conversation_id,
                source_message_ids=[last_reply],
            )
            self._publish("memories", memory_rules.memories_payload(self._store))
        except asyncio.CancelledError:
            # Shutdown: the rows stay parked and the next start reads them.
            cancelled = True
            raise
        except Exception as exc:
            logger.warning("memory extraction failed for conversation %s: %s", conversation_id, exc)
            self._store.record_memory_error(f"{exc.__class__.__name__}：{exc}")
        finally:
            if not cancelled:
                self._store.clear_memory_turns(run_ids)
