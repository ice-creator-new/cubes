"""The context budget: compact a conversation before its next request overflows.

A model's declared window decides how much one request may spend: older turns are folded
into a rolling summary, and whatever is still too big is trimmed to fit. Compaction runs
on the assembled request, so a tool loop's own growth is caught too.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from .. import prompts
from ..errors import RunStopped, UpstreamError
from ..providers import resolve_model
from ..tools import computer as computer_tools
from . import budget as budget_rules
from . import history
from .run_state import ActiveRun

logger = logging.getLogger(__name__)


class ContextWindow:
    """Fold old turns into a summary, then fit what is left into the model's window."""

    async def _turn_context(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        tools: Sequence[dict[str, Any]] | None,
        build: Any,
    ) -> list[dict[str, Any]]:
        """This turn's first request, compacted first when it would not fit.

        ``build`` is the role's own assembler, so a compaction that rewrote the conversation
        is simply rebuilt from the stored rows — there is one way to build a request.
        """
        messages = build()
        self._add_lane_note(active, conversation, author, messages)
        if await self._compact(active, conversation, author, messages, tools=tools):
            messages = build()
            self._add_lane_note(active, conversation, author, messages)
        return messages

    def _add_lane_note(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        messages: list[dict[str, Any]],
    ) -> None:
        """Tell a turn what its lane owes: a to-do board, or its group's real progress."""
        note = self._lane_system_note(active, conversation, author)
        if not note:
            return
        # The note belongs to this request only; it is never written to the transcript.
        # It rides in the system prompt so every protocol sees one system turn, and it goes
        # before the mood rule: that rule must stay the prompt's final instruction.
        if messages and messages[0].get("role") == "system":
            system = str(messages[0]["content"])
            if prompts.MOOD_RULE in system:
                system = system.replace(prompts.MOOD_RULE, f"{note}\n\n{prompts.MOOD_RULE}", 1)
            else:
                system = f"{system}\n\n{note}"
            messages[0]["content"] = system
        else:
            messages.insert(0, {"role": "system", "content": note})

    def _guard(
        self,
        active: ActiveRun,
        author: dict,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
    ) -> None:
        """Make the request fit before it is sent: prune pixels, then drop the oldest turns.

        This runs on the assembled list, so a tool loop's own growth is caught too. Only a
        single turn that alone overflows the window is an error: everything else is dropped
        oldest-first, and what must not be dropped is the newest turn.
        """
        # Per-tool pixel retention: the sandbox pack says how many screenshot turns of each
        # kind keep their pixels (screen 2, browser 1).
        history.drop_old_screenshots(messages, keep=computer_tools.SHOT_KEEP)
        window, _ = self._limits(author)
        total = budget_rules.fit(
            messages,
            tools=tools,
            budget=self._usable(author),
            head=history.summary_head(messages),
        )
        if total > window:
            raise UpstreamError(
                f"这条消息本身就超出了模型的上下文窗口（约 {total} tokens，窗口 {window}），"
                "请缩短内容后重试，或换一个窗口更大的模型。"
            )

    def _limits(self, author: dict) -> tuple[int, int]:
        """The context window and output limit this bot's model declares, in tokens."""
        resolved = resolve_model(self._store, author, self._settings)
        return resolved.context_window, resolved.max_output

    def _usable(self, author: dict) -> int:
        """How many tokens one request to this bot's model may spend."""
        window, output = self._limits(author)
        return budget_rules.usable_budget(window, output)

    # ------------------------------------------------------------------ composer meter

    def _request_for_context(
        self, conversation: dict, author: dict
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        """The request this bot's next model call would send, from the same assembler.

        The composer's numbers must be what a run really pays for, so this goes through the
        role's own builder (system prompt, memory, skills, history, runtime block) and the
        tool registry instead of re-assembling history by hand. The newest user message
        stands in for the trigger a run would use, so related memories are counted too.
        """
        trigger = self._last_user_text(conversation)
        if author["kind"] == "supervisor":
            messages = self._supervisor_context(conversation, trigger=trigger)
            tools = self._schemas("supervisor", conversation, author)
        else:
            messages = self._member_context(
                conversation, self._roster(conversation), author, trigger=trigger
            )
            tools = self._schemas("member", conversation, author)
        return messages, tools, trigger

    def _last_user_text(self, conversation: dict) -> str:
        """The user's newest message: what a run searches related memories with."""
        for row in reversed(self._store.list_messages(conversation["id"])):
            if row["kind"] == "user" and str(row["content"]).strip():
                return str(row["content"])
        return ""

    @staticmethod
    def _tagged_block(text: str, tag: str) -> str:
        """One ``<tag>…</tag>`` section of the system prompt, or "" when it has none."""
        start = text.find(f"<{tag}>")
        end = text.find(f"</{tag}>", start + 1)
        if start < 0 or end < 0:
            return ""
        return text[start : end + len(tag) + 3]

    def _runtime_tokens(self, conversation: dict, author: dict, trigger: str) -> int:
        """The clock and live-state block that rides at the end of the newest user turn.

        Built by the same hint list a real call uses — the working set included — so the
        meter's "system" share is what the request actually carries there.
        """
        block = prompts.runtime_block(
            moment=self._now(),
            timezone=self._settings.timezone,
            hints=self._runtime_hints(conversation, author, trigger),
        )
        return budget_rules.estimate_tokens(block)

    @staticmethod
    def _speaker_name(text: str) -> str | None:
        """The bot named at the start of another member's turn, e.g. ``[研究员] …``."""
        if not text.startswith("["):
            return None
        end = text.find("] ")
        return text[1:end] if end > 1 else None

    def _breakdown_categories(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        conversation: dict,
        author: dict,
        trigger: str,
    ) -> list[dict[str, Any]]:
        """Split one request into Cubes' own categories, from the very bytes it sends.

        Every message is charged exactly as :func:`budget.estimate_messages` charges it:
        framing, content, and each tool call's name and arguments. The summary, the other
        bots' turns and the tool traces are told apart by shape, so the rows add up to the
        same total the bar shows.
        """
        totals = {
            "system": 0,
            "memory": 0,
            "skills": 0,
            "tools": 0,
            "user": 0,
            "reply": 0,
            "calls": 0,
            "results": 0,
            "summary": 0,
        }
        result_tools: dict[str, int] = {}
        overhead = budget_rules.MESSAGE_OVERHEAD_TOKENS
        system = ""
        if messages and messages[0].get("role") == "system":
            system = history.text_of(messages[0].get("content"))
        memory = self._tagged_block(system, "memory")
        skills = self._tagged_block(system, "skills")
        totals["memory"] = budget_rules.estimate_tokens(memory)
        totals["skills"] = budget_rules.estimate_tokens(skills)
        totals["system"] = max(
            0,
            overhead
            + budget_rules.estimate_tokens(system)
            - totals["memory"]
            - totals["skills"],
        )
        totals["tools"] = budget_rules.estimate_tools(tools)
        names = {str(agent["name"]) for agent in self._store.list_agents()}
        calls_by_id: dict[str, str] = {}
        last = len(messages) - 1
        last_key = ""
        for index, message in enumerate(messages):
            role = message.get("role")
            content = message.get("content")
            text = history.text_of(content)
            tokens = overhead + budget_rules.estimate_content(content)
            if role == "system":
                continue
            if role == "assistant":
                totals["reply"] += tokens
                for call in message.get("tool_calls") or []:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "")
                    calls_by_id[str(call.get("id") or "")] = name
                    totals["calls"] += budget_rules.estimate_tokens(name)
                    totals["calls"] += budget_rules.estimate_tokens(
                        str(function.get("arguments") or "")
                    )
                if index == last:
                    last_key = "reply"
                continue
            if role == "tool":
                name = calls_by_id.get(str(message.get("tool_call_id") or ""), "") or "其他工具"
                result_tools[name] = result_tools.get(name, 0) + tokens
                totals["results"] += tokens
                if index == last:
                    last_key = "results"
                continue
            if not text:
                if index == last:
                    last_key = "user"
                continue
            if text.startswith("<摘要>"):
                totals["summary"] += tokens
                if index == last:
                    last_key = "summary"
                continue
            if text.startswith(history.TRACE_DIGEST_HEADER) or text.startswith(
                history.TEAM_DIGEST_PREFIX
            ):
                # An old turn's calls (this bot's, or a teammate's), condensed to one line
                # each by the history assembler.
                totals["calls"] += tokens
                if index == last:
                    last_key = "calls"
                continue
            speaker = self._speaker_name(text)
            if speaker is not None and speaker in names:
                totals["reply"] += tokens
                if index == last:
                    last_key = "reply"
            else:
                totals["user"] += tokens
                if index == last:
                    last_key = "user"
        # The clock and live hints ride inside the newest user turn but are prompt
        # furniture, so they count under the system prompt, as the reference tool does.
        if last_key and messages and messages[-1].get("role") == "user":
            runtime = self._runtime_tokens(conversation, author, trigger)
            moved = min(runtime, totals[last_key])
            totals[last_key] -= moved
            totals["system"] += moved
        categories = [
            {"id": "system", "label": "系统提示词", "tokens": totals["system"]},
            {"id": "memory", "label": "记忆", "tokens": totals["memory"]},
            {"id": "skills", "label": "技能", "tokens": totals["skills"]},
            {"id": "tools", "label": "工具定义", "tokens": totals["tools"]},
            {"id": "user", "label": "你的消息", "tokens": totals["user"]},
            {"id": "reply", "label": "机器人回复", "tokens": totals["reply"]},
            {"id": "calls", "label": "工具调用", "tokens": totals["calls"]},
            {
                "id": "results",
                "label": "工具结果",
                "tokens": totals["results"],
                "items": [
                    {"name": name, "tokens": tokens}
                    for name, tokens in sorted(
                        result_tools.items(), key=lambda pair: pair[1], reverse=True
                    )
                ],
            },
            {"id": "summary", "label": "摘要", "tokens": totals["summary"]},
        ]
        return [category for category in categories if category["tokens"] > 0]

    @staticmethod
    def _scaled_categories(categories: list[dict[str, Any]], total: int) -> list[dict]:
        """Scale the estimated rows to the provider's bigger reported prompt total."""
        base = sum(category["tokens"] for category in categories)
        if base <= 0 or total <= 0:
            return categories
        factor = total / base
        scaled: list[dict[str, Any]] = []
        for category in categories:
            copy = {**category, "tokens": max(0, int(round(category["tokens"] * factor)))}
            if category.get("items"):
                copy["items"] = [
                    {**item, "tokens": max(0, int(round(item["tokens"] * factor)))}
                    for item in category["items"]
                ]
            scaled.append(copy)
        # Rounding must not make the rows disagree with the big number above them.
        diff = total - sum(category["tokens"] for category in scaled)
        if diff:
            biggest = max(range(len(scaled)), key=lambda index: scaled[index]["tokens"])
            scaled[biggest]["tokens"] = max(0, scaled[biggest]["tokens"] + diff)
        return scaled

    def context_usage(self, conversation: dict, author: dict) -> dict:
        """How full this bot's window is, as the composer bar and its panel show it.

        The estimate is the same request a real call would send, built by the same
        assembler: system prompt, tool schemas, the rolling summary and every turn after
        it. A provider's last reported prompt can be bigger than the local estimate (tool
        rendering, tokeniser quirks); then the category rows are scaled to it. Nothing
        here calls a model.
        """
        conversation_id = conversation["id"]
        resolved = resolve_model(self._store, author, self._settings)
        messages, tools, trigger = self._request_for_context(conversation, author)
        estimate = budget_rules.estimate_request(messages, tools)
        window, output = resolved.context_window, resolved.max_output
        usable = budget_rules.usable_budget(window, output)
        threshold = budget_rules.compact_threshold(usable)
        measured = self._last_tokens.get(conversation_id, 0)
        used = max(estimate, measured)
        categories = self._breakdown_categories(messages, tools, conversation, author, trigger)
        if used > estimate:
            categories = self._scaled_categories(categories, used)
        return {
            "conversation_id": conversation_id,
            "agent_id": author["id"],
            "agent_name": author["name"],
            "model": resolved.model,
            "provider": resolved.provider_name,
            "tokens": used,
            "estimated": estimate,
            "measured": measured,
            "window": window,
            "usable": usable,
            "threshold": threshold,
            "threshold_percent": round(threshold * 100 / window, 1) if window > 0 else 0,
            "output_reserve": min(max(0, output), budget_rules.OUTPUT_HEADROOM_TOKENS),
            "buffer": max(0, window - threshold),
            "free": max(0, threshold - used),
            "percent": min(100, round(used * 100 / window)) if window > 0 else 0,
            "categories": categories,
        }

    async def compact_now(self, conversation: dict, author: dict) -> dict:
        """Fold older turns into a summary now, as the panel's 立即压缩 asks.

        It is the same compaction a run does before a big request, forced past the size
        threshold: the user asked, the newest :func:`budget.keep_turns` turns stay verbatim,
        and the answer says how much the next request shrank.
        """
        return await self._compact_from_panel(conversation, author, auto=False)

    async def _compact_from_panel(
        self, conversation: dict, author: dict, *, auto: bool
    ) -> dict:
        """Fold with the composer watching: announce the wait, then the result."""
        before = self.context_usage(conversation, author)["tokens"]
        messages: list[dict[str, Any]] = []
        tools: Sequence[dict[str, Any]] | None = None
        if auto:
            messages, tools, _ = self._request_for_context(conversation, author)
        self._publish_context(conversation["id"], "compacting", auto=auto, before=before)
        active = ActiveRun(run_id="", conversation_id=conversation["id"])
        try:
            changed = await self._compact(
                active, conversation, author, messages, tools=tools, force=not auto
            )
            after = self.context_usage(conversation, author)
        except Exception:
            # Whatever went wrong, the bar must stop saying it is still folding.
            self._publish_context(
                conversation["id"], "idle", auto=auto, before=before, compacted=False
            )
            raise
        self._publish_context(
            conversation["id"],
            "idle",
            auto=auto,
            before=before,
            after=after["tokens"],
            compacted=changed,
        )
        return {"compacted": changed, **after, "before": before, "after": after["tokens"]}

    def compact_after_run(self, conversation_id: str) -> None:
        """Compact a chat a run left above the threshold, so the bar never sits past it.

        Called when a run is finalized. It runs off the run loop's own task (the summary is
        a real model call) and only when nothing else is running in that chat.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # a synchronous test or shutdown: nothing to schedule on
            return
        task = loop.create_task(self._compact_if_full(conversation_id))
        self._compact_tasks.add(task)
        task.add_done_callback(self._compact_tasks.discard)

    async def _compact_if_full(self, conversation_id: str) -> None:
        # A background nicety must never surface as an unhandled task error.
        try:
            await self._fold_if_full(conversation_id)
        except Exception:
            logger.exception("会话 %s 的收尾压缩失败。", conversation_id)

    async def _fold_if_full(self, conversation_id: str) -> None:
        if self.is_running(conversation_id):
            return
        conversation = self._store.get_conversation(conversation_id)
        if conversation is None or conversation["kind"] == "background":
            return
        author = speaker_for(self._store, conversation)
        if author is None:
            return
        try:
            usage = self.context_usage(conversation, author)
        except UpstreamError:
            return
        if usage["tokens"] <= usage["threshold"]:
            return
        await self._compact_from_panel(conversation, author, auto=True)

    def _publish_context(
        self,
        conversation_id: str,
        state: str,
        *,
        auto: bool,
        before: int | None = None,
        after: int | None = None,
        compacted: bool | None = None,
    ) -> None:
        """Tell every open composer that a fold started or ended (and how far it moved)."""
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "state": state,
            "auto": auto,
        }
        if before is not None:
            payload["before"] = before
        if after is not None:
            payload["after"] = after
        if compacted is not None:
            payload["compacted"] = compacted
        self._publish("context", payload)


    async def _compact(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        messages: list[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None,
        force: bool = False,
    ) -> bool:
        """Fold this conversation's older turns into a summary, if the next request is big.

        Runs before a turn builds its first request: the summary is stored for the model, and
        the transcript clients see is untouched. A failed summariser is logged and skipped —
        the request is then trimmed to fit instead, because losing old context beats failing
        the run. ``force`` skips the size check: the panel's 立即压缩 asks for exactly this
        fold, whatever the estimate. A real run announces the wait on the composer's bar;
        the panel's own folds publish their events around this call instead.
        """
        usable = self._usable(author)
        estimate = budget_rules.estimate_request(messages, tools)
        measured = self._last_tokens.get(conversation["id"], 0)
        if not force and max(estimate, measured) <= budget_rules.compact_threshold(usable):
            return False
        conversation_id = conversation["id"]
        everything = self._store.list_messages(conversation_id)
        summary = self._store.latest_summary(conversation_id)
        boundary = self._keep_boundary(everything, conversation, summary, author)
        if boundary is None:
            return False
        since = summary["upto_seq"] if summary is not None else 0
        rows = [row for row in everything if since < row["seq"] <= boundary]
        if not rows:
            return False
        # The summary is a model call: the composer's bar shows it is working.
        if active.run_id:
            active.compaction_attempted = True
            if not active.background:
                self._publish_context(conversation_id, "compacting", auto=True, before=estimate)
        self._set_step(active, f"「{author['name']}」正在压缩较早的对话")
        folded = await self._summarise(
            active,
            conversation,
            author,
            rows=rows,
            previous=summary["summary"] if summary is not None else None,
        )
        if folded is None:
            logger.warning("会话 %s 的摘要生成失败，本次改为裁剪历史。", conversation_id)
            if active.run_id and not active.background:
                self._publish_context(conversation_id, "idle", auto=True, compacted=False)
            return False
        self._store.save_summary(
            conversation_id,
            upto_seq=boundary,
            summary=folded,
            tokens=budget_rules.estimate_tokens(folded),
        )
        self._store.count_compaction(active.run_id)
        self._last_tokens.pop(conversation_id, None)
        self._publish_run(active)
        if active.run_id and not active.background:
            self._publish_context(conversation_id, "idle", auto=True, compacted=True)
        return True

    def _keep_boundary(
        self, rows: Sequence[dict], conversation: dict, summary: dict | None, author: dict
    ) -> int | None:
        """The last message that may be folded away, keeping the newest turns verbatim.

        A private bot chat keeps fewer turns than a conversation a user reads; a wide window
        keeps more of a real work session, so a coding run does not lose its own recent work
        right after compacting. A scheduled run and a group work in their own conversation,
        so their summary is their own too.
        """
        window, _ = self._limits(author)
        keep = budget_rules.keep_turns(window, private=conversation["kind"] == "private")
        return history.keep_boundary(
            rows, keep=keep, since_seq=summary["upto_seq"] if summary is not None else 0
        )

    async def _summarise(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        *,
        rows: Sequence[dict],
        previous: str | None,
    ) -> str | None:
        """Ask this bot's own model for a checkpoint of ``rows``; ``None`` means it failed."""
        traces = [
            row
            for row in self._store.list_tool_traces(conversation["id"])
            if row["seq"] <= rows[-1]["seq"]
        ]
        transcript = history.summary_transcript(rows, traces=traces)
        prompt_text = prompts.summary_prompt(
            transcript=transcript,
            previous=previous,
            background=conversation["kind"] == "background",
        )
        messages = [
            {"role": "system", "content": prompts.SUMMARY_SYSTEM},
            {"role": "user", "content": prompt_text},
        ]
        try:
            stream = self._completion(
                messages, tools=None, author=author, max_tokens=budget_rules.SUMMARY_MAX_TOKENS
            )
        except (UpstreamError, RunStopped):
            return None
        pieces: list[str] = []
        try:
            async for chunk in stream.chunks(should_stop=lambda: active.stop_requested):
                pieces.append(chunk)
            stream.validate(allow_tool_calls=False)
        except RunStopped:
            raise
        except UpstreamError as exc:
            # A checkpoint cut by the output cap still carries everything before the cut;
            # that beats trimming history blind. Any other fault means no summary.
            if getattr(stream.result, "finish_reason", None) != "length":
                logger.warning("摘要生成失败：%s", exc)
                return None
            logger.warning("摘要被长度限制截断，保留已写出的部分。")
        except Exception as exc:  # a summariser fault never ends the run
            logger.warning("摘要生成失败：%s", exc.__class__.__name__)
            return None
        finally:
            self._record_usage(active, stream, author=author, purpose="compact")
        text = "".join(pieces).strip()
        return text or None

    def _record_usage(
        self, active: ActiveRun, stream: Any, *, author: dict, purpose: str
    ) -> None:
        """Add what one model call reported to its run and its agent, and remember the size.

        A provider that reports nothing leaves the counters alone: the local estimate is
        what drives the budget then.
        """
        usage = getattr(getattr(stream, "result", None), "usage", None)
        if usage is None or not usage.total:
            return
        self._store.add_run_usage(
            active.run_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
        )
        self._store.record_usage(
            agent_id=author["id"],
            conversation_id=active.conversation_id,
            # A manual 压缩 has no run of its own; its usage still belongs to the bot.
            run_id=active.run_id or None,
            purpose=purpose,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
        )
        self._last_tokens[active.conversation_id] = usage.input_tokens

def speaker_for(store: Any, conversation: dict) -> dict | None:
    """The bot whose window this conversation's composer meter shows.

    A group is answered for by the supervisor; any other chat by its own member — the
    supervisor also sits in a member's chat, so the first non-supervisor member wins and
    only the supervisor's own chat falls back to it.
    """
    if conversation.get("kind") == "group":
        return store.supervisor()
    for agent_id in conversation.get("member_ids") or []:
        agent = store.get_agent(agent_id)
        if agent is not None and agent["kind"] != "supervisor":
            return agent
    return store.supervisor()
