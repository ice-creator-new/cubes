"""Streaming one model reply: the mood tag, the deltas and the flush cadence.

These are engine components: they read the engine's store and publish on its event bus,
so :class:`SupervisorEngine` composes this class with the run loop.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..appearance import MoodStream
from ..errors import UpstreamError
from ..fallback import fallback_reason
from ..model_stream import ModelStream, StreamBroken, StreamSilent
from ..providers import ResolvedModel, resolve_fallback, resolve_model
from .run_state import ActiveRun

FLUSH_INTERVAL_SECONDS = 0.5
# A model call silent this long gets a live "still working" step, refreshed at this pace.
WAIT_REPORT_SECONDS = 5.0


class Streaming:
    """One reply's visible text: what may be published, what is flushed and when."""

    async def _stream_reply(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reply_to_id: str | None,
        allow_tool_calls: bool,
        silent: bool = False,
    ) -> tuple[ModelStream, dict | None, str, str | None, list[str], str | None]:
        """Stream one completion, publishing only the text outside the mood tag.

        The trailing ``<mood>`` tag is resolved at the very end of the reply, so a tail that
        might still become one is withheld: every ``delta`` is text that can never turn out
        to be part of the tag. Periodic flushes and any fault persist the visible text only,
        which is exactly what subscribers already received. ``silent`` runs the same call
        with no message row and no deltas: a subtask's words belong in the tool result.
        """
        await self._enforce_token_budget(active, conversation, author)
        self._reserve_model_call(active)
        try:
            return await self._stream_once(
                active, conversation, author, messages=messages, tools=tools,
                reply_to_id=reply_to_id, allow_tool_calls=allow_tool_calls, silent=silent,
            )
        except StreamSilent as exc:  # a silent or a broken stream
            # Only while nothing reached the user: a retry after shown text would say it twice,
            # so that fault ends the run with what was said. A second fault goes to the
            # workspace backup below, if the user configured one.
            if exc.shown:
                raise
            why = "连接断了" if isinstance(exc, StreamBroken) else "没有回应"
            self._set_step(active, f"「{author['name']}」的模型{why}，正在重试")
            try:
                return await self._stream_once(
                    active, conversation, author, messages=messages, tools=tools,
                    reply_to_id=reply_to_id, allow_tool_calls=allow_tool_calls, silent=silent,
                )
            except UpstreamError as second:
                return await self._fallback_retry(
                    active, conversation, author, messages=messages, tools=tools,
                    reply_to_id=reply_to_id, allow_tool_calls=allow_tool_calls,
                    failure=second, silent=silent,
                )
        except UpstreamError as exc:
            # The primary service refused or died before any text: the workspace backup, if
            # one is configured, answers this call instead. A 4xx auth/bad request is the
            # request's own fault and stays a visible failure (see ``fallback_reason``).
            return await self._fallback_retry(
                active, conversation, author, messages=messages, tools=tools,
                reply_to_id=reply_to_id, allow_tool_calls=allow_tool_calls, failure=exc,
                silent=silent,
            )

    async def _fallback_retry(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reply_to_id: str | None,
        allow_tool_calls: bool,
        failure: UpstreamError,
        silent: bool = False,
    ) -> tuple[ModelStream, dict | None, str, str | None, list[str], str | None]:
        """Retry one failed call on the workspace backup, once, then write a quiet note.

        Only before anything was shown for this call: a retry after streamed text would repeat
        it. The note is per call; the next call resolves the primary model again.
        """
        if getattr(failure, "shown", False):
            raise failure
        reason = fallback_reason(failure)
        if reason is None:
            raise failure
        primary = resolve_model(self._store, author, self._settings)
        fallback = resolve_fallback(self._store, self._settings, agent=author)
        if fallback is None or (
            fallback.provider_id == primary.provider_id and fallback.model == primary.model
        ):
            raise failure
        if not silent:
            self._append_notice(
                active,
                conversation,
                f"「{primary.provider_name} / {primary.model}」暂时不可用（{reason}），"
                f"这次改用「{fallback.provider_name} / {fallback.model}」。",
            )
            self._set_step(active, f"「{author['name']}」改用备用模型「{fallback.model}」")
        return await self._stream_once(
            active, conversation, author, messages=messages, tools=tools,
            reply_to_id=reply_to_id, allow_tool_calls=allow_tool_calls, model=fallback,
            silent=silent,
        )

    async def _stream_once(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reply_to_id: str | None,
        allow_tool_calls: bool,
        model: ResolvedModel | None = None,
        silent: bool = False,
    ) -> tuple[ModelStream, dict | None, str, str | None, list[str], str | None]:
        self._guard(active, author, messages, tools)
        stream = self._completion(
            messages,
            tools=tools,
            author=author,
            model=model,
            conversation=conversation,
            active=active,
        )
        mood = MoodStream()
        message: dict | None = None
        last_flush = 0.0
        tool_marked = False

        def note_tool_start() -> None:
            """Mark the streaming text row the moment the model starts emitting a tool call.

            That fragment is the exact point the words already streamed stopped being an
            answer: the response they belong to goes on to call tools, so they are narration
            for the step that is about to appear. Publishing the row right away lets the chat
            move it into the sandbox fold before the action row exists, instead of showing it
            as an answer and folding it seconds later.
            """
            nonlocal tool_marked
            if message is None or tool_marked:
                return  # a call that opened with a tool: no text row to mark yet
            tool_marked = True
            updated = self._store.update_message(message["id"], leads_to_tools=True)
            if updated is not None:
                self._publish_message("message_update", updated)

        stream.on_tool_start = note_tool_start
        # Until the first visible text, the run's step says the model is still working (and
        # thinking, once it streams reasoning), so a long call never looks like a hang.
        progress = asyncio.create_task(self._report_waiting(active, author, stream))
        try:
            async for chunk in stream.chunks(should_stop=lambda: active.stop_requested):
                if not progress.done():
                    progress.cancel()
                safe = mood.push(chunk)
                if message is None and not silent:
                    message = self._append_agent_message(
                        active,
                        conversation["id"],
                        author,
                        status="streaming",
                        reply_to_id=reply_to_id,
                    )
                    self._reset_task_text(active)
                    self._publish_message("message_start", message)
                    # A chunk may carry text and a tool call together: the hook ran before
                    # the row existed, so mark it as soon as the row is actually here.
                    if stream.saw_tool_call:
                        note_tool_start()
                if safe and message is not None:
                    self._note_task_text(active, safe)
                if safe and not active.background and message is not None:
                    self._publish(
                        "delta",
                        {
                            "conversation_id": conversation["id"],
                            "message_id": message["id"],
                            "text": safe,
                        },
                    )
                now = time.monotonic()
                if message is not None and now - last_flush >= FLUSH_INTERVAL_SECONDS:
                    self._store.update_message(message["id"], content=mood.visible)
                    last_flush = now
            stream.validate(allow_tool_calls=allow_tool_calls)
            # A CLI engine remembers the conversation only once the turn really succeeded.
            remember = getattr(stream, "remember", None)
            if callable(remember):
                remember()
        except BaseException as exc:
            # Persist whatever the clients already saw before any fault (stop, truncation,
            # upstream error); the run finalizer marks it interrupted, never complete.
            if message is not None:
                self._store.update_message(message["id"], content=mood.visible)
                # Any fault after text reached the user (a cut stream, a truncated or
                # refused answer) must not be retried on the backup: it would say it twice.
                exc.shown = True  # type: ignore[attr-defined]
            raise
        finally:
            progress.cancel()
            await asyncio.gather(progress, return_exceptions=True)
            await stream.aclose()
            # A call that failed midway still cost what the provider already counted.
            self._record_usage(active, stream, author=author, purpose="reply")
        body, value = mood.finish()
        return stream, message, body, value, mood.images, mood.reaction

    async def _report_waiting(self, active: ActiveRun, author: dict, stream: ModelStream) -> None:
        """Refresh the run's step while a model call has produced no visible text yet."""
        started = time.monotonic()
        last = ""
        while True:
            await asyncio.sleep(WAIT_REPORT_SECONDS)
            seconds = int(time.monotonic() - started)
            doing = "正在思考" if stream.result.reasoning_chars else "正在等模型回复"
            step = f"「{author['name']}」{doing}（{seconds} 秒）"
            if step != last:
                self._set_step(active, step)
                last = step
