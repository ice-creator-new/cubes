"""One bot turn as one activity: when it starts, what it did, and how it ended.

Every turn a bot takes in any conversation is an activity (see ``app.store.activities``):
it opens when the bot starts answering, its tool calls and the narration leading into them
become its steps, and it closes ``completed``, ``stopped``, ``error`` or ``limited`` — or
sits ``awaiting`` while a card waits for the user. The turn's messages carry its id, so a
group chat can show one card per running activity and only the final answer.

The title starts from the request's own opening words and is renamed once by a cheap
no-tool call on the dispatcher's model (after the first tool call, or at the end when the
turn never called one), with the summary written at the end. A failed call keeps the
heuristic title: naming is a nicety, never a reason to lose or delay the turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .. import dispatch as dispatch_rules
from .. import models
from ..errors import RunLimited, RunStopped, UpstreamError
from ..providers import CLI_PROTOCOLS, model_caps, resolve_dispatch
from ..store import activities as activity_rules
from .run_state import ACTIVITY, ActiveRun, TurnBudget

logger = logging.getLogger(__name__)

# A step reaches the activity's card at most this often (<= 1/s); a status change ignores it.
PROGRESS_SECONDS = 1.0
# Naming is background nicety: a slow dispatcher model must not hold a finished turn open.
TITLE_SECONDS = 20.0
TITLE_MAX_TOKENS = 400
# How much of the request and the reply the naming call reads.
TITLE_INPUT_CHARS = 400

TITLE_SYSTEM = "你给一次机器人工作的记录起名字。严格按格式输出，不要解释，不要引号。"
TITLE_PROMPT = (
    "请求：{request}\n\n"
    "第一行：这件事的动作标题，不超过 16 个字，像一个待办的名字。"
)
TITLE_PROMPT_WITH_SUMMARY = (
    "请求：{request}\n\n它最后对用户说的话：{answer}\n\n"
    "第一行：这件事的动作标题，不超过 16 个字，像一个待办的名字。\n"
    "第二行：一句话总结结果，不超过 40 个字。"
)
# Wrappers a small model likes to add around a line it was asked to emit bare.
_TITLE_PREFIXES = ("标题：", "标题:", "动作：", "动作:", "title:", "summary:", "摘要：", "摘要:")


def clean_title_line(line: str) -> str:
    """One line of a naming answer with the quotes, bullets and label a model may add."""
    text = " ".join(str(line).split()).strip("\"'“”‘’`#*•-— 「」")
    lowered = text.casefold()
    for prefix in _TITLE_PREFIXES:
        if lowered.startswith(prefix.casefold()):
            text = text[len(prefix) :].strip("\"'“”‘’`#*•-— 「」")
            break
    return text


def parse_title(text: str) -> tuple[str, str]:
    """The first line as a title and, when it is there, the second as a summary."""
    lines = [clean_title_line(line) for line in str(text).splitlines()]
    lines = [line for line in lines if line]
    title = lines[0] if lines else ""
    summary = lines[1] if len(lines) > 1 else ""
    return title, summary


class Activities:
    """The activity a bot turn writes, its live card and the steps behind it."""

    # ------------------------------------------------------------------ turn

    @asynccontextmanager
    async def _activity_turn(
        self,
        active: ActiveRun,
        conversation: dict,
        agent: dict,
        reply_to_id: str | None,
    ) -> AsyncIterator[TurnBudget]:
        """Open one activity around one bot turn, then record how it ended.

        Nested turns (an ``ask_agent`` answer, a private chat opened by ``direct_message``)
        read the outer activity's id from the context and become its children, which is how
        the detail page groups a supervisor's steps apart from the member it consulted.
        """
        activity = self._open_activity(active, conversation, agent, reply_to_id)
        if activity is None:
            with self._turn(active, agent) as turn:
                yield turn
            return
        token = ACTIVITY.set(str(activity["id"]))
        try:
            with self._turn(active, agent) as turn:
                try:
                    yield turn
                except RunStopped as exc:
                    await self._close_activity(
                        activity, activity_rules.STOPPED, str(exc) or "任务已停止。"
                    )
                    raise
                except RunLimited as exc:
                    await self._close_activity(activity, activity_rules.LIMITED, str(exc))
                    raise
                except UpstreamError as exc:
                    await self._close_activity(activity, activity_rules.ERROR, str(exc))
                    raise
                except BaseException as exc:
                    # A cancellation or an unexpected fault: the row still says it failed and
                    # keeps whatever it wrote, so no card is left saying it is still working.
                    await self._close_activity(
                        activity, activity_rules.ERROR, exc.__class__.__name__
                    )
                    raise
                limited = active.turn_limited.get(str(agent["id"]))
                if limited is not None or (turn is not None and turn.wrapped):
                    await self._close_activity(activity, activity_rules.LIMITED, limited or "")
                else:
                    await self._close_activity(activity, activity_rules.COMPLETED, "")
        finally:
            ACTIVITY.reset(token)

    def _open_activity(
        self,
        active: ActiveRun,
        conversation: dict,
        agent: dict,
        reply_to_id: str | None,
    ) -> dict | None:
        """Open the activity this turn writes; ``None`` for a run nobody watches."""
        if active.background:
            # A scheduled run's workspace is invisible: its activity would be a card nobody
            # can open. Its progress already reaches the schedules panel.
            return None
        request = self._store.get_message(reply_to_id) if reply_to_id else None
        if request is not None and request["conversation_id"] != conversation["id"]:
            request = None
        parent_id = ACTIVITY.get() or active.activity_parent
        if parent_id is None and request is not None:
            parent = self._store.activity_of_message(str(request["id"]))
            parent_id = str(parent["id"]) if parent is not None else None
        text = (
            str(request.get("content") or "")
            if request is not None
            else self._trigger_text(active, conversation)
        )
        activity = self._store.create_activity(
            conversation_id=conversation["id"],
            agent_id=str(agent["id"]),
            agent_name=str(agent["name"]),
            request=text,
            run_id=active.run_id,
            task_id=active.task_id,
            parent_id=parent_id,
            request_message_id=str(request["id"]) if request is not None else None,
        )
        self._publish_activity(activity, force=True)
        return activity

    async def _close_activity(self, activity: dict, status: str, reason: str) -> None:
        """Persist how the turn ended, then try to name it (the row is already safe)."""
        if activity is None:  # pragma: no cover - guarded by the caller
            return
        activity_id = str(activity["id"])
        answer = self._store.activity_answer(activity_id)
        text = self._activity_text.pop(activity_id, "")
        if text:
            self._store.update_activity(activity_id, latest_text=text)
        finished = self._store.finish_activity(
            activity_id,
            status=status,
            summary=self._activity_summary(status, reason, answer),
            answer_message_id=str(answer["id"]) if answer is not None else None,
        )
        if finished is not None:
            self._publish_activity(finished, force=True)
        # A naming call already running keeps its own task and applies the title when it
        # lands. The turn's exit never waits for it: suspending here would hand the loop to
        # other work (a thread this turn just opened) and reorder a run that never did.
        task = self._activity_title_tasks.pop(activity_id, None)
        naming = task is not None and not task.done()
        try:
            if (
                not naming
                and status == activity_rules.COMPLETED
                and activity_id not in self._activity_titled
            ):
                await self._rename_activity(finished or activity, with_summary=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # naming never changes what the turn did
            logger.exception("活动命名失败。")
        finally:
            self._activity_titled.discard(activity_id)
            self._activity_text.pop(activity_id, None)
            self._activity_published_at.pop(activity_id, None)

    @staticmethod
    def _activity_summary(status: str, reason: str, answer: dict | None) -> str:
        """One line for the list: the ending when there was one, else the answer's opening."""
        if status == activity_rules.STOPPED:
            return f"已停下：{(reason or '任务已停止。').rstrip('。')}"
        if status == activity_rules.ERROR:
            return f"出错了：{reason or '未知原因'}"
        if status == activity_rules.LIMITED:
            return reason or "步数用完了"
        return activity_rules.first_sentence(str((answer or {}).get("content") or ""))

    # ------------------------------------------------------------------ live

    def _note_activity_step(self, active: ActiveRun, step: str) -> None:
        """The run moved to ``step``: the open turn's card says the same thing.

        A step that waits on the user (「等待你…」, 「等「X」…」) is the turn parked, so the
        activity flips to ``awaiting`` — a real status the card's own buttons read.
        """
        activity_id = ACTIVITY.get()
        if not activity_id:
            return
        activity = self._store.get_activity(activity_id)
        if activity is None or activity["status"] not in activity_rules.UNFINISHED:
            return
        status = activity_rules.AWAITING if step.startswith("等") else activity_rules.RUNNING
        changed = status != activity["status"]
        updated = self._store.update_activity(activity_id, current_step=step, status=status)
        if updated is not None:
            self._publish_activity(updated, force=changed)

    def _note_activity_text(self, active: ActiveRun, safe: str) -> None:
        """The newest prose of the turn, for the card's quiet line (throttled like steps)."""
        activity_id = ACTIVITY.get()
        if not activity_id or not safe:
            return
        flat = " ".join((self._activity_text.get(activity_id, "") + safe).split())[-400:]
        self._activity_text[activity_id] = flat
        now = time.monotonic()
        last = self._activity_published_at.get(activity_id, float("-inf"))
        if now - last < PROGRESS_SECONDS:
            return
        updated = self._store.update_activity(activity_id, latest_text=flat)
        if updated is not None:
            self._publish_activity(updated, force=True)

    def _sync_activity_awaiting(self, activity_id: str | None) -> None:
        """A card of this turn was answered: move it back (or on to) ``awaiting``."""
        if not activity_id:
            return
        activity = self._store.get_activity(activity_id)
        if activity is None or activity["status"] not in activity_rules.UNFINISHED:
            return
        status = (
            activity_rules.AWAITING
            if self._store.activity_awaiting(activity_id)
            else activity_rules.RUNNING
        )
        if status == activity["status"]:
            return
        updated = self._store.update_activity(activity_id, status=status)
        if updated is not None:
            self._publish_activity(updated, force=True)

    def _publish_activity(self, activity: dict | None, *, force: bool = False) -> None:
        """Push one activity's payload, at most once a second unless its status changed."""
        if activity is None:
            return
        conversation_id = str(activity["conversation_id"])
        owner = self._runs.get(conversation_id)
        if owner is not None and owner.background:
            return
        activity_id = str(activity["id"])
        now = time.monotonic()
        last = self._activity_published_at.get(activity_id, float("-inf"))
        if not force and now - last < PROGRESS_SECONDS:
            return
        self._activity_published_at[activity_id] = now
        self._publish(
            "activity",
            models.activity_payload(self._store.activity_view(activity)),
        )

    def _settle_activities(self, active: ActiveRun, status: str, error: str | None) -> None:
        """A run ended with turns still open: every one of them gets the run's own ending."""
        ending = {
            "completed": activity_rules.COMPLETED,
            "stopped": activity_rules.STOPPED,
            "error": activity_rules.ERROR,
            "limited": activity_rules.LIMITED,
        }.get(status, activity_rules.ERROR)
        for activity in self._store.activities_of_run(active.run_id, unfinished=True):
            activity_id = str(activity["id"])
            self._activity_title_tasks.pop(activity_id, None)
            self._activity_titled.discard(activity_id)
            self._activity_text.pop(activity_id, None)
            answer = self._store.activity_answer(activity_id)
            finished = self._store.finish_activity(
                activity_id,
                status=ending,
                summary=self._activity_summary(ending, error or "", answer),
                answer_message_id=str(answer["id"]) if answer is not None else None,
            )
            if finished is not None:
                self._publish_activity(finished, force=True)

    # ------------------------------------------------------------------ naming

    def _maybe_rename_activity(self, activity_id: str | None) -> None:
        """Name the turn after its first tool call, without making the call wait for it."""
        if not activity_id or activity_id in self._activity_title_tasks:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._rename_activity_by_id(activity_id, with_summary=False)
            )
        except RuntimeError:  # pragma: no cover - turns always run inside the loop
            return
        self._activity_title_tasks[activity_id] = task

    async def _rename_activity_by_id(self, activity_id: str, *, with_summary: bool) -> None:
        activity = self._store.get_activity(activity_id)
        if activity is None:  # pragma: no cover - the row was created moments ago
            return
        await self._rename_activity(activity, with_summary=with_summary)

    async def _rename_activity(self, activity: dict, *, with_summary: bool) -> None:
        """One cheap no-tool call names the turn; failures keep the heuristic title."""
        activity_id = str(activity["id"])
        self._activity_titled.add(activity_id)
        model = resolve_dispatch(self._store, self._settings)
        if model is None or model.protocol in CLI_PROTOCOLS:
            # A local CLI engine is a whole coding session, not a cheap text call: naming a
            # turn must never open one (it would burn a request and a session of its own).
            return
        request = ""
        if activity.get("request_message_id"):
            message = self._store.get_message(str(activity["request_message_id"]))
            request = str((message or {}).get("content") or "")
        request = activity_rules.clip(request or activity.get("title") or "", TITLE_INPUT_CHARS)
        answer = ""
        if with_summary:
            reply = self._store.activity_answer(activity_id)
            answer = activity_rules.clip(
                str((reply or {}).get("content") or ""), TITLE_INPUT_CHARS
            )
        template = TITLE_PROMPT_WITH_SUMMARY if with_summary else TITLE_PROMPT
        messages = [
            {"role": "system", "content": TITLE_SYSTEM},
            {"role": "user", "content": template.format(request=request, answer=answer)},
        ]
        provider = self._store.get_provider(model.provider_id)
        caps = model_caps(provider, model.model, self._settings) if provider is not None else None
        # A reasoning model spends a small budget on thinking: name the effort exactly as the
        # dispatcher does, so the title call answers instead of only reasoning.
        effort = (
            dispatch_rules.reasoning_effort(caps.reasoning, caps.reasoning_levels)
            if caps is not None
            else None
        )
        pseudo = {
            "id": "",
            "name": "标题",
            "kind": "supervisor",
            "instructions": "",
            "avatar_seed": None,
        }
        stream = self._completion(
            messages,
            tools=None,
            author=pseudo,
            purpose="activity-title",
            max_tokens=TITLE_MAX_TOKENS,
            model=model,
            reasoning_effort=effort,
        )
        pieces: list[str] = []

        async def collect() -> None:
            async for chunk in stream.chunks(should_stop=lambda: False):
                pieces.append(chunk)
            stream.validate(allow_tool_calls=False)

        try:
            await asyncio.wait_for(collect(), timeout=TITLE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # a timeout, a dead model or unreadable prose: keep the title
            return
        finally:
            self._record_activity_usage(activity, stream)
        title, summary = parse_title("".join(pieces))
        updates: dict[str, Any] = {}
        if title:
            updates["title"] = activity_rules.activity_title(title)
        if with_summary and summary:
            updates["summary"] = activity_rules.clip(summary, activity_rules.SUMMARY_CHARS)
        if not updates:
            return
        updated = self._store.update_activity(activity_id, **updates)
        if updated is not None:
            self._publish_activity(updated, force=True)

    def _record_activity_usage(self, activity: dict, stream: Any) -> None:
        """One usage row for the naming call, under no bot: naming is not anyone's budget."""
        usage = getattr(getattr(stream, "result", None), "usage", None)
        if usage is None or not usage.total:
            return
        self._store.record_usage(
            agent_id="",
            conversation_id=str(activity.get("conversation_id") or ""),
            run_id=activity.get("run_id"),
            purpose="activity-title",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
        )
