"""Task-driven supervisor engine: lanes of runs, real model calls only.

A conversation may run several lanes at once: one work lane doing the job and reply lanes
answering newer user messages beside it (up to the launcher's caps). Different conversations
always run in parallel. A run is a queue of agents: the message that starts it decides who
replies first, every completed reply may hand the turn to further agents with ``@名字`` (at
most :data:`RELAY_HOPS` hand-offs, then one conclusion for the user), and whatever the model
produced before a stop, a budget stop or an upstream failure is kept and marked
``interrupted``/``error`` — never completed. Subscribers are engine-wide, so one SSE stream
reports every run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import httpx

from . import approvals
from . import computer as computer_rules
from . import dispatch as dispatch_rules
from . import models, permissions, prompts, schedules, usage as usage_rules
from . import skills as skills_rules
from . import step_budget
from . import workflows as workflow_rules
from .agent import budget as budget_rules
from .agent import dangling as dangling_rules
from .agent import history
from .agent import working_set
from .agent.activity import Activities
from .agent.context import ContextWindow
from .agent.memory import MemoryExtraction
from .agent.reactions import ReactionResponses
from .agent.run_state import (
    ACTIVITY,
    GROUP_TASK,
    LANES_PER_GROUP,
    ActiveRun,
    QueuedAgent,
    TurnBudget,
)
from .store import activities as activity_rules
from .store import group_tasks as task_rules
from .engine_workflows import WorkflowWalking
from .agent.streaming import Streaming
from .config import Settings
from .computer import ComputerHub
from .host_computer import HostComputer
from .connectors import ConnectorManager
from .errors import (
    TurnHandedOff,
    TurnLimited,
    ActionNotAwaiting,
    ActionUnknown,
    EngineBusy,
    RunLimited,
    RunStopped,
    WorkflowPaused,
    WorkflowWaiting,
    ToolError,
    UpstreamError,
)
from .mentions import EVERYONE_NAMES, mentions_everyone, opening_mentions, parse_mentions
from .model_stream import ModelStream, ToolCallFragment, open_stream
from .providers import (
    CLI_PROTOCOLS,
    ResolvedModel,
    model_caps,
    resolve_dispatch,
    resolve_fallback,
    resolve_model,
)
from .constants import STEP_COMPLETED, STEP_ERROR, STEP_LIMITED, STEP_STOPPED
from .store import Store, new_id, now_iso
from .toolkit import ToolInputError
from .tools import ask as ask_tools
from .tools import board as board_tools
from .tools import computer as computer_tools
from .tools import group_tasks as group_task_tools
from .tools import connectors as connector_tools
from .tools import pack as pack_tools
from .tools import reminder as reminder_tools
from .tools import resolve
from .tools import schedule as schedule_tools
from .tools import self_tools
from .tools import skills as skills_tools
from .tools import special as special_tools
from .tools import subtasks as subtask_tools
from .tools import task_threads as task_tool
from .tools import supervisor as supervisor_tools
from .tools import workflow as workflow_tools
from .tools.args import canonical_arguments
from .tools.names import ASK_USER, START_TASK
from .tools.registry import ToolRegistry
from .tools.types import ToolOffer, ToolRunContext, error_result

# A scheduled run's step reaches the schedules panel at most this often.
SCHEDULE_PROGRESS_SECONDS = 1.0
# A task's live payload reaches the group's card at most this often (<= 4/s).
TASK_PROGRESS_SECONDS = 0.25
# Bot-to-bot @ hand-offs with no user message in between: after this many, the last bot (or
# the group's supervisor) closes with one conclusion for the user instead of relaying on.
RELAY_HOPS = 2
# A destructive tool waits for the user; it re-checks the stop flag this often.
CONFIRM_POLL_SECONDS = 0.05
# How long POST /actions/{id}/confirm waits for the run to apply the decision.
CONFIRM_WAIT_SECONDS = 5.0
# The review answers one short JSON verdict, but a thinking model spends its reasoning out of
# the same output allowance; the summariser's cap leaves it that room.
REVIEW_MAX_TOKENS = budget_rules.SUMMARY_MAX_TOKENS
# How much of one older group line a runtime view keeps.
PLAN_LINE_CHARS = 120
# Slots that answer side by side, each blind to the others' answers to the same message.
OPINION_CUE = "opinion"
# Slots that pick up a group task where it stopped, or that a member handed on.
RESUME_CUE = "resume"
TRANSFER_CUE = "transfer"
# The one turn after the relay limit: nobody may be @-ed, the user gets the conclusion.
CONCLUDE_CUE = "conclude"
# A message that is only one of these, in a direct chat, picks up where the last run stopped.
CONTINUE_WORDS = frozenset(
    {"继续", "接着做", "继续做", "接着", "继续吧", "go on", "continue", "resume", "keep going"}
)
# How a run's ending settles the group tasks it left running.
TASK_ENDINGS = {
    "completed": task_rules.DONE,
    "stopped": task_rules.STOPPED,
    "error": task_rules.FAILED,
    "limited": task_rules.FAILED,
}


def is_continue(text: str) -> bool:
    """Whether a message only says 「继续」 (any punctuation around it ignored)."""
    core = str(text).strip().strip("。.!！~～ ").casefold()
    return core in CONTINUE_WORDS


# One subtask is a small piece of the job: its own turn gets a few tools and calls, not the
# whole bot's allowance, so five of them still leave the main turn room to summarise.
SUBTASK_MAX_TOOL_CALLS = 4
SUBTASK_MAX_MODEL_CALLS = 6
# Per-member tool permission packs, by tool name.
CODE_TOOL_NAMES = frozenset({"run_code"})
FILE_TOOL_NAMES = frozenset({"file_list", "file_read", "file_write"})

logger = logging.getLogger(__name__)


class SupervisorEngine(
    Activities,
    Streaming,
    ContextWindow,
    MemoryExtraction,
    ReactionResponses,
    WorkflowWalking,
):
    """Owns every conversation's run, the shared event bus and the run registry."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        # Keyed by run id: a conversation may hold a work lane and reply lanes at once.
        self._runs: dict[str, ActiveRun] = {}
        self._subscribers: set[asyncio.Queue[tuple[str, Any] | None]] = set()
        self._streams_closed = False
        self._client: httpx.AsyncClient | None = None
        # Actions parked on a user decision: the run waits on the event, the request sets it.
        self._confirm_waiters: dict[str, asyncio.Event] = {}
        self._confirm_decisions: dict[str, bool] = {}
        # A bot's question (``ask_user``) waits the same way, but carries a whole answer and
        # never times out: silence is never an answer to a question.
        self._answer_waiters: dict[str, asyncio.Event] = {}
        self._answers: dict[str, dict] = {}
        # The last prompt size a provider reported for a conversation, in tokens.
        self._last_tokens: dict[str, int] = {}
        # Compactions queued when a run ends above the threshold; shutdown cancels them.
        self._compact_tasks: set[asyncio.Task[None]] = set()
        # Sync callbacks invoked after each run's terminal state is persisted (the
        # scheduler records the outcome and looks for queued fires immediately).
        self._run_finished: list[Any] = []
        # Memory extractions still running after their run ended, so shutdown cancels them.
        self._memory_tasks: set[asyncio.Task[None]] = set()
        # Per conversation: the quiet-period timer that flushes its parked turns.
        self._memory_timers: dict[str, asyncio.TimerHandle] = {}
        # Per (conversation, agent): the debounce timer for its reaction response, the
        # messages the user reacted to in the window, and the running response tasks.
        self._reaction_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._reaction_pending: dict[tuple[str, str], list[str]] = {}
        self._reaction_tasks: set[asyncio.Task[None]] = set()
        # Walks started after a run ended; shutdown cancels them with everything else.
        self._pump_tasks: set[asyncio.Task[None]] = set()
        # The launcher is wired in after construction (it needs this engine); the workflow
        # tools queue a walk through it, and it starts the next one when a run ends.
        self.launcher: Any = None
        self.connectors = ConnectorManager(store, self._publish)
        host = (
            None
            if os.environ.get("CUBES_HOST_COMPUTER") == "0"
            else HostComputer(default_cwd=lambda: permissions.working_folder(self._settings))
        )
        # The target is read in ``startup``: building the engine (importing ``app.main``) must
        # never open the database, or a stray import would run a live database's restart
        # recovery and stop the runs another process is serving.
        self.computer = ComputerHub(self._publish, store.computer_token, host=host)
        # The hidden task-thread queue; the app wires it in after construction.
        self.task_scheduler: Any = None
        # When each task's live payload last reached its card (monotonic seconds).
        self._task_progress_at: dict[str, float] = {}
        # The flat prose a thread run has shown so far, for the card's latest line.
        self._task_text: dict[str, str] = {}
        # A bot turn is one activity: its newest prose, when its card last reached clients,
        # and the naming calls still running (one per turn, never repeated).
        self._activity_text: dict[str, str] = {}
        self._activity_published_at: dict[str, float] = {}
        self._activity_title_tasks: dict[str, asyncio.Task[None]] = {}
        self._activity_titled: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    @property
    def client(self) -> httpx.AsyncClient | None:
        """The shared client probes reuse; ``None`` until startup finished."""
        return self._client

    @client.setter
    def client(self, value: httpx.AsyncClient | None) -> None:
        """A narrow seam so tests can swap in a mock transport without a live network."""
        self._client = value

    async def startup(self) -> None:
        self.computer.restore_target(self._store.computer_target())
        # trust_env=False: the model endpoint is on the local/trusted network, so shell
        # proxy variables (HTTP_PROXY/ALL_PROXY) must not hijack these requests.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=self._settings.request_timeout,
                write=30.0,
                pool=10.0,
            ),
            follow_redirects=True,
            trust_env=False,
        )
        await self.connectors.startup()
        self._resume_memory_flushes()

    async def shutdown(self) -> None:
        for active in list(self._runs.values()):
            active.stop_requested = True
            if active.task is not None and not active.task.done():
                await self._cancel(active.task)
        for timer in self._memory_timers.values():
            timer.cancel()
        self._memory_timers.clear()
        for task in list(self._memory_tasks):
            task.cancel()
        if self._memory_tasks:
            await asyncio.gather(*self._memory_tasks, return_exceptions=True)
        self._memory_tasks.clear()
        for timer in self._reaction_timers.values():
            timer.cancel()
        self._reaction_timers.clear()
        self._reaction_pending.clear()
        for task in list(self._reaction_tasks):
            task.cancel()
        if self._reaction_tasks:
            await asyncio.gather(*self._reaction_tasks, return_exceptions=True)
        self._reaction_tasks.clear()
        for task in list(self._pump_tasks):
            task.cancel()
        if self._pump_tasks:
            await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        self._pump_tasks.clear()
        for task in list(self._activity_title_tasks.values()):
            task.cancel()
        if self._activity_title_tasks:
            await asyncio.gather(*self._activity_title_tasks.values(), return_exceptions=True)
        self._activity_title_tasks.clear()
        for task in list(self._compact_tasks):
            task.cancel()
        if self._compact_tasks:
            await asyncio.gather(*self._compact_tasks, return_exceptions=True)
        self._compact_tasks.clear()
        self._runs.clear()
        self._confirm_waiters.clear()
        self._confirm_decisions.clear()
        await self.connectors.shutdown()
        await self.computer.shutdown()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    async def _cancel(task: asyncio.Task[None]) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # ------------------------------------------------------------------ public API

    @staticmethod
    def _alive(active: ActiveRun | None) -> bool:
        return active is not None and active.task is not None and not active.task.done()

    def is_running(self, conversation_id: str | None = None) -> bool:
        """Whether one conversation (or any conversation) currently has a live lane."""
        if conversation_id is None:
            return any(self._alive(active) for active in self._runs.values())
        return any(
            self._alive(active)
            for active in self._runs.values()
            if active.conversation_id == conversation_id
        )

    def live_lanes(self, conversation_id: str) -> list[ActiveRun]:
        """Every live lane of one conversation, oldest first."""
        return [
            active
            for active in self._runs.values()
            if active.conversation_id == conversation_id and self._alive(active)
        ]

    def live_run_in(self, conversation_id: str) -> ActiveRun | None:
        """A live lane in this conversation, preferring the work lane, whatever rows say."""
        lanes = self.live_lanes(conversation_id)
        for active in lanes:
            if active.lane != "reply":
                return active
        return lanes[0] if lanes else None

    def live_run(self, run_id: str) -> ActiveRun | None:
        """The in-process run behind one row, if its task is still alive."""
        active = self._runs.get(run_id)
        return active if self._alive(active) else None

    def add_note(
        self,
        active: ActiveRun,
        *,
        kind: str,
        text: str,
        source_message_id: str | None = None,
    ) -> None:
        """Save one note for a live lane; its loop reads it at the next call boundary."""
        active.notes.append(
            {"kind": kind, "text": text, "source_message_id": source_message_id}
        )

    def _deliver_notes(self, active: ActiveRun, messages: list[dict[str, Any]]) -> bool:
        """Append every waiting note as a user turn; True when one was waiting."""
        notes = active.take_notes()
        for note in notes:
            lead = (
                prompts.NOTE_REQUEST_NOTE
                if note.get("kind") == "request"
                else prompts.NOTE_CONTEXT_NOTE
            )
            messages.append({"role": "user", "content": f"{lead}\n{note['text']}"})
        return bool(notes)

    def start(
        self,
        run_id: str,
        conversation_id: str,
        *,
        message_id: str | None = None,
        schedule_id: str | None = None,
        computer_allowed: bool = False,
        workflow_run_id: str | None = None,
        task_id: str | None = None,
        task_instruction: str | None = None,
        route_override: list[str] | None = None,
        lane: str = "work",
        model_override: Any = None,
    ) -> None:
        """Start one conversation's lane; the caller must have reserved that slot.

        ``schedule_id`` marks a run a scheduled task started: it is offered no schedule
        tools (no recursion) and, when the user pre-approved the sandbox, it starts with
        the computer already allowed so no consent card is shown. ``route_override`` names
        the speakers for this one message (the user's own reroute).
        """
        if run_id in self._runs:
            raise EngineBusy("这次任务已经在运行了。")
        if len(self.live_lanes(conversation_id)) >= LANES_PER_GROUP:
            raise EngineBusy("这个会话同时在跑的任务已经到上限，等其中一路结束再开始。")
        active = ActiveRun(
            run_id=run_id,
            conversation_id=conversation_id,
            lane=lane,
            model_override=model_override,
            trigger_message_id=message_id,
            schedule_id=schedule_id,
            background=self._store.is_background_conversation(conversation_id),
            computer_allowed=computer_allowed,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            task_instruction=task_instruction,
            route_override=list(route_override or []),
            activity_parent=ACTIVITY.get(),
        )
        self._runs[run_id] = active
        active.task = asyncio.create_task(self._supervise(active))

    async def stop(self, run_id: str, *, timeout: float | None = None) -> bool:
        """Request a stop and await full cancellation and persistence. True if active.

        ``timeout`` bounds the cooperative wait before the task is cancelled outright;
        ``None`` uses the configured ``STOP_TIMEOUT``.
        """
        active = self._runs.get(run_id)
        if active is None or active.task is None:
            return False
        active.stop_requested = True
        wait = self._settings.stop_timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(asyncio.shield(active.task), timeout=wait)
        except asyncio.TimeoutError:
            await self._cancel(active.task)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The run already persisted its own terminal state and reason.
            pass
        return True

    async def confirm(self, action_id: str, approve: bool) -> dict:
        """Record the user's answer to a waiting action and return it once applied.

        The decision is handed to the run that is parked on this action; the caller gets
        the action as the run finished it (completed or error), never the stale row.
        """
        action = self._store.get_action(action_id)
        if action is None:
            raise ActionUnknown("操作不存在。")
        waiter = self._confirm_waiters.get(action_id)
        if action["status"] != "awaiting" or waiter is None or waiter.is_set():
            raise ActionNotAwaiting("这个操作已经处理过了。")
        # No await between the check above and the signal: only one answer can win.
        self._confirm_decisions[action_id] = bool(approve)
        waiter.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_WAIT_SECONDS
        while loop.time() < deadline:
            current = self._store.get_action(action_id)
            if current is None or current["status"] != "awaiting":
                return current or action
            await asyncio.sleep(0.02)
        return self._store.get_action(action_id) or action

    async def answer(self, action_id: str, answer: dict) -> dict:
        """Hand the user's answer to the ``ask_user`` question a run is parked on.

        Mirrors :meth:`confirm`: the run applies the answer before this returns, so the
        caller gets the finished row rather than the stale waiting one.
        """
        action = self._store.get_action(action_id)
        if action is None:
            raise ActionUnknown("操作不存在。")
        confirmation = action.get("confirmation") or {}
        waiter = self._answer_waiters.get(action_id)
        if (
            action["status"] != "awaiting"
            or confirmation.get("kind") != "question"
            or waiter is None
            or waiter.is_set()
        ):
            raise ActionNotAwaiting("这个操作已经处理过了。")
        questions = confirmation.get("questions") or []
        if answer.get("skip"):
            payload = {"skip": True}
        else:
            by_index: dict[int, dict] = {}
            for item in answer.get("answers") or []:
                index = item.get("question_index")
                if not isinstance(index, int) or not 0 <= index < len(questions):
                    raise ActionNotAwaiting("回答的问题编号不对。")
                if index in by_index:
                    raise ActionNotAwaiting("同一个问题回答了两遍。")
                question = questions[index]
                allowed = {
                    str(option.get("label")) for option in question.get("options") or []
                }
                labels = [str(label) for label in item.get("labels") or []]
                if any(label not in allowed for label in labels):
                    raise ActionNotAwaiting("回答里有这个问题没有给出的选项。")
                if len(labels) > 1 and not question.get("multi"):
                    raise ActionNotAwaiting("这个问题只能选一个选项。")
                other = str(item.get("other") or "").strip()
                if not labels and not other:
                    raise ActionNotAwaiting("还没有给出回答。")
                by_index[index] = {
                    "question_index": index,
                    "labels": labels,
                    "other": other or None,
                }
            if len(by_index) != len(questions):
                raise ActionNotAwaiting("每个问题都要回答，或选择跳过。")
            payload = {
                "skip": False,
                "answers": [by_index[index] for index in range(len(questions))],
            }
        # No await between the checks above and the signal: only one answer can win.
        self._answers[action_id] = payload
        waiter.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_WAIT_SECONDS
        while loop.time() < deadline:
            current = self._store.get_action(action_id)
            if current is None or current["status"] != "awaiting":
                return current or action
            await asyncio.sleep(0.02)
        return self._store.get_action(action_id) or action

    def on_run_finished(self, callback: Any) -> None:
        """Register a synchronous callback ``(run_id)`` fired once a run is finalized."""
        self._run_finished.append(callback)

    def subscribe(self) -> asyncio.Queue[tuple[str, Any] | None]:
        """One queue fed by every run, so one stream can show the whole workspace."""
        queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        if self._streams_closed:
            # A client that reconnected in the moment before the listener closed.
            queue.put_nowait(None)
            return queue
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def close_streams(self) -> None:
        """End every open event stream, and any opened after: each queue gets ``None``.

        The server calls this as it begins shutting down. An event stream never ends by
        itself, so without it the server waits out its whole graceful-shutdown timeout for
        the webview's stream before it can exit.
        """
        self._streams_closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    @staticmethod
    def drain(queue: asyncio.Queue) -> None:
        """Drop events published before the subscriber's snapshot was taken.

        Callers must invoke this synchronously right after subscribing *and* building the
        snapshot, before yielding socket control; with no await in that window nothing live
        can be queued, so only already-reflected events are removed.
        """
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    # ------------------------------------------------------------------ run loop

    async def _supervise(self, active: ActiveRun) -> None:
        status, error, step = "error", "任务中断：未知错误。", STEP_ERROR
        # A fresh task starts with the context of whoever created it; a run owns no turn yet.
        active.turn = None
        try:
            run = self._store.get_run(active.run_id)
            if run is None:
                return
            conversation = self._store.get_conversation(run["conversation_id"])
            if conversation is None or conversation["kind"] not in (
                "direct",
                "group",
                "background",
                "workflow",
                "thread",
            ):
                raise UpstreamError("会话不存在，任务无法继续。")
            await self._step_loop(active, conversation)
            status, error, step = "completed", None, STEP_COMPLETED
        except WorkflowPaused:
            status, error, step = "stopped", None, "工作流已暂停"
        except WorkflowWaiting:
            status, error, step = "stopped", None, "等你回答"
        except RunLimited as exc:
            status, error, step = "limited", str(exc), STEP_LIMITED
        except RunStopped:
            status, error, step = "stopped", None, STEP_STOPPED
        except UpstreamError as exc:
            status, error, step = "error", str(exc), STEP_ERROR
            active.upstream_error = True
        except asyncio.CancelledError:
            status, error, step = "stopped", None, STEP_STOPPED
            raise
        except Exception as exc:  # defensive: an unexpected fault still ends honestly
            logger.exception("任务 %s 意外中断", active.run_id)
            status, error, step = "error", f"任务中断：{exc.__class__.__name__}。", STEP_ERROR
        finally:
            self._finalize(active, status, error, step)

    async def _step_loop(self, active: ActiveRun, conversation: dict) -> None:
        """Serve the run's queue, or walk a workflow one step at a time."""
        if active.workflow_run_id:
            await self._workflow_loop(active, conversation)
            return
        roster = self._roster(conversation)
        trigger = self._trigger_message(active, conversation)
        self._note_continuing(active, conversation, trigger)
        pending = await self._initial_queue(active, conversation, roster, trigger)
        await self._serve_queue(active, conversation, roster, pending)

    def _note_continuing(
        self, active: ActiveRun, conversation: dict, trigger: dict | None
    ) -> None:
        """「继续」 in a direct chat after a run stopped at its budget: cue the bots to resume.

        A group routes 「继续」 through its dispatcher (``app.dispatch``), which decides
        whether a task resumes; a direct chat has no such call, so the previous run's
        ended-at-a-budget state is the whole signal.
        """
        if conversation["kind"] != "direct" or trigger is None or trigger["kind"] != "user":
            return
        if not is_continue(self._strip_mentions(str(trigger["content"] or ""), conversation)):
            return
        previous = self._store.previous_run(conversation["id"], active.run_id)
        if previous is None:
            return
        if previous["status"] != "limited" and not previous["wrapped"]:
            return
        active.continue_cue = prompts.continue_instruction(int(previous["tool_calls"] or 0) or None)

    @staticmethod
    def _add_continue_cue(messages: list[dict[str, Any]], active: ActiveRun) -> None:
        """Put the 「继续」 cue at the end of the newest user turn, for this run's requests only."""
        if not active.continue_cue:
            return
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, list):
                content.append({"type": "text", "text": active.continue_cue})
            elif isinstance(content, str):
                if active.continue_cue in content:
                    return
                message["content"] = f"{content}\n\n{active.continue_cue}"
            return

    def _strip_mentions(self, text: str, conversation: dict) -> str:
        """The message without its @ names, so 「@甲 继续」 still reads as 「继续」."""
        result = str(text)
        names = [str(agent["name"]) for agent in self._roster(conversation)]
        # Longest first, so 「@原型设计师」 is not cut down to 「设计师」 by 「@原型」.
        for name in sorted([*names, *EVERYONE_NAMES], key=len, reverse=True):
            for marker in ("@", "＠"):
                result = result.replace(f"{marker}{name}", "")
        return " ".join(result.split())

    async def _serve_queue(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        pending: list[QueuedAgent],
    ) -> list[dict]:
        """Let each queued bot reply in turn, queueing whoever a reply @-s; returns the replies.

        The run's own group or chat goes through here, and so does a discussion one bot opens in
        a group from inside its turn (``ask_group``): the same order, @ hand-offs and hop limit.
        """
        hops = self._require_run(active)["hops"]
        # Members who spoke, in order: the supervisor's closing names them.
        spoke: list[str] = []
        replies: list[dict] = []
        while pending:
            if active.stop_requested:
                raise RunStopped("任务已停止。")
            if pending[0].cue == OPINION_CUE:
                batch: list[QueuedAgent] = []
                while pending and pending[0].cue == OPINION_CUE:
                    batch.append(pending.pop(0))
                answered = await self._serve_opinions(active, conversation, roster, batch, pending, hops)
                hops = self._queue_transfers(
                    active,
                    conversation,
                    pending,
                    hops,
                    last_agent_id=answered[-1][0]["id"] if answered else None,
                )
                for agent, message in answered:
                    replies.append(message)
                    if agent["name"] not in spoke:
                        spoke.append(agent["name"])
                queued = {queued.agent_id for queued in pending}
                targets: list[str] = []
                outside: list[str] = []
                for agent, message in answered:
                    for agent_id in message["mentions"]:
                        if agent_id != agent["id"] and agent_id not in queued and agent_id not in targets:
                            targets.append(agent_id)
                    for agent_id in self._outside_mentions(
                        conversation, str(message["content"])
                    ):
                        if agent_id not in outside:
                            outside.append(agent_id)
                ref_agent = answered[-1][0] if answered else None
                ref_message = answered[-1][1] if answered else None
                extra = (
                    1
                    if outside
                    and ref_agent is not None
                    and self._invite_needs_supervisor(conversation, ref_agent)
                    else 0
                )
                if targets or outside:
                    if hops + len(targets) + extra > RELAY_HOPS:
                        self._queue_conclusion(
                            active,
                            conversation,
                            pending,
                            hops,
                            ref_agent["id"] if ref_agent else None,
                            ref_message["id"] if ref_message else None,
                        )
                    else:
                        hops += len(targets)
                        at = next((i for i, slot in enumerate(pending) if slot.cue == "closing"), len(pending))
                        pending[at:at] = [
                            QueuedAgent(agent_id, answered[0][1]["id"])
                            for agent_id in targets
                        ]
                        if outside and ref_agent is not None and ref_message is not None:
                            hops = self._queue_outside_invite(
                                active,
                                conversation,
                                pending,
                                hops,
                                ref_agent,
                                ref_message,
                                outside,
                            )
                        self._advance(active, None, pending, hops)
                continue
            item = pending.pop(0)
            if item.needs:
                # An invite slot waits for the supervisor's membership change; if the person
                # never joined (refused, failed), their turn is skipped and the reply explains.
                fresh = self._store.get_conversation(conversation["id"]) or conversation
                if any(agent_id not in fresh["member_ids"] for agent_id in item.needs):
                    self._advance(active, None, pending, hops)
                    continue
                if fresh["member_ids"] != conversation["member_ids"]:
                    conversation = fresh
                    roster = self._roster(conversation)
            agent = self._store.get_agent(item.agent_id)
            if agent is None:  # the member was removed while it was queued
                self._advance(active, None, pending, hops)
                continue
            self._advance(active, agent["id"], pending, hops, f"「{agent['name']}」正在回复")
            if agent["kind"] == "supervisor" and conversation["kind"] != "thread":
                # A group supervisor's own words are never a task; a thread run is one.
                message = await self._supervisor_reply(
                    active,
                    conversation,
                    roster,
                    item.reply_to_id,
                    closing=spoke if item.cue in ("closing", CONCLUDE_CUE) else None,
                    concluding=item.cue == CONCLUDE_CUE,
                    focus=item.focus or None,
                )
            else:
                message = await self._member_reply(active, conversation, roster, agent, item)
            hops = self._queue_transfers(
                active, conversation, pending, hops, last_agent_id=agent["id"]
            )
            if message is None:  # a reaction-only reply is answered with no text
                continue
            replies.append(message)
            if agent["kind"] != "supervisor" and agent["name"] not in spoke:
                spoke.append(agent["name"])
            if conversation["kind"] == "thread" or item.cue == CONCLUDE_CUE:
                # A thread belongs to one task: its result may name colleagues for the group,
                # but that is a group hand-off, never another turn inside this thread. The
                # conclusion is the last word either way: it may not start another relay.
                continue
            queued = {queued.agent_id for queued in pending}
            targets = [
                agent_id
                for agent_id in message["mentions"]
                if agent_id != agent["id"] and agent_id not in queued
            ]
            outside = self._outside_mentions(conversation, str(message["content"]))
            extra = 1 if self._invite_needs_supervisor(conversation, agent) and outside else 0
            if not targets and not outside:
                continue
            if hops + len(targets) + extra > RELAY_HOPS:
                self._queue_conclusion(
                    active, conversation, pending, hops, agent["id"], message["id"]
                )
                continue
            hops += len(targets)
            # Someone @-ed answers before the supervisor's closing word, so the closing comes
            # last; without a closing slot the queue grows at its end, as it always has.
            at = next((i for i, queued in enumerate(pending) if queued.cue == "closing"), len(pending))
            pending[at:at] = [QueuedAgent(agent_id, message["id"]) for agent_id in targets]
            if outside:
                hops = self._queue_outside_invite(
                    active, conversation, pending, hops, agent, message, outside
                )
            self._advance(active, None, pending, hops)
        return replies

    async def _serve_opinions(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        batch: Sequence[QueuedAgent],
        pending: Sequence[QueuedAgent],
        hops: int,
    ) -> list[tuple[dict, dict]]:
        """Let every slot of ``batch`` answer at once; each sees none of the others' answers.

        Every turn has its own budget (the turn is a context variable), so one member cannot
        spend another's calls. Answers come back in the batch's order, not the order they
        finished in, so the closing and any @ hand-off read the same way every time.
        """
        agents = [agent for agent in (self._store.get_agent(slot.agent_id) for slot in batch) if agent]
        if not agents:
            return []
        names = "、".join(f"「{agent['name']}」" for agent in agents)
        self._advance(active, agents[0]["id"], [*batch[1:], *pending], hops, f"{names}正在各自回答")
        siblings = {agent["id"] for agent in agents}

        async def one(slot: QueuedAgent) -> tuple[dict, dict] | None:
            agent = self._store.get_agent(slot.agent_id)
            if agent is None:
                return None
            message = await self._member_reply(
                active, conversation, roster, agent, slot, blind_to=siblings - {agent["id"]}
            )
            return None if message is None else (agent, message)

        tasks = [asyncio.create_task(one(slot)) for slot in batch]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        return [outcome for outcome in outcomes if outcome is not None]

    async def _group_discussion(
        self, active: ActiveRun, group: dict, asker: dict, question: dict
    ) -> list[dict]:
        """A discussion one bot opened in ``group`` with ``question`` (already posted there).

        Every member of the group except the asker answers the question in turn, then the
        supervisor closes when it was not the asker. No hidden call decides who speaks: the
        group is small and a bot bringing a question wants the whole room's opinion.
        """
        roster = self._roster(group)
        supervisor = self._store.supervisor()
        members = [
            agent for agent in roster if agent["kind"] == "member" and agent["id"] != asker["id"]
        ]
        pending = [QueuedAgent(agent["id"], question["id"], cue="turn") for agent in members]
        # The supervisor closes only when it is in this group; a group without one is fine.
        if (
            supervisor is not None
            and supervisor["id"] != asker["id"]
            and supervisor["id"] in group["member_ids"]
        ):
            pending.append(QueuedAgent(supervisor["id"], question["id"], cue="closing"))
        if not pending:
            return []
        # The discussion's own queue is reported while it runs, then the asker's turn resumes.
        try:
            return await self._serve_queue(active, group, roster, pending)
        finally:
            self._advance(active, asker["id"], [], self._require_run(active)["hops"])

    def _last_speaker(self, conversation: dict) -> dict | None:
        """The group's newest speaker, for routing an un-@ message.

        An un-@ message belongs to whoever last spoke here, the way a person continues a
        conversation with the one they were talking to. A card counts as that member working.
        Notices, actions, the user and authors no longer in the group are ignored.
        """
        member_ids = {str(agent_id) for agent_id in conversation["member_ids"]}
        for row in reversed(self._store.list_messages(conversation["id"])):
            if row["kind"] not in ("agent", "task"):
                continue
            author_id = row.get("author_id")
            if author_id not in member_ids:
                continue
            agent = self._store.get_agent(author_id)
            if agent is not None:
                return agent
        return None

    # ------------------------------------------------------------------ invites

    def _outside_mentions(self, conversation: dict, text: str) -> list[str]:
        """Team agents @-ed in a bot's own words who are not in this group, in order."""
        if not text:
            return []
        members = set(str(agent_id) for agent_id in conversation["member_ids"])
        outside = [
            agent
            for agent in self._store.list_agents()
            if str(agent["id"]) not in members
        ]
        return [
            str(agent_id)
            for agent_id in parse_mentions(text, outside)
            if str(agent_id) not in members
        ][: dispatch_rules.MAX_SPEAKERS]

    def _invite_needs_supervisor(self, conversation: dict, author: Mapping[str, Any]) -> bool:
        """Whether this @ to an outsider goes to the group's supervisor rather than the engine."""
        boss = self._supervisor()
        return bool(
            boss is not None
            and str(boss["id"]) in conversation["member_ids"]
            and str(author.get("id")) != str(boss["id"])
        )

    def _add_members(self, active: ActiveRun, conversation: dict, agent_ids: Sequence[str]) -> dict:
        """Pull team agents into the group, with the one quiet notice that says so."""
        fresh = self._store.get_conversation(conversation["id"]) or conversation
        wanted = [str(agent_id) for agent_id in agent_ids]
        added = [agent_id for agent_id in wanted if agent_id not in fresh["member_ids"]]
        if not added:
            return fresh
        members = [*fresh["member_ids"], *added]
        updated = self._store.update_conversation(fresh["id"], member_ids=members) or fresh
        names = "、".join(self._agent_name(agent_id) for agent_id in added)
        self._append_notice(active, conversation, f"已把 {names} 拉进群")
        self._publish_workspace()
        return updated

    def _append_invite_notice(
        self,
        active: ActiveRun,
        conversation: dict,
        agent_id: str,
        message_id: str | None,
    ) -> None:
        """A quiet row offering 「拉进群」 when the group has no supervisor to decide it."""
        name = self._agent_name(agent_id)
        message = self._store.append_message(
            conversation_id=conversation["id"],
            author_id=None,
            author_name="系统",
            avatar_seed=None,
            kind="notice",
            content=f"{name} 不在这个群里",
            status="complete",
            run_id=active.run_id,
            reply_to_id=message_id,
            invite={"agent_id": agent_id, "name": name, "message_id": message_id},
        )
        self._publish_message("message_start", message)
        self._publish_message("message_end", message)

    def _queue_outside_invite(
        self,
        active: ActiveRun,
        conversation: dict,
        pending: list[QueuedAgent],
        hops: int,
        author: Mapping[str, Any],
        message: Mapping[str, Any],
        outside_ids: Sequence[str],
    ) -> int:
        """Turn a member's @ to an outsider into a pull-in turn, as the relay's next hop.

        With a supervisor in the group it gets the membership turn and the invited bot answers
        after it; without one the engine adds the person at once when the supervisor wrote the
        @, and otherwise leaves a notice the user can confirm.
        """
        names = [self._agent_name(agent_id) for agent_id in outside_ids]
        sentence = str(message.get("content") or "")
        slots: list[QueuedAgent] = []
        boss = self._supervisor()
        if self._invite_needs_supervisor(conversation, author) and boss is not None:
            slots.append(
                QueuedAgent(
                    str(boss["id"]),
                    str(message["id"]),
                    cue="invite",
                    focus=prompts.bot_invite_instruction(
                        str(author.get("name") or ""), names, sentence
                    ),
                )
            )
            for agent_id in outside_ids:
                slots.append(
                    QueuedAgent(
                        str(agent_id),
                        str(message["id"]),
                        cue="turn",
                        focus=prompts.bot_invited_focus_instruction(
                            str(author.get("name") or ""), sentence
                        ),
                        needs=tuple(str(agent_id) for agent_id in outside_ids),
                    )
                )
        elif boss is not None and str(boss["id"]) in conversation["member_ids"]:
            # The supervisor itself @-ed them: it already has set_group_members.
            fresh = self._add_members(active, conversation, outside_ids)
            for agent_id in outside_ids:
                if str(agent_id) not in fresh["member_ids"]:
                    continue
                slots.append(
                    QueuedAgent(
                        str(agent_id),
                        str(message["id"]),
                        cue="turn",
                        focus=prompts.bot_invited_focus_instruction(str(boss["name"]), sentence),
                    )
                )
        else:
            # No supervisor to ask: the user gets the button instead of a silent drop.
            for agent_id in outside_ids:
                self._append_invite_notice(active, conversation, str(agent_id), str(message["id"]))
            return hops
        if not slots:
            return hops
        at = next((i for i, slot in enumerate(pending) if slot.cue == "closing"), len(pending))
        pending[at:at] = slots
        return hops + (1 if boss is not None and self._invite_needs_supervisor(conversation, author) else 0)

    # ------------------------------------------------------------------ replies

    async def _supervisor_reply(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        reply_to_id: str | None,
        *,
        closing: Sequence[str] | None = None,
        concluding: bool = False,
        focus: str | None = None,
    ) -> dict | None:
        """Run the supervisor's native tool loop until it produces a final answer.

        ``closing`` names the members who just spoke in a discussion (``ask_group``): its turn
        then ends on a short closing word instead of a fresh answer. ``concluding`` is the
        turn after a spent relay: it writes the user a short conclusion and may not @ anyone.
        A progress question never reaches here: the dispatcher's card answers it with no agent.
        """
        author = self._supervisor()
        # notify_user is allowed once per bot turn, and this reply starts a fresh turn.
        active.notified.discard((conversation["id"], author["id"]))
        trigger = self._trigger_text(active, conversation)
        # The closing speaks to the message its slot answers: the user's, or the question a bot
        # brought to this group (``ask_group``).
        answered = (
            self._store.get_message(reply_to_id)
            if (closing or concluding) and reply_to_id
            else None
        )
        if focus is not None:
            cue = focus
        elif concluding and answered is not None:
            cue = prompts.relay_conclusion_instruction(answered["content"], closing or ())
        else:
            cue = (
                prompts.closing_instruction(
                    answered["content"],
                    closing,
                    None if answered["kind"] == "user" else answered["author_name"],
                )
                if closing and answered is not None
                else None
            )

        def context() -> list[dict[str, Any]]:
            return self._supervisor_context(conversation, trigger=trigger, cue=cue)

        # A group's supervisor is its coordinator, so the tools that do the work stay out of
        # reach; the team setting (or an opening @) gives them back.
        coordinating = self._supervisor_coordinates(active, conversation, author)

        def tools() -> list[dict[str, Any]]:
            return self._schemas(
                "supervisor",
                conversation,
                author,
                scheduled=active.schedule_id is not None,
                coordinating=coordinating,
                read_only=active.lane == "reply",
            )

        messages = await self._turn_context(active, conversation, author, tools(), context)
        self._add_continue_cue(messages, active)
        message: dict | None = None
        pending_nudge = False
        nudged = False
        said: list[str] = []
        reply_mood: str | None = None
        reply_images: list[str] = []
        reply_reaction: str | None = None
        async with self._activity_turn(active, conversation, author, reply_to_id) as turn:
            while True:
                if active.stop_requested:
                    raise RunStopped("任务已停止。")
                # A note another lane left joins here, at a call boundary, never mid-stream.
                self._deliver_notes(active, messages)
                tools_now = tools()
                stream, current, body, mood, images, reaction = await self._stream_reply(
                    active,
                    conversation,
                    author,
                    messages=messages,
                    # Recomputed per call: a sandbox that disconnects mid-turn must lose its
                    # tools before the next request, not keep offering them.
                    tools=tools_now,
                    reply_to_id=reply_to_id,
                    allow_tool_calls=True,
                    silent=pending_nudge,
                )
                calls = stream.tool_calls
                nudge = False
                if pending_nudge:
                    # The follow-up continues the same reply: its words join the row the
                    # first call opened, which stayed open on purpose. The note itself is
                    # for that one call only; the rest of the turn reads a clean transcript.
                    pending_nudge = False
                    if messages and messages[-1].get("content") == prompts.ASK_FOLLOW_UP_NOTE:
                        messages.pop()
                    said.append(body)
                    if message is not None:
                        message = self._complete_message(
                            active,
                            message,
                            "".join(said),
                            reply_mood,
                            roster,
                            reply_images,
                            reply_reaction,
                            calls,
                            tool_started=stream.saw_tool_call,
                        )
                        if message is not None:
                            self._publish_message("message_end", message)
                else:
                    said = [body]
                    reply_mood, reply_images, reply_reaction = mood, images, reaction
                    nudge = not calls and not concluding and self._should_nudge(
                        active, author, tools_now, body, nudged
                    )
                    if current is not None and not nudge:
                        current = self._complete_message(
                            active,
                            current,
                            body,
                            mood,
                            roster,
                            images,
                            reaction,
                            calls,
                            tool_started=stream.saw_tool_call,
                        )
                        if current is not None:
                            self._publish_message("message_end", current)
                    message = current or message
                if not calls:
                    # The reply is done but a note may have landed while it streamed: read
                    # it now, so the next call answers with it in context.
                    if active.notes:
                        if body:
                            messages.append({"role": "assistant", "content": body})
                        self._deliver_notes(active, messages)
                        continue
                    if nudge:
                        # The reply stopped on a lead-in without ask_user: one more call,
                        # with the rest of the turn told to ask or finish.
                        nudged = True
                        messages.append({"role": "assistant", "content": body})
                        messages.append(
                            {"role": "user", "content": prompts.ASK_FOLLOW_UP_NOTE}
                        )
                        pending_nudge = True
                        continue
                    self._note_turn_wrap(active, conversation, turn)
                    return message
                messages.append(self._assistant_tool_turn(body, calls))
                try:
                    await self._run_tool_calls(active, conversation, calls, author, messages)
                except TurnLimited as exc:
                    # This bot's turn spent its budget: its text so far stands, the run goes on.
                    active.turn_limited[author["id"]] = str(exc)
                    self._note_turn_wrap(active, conversation, turn)
                    return message
                except TurnHandedOff:
                    # A tool handed the work off (a workflow's own page): stop here, so the
                    # bot does not go on to do the same work itself.
                    return message

    async def _member_reply(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        agent: dict,
        item: QueuedAgent,
        *,
        blind_to: set[str] | frozenset[str] = frozenset(),
    ) -> dict | None:
        """One member answers the message that mentioned it, with its own tools.

        ``blind_to`` names the members answering the same message beside it: their answers
        stay out of its history, so each opinion is its own. A thread conversation is a task
        run of its own (see :meth:`_thread_reply`); in a group a resume or a transfer of a
        threaded task goes to that thread's scheduler instead of running here.

        A plain group answer makes no task row: a task exists only when the member calls
        ``start_task`` (which creates it) or a part is handed on with ``group_assign``.
        """
        if conversation["kind"] == "thread":
            return await self._thread_reply(active, conversation, agent, item)
        answering = self._store.get_message(item.reply_to_id) if item.reply_to_id else None
        if answering is not None and answering["conversation_id"] != conversation["id"]:
            answering = None
        if conversation["kind"] != "group":
            answering = None
        if conversation["kind"] == "group":
            threaded = self._threaded_target(conversation, agent, item)
            if threaded is not None:
                await self._resume_thread_task(threaded, agent, item)
                return None
        trigger = self._trigger_text(active, conversation)
        note = item.focus or (
            prompts.relay_conclusion_instruction(answering["content"], ())
            if item.cue == CONCLUDE_CUE and answering is not None
            else None
        )
        messages = await self._turn_context(
            active,
            conversation,
            agent,
            self._schemas(
                "member", conversation, agent, read_only=active.lane == "reply"
            ),
            lambda: self._member_context(
                conversation,
                roster,
                agent,
                answering=answering,
                taking_turn=item.cue in ("turn", OPINION_CUE),
                opinion=item.cue == OPINION_CUE,
                hidden=self._sibling_answers(conversation, item.reply_to_id, blind_to),
                trigger=trigger,
                note=note,
            ),
        )
        _, message = await self._member_call(
            active,
            conversation,
            agent,
            messages,
            item.reply_to_id,
            roster,
            concluding=item.cue == CONCLUDE_CUE,
        )
        return message

    # ------------------------------------------------------------------ task threads

    def _thread_of(self, task: dict, agent: dict) -> dict:
        """The task's thread, created on first use and re-owned when the task changes hands."""
        thread_id = task.get("thread_id")
        thread = self._store.get_conversation(thread_id) if thread_id else None
        if thread is not None:
            if thread["member_ids"] != [agent["id"]]:
                # A transfer or a resume by another bot: the thread follows its owner.
                thread = (
                    self._store.update_conversation(thread["id"], member_ids=[agent["id"]])
                    or thread
                )
            return thread
        thread = self._store.create_conversation(
            kind="thread",
            title=f"{agent['name']} · {task['title']}",
            # The contract's ``purpose``: the title of the task this thread works on.
            purpose=task["title"],
            member_ids=[agent["id"]],
        )
        self._store.update_group_task(task["id"], thread_id=thread["id"])
        return thread

    def _append_task_card(
        self, active: ActiveRun, group: dict, agent: dict, task: dict
    ) -> dict:
        """The one card the group shows for this task; the client draws it from live tasks.

        ``streaming`` while the work runs, so it is neither a finished reply nor unread; the
        settle paths flip it to ``complete`` (done) or ``interrupted`` (stopped/failed), and
        only the latter is counted as unread for the group.
        """
        card = self._store.append_message(
            conversation_id=group["id"],
            author_id=agent["id"],
            author_name=agent["name"],
            avatar_seed=agent["avatar_seed"],
            kind="task",
            content=task["title"],
            status="streaming",
            reply_to_id=task["request_message_id"],
            run_id=active.run_id,
            task_id=task["id"],
        )
        self._publish_message("message_start", card)
        self._publish_message("message_end", card)
        return card

    def _touch_task_card(self, task: dict, *, status: str, content: str | None = None) -> None:
        """Move a task's card to its final state, if it has one."""
        card_id = task.get("card_message_id")
        if not card_id:
            return
        updated = self._store.update_message(
            card_id, status=status, **({"content": content} if content is not None else {})
        )
        if updated is not None:
            self._publish_message("message_end", updated)

    def _threaded_target(
        self, conversation: dict, agent: dict, item: QueuedAgent
    ) -> dict | None:
        """The threaded task this group slot continues, if it continues one at all.

        Only a resume or a transfer names a task; a plain answer, a reply to someone's
        message and a re-dispatch all stay in the group (the user's 「接着做」 goes through
        the dispatcher or the task's own endpoint, never a quoted reply).
        """
        task = self._store.get_group_task(item.task_id) if item.task_id else None
        if task is not None and item.cue in (RESUME_CUE, TRANSFER_CUE):
            return task if task.get("thread_id") else None
        return None

    async def _resume_thread_task(
        self, task: dict, agent: dict, item: QueuedAgent
    ) -> dict | None:
        """Queue a thread run for a resumed or transferred task, and end this group turn."""
        if self.task_scheduler is None:  # pragma: no cover - the app wires it in
            return None
        self._thread_of(task, agent)
        if item.cue == TRANSFER_CUE:
            giver = self._agent_name(task["assigned_by"]) if task.get("assigned_by") else "同事"
            instruction = prompts.transfer_instruction(
                task["instruction"] or task["title"], giver, item.note
            )
            return await self.task_scheduler.enqueue(task["id"], instruction=instruction)
        rounds = int(task["rounds"] or 1) + 1
        instruction = prompts.resume_instruction(
            task["instruction"] or task["title"], rounds
        )
        return await self.task_scheduler.enqueue(
            task["id"], instruction=instruction, another_round=True
        )

    async def _start_task(
        self,
        active: ActiveRun,
        task: dict | None,
        title: str,
        plan: str,
        *,
        owner_id: str | None = None,
    ) -> dict:
        """Turn this chat turn into a hidden thread run (``start_task``).

        A plain reply makes no task row, so the row is created here — the first moment the
        bot says the work needs a thread. The card appears right away, so the chat sees the
        work from its first second; the actual turn runs in the task's thread. ``owner_id``
        is the supervisor handing the work to a member: the card and the thread are theirs,
        and the task never waits for the supervisor's own lane.
        """
        chat = self._store.get_conversation(active.conversation_id) or {}
        author = self._store.get_agent(active.turn.agent_id) if active.turn else None
        if task is None:
            request = self._trigger_message(active, chat) if chat else None
            if (
                author is None
                or not chat
                or chat["kind"] not in ("group", "direct")
                or request is None
                or request["kind"] not in ("user", "agent")
            ):
                raise UpstreamError("任务已经失效，无法开始。")
            owner = self._store.get_agent(owner_id) if owner_id else author
            if owner is None:
                raise UpstreamError("要交给的成员不在了，无法开始。")
            task = self._store.create_group_task(
                conversation_id=chat["id"],
                owner_id=owner["id"],
                title=title,
                instruction=str(request.get("content") or title),
                request_message_id=request["id"],
                assigned_by=author["id"] if owner["id"] != author["id"] else None,
                run_id=None,
                status=task_rules.QUEUED,
            )
        owner_ref = owner_id or (task["owner_id"] if task else None)
        agent = self._store.get_agent(owner_ref) if owner_ref else None
        if agent is None or not chat:  # pragma: no cover - the caller's own task
            raise UpstreamError("任务已经失效，无法开始。")
        instruction = task.get("instruction") or task["title"]
        if plan:
            instruction = f"{instruction}\n\n{agent['name']}的计划：{plan}"
        changes: dict[str, Any] = {
            "title": title,
            "instruction": instruction,
            "owner_id": agent["id"],
        }
        if author is not None and author["id"] != agent["id"]:
            changes["assigned_by"] = author["id"]
        task = self._store.update_group_task(task["id"], **changes) or task
        self._thread_of(task, agent)
        card = self._append_task_card(active, chat, agent, task)
        task = self._store.update_group_task(
            task["id"],
            card_message_id=card["id"],
            status=task_rules.QUEUED,
            run_id=None,
            current_step="排队中",
            started_at=None,
            latest_text="",
        ) or task
        opening = self._thread_opening(chat, task, task.get("request_message_id"))
        if plan:
            opening = f"{opening}\n\n你打算这样做：{plan}"
        self._publish_group_tasks(None, chat["id"])
        self._publish_workspace()
        if self.task_scheduler is None:  # pragma: no cover - the app wires it in
            raise UpstreamError("任务通道没有就绪，暂时无法开始。")
        # A reply lane that starts its own bot's work waits for that bot's live lane. Work
        # handed to another member runs in that member's thread and needs no wait.
        behind = active.lane == "reply" and author is not None and author["id"] == agent["id"]
        started = await self.task_scheduler.enqueue(
            task["id"], instruction=opening, behind_lane=behind
        )
        return started or task

    async def _thread_reply(
        self, active: ActiveRun, thread: dict, agent: dict, item: QueuedAgent
    ) -> dict | None:
        """A run of a task's own thread: the member works there and answers the group once."""
        task = self._store.task_of_thread(thread["id"])
        group = self._store.get_conversation(task["conversation_id"]) if task else None
        if task is None or group is None:
            # The task or its group is gone: answer here, with nowhere to post a result.
            roster = self._roster(thread)
            messages = await self._turn_context(
                active,
                thread,
                agent,
                self._schemas("member", thread, agent),
                lambda: self._member_context(
                    thread, roster, agent, trigger=str(thread.get("purpose") or "")
                ),
            )
            _, message = await self._member_call(
                active, thread, agent, messages, item.reply_to_id, roster
            )
            return message
        # A scheduler-started run already marked the task running; a manual post in the
        # thread makes the task live again and counts one more round.
        if active.task_instruction is None and (
            task["run_id"] != active.run_id or task["status"] != task_rules.RUNNING
        ):
            rounds = task["status"] in (task_rules.DONE, task_rules.STOPPED, task_rules.FAILED)
            task = self._store.update_group_task(
                task["id"],
                status=task_rules.RUNNING,
                owner_id=agent["id"],
                run_id=active.run_id,
                another_round=rounds,
                current_step="进行中",
            ) or task
            self._touch_task_card(task, status="streaming", content=task["title"])
            self._publish_group_tasks(None, group["id"])
        active.task_id = task["id"]
        instruction = active.task_instruction
        if instruction is None and item.reply_to_id and is_continue(
            self._strip_mentions(self._trigger_text(active, thread), thread)
        ):
            instruction = prompts.resume_instruction(
                task["instruction"] or task["title"], int(task["rounds"])
            )
        roster = self._roster(group)
        trigger = self._trigger_text(active, thread) or str(task["title"])
        messages = await self._turn_context(
            active,
            thread,
            agent,
            self._schemas("member", group, agent),
            lambda: self._thread_context(
                thread,
                group,
                roster,
                agent,
                task,
                instruction=instruction,
                trigger=trigger,
                reply_to_id=item.reply_to_id,
            ),
        )
        token = GROUP_TASK.set(task["id"])
        try:
            body, thread_message = await self._member_call(
                active,
                thread,
                agent,
                messages,
                item.reply_to_id,
                roster,
                surface=group,
            )
        finally:
            GROUP_TASK.reset(token)
        # ``group_transfer`` inside the thread hands the work to its next owner's run.
        if active.transfers:
            await self._hand_thread_transfers(active)
            return thread_message
        current = self._store.get_group_task(task["id"])
        if (
            current is None
            or current["status"] != task_rules.RUNNING
            or current["owner_id"] != agent["id"]
        ):
            return thread_message
        result = self._post_task_result(active, group, agent, task, body, thread_message)
        self._close_task(task, result)
        return result

    async def _hand_thread_transfers(self, active: ActiveRun) -> None:
        """Queue the next owner of every task a thread turn handed on."""
        while active.transfers:
            task_id, owner_id, note = active.transfers.pop(0)
            task = self._store.get_group_task(task_id)
            agent = self._store.get_agent(owner_id)
            if task is None or agent is None or self.task_scheduler is None:
                continue
            self._thread_of(task, agent)
            giver = self._agent_name(task["assigned_by"]) if task.get("assigned_by") else "同事"
            instruction = prompts.transfer_instruction(
                task["instruction"] or task["title"], giver, note
            )
            await self.task_scheduler.enqueue(task["id"], instruction=instruction)

    def _post_task_result(
        self,
        active: ActiveRun,
        group: dict,
        agent: dict,
        task: dict,
        body: str,
        thread_message: dict | None,
    ) -> dict:
        """The task's one group reply: the thread's last words, or a short status line."""
        content = (body or "").strip()
        if content:
            mood = (thread_message or {}).get("mood")
        else:
            content = prompts.task_result_line(agent["name"], task["title"])
            mood = None
        images = [str(image["id"]) for image in (thread_message or {}).get("attachments") or []]
        roster = self._roster(group)
        result = self._store.append_message(
            conversation_id=group["id"],
            author_id=agent["id"],
            author_name=agent["name"],
            avatar_seed=agent["avatar_seed"],
            kind="agent",
            content=content,
            status="complete",
            mentions=parse_mentions(content, roster),
            reply_to_id=task["request_message_id"],
            mood=mood,
            attachment_ids=images,
            run_id=active.run_id,
        )
        self._publish_message("message_start", result)
        self._publish_message("message_end", result)
        return result

    def _thread_context(
        self,
        thread: dict,
        group: dict,
        roster: Sequence[Mapping[str, str]],
        agent: dict,
        task: dict,
        *,
        instruction: str | None,
        trigger: str,
        reply_to_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """A thread turn's request: the thread's own history plus a compact view of the chat.

        The system prompt states the surface (a group's title, rules and members, or the user's
        own chat) because that is who the answer is for; the working set is keyed to that chat,
        so files stay shared between the task's thread and everyone else working there.
        """
        direct = group["kind"] == "direct"
        system = prompts.member_system(
            agent=agent,
            conversation_kind="thread",
            title=group["title"],
            purpose=task["title"],
            roster=roster,
            team_roster=self._team_roster(agent),
            groups=self._member_groups(agent),
            memory=self._memory_block(group, agent, trigger),
            skills=self._skills_block(agent),
            hints=(prompts.group_rules_block(group.get("rules") or ""),)
            if not direct
            else (),
            thread_surface="direct" if direct else "group",
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(
            self._history_turns(
                thread,
                agent,
                vision=self._sees_images(agent),
                pdf=self._reads_pdf(agent),
            )
        )
        opening = instruction
        if opening is None and (not messages or messages[-1].get("role") != "user"):
            opening = self._thread_opening(group, task, reply_to_id)
        if opening is not None:
            messages.append({"role": "user", "content": opening})
        view = prompts.thread_group_view(
            group["title"], self._recent_group_lines(group), direct=direct
        )
        history.attach_runtime(
            messages,
            moment=self._now(),
            timezone=self._settings.timezone,
            hints=[
                *self._runtime_hints(group, agent, trigger),
                self._sibling_tasks_hint(group, task),
                view,
            ],
        )
        return messages

    def _thread_opening(self, group: dict, task: dict, reply_to_id: str | None) -> str:
        """What the empty thread says first: the group message this task came from.

        A user's message keeps its quote (a reply to a member's answer); a hand-off names the
        teammate that started it. Anything else falls back to the task's own wording.
        """
        answered = self._store.get_message(reply_to_id) if reply_to_id else None
        where = "聊天里" if group["kind"] == "direct" else "群里"
        if answered is not None and answered["conversation_id"] == group["id"]:
            if answered["kind"] == "user":
                quoted = (
                    self._store.get_message(answered["reply_to_id"])
                    if answered.get("reply_to_id")
                    else None
                )
                text = history.user_turn_content(
                    answered, {quoted["id"]: quoted} if quoted is not None else {}
                )
                opening = prompts.thread_work_instruction(task["title"], text, where=where)
                giver = (
                    self._store.get_agent(str(task["assigned_by"]))
                    if task.get("assigned_by")
                    else None
                )
                if giver is not None and giver["id"] != task["owner_id"]:
                    opening = f"「{giver['name']}」把用户这件事交给你，由你负责到底。\n\n{opening}"
                return opening
            if answered["kind"] == "agent":
                return prompts.mention_instruction(answered["author_name"], answered["content"])
        return prompts.thread_task_instruction(
            task["instruction"] or task["title"], task["title"], where=where
        )

    def _sibling_tasks_hint(self, group: dict, task: dict) -> str:
        """The group's other live task threads, so two members do not fight over one file."""
        now = self._now()
        lines: list[str] = []
        for other in self._store.list_group_tasks(group["id"], limit=20):
            if other["id"] == task["id"] or other["status"] != task_rules.RUNNING:
                continue
            if not other.get("thread_id"):
                continue
            owner = self._store.get_agent(other["owner_id"])
            name = owner["name"] if owner is not None else "同事"
            parts = [f"「{name}」{other['title']}"]
            if other.get("current_step"):
                parts.append(str(other["current_step"]))
            files = [
                entry["path"]
                for entry in self._store.list_working_files(group["id"], limit=8)
                if entry.get("author_id") == other["owner_id"]
            ]
            if files:
                parts.append("最近碰过 " + "、".join(files[:3]))
            parts.append(working_set.age(str(other["updated_at"]), now))
            lines.append("，".join(parts))
        if not lines:
            return ""
        place = "这个聊天里" if group["kind"] == "direct" else "同一个群里"
        return (
            f"<siblings>\n{place}还有这些任务在同时做（不要改对方正在做的文件，"
            "需要它时先等它或问用户）：\n" + "\n".join(lines) + "\n</siblings>"
        )

    def _recent_group_lines(self, group: dict, limit: int = 6) -> list[str]:
        """The last few real lines of the group, for a thread turn's compact view."""
        rows = [
            row
            for row in self._store.list_messages(group["id"])
            if row["kind"] in ("user", "agent") and str(row["content"]).strip()
        ]
        return [
            f"[{row['author_name']}] {' '.join(str(row['content']).split())[:PLAN_LINE_CHARS]}"
            for row in rows[-limit:]
        ]

    # ------------------------------------------------------------------ group tasks

    def note_work_activity(
        self,
        activity_id: str,
        *,
        kind: str,
        text: str,
        source_message_id: str | None = None,
    ) -> bool:
        """Leave a note on one live work activity and wake the lane serving it."""
        activity = self._store.get_activity(activity_id)
        if activity is None or activity["status"] not in activity_rules.UNFINISHED:
            return False
        self._store.add_activity_note(
            activity_id,
            kind=kind,
            text=text,
            source_message_id=source_message_id,
        )
        active = self.live_run(str(activity.get("run_id") or ""))
        if active is not None:
            self.add_note(
                active, kind=kind, text=text, source_message_id=source_message_id
            )
        return True

    async def _steer_task(self, conversation: dict, task_id: str | None, trigger: dict) -> bool:
        """Deliver one user message into the task it is really about.

        A running (or waiting) task's work activity takes it as a ``request`` note at its
        next call boundary; a queued one keeps it for the opening its thread starts with.
        ``False`` when the task is gone or already finished, so the caller can still answer
        in the group.
        """
        task = self._store.get_group_task(task_id) if task_id else None
        if task is None or task["conversation_id"] != conversation["id"]:
            return False
        if task.get("dismissed_at"):
            return False
        text = str(trigger.get("content") or "").strip()
        if not text:
            return False
        if task["status"] in (task_rules.RUNNING, task_rules.AWAITING):
            active = self.live_run(str(task.get("run_id") or ""))
            if active is None:
                return False
            noted = False
            for activity in self._store.activities_of_run(
                str(task.get("run_id") or ""), unfinished=True
            ):
                if str(activity.get("agent_id") or "") != str(task["owner_id"]):
                    continue
                noted = (
                    self.note_work_activity(
                        str(activity["id"]),
                        kind="request",
                        text=text,
                        source_message_id=str(trigger.get("id") or "") or None,
                    )
                    or noted
                )
            return noted
        if task["status"] == task_rules.QUEUED and self.task_scheduler is not None:
            updated = self.task_scheduler.note_instruction(str(task["id"]), text)
            if updated is not None:
                self._publish_group_tasks(None, conversation["id"])
                self._publish_workspace()
            return updated is not None
        return False

    async def _resume_task(self, conversation: dict, task_id: str | None, trigger: dict) -> bool:
        """Continue exactly one stopped/failed task in its own thread."""
        task = self._store.get_group_task(task_id) if task_id else None
        if task is None or task["conversation_id"] != conversation["id"]:
            return False
        if task.get("dismissed_at") or task["status"] not in (task_rules.STOPPED, task_rules.FAILED):
            return False
        owner = self._store.get_agent(str(task["owner_id"]))
        if owner is None or self.task_scheduler is None:
            return False
        await self._resume_thread_task(
            task,
            owner,
            QueuedAgent(
                owner["id"], str(trigger.get("id") or ""), cue=RESUME_CUE, task_id=task["id"]
            ),
        )
        return True

    def _post_status_card(self, active: ActiveRun, conversation: dict) -> None:
        """Answer a progress question without any agent: one structured card in the group."""
        card = self._store.append_message(
            conversation_id=conversation["id"],
            author_id=None,
            author_name="进度",
            avatar_seed=None,
            kind="status",
            content="",
            status="complete",
            run_id=active.run_id,
            status_card=self._status_payload(conversation),
        )
        self._publish_message("message_start", card)
        self._publish_message("message_end", card)

    def _status_payload(self, conversation: dict) -> dict[str, Any]:
        """The progress card's data: every unfinished task, and who is free.

        Built from the group's own rows at request time — the same facts the card renders —
        so it is what the group knows now, not what some bot remembers of it.
        """
        by_owner: dict[str, dict] = {
            agent["id"]: agent
            for agent in (
                self._store.get_agent(agent_id) for agent_id in conversation["member_ids"]
            )
            if agent is not None and agent["kind"] == "member"
        }
        busy: set[str] = set()
        tasks: list[dict[str, Any]] = []
        for task in self._store.list_group_tasks(conversation["id"], limit=20):
            if task.get("dismissed_at"):
                continue
            status = str(task["status"])
            if status not in (
                task_rules.QUEUED,
                task_rules.RUNNING,
                task_rules.AWAITING,
                task_rules.STOPPED,
                task_rules.FAILED,
            ):
                continue
            view = self._store.task_payload(task)
            owner = self._store.get_agent(task["owner_id"])
            waiting_for = view.get("waiting_for")
            if not waiting_for and view.get("awaiting_action_id"):
                action = self._store.get_action(str(view["awaiting_action_id"]))
                waiting_for = f"等你回应：{action['summary']}" if action is not None else "等你回应"
            if status in (task_rules.QUEUED, task_rules.RUNNING, task_rules.AWAITING):
                busy.add(str(task["owner_id"]))
            tasks.append(
                {
                    "owner": owner["name"] if owner is not None else "已离开的成员",
                    "title": task["title"],
                    "status": status,
                    "step": int(task.get("steps") or 0),
                    "current": str(task.get("current_step") or ""),
                    "latest": str(task.get("latest_text") or ""),
                    "waiting_for": waiting_for,
                }
            )
        activities: list[dict[str, Any]] = []
        for activity in self._store.live_work_activities(conversation["id"]):
            activity_id = str(activity["id"])
            todos = self._store.list_activity_todos(activity_id)
            activities.append(
                {
                    "activity_id": activity_id,
                    "agent": activity["agent_name"],
                    "title": activity["title"],
                    "current_step": str(activity.get("current_step") or ""),
                    "latest": str(activity.get("latest_text") or ""),
                    "todos": todos,
                    "todo_done": sum(1 for item in todos if item["status"] == "done"),
                    "todo_total": len(todos),
                }
            )
            busy.add(str(activity["agent_id"]))
        idle = [agent["name"] for agent_id, agent in by_owner.items() if agent_id not in busy]
        return {"tasks": tasks, "activities": activities, "idle": idle}

    def _close_task(self, task: dict | None, message: dict | None) -> None:
        """A turn that ended normally finished its task, unless it handed the task on."""
        if task is None:
            return
        current = self._store.get_group_task(task["id"])
        if current is None or current["status"] != task_rules.RUNNING:
            return
        if current["owner_id"] != task["owner_id"]:
            return  # transferred: the new owner's turn finishes it
        if current.get("run_id") != task.get("run_id"):
            return  # handed to its own thread run: that run finishes it
        closed = self._store.update_group_task(
            task["id"],
            status=task_rules.DONE,
            result_message_id=(message or {}).get("id") or current["result_message_id"],
        )
        if closed is not None:
            self._touch_task_card(closed, status="complete", content=closed["title"])
            self._emit_task_progress(closed, force=True)
        self._publish_group_tasks(None, task["conversation_id"])
        self._publish_workspace()

    def _queue_transfers(
        self,
        active: ActiveRun,
        conversation: dict,
        pending: list[QueuedAgent],
        hops: int,
        *,
        last_agent_id: str | None = None,
    ) -> int:
        """Queue the new owner of every task handed on during the last turn(s)."""
        while active.transfers:
            task_id, owner_id, note = active.transfers.pop(0)
            task = self._store.get_group_task(task_id)
            if task is None:
                continue
            if hops + 1 > RELAY_HOPS:
                # The relay budget is spent: the last bot (or the supervisor) closes instead
                # of handing the task on again; further hand-offs wait for the next message.
                active.transfers.clear()
                self._queue_conclusion(
                    active, conversation, pending, hops, last_agent_id, task["request_message_id"]
                )
                break
            hops += 1
            at = next((i for i, slot in enumerate(pending) if slot.cue == "closing"), len(pending))
            pending.insert(
                at,
                QueuedAgent(
                    owner_id, task["request_message_id"], cue=TRANSFER_CUE, task_id=task_id, note=note
                ),
            )
            self._advance(active, None, pending, hops)
        return hops

    def _queue_conclusion(
        self,
        active: ActiveRun,
        conversation: dict,
        pending: list[QueuedAgent],
        hops: int,
        last_agent_id: str | None,
        reply_to_id: str | None,
    ) -> None:
        """After the relay limit, one last turn writes the user a short conclusion."""
        if any(slot.cue == CONCLUDE_CUE for slot in pending):
            return
        target = self._conclusion_speaker(conversation, last_agent_id)
        if target is None:
            return
        pending.append(QueuedAgent(target, reply_to_id, cue=CONCLUDE_CUE))
        self._advance(active, None, pending, hops)

    def _conclusion_speaker(self, conversation: dict, last_agent_id: str | None) -> str | None:
        """Who closes a spent relay: the supervisor in this group, else the bot that just spoke."""
        known = {str(agent["id"]) for agent in self._roster(conversation)}
        boss = self._store.supervisor()
        if boss is not None and str(boss["id"]) in known:
            return str(boss["id"])
        if last_agent_id and last_agent_id in known:
            return last_agent_id
        return None

    async def _assign_part(
        self,
        active: ActiveRun,
        group: dict,
        assigner: dict,
        agent: dict,
        instruction: str,
        parent: dict,
        reply_to_id: str | None,
    ) -> tuple[str, dict | None, dict]:
        """A member hands one part of its task on; the part runs in its own thread run.

        The caller keeps waiting for it (the tool result is the part's answer), but the part
        is not nested inside this run: it is a task of its own, queued through the scheduler
        like any other, so another member's work really runs in parallel.
        """
        child = self._store.create_group_task(
            conversation_id=group["id"],
            owner_id=agent["id"],
            title=instruction,
            instruction=instruction,
            request_message_id=parent["request_message_id"],
            parent_id=parent["id"],
            assigned_by=assigner["id"],
            run_id=None,
            status=task_rules.QUEUED,
        )
        self._thread_of(child, agent)
        child = self._store.get_group_task(child["id"]) or child
        self._publish_group_tasks(active, group["id"])
        self._set_step(active, f"「{agent['name']}」在做「{assigner['name']}」交给它的部分")
        if self.task_scheduler is None:  # pragma: no cover - the app wires it in
            raise UpstreamError("分派通道没有就绪，本次调用未执行。")
        future = self.task_scheduler.part_future(child["id"], parent["id"])
        await self.task_scheduler.enqueue(
            child["id"],
            instruction=prompts.task_instruction(instruction, assigner["name"]),
        )
        try:
            outcome = await future
        except asyncio.CancelledError:
            # The assigner's run stopped while waiting: nothing should keep working for it.
            await self.task_scheduler.abandon_part(child["id"], parent["id"])
            raise
        done = self._store.get_group_task(child["id"]) or child
        self._publish_group_tasks(active, group["id"])
        message = (
            self._store.get_message(outcome["message_id"])
            if outcome.get("message_id")
            else None
        )
        text = str(outcome.get("text") or "")
        if outcome.get("status") != task_rules.DONE and not text:
            text = f"「{agent['name']}」没能做完这一部分。"
        return text, message, done

    def _note_task_step(self, active: ActiveRun, step: str) -> None:
        """Count one tool call on the group task this turn works on, and say where it is.

        The row is always written; the SSE push is throttled, because a busy member finishes
        several calls a second and the card only needs to look alive.
        """
        task_id = GROUP_TASK.get()
        if not task_id:
            return
        task = self._store.bump_group_task_step(task_id, step)
        if task is None:
            return
        self._emit_task_progress(task)

    def _emit_task_progress(self, task: dict, *, force: bool = False) -> None:
        """Push one task's live fields to its card, at most four times a second."""
        pending = self._task_text.get(task["id"])
        if pending is not None and pending != task.get("latest_text"):
            # A step often follows its narration within the throttle window: keep the line.
            task = self._store.update_task_text(task["id"], pending) or task
        if self.task_scheduler is not None:
            self.task_scheduler.publish_progress(task, force=force)
            return
        now = time.monotonic()
        if not force and now - self._task_progress_at.get(task["id"], float("-inf")) < TASK_PROGRESS_SECONDS:
            return
        self._task_progress_at[task["id"]] = now
        view = self._store.task_payload(task)
        self._publish(
            "task_progress",
            {
                "conversation_id": view["conversation_id"],
                "task_id": view["id"],
                "latest_text": view.get("latest_text") or "",
                "recent_steps": view.get("recent_steps") or [],
                "steps": int(view.get("steps") or 0),
                "current_step": view.get("current_step") or "",
                "awaiting_action_id": view.get("awaiting_action_id"),
                "waiting_for": view.get("waiting_for"),
                "status": view.get("status"),
            },
        )

    def _reset_task_text(self, active: ActiveRun) -> None:
        """A new message in the thread starts the card's latest line over."""
        if active.task_id:
            self._task_text[active.task_id] = ""

    def _note_task_text(self, active: ActiveRun, safe: str) -> None:
        """The thread's newest prose, throttled: the card reads it without polling the thread."""
        task_id = active.task_id
        if not task_id or not safe:
            return
        flat = " ".join((self._task_text.get(task_id, "") + safe).split())[-400:]
        self._task_text[task_id] = flat
        now = time.monotonic()
        last = self._task_progress_at.get(task_id, float("-inf"))
        if now - last < TASK_PROGRESS_SECONDS:
            return
        updated = self._store.update_task_text(task_id, flat)
        if updated is not None:
            self._emit_task_progress(updated, force=True)

    def _refresh_task_progress(self) -> None:
        """A card started or stopped waiting: move it to ``awaiting``/back, and push the row.

        The status is real, not derived from the action: the sidebar's chip list, the
        scheduler's slot counting and the card's buttons all read it.
        """
        task_id = GROUP_TASK.get()
        if not task_id:
            return
        task = self._store.get_group_task(task_id)
        if task is None:
            return
        awaiting = self._store.awaiting_action_id(task.get("thread_id"))
        if awaiting and task["status"] == task_rules.RUNNING:
            task = self._store.update_group_task(
                task["id"], status=task_rules.AWAITING, current_step="等你回应"
            ) or task
            self._publish_group_tasks(None, task["conversation_id"])
            self._publish_workspace()
        elif not awaiting and task["status"] == task_rules.AWAITING:
            task = self._store.update_group_task(
                task["id"], status=task_rules.RUNNING
            ) or task
            self._publish_group_tasks(None, task["conversation_id"])
            self._publish_workspace()
        self._emit_task_progress(task, force=True)

    def _flush_task_text(self, active: ActiveRun) -> None:
        """A run ended: put the last prose line the card still needs into its row."""
        task_id = active.task_id
        if not task_id:
            return
        text = self._task_text.pop(task_id, "")
        if not text:
            return
        updated = self._store.update_task_text(task_id, text)
        if updated is not None:
            self._emit_task_progress(updated, force=True)

    def _publish_group_tasks(self, active: ActiveRun | None, conversation_id: str) -> None:
        if active is not None and active.background:
            return
        self._publish(
            "group_tasks",
            {
                "conversation_id": conversation_id,
                "tasks": self._store.group_tasks_payload(conversation_id),
            },
        )

    @staticmethod
    def _task_end_reason(
        ending: str, status: str, error: str | None, active: ActiveRun
    ) -> str | None:
        """Why a task ended, in the words its card shows; ``None`` for a task that finished."""
        if ending == task_rules.DONE:
            return None
        if status == "limited":
            return "步数用完了"
        if ending == task_rules.FAILED:
            if active.upstream_error:
                return f"模型服务失败：{error or '连接中断'}"
            return f"出错了：{error or '未知原因'}"
        return None

    def _settle_group_tasks(self, active: ActiveRun, status: str, error: str | None = None) -> None:
        """The run's ending settles every task it left unfinished, then each group hears once.

        One ``group_work_done`` per group the run touched, sent only now: work that wrote many
        replies, handed parts on or asked along the way still tells the user once, at its end.
        The row keeps why it ended and who ended it, so a stopped card can say more than 「已停下」.
        """
        ending = TASK_ENDINGS.get(status, task_rules.FAILED)
        settled = self._store.settle_group_tasks(
            active.run_id,
            ending,
            end_reason=self._task_end_reason(ending, status, error, active),
            stopped_by=active.stopped_by if ending == task_rules.STOPPED else None,
        )
        if not settled or active.background:
            return
        by_group: dict[str, list[dict]] = {}
        for task in settled:
            # A card that stopped or failed is news for the group (and its only unread); a card
            # that merely ended with its run is closed quietly so no card is left "live".
            if task["status"] in (task_rules.STOPPED, task_rules.FAILED):
                self._touch_task_card(
                    task,
                    status="interrupted",
                    content=prompts.task_stopped_line(task["title"], task["status"]),
                )
            else:
                self._touch_task_card(task, status="complete", content=task["title"])
            by_group.setdefault(task["conversation_id"], []).append(task)
        for conversation_id, tasks in by_group.items():
            conversation = self._store.get_conversation(conversation_id)
            if conversation is None:
                continue
            counts = {key: sum(1 for task in tasks if task["status"] == key) for key in task_rules.TASK_STATUSES}
            self._publish_group_tasks(None, conversation_id)
            for task in tasks:
                self._emit_task_progress(task, force=True)
            self._publish_workspace()
            self._publish(
                "group_work_done",
                {
                    "conversation_id": conversation_id,
                    "title": conversation["title"],
                    "run_id": active.run_id,
                    "status": status,
                    "done": counts[task_rules.DONE],
                    "failed": counts[task_rules.FAILED],
                    "stopped": counts[task_rules.STOPPED],
                    "message_id": self._last_reply_id(conversation_id, active.run_id),
                },
            )

    def _last_reply_id(self, conversation_id: str, run_id: str) -> str | None:
        rows = [
            row
            for row in self._store.list_messages(conversation_id)
            if row.get("run_id") == run_id and row["kind"] == "agent"
        ]
        return rows[-1]["id"] if rows else None

    def _sibling_answers(
        self, conversation: dict, reply_to_id: str | None, blind_to: set[str] | frozenset[str]
    ) -> frozenset[str]:
        """Ids of the replies ``blind_to`` already wrote to ``reply_to_id`` in this conversation.

        Read again whenever the context is rebuilt (a compaction mid-turn), so a sibling that
        finished first never leaks into a member still answering.
        """
        if not blind_to or reply_to_id is None:
            return frozenset()
        return frozenset(
            row["id"]
            for row in self._store.list_messages(conversation["id"])
            if row["reply_to_id"] == reply_to_id and row["author_id"] in blind_to
        )

    async def _member_call(
        self,
        active: ActiveRun,
        conversation: dict,
        agent: dict,
        messages: list[dict[str, Any]],
        reply_to_id: str | None,
        roster: Sequence[Mapping[str, str]] = (),
        *,
        role: str = "member",
        limited_into: dict[str, str] | None = None,
        surface: dict | None = None,
        concluding: bool = False,
    ) -> tuple[str, dict | None]:
        """Run one bot's native tool loop, streaming text before and after calls.

        ``role`` is the tool list this turn may use: a workflow step owned by the supervisor
        runs with the supervisor's management tools, not a member's. ``limited_into`` collects
        this turn's own budget limit, for a caller — a workflow branch — that shares the bot
        with a sibling branch and cannot read ``turn_limited[agent id]`` unambiguously.
        ``surface`` is the conversation whose kind, rules and working set the tools see: a
        task thread's rows land in the thread while its tools still act in that group.
        """
        message: dict | None = None
        body = ""
        pending_nudge = False
        nudged = False
        said: list[str] = []
        reply_mood: str | None = None
        reply_images: list[str] = []
        reply_reaction: str | None = None
        self._add_continue_cue(messages, active)
        active.notified.discard((conversation["id"], agent["id"]))
        async with self._activity_turn(active, conversation, agent, reply_to_id) as turn:
            while True:
                if active.stop_requested:
                    raise RunStopped("任务已停止。")
                # A note another lane left joins here, at a call boundary, never mid-stream.
                self._deliver_notes(active, messages)
                tools_now = self._schemas(
                    role,
                    surface or conversation,
                    agent,
                    from_thread=bool(active.task_id)
                    or self._conversation_kind(active.conversation_id) == "thread",
                    read_only=active.lane == "reply",
                )
                stream, current, body, mood, images, reaction = await self._stream_reply(
                    active,
                    conversation,
                    agent,
                    messages=messages,
                    tools=tools_now,
                    reply_to_id=reply_to_id,
                    allow_tool_calls=True,
                    silent=pending_nudge,
                )
                calls = stream.tool_calls
                nudge = False
                if pending_nudge:
                    pending_nudge = False
                    if messages and messages[-1].get("content") == prompts.ASK_FOLLOW_UP_NOTE:
                        messages.pop()
                    said.append(body)
                    if message is not None:
                        message = self._complete_message(
                            active,
                            message,
                            "".join(said),
                            reply_mood,
                            roster,
                            reply_images,
                            reply_reaction,
                            calls,
                            tool_started=stream.saw_tool_call,
                        )
                        if message is not None:
                            self._publish_message("message_end", message)
                else:
                    said = [body]
                    reply_mood, reply_images, reply_reaction = mood, images, reaction
                    nudge = not calls and not concluding and self._should_nudge(
                        active, agent, tools_now, body, nudged
                    )
                    if current is not None and not nudge:
                        current = self._complete_message(
                            active,
                            current,
                            body,
                            mood,
                            roster,
                            images,
                            reaction,
                            calls,
                            tool_started=stream.saw_tool_call,
                        )
                        if current is not None:
                            self._publish_message("message_end", current)
                    message = current or message
                if not calls:
                    # A reply that ended while a note was landing still reads it here.
                    if active.notes:
                        if body:
                            messages.append({"role": "assistant", "content": body})
                        self._deliver_notes(active, messages)
                        continue
                    if nudge:
                        nudged = True
                        messages.append({"role": "assistant", "content": body})
                        messages.append(
                            {"role": "user", "content": prompts.ASK_FOLLOW_UP_NOTE}
                        )
                        pending_nudge = True
                        continue
                    # A reaction-only reply is answered without text: ``message`` stays None.
                    self._note_turn_wrap(active, conversation, turn)
                    return ("".join(said) or body), message
                messages.append(self._assistant_tool_turn(body, calls))
                try:
                    await self._run_tool_calls(
                        active, conversation, calls, agent, messages, surface=surface
                    )
                except TurnLimited as exc:
                    # This bot's turn spent its budget: its text so far stands, the run goes on.
                    active.turn_limited[agent["id"]] = str(exc)
                    if limited_into is not None:
                        limited_into["reason"] = str(exc)
                    self._note_turn_wrap(active, conversation, turn)
                    return ("".join(said) or body), message
                except TurnHandedOff:
                    return ("".join(said) or body), message

    @contextmanager
    def _turn(self, active: ActiveRun, agent: dict):
        """One bot's tool loop counts against its own budget; a nested turn restores the outer."""
        outer = active.turn
        active.turn = TurnBudget(agent_id=agent["id"], agent_name=agent["name"])
        try:
            yield active.turn
        finally:
            active.turn = outer

    async def _run_tool_calls(
        self,
        active: ActiveRun,
        conversation: dict,
        calls: list[ToolCallFragment],
        author: dict,
        messages: list[dict[str, Any]],
        *,
        surface: dict | None = None,
    ) -> None:
        """Run one batch of calls; images that came back follow as one user turn each.

        A tool result can only be text, so the pixels ride in a turn of their own right after
        the batch. It lives in this request only; the private chat keeps the real message.
        Screenshots are labelled as such; how many keep their pixels is decided when the
        assembled request is pruned, not here.
        """
        received: dict[str, list[str]] = {}
        for call in calls:
            result, images = await self._handle_tool_call(
                active, conversation, call, author, surface=surface
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            label = computer_tools.SHOT_LABEL.get(call.name)
            if label is None:
                # A local picture the bot asked to see, not a screenshot or a chat upload.
                label = "[你看的图片]" if call.name == "view_image" else "[私聊里收到的图片]"
            bucket = received.setdefault(label, [])
            bucket.extend(image for image in images if image not in bucket)
        if not self._sees_images(author):
            return
        for label, ids in received.items():
            rows = [self._store.get_attachment(image) for image in ids]
            found = [row for row in rows if row is not None]
            if found:
                messages.append(
                    {
                        "role": "user",
                        "content": history.with_attachments(
                            label,
                            found,
                            {row["id"] for row in found},
                            attachments_dir=self._store.attachments_dir,
                        ),
                    }
                )

    # ------------------------------------------------------------------ tool calls

    async def _handle_tool_call(
        self,
        active: ActiveRun,
        conversation: dict,
        call: ToolCallFragment,
        author: dict,
        *,
        surface: dict | None = None,
    ) -> tuple[str, list[str]]:
        """Execute one tool call; return the result text and any images sent back with it.

        ``conversation`` is where this call's rows (its action, notices) are written;
        ``surface`` is what the tools themselves see — for a task thread's turn, the group.
        """
        if active.stop_requested:
            raise RunStopped("任务已停止。")
        if not call.id.strip():
            raise UpstreamError("模型返回的工具调用缺少 id，本次回复无法执行。")
        if not call.name.strip():
            raise UpstreamError("模型返回的工具调用缺少名称，本次回复无法执行。")

        run = self._require_run(active)
        surface = surface or conversation
        offer = self._offer(active, surface, author)
        registry = self._registry(offer)
        handler = registry.resolve(offer, call.name)
        label = registry.label(call.name)
        arguments = canonical_arguments(call.arguments)

        invocation = self._store.get_invocation(active.run_id, call.id)
        if invocation is not None:
            if invocation["arguments"] == arguments:
                return (
                    invocation["result"]
                    or error_result(f"tool_call_id「{call.id}」已执行过，但没有可用结果。"),
                    [],
                )
            message = f"tool_call_id「{call.id}」已用于其他参数，本次调用未执行。"
            self._record_unexecuted(active, conversation, call, label, message, author)
            return error_result(message), []

        turn = active.turn
        if turn is not None and turn.wrapping and not (
            # A reply lane's way out is handing the work off: the lookup cap's refusal names
            # ``start_task``, so that one call still runs while the turn wraps.
            active.lane == "reply" and call.name == START_TASK
        ):
            # The bot already had the call it may answer with, and asked for another tool:
            # end its turn with what it wrote. Never the run it is nested in.
            reason = f"{turn.label}的调用预算用完了"
            self._record_unexecuted(active, conversation, call, label, reason, author)
            raise TurnLimited(f"{reason}，这一轮先停在这里。")
        if (
            active.lane == "reply"
            and turn is not None
            and call.name != START_TASK
            and turn.reply_calls >= prompts.REPLY_TOOL_CAP
        ):
            # A reply lane answers beside the work and may look a couple of things up; the
            # reading a task needs belongs to the task's own thread. ``start_task`` is the
            # hand-off, so it never spends this cap.
            reason = prompts.REPLY_TOOL_LIMIT_NOTE
            self._record_unexecuted(
                active, conversation, call, label, f"{reason}，本次调用没有执行。", author
            )
            # The rest of the batch is refused the same way; its next call is the answer.
            turn.wrap_pending = True
            return error_result(f"{reason}。{prompts.TURN_WRAP_NOTE}"), []
        uses_computer = handler is not None and handler.computer_step
        budget = step_budget.budget_for(self._store, self._settings)
        if uses_computer:
            reason = self._computer_budget_reason(active, call.name, author["name"], budget)
            if reason is not None:
                self._record_unexecuted(active, conversation, call, label, reason, author)
                raise RunLimited(reason)
        tool_cap = (
            budget.workflow_tool_calls
            if active.workflow_run_id
            else budget.run_tool_calls
        )
        # Zero means no ceiling: the task ends when the model stops asking, not at a count.
        if (
            tool_cap
            and not uses_computer
            and (run["tool_calls"] - active.computer_steps >= tool_cap)
        ):
            reason = f"已达本次任务的工具执行上限（{tool_cap}），任务已结束。"
            self._record_unexecuted(active, conversation, call, label, reason, author)
            raise RunLimited(reason)
        if (
            turn is not None
            and not uses_computer
            and budget.turn_tool_calls
            and turn.tool_calls >= budget.turn_tool_calls
        ):
            reason = f"已达{turn.label}的工具调用上限（{budget.turn_tool_calls}）"
            self._record_unexecuted(active, conversation, call, label, reason + "，没有执行。", author)
            # The rest of the batch is refused the same way; the next call is the bot's last.
            turn.wrap_pending = True
            turn.wrapped = True
            return error_result(f"{reason}。{prompts.TURN_WRAP_NOTE}"), []

        if uses_computer:
            active.computer_steps += 1
            active.computer_by_tool[call.name] = active.computer_by_tool.get(call.name, 0) + 1
        if turn is not None:
            turn.tool_calls += 0 if uses_computer else 1
            turn.computer_steps += 1 if uses_computer else 0
            if active.lane == "reply" and call.name != START_TASK:
                # Counted after the gate above, so the cap allows exactly this many lookups.
                turn.reply_calls += 1
        self._store.update_run(active.run_id, tool_calls=run["tool_calls"] + 1)
        if call.name != START_TASK:
            # Opening the task is the group turn's own decision, not one of its work steps.
            self._note_task_step(active, f"正在{label}")
        # The turn's card says what it is doing right now: the call's own words when it has
        # them (「运行「npm init -y」」), else the tool's category.
        step = f"正在{label}"
        if handler is not None:
            try:
                step = getattr(handler.parse(call.arguments), "summary", None) or step
            except Exception:
                pass
        self._publish_run(active)
        action, action_message = self._store.create_action(
            run_id=active.run_id,
            conversation_id=conversation["id"],
            tool_call_id=call.id,
            tool_name=call.name,
            arguments=arguments,
            summary=f"正在{label}",
            author_id=author["id"],
            author_name=author["name"],
            avatar_seed=author["avatar_seed"],
            activity_id=ACTIVITY.get(),
        )
        self._publish_message("message_start", action_message)
        # After the row exists, so the card's ``current_tool`` names this call, not the last.
        self._note_activity_step(active, step)
        self._maybe_rename_activity(ACTIVITY.get())
        if active.background:
            # Nobody sees this row; the schedules panel reads the run's step instead.
            self._set_step(active, f"「{author['name']}」正在{label}")

        context = self._tool_context(
            active, conversation, author, action, action_message["id"], surface=surface
        )
        try:
            if handler is None:
                raise ToolError(f"未知工具「{call.name}」，本次调用未执行。")
            # A run nobody watches cannot answer a card: the policy says so per handler.
            refused = approvals.refusal(
                context, handler, scheduled=active.schedule_id is not None
            )
            if refused is not None:
                raise ToolError(refused)
            outcome = await handler.run(context, handler.parse(call.arguments))
            if handler.name == ASK_USER and turn is not None:
                # The user got a question this turn; a reply that reads like a lead-in is
                # now answered by the card, not by a nudge.
                turn.asked = True
        except (RunLimited, RunStopped) as exc:
            reason = str(exc) or "任务已停止。"
            self._finish_action(
                active,
                action,
                status="error",
                summary=f"{label}未完成：{reason}",
                error=reason,
                result=error_result(reason),
                message_status="interrupted" if isinstance(exc, RunStopped) else "complete",
            )
            raise
        except (UpstreamError, ToolError) as exc:
            reason = str(exc)
            self._finish_action(
                active,
                action,
                status="error",
                summary=f"{label}失败：{reason}",
                error=reason,
                result=error_result(reason),
            )
            if isinstance(exc, UpstreamError):
                raise
            self._record_invocation(active, call, arguments, action, error_result(reason))
            return error_result(reason), []
        except ToolInputError as exc:
            # The model can see and correct its own mistake; the run keeps going.
            reason = str(exc)
            self._finish_action(
                active,
                action,
                status="error",
                summary=f"{label}失败：{reason}",
                error=reason,
                result=error_result(reason),
            )
            self._record_invocation(active, call, arguments, action, error_result(reason))
            return error_result(reason), []
        except Exception as exc:  # one broken tool never takes the whole run down
            reason = f"{label}执行失败：{exc.__class__.__name__}。"
            self._finish_action(
                active,
                action,
                status="error",
                summary=reason,
                error=reason,
                result=error_result(reason),
            )
            self._record_invocation(active, call, arguments, action, error_result(reason))
            return error_result(reason), []

        if outcome.handled:
            # A tool that finished its own action (a user decision) already published it.
            self._record_invocation(active, call, arguments, action, outcome.result_json)
            return outcome.result_json, []

        self._finish_action(
            active,
            action,
            status="completed",
            summary=outcome.summary,
            result=outcome.result_json,
            detail=outcome.detail,
            target_agent_id=outcome.target_agent_id,
            target_conversation_id=outcome.target_conversation_id,
        )
        self._record_invocation(active, call, arguments, action, outcome.result_json)
        # What this call touched, for every other member of the conversation: the next bot
        # then knows a file's state without reading it back.
        working_set.record(
            self._store,
            self._settings,
            surface,
            author,
            tool_name=call.name,
            arguments=arguments,
            detail=outcome.detail,
        )
        if handler is not None and handler.hands_off:
            raise TurnHandedOff(outcome.summary)
        return outcome.result_json, outcome.images

    def _record_invocation(
        self,
        active: ActiveRun,
        call: ToolCallFragment,
        arguments: str,
        action: dict,
        result: str,
    ) -> None:
        self._store.record_invocation(
            run_id=active.run_id,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments=arguments,
            result=result,
            action_id=action["id"],
        )

    def _source_message_id(self, active: ActiveRun, author: dict) -> str | None:
        """The message a memory write points at: this bot's reply, else the user's message.

        A tool call runs inside the reply the bot is composing, so that reply is the right
        source when it exists; the run's trigger stands in when the bot called the tool
        before writing anything.
        """
        for agent_id, message_id in reversed(active.replies):
            if agent_id == author["id"]:
                return message_id
        return self._origin_id(active)

    def _origin_id(self, active: ActiveRun) -> str | None:
        """The user message this run answers, so a private chat can show who asked."""
        conversation = self._store.get_conversation(active.conversation_id)
        if conversation is None or conversation["kind"] == "private":
            return None
        trigger = self._trigger_message(active, conversation)
        return trigger["id"] if trigger is not None and trigger["kind"] == "user" else None

    async def _consult_member(
        self,
        active: ActiveRun,
        group: dict,
        agent: dict,
        task: str,
        *,
        reply_to_id: str | None = None,
    ) -> tuple[str, dict, dict]:
        """Really call the model as one member assigned a task by the supervisor."""
        self._set_step(active, f"正在咨询「{agent['name']}」")
        roster = self._roster(group)
        messages = await self._turn_context(
            active,
            group,
            agent,
            self._schemas("member", group, agent),
            lambda: self._member_context(group, roster, agent, task=task, trigger=task),
        )
        text, message = await self._member_call(
            active, group, agent, messages, reply_to_id, roster
        )
        first = message or {}
        text, message, review = await self._review_member_answer(
            active,
            group,
            agent,
            task,
            messages,
            reply_to_id=reply_to_id,
            roster=roster,
            first_text=text,
            first_message=first,
        )
        return text, message, review

    # ------------------------------------------------------------------ subtasks

    async def _run_subtasks(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        items: list[dict],
        states: list[dict],
        progress: Any,
    ) -> list[dict]:
        """Run every subtask as its own task of the same bot, then collect the answers.

        Siblings share the run but not a turn: their ``TurnBudget`` lives in the context
        variable, so each spends and wraps its own small allowance. ``states`` is the list
        the tool's action row shows; it is rewritten as each one settles and pushed by
        ``progress``. A stop reaches every sibling through ``stop_requested``.
        """
        role = "supervisor" if author["kind"] == "supervisor" else "member"

        async def one(index: int) -> None:
            item = items[index]
            try:
                answer = await self._subtask_turn(active, conversation, author, role, item)
            except (RunStopped, RunLimited):
                raise
            except Exception as exc:  # one failed subtask never takes the others down
                states[index] = {
                    "title": item["title"],
                    "status": "failed",
                    "output": str(exc) or exc.__class__.__name__,
                }
            else:
                states[index] = {
                    "title": item["title"],
                    "status": "done",
                    "output": answer,
                }
            finally:
                progress()

        active.subtask_depth += 1
        tasks = [asyncio.create_task(one(index)) for index in range(len(items))]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            active.subtask_depth -= 1
        for outcome in outcomes:
            if isinstance(outcome, (RunStopped, RunLimited)):
                raise outcome
        return [
            {
                "title": state["title"],
                "status": state["status"],
                "answer": state["output"],
            }
            for state in states
        ]

    async def _subtask_turn(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        role: str,
        item: dict,
    ) -> str:
        """One focused attempt: the bot's own context, its tools minus subtasks, small budget.

        Nothing it writes reaches the transcript: the caller's tool result carries the
        answers, and the action row carries the progress. The model's own text is returned.
        """
        roster = self._roster(conversation)
        tools = [
            schema
            for schema in self._schemas(role, conversation, author)
            if schema["function"]["name"] != "run_subtasks"
        ]
        prompt = (
            f"你现在在处理一个小任务，只管这一部分：「{item['title']}」\n{item['brief']}\n\n"
            "直接做这一部分，最后给出它自己的结论或成果；不要重复背景，不要提及其他小任务，"
            "也不要再把它拆开。"
        )
        if role == "supervisor":
            messages = self._supervisor_context(conversation, cue=prompt)
        else:
            messages = self._member_context(conversation, roster, author, task=prompt)
        body = ""
        with self._turn(active, author):
            for _round in range(SUBTASK_MAX_MODEL_CALLS):
                if active.stop_requested:
                    raise RunStopped("任务已停止。")
                turn = active.turn
                spent = turn is not None and turn.tool_calls >= SUBTASK_MAX_TOOL_CALLS
                stream, _message, body, _mood, _images, _reaction = await self._stream_reply(
                    active,
                    conversation,
                    author,
                    messages=messages,
                    tools=None if spent else tools,
                    reply_to_id=None,
                    allow_tool_calls=not spent,
                    silent=True,
                )
                calls = stream.tool_calls
                if not calls:
                    return body.strip()
                messages.append(self._assistant_tool_turn(body, calls))
                for call in calls:
                    if spent or (
                        turn is not None and turn.tool_calls >= SUBTASK_MAX_TOOL_CALLS
                    ):
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": error_result(
                                    "这个小任务的工具调用预算用完了，请直接用已有的内容给结论。"
                                ),
                            }
                        )
                        continue
                    result, _images = await self._handle_tool_call(
                        active, conversation, call, author
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result}
                    )
        return body.strip()

    # ------------------------------------------------------------------ review

    async def _review_member_answer(
        self,
        active: ActiveRun,
        conversation: dict,
        agent: dict,
        task: str,
        messages: list[dict[str, Any]],
        *,
        reply_to_id: str | None,
        roster: Sequence[Mapping[str, str]],
        first_text: str,
        first_message: dict,
    ) -> tuple[str, dict, dict]:
        """Run the supervisor's review loop over a member's first answer.

        Returns ``(text, message, review)``; ``review`` is the tool-result payload
        ``{"rounds", "passed", "feedback"}``, or ``{}`` when review is switched off.
        """
        settings = self._store.team_settings()
        review = settings["review"]
        if not review["enabled"]:
            return first_text, first_message, {}
        max_rounds = int(review["max_rounds"])
        supervisor = self._supervisor()
        original = self._run_original_request(active)
        name = agent["name"]
        rounds = 0
        feedback: list[str] = []
        text, message = first_text, first_message
        passed = False
        while True:
            verdict, fb = await self._review_call(active, agent, task, original, text)
            if verdict == "pass":
                passed = True
                break
            if rounds >= max_rounds:
                break
            feedback.append(fb)
            self._write_review_action(
                active,
                conversation,
                supervisor,
                agent,
                summary=f"打回「{name}」：{self._short_feedback(fb)}",
                result={"kind": "revise", "feedback": fb},
            )
            # The member sees its own previous answer, then the supervisor's feedback.
            messages.append({"role": "assistant", "content": text})
            messages.append(
                {
                    "role": "user",
                    "content": f"{supervisor['name']}的验收意见：{fb} 请据此修改后重新给出完整答复。",
                }
            )
            text, message = await self._member_call(
                active, conversation, agent, messages, reply_to_id, roster
            )
            rounds += 1
        if passed:
            suffix = f"（打回 {rounds} 次后）" if rounds else ""
            summary = f"验收通过「{name}」{suffix}"
            result: dict[str, Any] = {"kind": "pass"}
        else:
            summary = f"「{name}」打回 {rounds} 次仍未通过，按现有结果交付"
            result = {"kind": "fail", "rounds": rounds}
        self._write_review_action(
            active, conversation, supervisor, agent, summary=summary, result=result
        )
        return text, message, {"rounds": rounds, "passed": passed, "feedback": feedback}

    async def _review_call(
        self,
        active: ActiveRun,
        agent: dict,
        task: str,
        original: str,
        answer: str,
    ) -> tuple[str, str]:
        """One no-tool review model call, attributed to the supervisor as ``review`` usage."""
        supervisor = self._supervisor()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.REVIEW_SYSTEM},
            {
                "role": "user",
                "content": prompts.review_prompt(
                    agent_name=agent["name"], task=task, original=original, answer=answer
                ),
            },
        ]
        self._reserve_model_call(active)
        stream = self._completion(
            messages, tools=None, author=supervisor, purpose="review", max_tokens=REVIEW_MAX_TOKENS
        )
        pieces: list[str] = []
        try:
            async for chunk in stream.chunks(should_stop=lambda: active.stop_requested):
                pieces.append(chunk)
            stream.validate(allow_tool_calls=False)
        except RunStopped:
            raise
        except UpstreamError as exc:
            logger.warning("主管验收调用失败：%s", exc)
        finally:
            self._record_usage(active, stream, author=supervisor, purpose="review")
        return self._parse_review("".join(pieces).strip())

    @staticmethod
    def _parse_review(text: str) -> tuple[str, str]:
        """Parse a review verdict; anything unreadable counts as ``pass``.

        Models often fence the object (```json … ```) or say a word around it, so the first
        ``{`` … last ``}`` span is what is parsed.
        """
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1] if 0 <= start < end else text)
        except (ValueError, TypeError):
            logger.warning("主管验收返回了无法解析的内容：%r", text)
            return "pass", ""
        if not isinstance(data, dict):
            logger.warning("主管验收返回的不是 JSON 对象：%r", text)
            return "pass", ""
        verdict = str(data.get("verdict") or "").strip().lower()
        feedback = " ".join(str(data.get("feedback") or "").split())
        if verdict == "revise":
            return "revise", feedback or "请改进后重新回答。"
        if verdict != "pass":
            logger.warning("主管验收返回了未知 verdict：%r", text)
        return "pass", feedback

    @staticmethod
    def _short_feedback(feedback: str) -> str:
        text = " ".join(str(feedback).split())
        return text if len(text) <= 60 else text[:59] + "…"

    def _run_original_request(self, active: ActiveRun) -> str:
        """The user message this run answers, whatever conversation the review happens in."""
        origin = self._store.get_conversation(active.conversation_id)
        if origin is None:
            return ""
        return self._trigger_text(active, origin)

    def _write_review_action(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        agent: dict,
        *,
        summary: str,
        result: dict[str, Any],
    ) -> None:
        """One synthetic ``review`` action row, authored by the supervisor, targeting the member."""
        action, message = self._store.create_action(
            run_id=active.run_id,
            conversation_id=conversation["id"],
            tool_call_id=new_id("v"),
            tool_name="review",
            arguments="{}",
            summary=summary,
            author_id=author["id"],
            author_name=author["name"],
            avatar_seed=author["avatar_seed"],
            activity_id=ACTIVITY.get(),
        )
        self._publish_message("message_start", message)
        self._finish_action(
            active,
            action,
            status="completed",
            summary=summary,
            result=json.dumps(result, ensure_ascii=False),
            target_agent_id=agent["id"],
        )

    # ---------------------------------------------------------------- schedules

    def _publish_schedules(self) -> None:
        schedule_tools.publish_schedules(self._store, self._settings, self._publish)

    async def _await_decision(self, active: ActiveRun, action_id: str) -> str:
        """Wait for the user's answer; a stopped run counts as keeping the target."""
        waiter = asyncio.Event()
        self._confirm_waiters[action_id] = waiter
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.confirm_timeout_seconds
        try:
            while True:
                if waiter.is_set():
                    return "approve" if self._confirm_decisions.get(action_id) else "deny"
                if active.stop_requested:
                    return "stop"
                if loop.time() >= deadline:
                    return "timeout"
                await asyncio.sleep(CONFIRM_POLL_SECONDS)
        finally:
            self._confirm_waiters.pop(action_id, None)
            self._confirm_decisions.pop(action_id, None)

    async def _await_answer(self, active: ActiveRun, action_id: str) -> dict | None:
        """Wait for the answer to a question, however long it takes.

        No deadline: unlike a confirmation card, silence never means anything here, so the
        run simply keeps waiting. A stop or a shutdown ends the wait with ``None``.
        """
        waiter = asyncio.Event()
        self._answer_waiters[action_id] = waiter
        try:
            while True:
                if waiter.is_set():
                    return self._answers.get(action_id) or {"skip": True}
                if active.stop_requested:
                    return None
                await asyncio.sleep(CONFIRM_POLL_SECONDS)
        finally:
            self._answer_waiters.pop(action_id, None)
            self._answers.pop(action_id, None)

    # ------------------------------------------------------------------ routing

    def _trigger_message(self, active: ActiveRun, conversation: dict) -> dict | None:
        """The message that started this run, i.e. the one agent replies point at."""
        if active.trigger_message_id:
            message = self._store.get_message(active.trigger_message_id)
            if message is not None and message["conversation_id"] == conversation["id"]:
                return message
        users = [
            message
            for message in self._store.list_messages(conversation["id"])
            if message["kind"] == "user"
        ]
        return users[-1] if users else None

    async def _initial_queue(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        trigger: dict | None,
    ) -> list[QueuedAgent]:
        """Who replies first: the one member of a direct chat, the dispatcher for a group.

        A group's user message goes through its invisible dispatcher (``app.dispatch``) once.
        ``@所有人`` needs no model call; a re-dispatched message uses the user's own pick; and
        a failure falls back to the addressed members, the last speaker, or the first member,
        so a group message is never silently dropped.
        """
        reply_to = trigger["id"] if trigger is not None else None
        if conversation["kind"] in ("direct", "thread"):
            # A thread belongs to one member: what the user writes there is for that member.
            members = conversation["member_ids"]
            return [QueuedAgent(members[0], reply_to)] if members else []
        known = {str(agent["id"]) for agent in roster}
        addressed = [
            str(agent_id)
            for agent_id in ((trigger or {}).get("addresses") or [])
            if str(agent_id) in known
        ]
        if conversation["kind"] == "group" and active.route_override:
            # A re-dispatch names its speakers itself.
            speakers = [
                agent_id for agent_id in active.route_override if agent_id in known
            ]
            if speakers:
                cue = OPINION_CUE if len(speakers) > 1 else "turn"
                return [QueuedAgent(agent_id, reply_to, cue=cue) for agent_id in speakers]
        if conversation["kind"] == "group" and trigger is not None and trigger["kind"] == "user":
            return await self._group_queue(active, conversation, roster, trigger, addressed)
        # A message a bot brought into the group, or a reminder: whoever it named answers,
        # else whoever last spoke, else the first member. No supervisor is required.
        wanted: list[str] = []
        if trigger is not None:
            explicit = [
                *addressed,
                *(str(agent_id) for agent_id in trigger.get("mentions") or []),
                *parse_mentions(str(trigger.get("content") or ""), roster),
            ]
            for agent_id in explicit:
                if agent_id in known and agent_id not in wanted:
                    wanted.append(agent_id)
        if wanted:
            return [QueuedAgent(agent_id, reply_to, cue="turn") for agent_id in wanted]
        last = self._last_speaker(conversation)
        if last is not None:
            return [QueuedAgent(last["id"], reply_to, cue="default")]
        first = self._first_member(conversation, roster)
        return [QueuedAgent(first, reply_to, cue="default")] if first else []

    async def _group_queue(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        trigger: dict,
        addressed: Sequence[str],
    ) -> list[QueuedAgent]:
        """Route one group user message: @所有人, a re-dispatch, or the dispatcher's decision."""
        reply_to = trigger["id"]
        if mentions_everyone(str(trigger.get("content") or "")):
            members = [str(agent["id"]) for agent in roster]
            if not members:
                return []
            decision = dispatch_rules.everyone(members)
            self._record_route(trigger, decision)
            return self._queue_for(decision, reply_to)
        if not addressed:
            # 「继续」 goes back to the bot whose work the last stop cut short, with no call.
            interrupted = self._interrupted_agent(conversation, roster)
            if interrupted and is_continue(
                self._strip_mentions(str(trigger.get("content") or ""), conversation)
            ):
                self._store.clear_interrupted_agent(conversation["id"])
                decision = dispatch_rules.Decision(
                    kind=dispatch_rules.KIND_ANSWER,
                    speakers=(interrupted,),
                    reason="接着刚才被打断的成员继续。",
                )
                self._record_route(trigger, decision)
                return self._queue_for(decision, reply_to)
        self._mark_route_pending(trigger)
        self._set_step(active, "正在决定谁来接")
        decision = await self._dispatch_decision(active, conversation, roster, trigger, addressed)
        decision = self._with_user_invites(conversation, roster, trigger, decision)
        if self._store.get_message(trigger["id"]) is not None:
            self._record_route(trigger, decision)
        return await self._apply_decision(active, conversation, roster, decision, trigger, addressed)

    def _with_user_invites(
        self,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        trigger: dict,
        decision: dispatch_rules.Decision,
    ) -> dispatch_rules.Decision:
        """Make sure the user's @ to someone outside the group becomes an invite slice."""
        content = str(trigger.get("content") or "")
        outside = self._outside_mentions(conversation, content)
        if not outside:
            return decision
        already = {
            agent_id for intent in decision.intents for agent_id in intent.invite
        }
        missing = [agent_id for agent_id in outside if agent_id not in already]
        if not missing:
            return decision
        text = " ".join(content.split())[: dispatch_rules.INTENT_TEXT_CHARS]
        invite = dispatch_rules.Intent(
            text=text,
            kind=dispatch_rules.KIND_INVITE,
            speakers=tuple(missing),
            invite=tuple(missing),
        )
        heads = list(decision.intents)
        if not heads:
            if decision.speakers and decision.kind == dispatch_rules.KIND_ANSWER:
                heads.append(
                    dispatch_rules.Intent(
                        text=text,
                        kind=dispatch_rules.KIND_ANSWER,
                        speakers=decision.speakers,
                    )
                )
            elif decision.kind in (dispatch_rules.KIND_STEER, dispatch_rules.KIND_RESUME):
                heads.append(
                    dispatch_rules.Intent(
                        text=text,
                        kind=decision.kind,
                        task_id=decision.task_id,
                    )
                )
            elif decision.kind == dispatch_rules.KIND_STATUS:
                heads.append(dispatch_rules.Intent(text=text, kind=decision.kind))
        intents = tuple([*heads, invite][: dispatch_rules.MAX_INTENTS])
        speakers: list[str] = []
        for intent in intents:
            for agent_id in intent.speakers:
                if agent_id not in speakers:
                    speakers.append(agent_id)
        return dispatch_rules.Decision(
            kind=dispatch_rules.KIND_ANSWER,
            speakers=tuple(speakers),
            opinions=decision.opinions,
            reason=decision.reason,
            by=decision.by,
            intents=intents,
        )

    def _queue_for(self, decision: dispatch_rules.Decision, reply_to: str) -> list[QueuedAgent]:
        """The run's queue for one answer decision: side by side, or in the decided order."""
        if decision.kind != dispatch_rules.KIND_ANSWER:
            return []
        cue = OPINION_CUE if decision.opinions and len(decision.speakers) > 1 else "turn"
        return [QueuedAgent(agent_id, reply_to, cue=cue) for agent_id in decision.speakers]

    async def _apply_decision(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        decision: dispatch_rules.Decision,
        trigger: dict,
        addressed: Sequence[str],
    ) -> list[QueuedAgent]:
        """Carry out one decision: reply, steer into a task, resume a task, a card, or silence."""
        if decision.intents:
            return await self._apply_intents(
                active, conversation, roster, decision, trigger, addressed
            )
        if decision.kind == dispatch_rules.KIND_NONE:
            return []
        if decision.kind == dispatch_rules.KIND_STATUS:
            self._post_status_card(active, conversation)
            return []
        if decision.kind == dispatch_rules.KIND_STEER:
            if decision.affects and self.note_work_activity(
                decision.affects,
                kind="request",
                text=str(trigger.get("content") or ""),
                source_message_id=str(trigger.get("id") or "") or None,
            ):
                return []
            if await self._steer_task(conversation, decision.task_id, trigger):
                return []
        elif decision.kind == dispatch_rules.KIND_RESUME:
            if await self._resume_task(conversation, decision.task_id, trigger):
                return []
        elif self._queue_for(decision, trigger["id"]):
            return self._queue_for(decision, trigger["id"])
        # The task stopped existing, or steer/resume was refused: someone still answers.
        rescue = self._fallback_decision(conversation, roster, addressed)
        if rescue is None:
            return []
        self._record_route(trigger, rescue)
        return self._queue_for(rescue, trigger["id"])

    async def _apply_intents(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        decision: dispatch_rules.Decision,
        trigger: dict,
        addressed: Sequence[str],
    ) -> list[QueuedAgent]:
        """Run one user message's slices in order, each with the focus it is owed."""
        members = {str(agent["id"]) for agent in roster}
        intents = decision.intents
        pending: list[QueuedAgent] = []
        posted_status = False
        queued_any = False
        for index, intent in enumerate(intents):
            if intent.kind == dispatch_rules.KIND_STATUS:
                if not posted_status:
                    self._post_status_card(active, conversation)
                    posted_status = True
                continue
            if intent.affects and self.note_work_activity(
                intent.affects,
                kind="request",
                text=intent.text or str(trigger.get("content") or ""),
                source_message_id=str(trigger.get("id") or "") or None,
            ):
                queued_any = True
                continue
            if intent.kind == dispatch_rules.KIND_STEER:
                if await self._steer_task(conversation, intent.task_id, trigger):
                    queued_any = True
                    continue
            elif intent.kind == dispatch_rules.KIND_RESUME:
                if await self._resume_task(conversation, intent.task_id, trigger):
                    queued_any = True
                    continue
            if intent.kind == dispatch_rules.KIND_INVITE:
                invites = [agent_id for agent_id in intent.invite if agent_id not in members]
                if invites:
                    pending.extend(
                        self._intent_invite_slots(
                            active, conversation, intent, invites, trigger, intents, index
                        )
                    )
                    queued_any = True
                    continue
            speakers = [
                agent_id
                for agent_id in (*intent.speakers, *intent.invite)
                if agent_id in members
            ]
            if not speakers:
                continue
            focus = prompts.intent_focus_instruction(
                intent.text or str(trigger.get("content") or ""),
                self._other_intent_names(intents, index, members),
            )
            cue = (
                OPINION_CUE
                if decision.opinions and len(speakers) > 1
                else "turn"
            )
            pending.extend(
                QueuedAgent(agent_id, trigger["id"], cue=cue, focus=focus)
                for agent_id in speakers
            )
            queued_any = True
        # A member the user addressed answers even if no intent named them: that rule is the
        # one thing the model may not override.
        queued = {slot.agent_id for slot in pending}
        missing = [
            agent_id
            for agent_id in addressed
            if agent_id in members and agent_id not in queued
        ]
        if missing:
            focus = prompts.intent_focus_instruction(str(trigger.get("content") or ""))
            pending.extend(
                QueuedAgent(agent_id, trigger["id"], cue="turn", focus=focus)
                for agent_id in missing
            )
        if pending:
            return pending
        if posted_status or queued_any:
            return []
        rescue = self._fallback_decision(conversation, roster, addressed)
        if rescue is None:
            return []
        self._record_route(trigger, rescue)
        return self._queue_for(rescue, trigger["id"])

    def _other_intent_names(
        self,
        intents: Sequence[dispatch_rules.Intent],
        index: int,
        members: set[str],
    ) -> list[str]:
        """The people the other slices went to, for this turn's focus line."""
        names: list[str] = []
        for other_index, other in enumerate(intents):
            if other_index == index:
                continue
            for agent_id in (*other.speakers, *other.invite):
                name = self._agent_name(agent_id) if agent_id in members else None
                if name and name not in names:
                    names.append(name)
        return names

    def _intent_invite_slots(
        self,
        active: ActiveRun,
        conversation: dict,
        intent: dispatch_rules.Intent,
        invites: Sequence[str],
        trigger: dict,
        intents: Sequence[dispatch_rules.Intent],
        index: int,
    ) -> list[QueuedAgent]:
        """The supervisor's pull-in turn, then the invited bot's own slice."""
        names = [self._agent_name(agent_id) for agent_id in invites]
        text = intent.text or str(trigger.get("content") or "")
        boss = self._supervisor()
        slots: list[QueuedAgent] = []
        supervisor_in_group = bool(
            boss is not None and str(boss["id"]) in conversation["member_ids"]
        )
        others = self._other_intent_names(intents, index, set(conversation["member_ids"]))
        if supervisor_in_group and boss is not None and str(boss["id"]) not in invites:
            slots.append(
                QueuedAgent(
                    str(boss["id"]),
                    trigger["id"],
                    cue="invite",
                    focus=prompts.invite_instruction(names, text),
                )
            )
            for agent_id in invites:
                slots.append(
                    QueuedAgent(
                        str(agent_id),
                        trigger["id"],
                        cue="turn",
                        focus=prompts.invited_focus_instruction(
                            text, [str(boss["name"]), *others]
                        ),
                        needs=tuple(str(agent_id) for agent_id in invites),
                    )
                )
            # The slice may also name someone already here ("让画师辅助匠师"): they take it too.
            for agent_id in intent.speakers:
                if agent_id in conversation["member_ids"] and agent_id not in invites:
                    slots.append(
                        QueuedAgent(
                            agent_id,
                            trigger["id"],
                            cue="turn",
                            focus=prompts.intent_focus_instruction(text, others),
                        )
                    )
            return slots
        # No supervisor to ask (or it is the one being invited): the engine adds them itself.
        fresh = self._add_members(active, conversation, invites)
        added = [str(agent_id) for agent_id in invites if str(agent_id) in fresh["member_ids"]]
        for agent_id in added:
            slots.append(
                QueuedAgent(
                    agent_id,
                    trigger["id"],
                    cue="turn",
                    focus=prompts.invited_focus_instruction(text, others),
                    needs=tuple(added),
                )
            )
        # A slice may also name someone already here ("让画师辅助匠师"): they take it too.
        for agent_id in intent.speakers:
            if agent_id in fresh["member_ids"] and agent_id not in added:
                slots.append(
                    QueuedAgent(
                        agent_id,
                        trigger["id"],
                        cue="turn",
                        focus=prompts.intent_focus_instruction(text, others),
                    )
                )
        return slots

    async def _dispatch_decision(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        trigger: dict,
        addressed: Sequence[str],
    ) -> dispatch_rules.Decision:
        """One no-tool call decides who answers, or the deterministic fallback does."""
        decision = await self._dispatch_call(active, conversation, roster, trigger, addressed)
        if decision is None:
            rescue = self._fallback_decision(conversation, roster, addressed)
            assert rescue is not None  # every group keeps at least one member
            return rescue
        return dispatch_rules.normalize(decision, addressed)

    async def _dispatch_call(
        self,
        active: ActiveRun,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        trigger: dict,
        addressed: Sequence[str],
    ) -> dispatch_rules.Decision | None:
        """Ask the dispatcher; ``None`` on a timeout, a failure or an unreadable answer."""
        model = resolve_dispatch(self._store, self._settings)
        if model is None:
            return None
        last = self._last_speaker(conversation)
        quoted = (
            self._store.get_message(trigger["reply_to_id"])
            if trigger.get("reply_to_id")
            else None
        )
        tasks = self._open_task_lines(conversation)
        activities = self._open_activity_lines(conversation)
        interrupted = self._interrupted_agent(conversation, roster)
        member_ids = {str(agent["id"]) for agent in roster}
        outside = [
            agent
            for agent in self._store.list_agents()
            if str(agent["id"]) not in member_ids
        ]
        context = {
            "group": {
                "title": conversation["title"],
                "purpose": conversation["purpose"],
                "rules": conversation.get("rules") or "",
            },
            "members": [
                {
                    "id": str(agent["id"]),
                    "name": agent["name"],
                    "kind": agent["kind"],
                    "instructions": " ".join(str(agent["instructions"]).split())[
                        : dispatch_rules.INSTRUCTION_CHARS
                    ],
                }
                for agent in roster
            ],
            "team_outside": [
                {
                    "id": str(agent["id"]),
                    "name": agent["name"],
                    "instructions": " ".join(str(agent["instructions"]).split())[
                        : dispatch_rules.INSTRUCTION_CHARS
                    ],
                }
                for agent in outside
            ],
            "absent": self._absent_names(conversation),
            "addressed": [
                {"id": agent_id, "name": self._agent_name(agent_id)} for agent_id in addressed
            ],
            "quoting": (quoted or {}).get("author_name"),
            "last_speaker": last["name"] if last is not None else None,
            "interrupted": self._agent_name(interrupted) if interrupted else None,
            "recent": self._recent_group_lines(conversation, limit=12),
            "tasks": tasks,
            "activities": activities,
            "message": str(trigger.get("content") or ""),
        }
        messages = dispatch_rules.messages(context)
        pseudo = {
            "id": "",
            "name": "分发",
            "kind": "supervisor",
            "instructions": "",
            "avatar_seed": None,
        }
        provider = self._store.get_provider(model.provider_id)
        caps = model_caps(provider, model.model, self._settings) if provider is not None else None
        effort = (
            dispatch_rules.reasoning_effort(caps.reasoning, caps.reasoning_levels)
            if caps is not None
            else None
        )
        stream = self._completion(
            messages,
            tools=None,
            author=pseudo,
            purpose="dispatch",
            max_tokens=dispatch_rules.MAX_TOKENS,
            model=model,
            reasoning_effort=effort,
        )
        pieces: list[str] = []

        async def collect() -> None:
            async for chunk in stream.chunks(should_stop=lambda: active.stop_requested):
                pieces.append(chunk)
            stream.validate(allow_tool_calls=False)

        try:
            await asyncio.wait_for(collect(), timeout=dispatch_rules.TIMEOUT_SECONDS)
        except RunStopped:
            raise
        except asyncio.TimeoutError:
            logger.warning("群组分发失败：TimeoutError")
            self._store.record_dispatch_failure("TimeoutError")
            return None
        except UpstreamError as exc:
            logger.warning("群组分发失败：%s", type(exc).__name__)
            self._store.record_dispatch_failure("UpstreamError")
            return None
        finally:
            self._record_dispatch_usage(active, conversation, stream)
        task_ids = [str(task["id"]) for task in tasks]
        answer = "".join(pieces)
        decision = dispatch_rules.parse(
            answer,
            member_ids=[str(agent["id"]) for agent in roster],
            task_ids=task_ids,
            invite_ids=[str(agent["id"]) for agent in outside],
            activity_ids=[str(activity["id"]) for activity in activities],
        )
        if decision is None:
            logger.warning("群组分发结果读不懂：%s", " ".join(answer.split())[:300])
            self._store.record_dispatch_failure("Unreadable")
        return decision

    def _record_dispatch_usage(self, active: ActiveRun, conversation: dict, stream: Any) -> None:
        """One usage row for the dispatcher, under no bot: routing is not anyone's budget."""
        usage = getattr(getattr(stream, "result", None), "usage", None)
        if usage is None or not usage.total:
            return
        self._store.record_usage(
            agent_id="",
            conversation_id=conversation["id"],
            run_id=active.run_id or None,
            purpose="dispatch",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
        )

    def _open_task_lines(self, conversation: dict) -> list[dict[str, Any]]:
        """Every task the dispatcher may steer or resume, with the fields it needs."""
        now = self._now()
        words = {
            task_rules.QUEUED: "排队中",
            task_rules.RUNNING: "进行中",
            task_rules.AWAITING: "等你回应",
            task_rules.STOPPED: "停下了",
            task_rules.FAILED: "没做完",
        }
        lines: list[dict[str, Any]] = []
        for task in self._store.open_tasks(conversation["id"]):
            owner = self._store.get_agent(task["owner_id"])
            lines.append(
                {
                    "id": task["id"],
                    "owner": owner["name"] if owner is not None else "已离开的成员",
                    "title": task["title"],
                    "status": words.get(str(task["status"]), str(task["status"])),
                    "step": int(task.get("steps") or 0),
                    "current": str(task.get("current_step") or ""),
                    "latest": " ".join(str(task.get("latest_text") or "").split())[
                        : dispatch_rules.LINE_CHARS
                    ],
                    "reason": task.get("end_reason") or "",
                    "age": working_set.age(str(task["updated_at"]), now),
                }
            )
        return lines

    def _open_activity_lines(self, conversation: dict) -> list[dict[str, Any]]:
        """Every live working lane the dispatcher may steer a new requirement into."""
        lines: list[dict[str, Any]] = []
        for activity in self._store.live_work_activities(conversation["id"]):
            activity_id = str(activity["id"])
            todos = self._store.list_activity_todos(activity_id)
            lines.append(
                {
                    "id": activity_id,
                    "agent": activity["agent_name"],
                    "title": activity["title"],
                    "status": activity["status"],
                    "current": str(activity.get("current_step") or ""),
                    "todos": todos,
                    "done": sum(1 for item in todos if item["status"] == "done"),
                    "total": len(todos),
                }
            )
        return lines

    def _absent_names(self, conversation: dict) -> list[str]:
        """Members the conversation still names but the team no longer has."""
        names: list[str] = []
        for agent_id in conversation["member_ids"]:
            if self._store.get_agent(agent_id) is not None:
                continue
            name = None
            for row in reversed(self._store.list_messages(conversation["id"])):
                if row.get("author_id") == agent_id and row.get("author_name"):
                    name = str(row["author_name"])
                    break
            names.append(name or str(agent_id))
        return names

    def _record_route(self, trigger: dict, decision: dispatch_rules.Decision) -> None:
        """Store the route on the user's message and let every window draw its hint."""
        updated = self._store.set_message_route(trigger["id"], decision.route())
        if updated is not None:
            self._publish_message("message_end", updated)

    def _mark_route_pending(self, trigger: dict) -> None:
        """Say the message is being routed, before the dispatcher's call has an answer."""
        updated = self._store.set_message_route(trigger["id"], {"pending": True})
        if updated is not None:
            self._publish_message("message_end", updated)

    def _fallback_decision(
        self,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        addressed: Sequence[str],
    ) -> dispatch_rules.Decision | None:
        """The deterministic route when the dispatcher gives nothing usable."""
        last = self._last_speaker(conversation)
        return dispatch_rules.fallback(
            addressed,
            self._last_reply_mentions(conversation, roster),
            self._interrupted_agent(conversation, roster),
            last["id"] if last is not None else None,
            self._first_member(conversation, roster),
        )

    def _last_reply_mentions(
        self, conversation: dict, roster: Sequence[Mapping[str, str]]
    ) -> list[str]:
        """Who the newest bot reply @-ed, if they are still in this group."""
        known = [str(agent["id"]) for agent in roster]
        member_ids = set(known)
        for row in reversed(self._store.list_messages(conversation["id"])):
            if row["kind"] != "agent" or row.get("author_id") not in member_ids:
                continue
            return [
                agent_id
                for agent_id in (str(item) for item in row.get("mentions") or [])
                if agent_id in member_ids
            ][: dispatch_rules.MAX_SPEAKERS]
        return []

    def _interrupted_agent(
        self, conversation: dict, roster: Sequence[Mapping[str, str]]
    ) -> str | None:
        """The bot a stopped turn cut short, while it is still a member of this group."""
        agent_id = self._store.interrupted_agent(conversation["id"])
        known = {str(agent["id"]) for agent in roster}
        return agent_id if agent_id and agent_id in known else None

    def _first_member(
        self, conversation: dict, roster: Sequence[Mapping[str, str]] | None = None
    ) -> str | None:
        """The group's first member, in the order the group lists them."""
        agents = roster if roster is not None else self._roster(conversation)
        return str(agents[0]["id"]) if agents else None

    def first_responder(self, conversation: dict, trigger: dict) -> str | None:
        """Who answers ``trigger`` first, guessed before the run starts.

        No model call here: the run's first payload only needs a plausible name, and the
        dispatcher replaces it a moment later. Addressed members, the last speaker, then the
        group's first member — never assuming a supervisor.
        """
        if conversation["kind"] in ("direct", "thread"):
            members = conversation["member_ids"]
            return members[0] if members else None
        roster = self._roster(conversation)
        known = {str(agent["id"]) for agent in roster}
        if trigger is not None:
            for agent_id in [*(trigger.get("addresses") or []), *(trigger.get("mentions") or [])]:
                if str(agent_id) in known:
                    return str(agent_id)
            for agent_id in parse_mentions(str(trigger.get("content") or ""), roster):
                if agent_id in known:
                    return agent_id
        last = self._last_speaker(conversation)
        return last["id"] if last is not None else self._first_member(conversation, roster)

    def _roster(self, conversation: dict) -> list[dict]:
        return resolve.roster(self._store, conversation)

    def _peer(self, conversation: dict, agent: dict) -> dict | None:
        """The other bot in a private chat, or ``None`` outside one."""
        return resolve.peer(self._store, conversation, agent)

    # ------------------------------------------------------------------ context

    def _lane_system_note(
        self, active: ActiveRun, conversation: dict, author: dict
    ) -> str:
        """What this lane owes its turn: a to-do board for work, real progress for a reply."""
        if active.lane == "reply":
            progress = self._reply_progress_note(conversation, author)
            return f"{prompts.REPLY_LANE_RULE}\n\n{progress}" if progress else prompts.REPLY_LANE_RULE
        if conversation["kind"] in ("group", "thread", "direct"):
            return prompts.TODO_RULE
        return ""

    def _board_lines(self, activity: dict, *, prefix: str = "") -> str:
        """One activity's board as the sentence a reply lane may quote."""
        activity_id = str(activity["id"])
        todos = self._store.list_activity_todos(activity_id)
        done = "、".join(item["text"] for item in todos if item["status"] == "done")
        doing = "、".join(item["text"] for item in todos if item["status"] == "doing")
        pending = "、".join(
            item["text"] for item in todos if item["status"] in ("pending", "doing")
        )
        steps = "；".join(self._store.activity_recent_steps(activity_id, 3))
        doing = doing or str(activity.get("current_step") or "")
        steps = steps or str(activity.get("latest_text") or "")
        return prompts.REPLY_PROGRESS_NOTE.format(
            title=f"{prefix}{activity['title']}",
            done=done or "暂无",
            doing=doing or "暂无",
            pending=pending or "暂无",
            steps=steps or "暂无",
        )

    def _reply_progress_note(self, conversation: dict, author: dict) -> str:
        """The board of every live lane in this conversation, its own work lane first."""
        live = self._store.live_work_activities(conversation["id"])
        if not any(str(row.get("agent_id")) == str(author.get("id")) for row in live):
            # Its own work ended (or waits on the user's answers): its last board still says
            # what it did and what is left, which is exactly what a progress question asks.
            last = self._store.last_work_activity(conversation["id"], str(author.get("id")))
            if last is not None:
                live.append(last)
        if not live:
            return ""
        live.sort(key=lambda row: str(row.get("agent_id")) != str(author.get("id")))
        lines = [
            self._board_lines(
                activity,
                prefix=""
                if str(activity.get("agent_id")) == str(author.get("id"))
                else f"「{activity.get('agent_name') or '同事'}」",
            )
            for activity in live
        ]
        return "\n".join(lines)

    def _supervisor_context(
        self,
        conversation: dict,
        *,
        trigger: str = "",
        cue: str | None = None,
        workflow: str = "",
    ) -> list[dict[str, Any]]:
        """The supervisor's request; ``cue`` is a last user turn telling it why it speaks now."""
        supervisor = self._supervisor()
        private_peer = None
        if conversation["kind"] == "private":
            peer = self._peer(conversation, supervisor)
            private_peer = peer["name"] if peer is not None else "另一位成员"
        system = prompts.supervisor_system(
            name=supervisor["name"],
            instructions=supervisor["instructions"],
            team=self._team_directory(),
            groups=self._group_directory(),
            conversation=self._conversation_line(conversation),
            private_peer=private_peer,
            in_group=conversation["kind"] == "group",
            react=conversation["kind"] in ("direct", "group"),
            memory=self._memory_block(conversation, supervisor, trigger),
            skills=self._skills_block(supervisor),
            workflow=workflow,
            hints=(prompts.group_rules_block(conversation.get("rules") or ""),)
            if conversation["kind"] == "group"
            else (),
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(
            self._history_turns(
                conversation,
                supervisor,
                vision=self._sees_images(supervisor),
                pdf=self._reads_pdf(supervisor),
            )
        )
        if cue is not None:
            messages.append({"role": "user", "content": cue})
        history.attach_runtime(
            messages,
            moment=self._now(),
            timezone=self._settings.timezone,
            hints=self._runtime_hints(conversation, supervisor, trigger),
        )
        return messages

    def _member_context(
        self,
        conversation: dict,
        roster: Sequence[Mapping[str, str]],
        agent: dict,
        *,
        task: str | None = None,
        answering: dict | None = None,
        taking_turn: bool = False,
        opinion: bool = False,
        hidden: frozenset[str] = frozenset(),
        instruction: str | None = None,
        assigner: str | None = None,
        trigger: str = "",
        workflow: str = "",
        note: str | None = None,
    ) -> list[dict[str, Any]]:
        """Only this member's instructions, this conversation's history and its trigger.

        ``hidden`` drops rows from the history (sibling opinions on the same message);
        ``opinion`` asks for this member's own view rather than a reply to the others.
        ``note`` is a final cue for this turn alone (e.g. the relay's closing instruction).
        """
        peer = self._peer(conversation, agent)
        system = prompts.member_system(
            agent=agent,
            conversation_kind=conversation["kind"],
            title=conversation["title"],
            purpose=conversation["purpose"],
            peer=peer["name"] if peer is not None else None,
            roster=roster,
            team_roster=self._team_roster(agent),
            groups=self._member_groups(agent),
            memory=self._memory_block(conversation, agent, trigger),
            skills=self._skills_block(agent),
            workflow=workflow,
            hints=(prompts.group_rules_block(conversation.get("rules") or ""),)
            if conversation["kind"] == "group"
            else (),
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(
            self._history_turns(
                conversation,
                agent,
                vision=self._sees_images(agent),
                pdf=self._reads_pdf(agent),
                hidden=hidden,
            )
        )
        if instruction is not None:
            messages.append({"role": "user", "content": instruction})
        elif task is not None:
            messages.append(
                {
                    "role": "user",
                    "content": prompts.task_instruction(task, assigner or self._supervisor()["name"]),
                }
            )
        elif taking_turn and answering is not None:
            # A discussion turn: @所有人, or a question another bot brought to this group.
            asker = None if answering["kind"] == "user" else answering["author_name"]
            build = prompts.opinion_instruction if opinion else prompts.turn_instruction
            messages.append({"role": "user", "content": build(answering["content"], asker)})
        elif answering is not None:
            messages.append(
                {
                    "role": "user",
                    "content": prompts.mention_instruction(
                        answering["author_name"], answering["content"]
                    ),
                }
            )
        if note:
            # One closing cue for this turn, appended to the user turn the model reads last.
            if messages and messages[-1].get("role") == "user":
                messages[-1]["content"] = f"{messages[-1]['content']}\n\n{note}"
            else:
                messages.append({"role": "user", "content": note})
        history.attach_runtime(
            messages,
            moment=self._now(),
            timezone=self._settings.timezone,
            hints=self._runtime_hints(conversation, agent, trigger),
        )
        return messages

    def _history_turns(
        self,
        conversation: dict,
        author: dict,
        *,
        vision: bool,
        pdf: bool,
        hidden: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """This conversation's history as ``author`` reads it, after the rolling summary.

        The replay plan follows this bot's own window: a 1M-token model sees its last dozen
        tool turns nearly in full, so it stops re-reading files it wrote itself.
        """
        conversation_id = conversation["id"]
        rows = [row for row in self._store.list_messages(conversation_id) if row["id"] not in hidden]
        summary = self._store.latest_summary(conversation_id)
        window = resolve_model(self._store, author, self._settings).context_window
        return [
            *([history.summary_message(summary["summary"])] if summary is not None else []),
            *history.to_chat_history(
                rows,
                self_id=author["id"],
                vision=vision,
                pdf=pdf,
                attachments_dir=self._store.attachments_dir,
                traces=self._store.list_tool_traces(conversation_id),
                since_seq=summary["upto_seq"] if summary is not None else 0,
                plan=budget_rules.replay_plan(window),
            ),
        ]

    def _now(self) -> datetime:
        """This turn's clock, in the timezone the schedules are written in."""
        return datetime.now(timezone.utc).astimezone(schedules.zone_of(self._settings.timezone))

    def _sees_images(self, agent: dict) -> bool:
        """Whether the model this bot answers with right now was marked as able to see images."""
        return resolve.sees_images(self._store, agent)

    def _reads_pdf(self, agent: dict) -> bool:
        """Whether that model can be handed a PDF document as well."""
        return resolve.reads_pdf(self._store, agent)

    def _skills_block(self, author: dict) -> str:
        """The per-bot ``<skills>`` block, or "" when this bot has no available skill."""
        return prompts.skills_block(
            [(skill["name"], skill["description"]) for skill in self._store.available_skills(author)]
        )

    def _team_directory(self) -> str:
        members = [agent for agent in self._store.list_agents() if agent["kind"] == "member"]
        return prompts.team_directory(self._supervisor(), members)

    def _group_directory(self) -> str:
        groups = [
            conversation
            for conversation in self._store.list_conversations()
            if conversation["kind"] == "group"
        ]
        return prompts.group_directory(groups, self._agent_name)

    def _conversation_line(self, conversation: dict) -> str:
        peers: list[str] = []
        if conversation["kind"] == "private":
            supervisor = self._supervisor()
            peers = [
                name
                for name in (self._agent_name(member_id) for member_id in conversation["member_ids"])
                if name != supervisor["name"]
            ]
        return prompts.conversation_line(
            kind=conversation["kind"],
            title=conversation["title"],
            purpose=conversation["purpose"],
            peers=peers,
        )

    def _agent_name(self, agent_id: str) -> str:
        return resolve.agent_name(self._store, agent_id)

    def _team_roster(self, agent: dict) -> str:
        others = [other for other in self._store.list_agents() if other["id"] != agent["id"]]
        return prompts.team_roster(others)

    def _member_groups(self, agent: dict) -> str:
        """The groups this bot belongs to, one line each with members and purpose."""
        groups = [
            conversation
            for conversation in self._store.list_conversations()
            if conversation["kind"] == "group" and agent["id"] in conversation["member_ids"]
        ]
        return prompts.group_directory(groups, self._agent_name) if groups else ""

    def _conversation_kind(self, conversation_id: str) -> str | None:
        conversation = self._store.get_conversation(conversation_id)
        return None if conversation is None else str(conversation["kind"])

    def _trigger_text(self, active: ActiveRun, conversation: dict) -> str:
        """The user message this turn answers, as the auto-recall search sees it."""
        trigger = self._trigger_message(active, conversation)
        if trigger is None or trigger["kind"] != "user":
            return ""
        return str(trigger["content"] or "")

    def _skill_hints(self, author: dict, trigger: str) -> list[str]:
        """The skills the user's message calls by ``/名称``, as hints this bot must act on.

        Every skill name is matched, so a disabled one (or one turned off for this bot) is
        named too: the bot says so instead of silently ignoring what the user asked for.
        """
        if "/" not in trigger:
            return []
        names = {skill["name"] for skill in self._store.list_skills()}
        available = {skill["name"] for skill in self._store.available_skills(author)}
        return [
            (prompts.SKILL_CALLED_HINT if name in available else prompts.SKILL_UNAVAILABLE_HINT).format(
                name=name
            )
            for name in skills_rules.named_skills(trigger, names)
        ]

    def _runtime_hints(
        self,
        conversation: dict,
        author: dict,
        trigger: str,
    ) -> list[str]:
        """Every live fact this turn's runtime block carries, in a stable order.

        One builder for a real call and for the composer's meter, so the number the bar shows
        is the number the request pays. The working set rides here (not in the system prompt)
        because it changes with every tool call: it must never break the prefix cache.
        """
        hints = [
            self.connectors.prompt_hint(),
            permissions.prompt_hint(self._store, self._settings, author),
            self._computer_hint(author),
        ]
        if author.get("kind") == "supervisor":
            hints.append(self._member_profile_hint())
        hints.extend(self._skill_hints(author, trigger))
        hints.append(
            working_set.hint(self._store, self._settings, conversation, now=self._now())
        )
        return [hint for hint in hints if hint]

    def _should_nudge(
        self, active: ActiveRun, author: dict, tools: Sequence[dict[str, Any]], body: str, nudged: bool
    ) -> bool:
        """Whether a finished reply stopped on a lead-in and deserves one more call.

        Only when ``ask_user`` was really offered and not called in this turn, the model is an
        API one (a CLI engine owns its own turn), and the text reads as an unfinished lead-in
        (see :func:`app.agent.dangling.is_dangling`). Once per turn.
        """
        if nudged or not body.strip():
            return False
        turn = active.turn
        if turn is not None and turn.asked:
            return False
        if ASK_USER not in {schema["function"]["name"] for schema in tools}:
            return False
        if not dangling_rules.is_dangling(body):
            return False
        return resolve_model(self._store, author, self._settings).protocol not in CLI_PROTOCOLS

    def _supervisor_coordinates(self, active: ActiveRun, conversation: dict, author: dict) -> bool:
        """Whether this supervisor turn runs the group instead of doing its work.

        True in a group by default: the tools that execute on the machine are withheld. The
        team setting ``supervisor_works_in_groups`` gives them back; so does the user naming
        the supervisor with ``@`` in that message.
        """
        if author.get("kind") != "supervisor" or conversation.get("kind") != "group":
            return False
        if self._store.team_settings().get("supervisor_works_in_groups") is True:
            return False
        trigger = self._trigger_message(active, conversation)
        if trigger is not None and trigger.get("kind") == "user":
            # Only a message that opens by addressing the supervisor hands it the machine.
            # A name further in is usually a reference (「按照 @幕僚长 的方案继续」), and
            # treating it as "do it yourself" is what had the supervisor take over the work.
            content = str(trigger.get("content") or "").lstrip()
            leading = parse_mentions(opening_mentions(content), self._roster(conversation))
            if author["id"] in leading:
                return False
        return True

    # ------------------------------------------------------------------ helpers

    def _computer_hint(self, author: dict) -> str:
        info = self.computer.info
        if info is None or not self.computer.connected:
            return ""
        hint = computer_rules.prompt_hint(
            info["name"],
            info["os"],
            info["screen"],
            vision=self._sees_images(author),
            host=bool(info.get("host")),
        )
        if self.computer.target == "mixed":
            hint += (
                "\n用户允许本机和云端两台电脑。调用时用 place 指定 local 或 cloud；"
                "不写 place 就用本机（就是用户正在用的这台电脑）。"
            )
        elif self.computer.target == "cloud":
            hint += "\n用户现在只允许操作云端电脑，不要假设这是他的 Mac 旁边那台沙盒。"
        return hint

    def _member_profile_hint(self) -> str:
        """A compact per-member stats line for the supervisor, at most 12 members."""
        members = [agent for agent in self._store.list_agents() if agent["kind"] == "member"]
        if not members:
            return ""
        now = datetime.now(timezone.utc)
        zone = schedules.zone_of(self._settings.timezone)
        entries: list[str] = []
        for member in members[:12]:
            profile = self._store.agent_profile(member["id"], period="week", now=now, zone=zone)
            if profile is None:
                continue
            tools = "、".join(tool["tool_name"] for tool in profile["top_tools"][:2])
            line = (
                f"「{member['name']}」完成 {profile['runs_completed']} 次，"
                f"验收打回 {profile['reviews']['revised']} 次"
            )
            if tools:
                line += f"，常用 {tools}"
            entries.append(line)
        return prompts.member_profile_hint(entries)

    def _schemas(
        self,
        role: str,
        conversation: dict | None = None,
        author: dict | None = None,
        *,
        scheduled: bool = False,
        coordinating: bool = False,
        from_thread: bool = False,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Every tool this role may call here, straight from the registry.

        The registry decides what a role sees: management tools for the supervisor, the
        packs its settings allow, connected connectors, the sandbox while it is up, and
        ``notify_user`` only inside a private bot chat. ``coordinating`` is the group
        supervisor turn that delegates instead of doing the work (see ``_offer``).
        """
        offer = ToolOffer(
            role=role,
            conversation=conversation,
            author=author,
            scheduled=scheduled,
            coordinating=coordinating,
            from_thread=from_thread,
            read_only=read_only,
        )
        return self._registry(offer).schemas(offer)

    def _offer(self, active: ActiveRun, conversation: dict, author: dict) -> ToolOffer:
        """Who is calling a tool, where, and whether anyone can answer a card here."""
        return ToolOffer(
            role=author["kind"],
            conversation=conversation,
            author=author,
            scheduled=active.schedule_id is not None,
            coordinating=self._supervisor_coordinates(active, conversation, author),
            # A reply lane may look up and answer, never change the workspace.
            read_only=active.lane == "reply",
            # A thread run already works a task; offering to open another is noise.
            from_thread=bool(active.task_id)
            or self._conversation_kind(active.conversation_id) == "thread",
        )

    def _registry(self, offer: ToolOffer) -> ToolRegistry:
        """Every family, in prompt order, for one offer."""
        handlers = [
            *supervisor_tools.handlers(),
            *schedule_tools.handlers(),
            *reminder_tools.handlers(),
            *pack_tools.handlers(self._settings, self._store),
            *connector_tools.handlers(self.connectors),
            *computer_tools.handlers(
                self.computer,
                sees_screen=lambda offered: offered.author is not None
                and self._sees_images(offered.author),
            ),
            *skills_tools.handlers(self._store, self.computer),
            *workflow_tools.handlers(self._store, self._run_workflow),
            *special_tools.handlers(),
            *board_tools.handlers(),
            *group_task_tools.handlers(),
            *task_tool.handlers(),
            *ask_tools.handlers(),
            *self_tools.handlers(),
            *subtask_tools.handlers(),
        ]
        if offer.author is not None:
            handlers = self._apply_tool_permissions(handlers, offer.author)
        return ToolRegistry(handlers)

    def _apply_tool_permissions(self, handlers: list, author: dict) -> list:
        """Hide the packs a member may not use; a hidden tool called anyway is unknown."""
        permissions = author.get("tool_permissions")
        if not isinstance(permissions, dict) or not permissions:
            return handlers

        def allowed(name: str) -> bool:
            if name in CODE_TOOL_NAMES:
                return bool(permissions.get("code"))
            if name in FILE_TOOL_NAMES:
                return bool(permissions.get("files"))
            if name in computer_rules.TOOL_NAMES or name == skills_tools.SKILL_TO_COMPUTER:
                return bool(permissions.get("computer"))
            connectors = permissions.get("connectors", "all")
            if connectors != "all":
                target = self.connectors.resolve(name)
                if target is not None:
                    if connectors == "none":
                        return False
                    return target.read_only
            return True

        return [
            replace(
                handler,
                offered_when=lambda offer, h=handler: h.offered_when(offer) and allowed(h.name),
                callable_when=lambda offer, h=handler: h.callable_when(offer) and allowed(h.name),
            )
            for handler in handlers
        ]

    def _tool_context(
        self,
        active: ActiveRun,
        conversation: dict,
        author: dict,
        action: dict,
        action_message_id: str,
        *,
        surface: dict | None = None,
    ) -> ToolRunContext:
        """Everything one tool call may touch, with the run-level services bound to it.

        ``conversation`` stays where this turn's rows go (a task's thread); ``surface`` is the
        chat the tools belong to (its group), so ``group_assign`` and the working set keep
        meaning what they mean in a group.
        """

        def finish(target: dict, **changes: Any) -> None:
            self._finish_action(active, target, **changes)

        return ToolRunContext(
            store=self._store,
            settings=self._settings,
            run=active,
            conversation=surface or conversation,
            author=author,
            action=action,
            action_message_id=action_message_id,
            http=self._client,
            computer=self.computer,
            connectors=self.connectors,
            publish=self._publish,
            publish_message=self._publish_message,
            publish_activity=lambda activity_id: self._publish_activity(
                self._store.get_activity(activity_id), force=True
            ),
            activity_id=ACTIVITY.get(),
            publish_workspace=self._publish_workspace,
            publish_sidebar=self._publish_sidebar,
            publish_schedules=self._publish_schedules,
            publish_decisions=self.publish_decisions,
            set_step=lambda step: self._set_step(active, step),
            refresh_task=self._refresh_task_progress,
            finish_action=finish,
            await_decision=lambda action_id: self._await_decision(active, action_id),
            await_answer=lambda action_id: self._await_answer(active, action_id),
            run_subtasks=lambda items, states, progress: self._run_subtasks(
                active, conversation, author, items, states, progress
            ),
            roster=lambda target: resolve.roster(self._store, target),
            supervisor_reply=lambda target, roster, reply_to_id: self._supervisor_reply(
                active, target, roster, reply_to_id
            ),
            member_context=self._member_context,
            member_call=lambda target, agent, messages, reply_to_id, roster=(): self._member_call(
                active, target, agent, messages, reply_to_id, roster
            ),
            consult_member=lambda group, agent, task, reply_to_id=None: self._consult_member(
                active, group, agent, task, reply_to_id=reply_to_id
            ),
            group_discussion=lambda group, asker, question: self._group_discussion(
                active, group, asker, question
            ),
            assign_part=lambda group, agent, instruction, parent: self._assign_part(
                active, group, author, agent, instruction, parent, action_message_id
            ),
            start_task=lambda task, title, plan, owner_id=None: self._start_task(
                active, task, title, plan, owner_id=owner_id
            ),
            group_busy=lambda conversation_id: conversation_id != active.conversation_id
            and self.is_running(conversation_id),
            review_member=lambda conversation, agent, task, messages, reply_to_id, roster, text, message: self._review_member_answer(
                active,
                conversation,
                agent,
                task,
                messages,
                reply_to_id=reply_to_id,
                roster=roster,
                first_text=text,
                first_message=message,
            ),
            origin_message_id=lambda: self._origin_id(active),
            source_message_id=lambda: self._source_message_id(active, author),
        )

    def _completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        author: dict,
        purpose: str | None = None,
        max_tokens: int | None = None,
        model: ResolvedModel | None = None,
        conversation: dict | None = None,
        active: ActiveRun | None = None,
        reasoning_effort: str | None = None,
    ) -> ModelStream:
        """Resolve this bot's model once per call, then open the matching protocol stream.

        Resolving here (not at startup) means a settings change applies from the next call,
        and a bot whose picked model vanished falls back instead of failing the run. This is
        the only way a request reaches a model, so the summariser is accounted for too.
        ``model`` overrides the resolution: the backup model retry passes its own pick.
        ``conversation`` lets a CLI engine continue its own session instead of re-reading the
        whole history from scratch. A CLI turn with tools gets a bridge to Cubes' own tools
        (:mod:`app.cli_tools`), so that bot can manage the team like an API model does.
        """
        resolved = model or resolve_model(self._store, author, self._settings)
        session = None
        prompt_messages = None
        remember = None
        bridge = None
        if conversation is not None and resolved.protocol in ("claude-code", "codex"):
            from . import cli_engines

            session, prompt_messages, remember = self._cli_turn(
                cli_engines, resolved, author, conversation, messages
            )
            if tools and active is not None:
                hub = getattr(self, "cli_tools", None)
                if hub is not None:
                    bridge = hub.begin(self, active, conversation, author, tools)
        stream = open_stream(
            self._client,
            resolved,
            messages=messages,
            tools=tools,
            purpose=purpose,
            max_tokens=max_tokens,
            session=session,
            prompt_messages=prompt_messages,
            cli_tools=bridge,
            reasoning_effort=reasoning_effort,
        )
        if remember is not None:
            # Called once the turn is known to have succeeded: a failed attempt must leave
            # the stored session where it was, so the next turn repeats only what is new.
            stream.remember = lambda: remember(stream)  # type: ignore[attr-defined]
        return stream

    def _cli_turn(
        self,
        cli_engines: Any,
        resolved: ResolvedModel,
        author: dict,
        conversation: dict,
        messages: list[dict[str, Any]],
    ) -> tuple[Any, list[dict[str, Any]] | None, Callable[[Any], None] | None]:
        """The CLI session this turn continues, the new part to send, and how to remember it.

        A stored session is resumed only when the model is the same and the history still
        starts with exactly what that session was given. A rewind, an edit, a compaction or
        a model change no longer matches, which is what starts a fresh session with the full
        rendered history once.
        """
        from .cli_engines import (
            CLISession,
            conversation_messages,
            messages_digest,
            new_messages,
            new_session_id,
        )

        agent_id = str(author.get("id") or "")
        conversation_id = str(conversation.get("id") or "")
        history = conversation_messages(messages)
        digest = messages_digest(history)
        signature = f"{resolved.provider_id}|{resolved.model}|{resolved.effort}"
        stored = self._store.get_cli_session(agent_id, conversation_id)
        session = None
        tail: list[dict[str, Any]] | None = None
        if stored is not None and stored["signature"] == signature:
            count = int(stored["message_count"] or 0)
            if 0 < count <= len(history) and messages_digest(history[:count]) == stored["digest"]:
                tail = new_messages(messages, count)
                if tail:
                    session = CLISession(str(stored["session_id"]), resume=True)
        if session is None:
            session = CLISession(new_session_id())
            tail = None

        def remember(stream: Any) -> None:
            # Codex only names its session in the event stream, so read the id back from it.
            used = str(getattr(stream, "session_id", "") or "") or session.id
            self._store.save_cli_session(
                agent_id,
                conversation_id,
                signature=signature,
                session_id=used,
                digest=digest,
                message_count=len(history),
            )

        return session, tail, remember

    async def _run_workflow(self, workflow: dict, input: str, started_by: str) -> tuple[str, str]:
        """Queue a walk from a tool call; it runs in its owner's own chat, after what is going on.

        Raises :class:`~app.errors.WorkflowBusy` when that bot already has a walk.
        """
        if self.launcher is None:  # pragma: no cover - the app wires it in at startup
            raise UpstreamError("工作流暂时无法启动。")
        return await self.launcher.start_workflow(workflow, input=input, started_by=started_by)

    def _supervisor(self) -> dict:
        return resolve.supervisor(self._store)

    def _require_run(self, active: ActiveRun) -> dict:
        run = self._store.get_run(active.run_id)
        if run is None:
            raise UpstreamError("任务不存在，无法继续。")
        return run

    def _computer_budget_reason(
        self, active: ActiveRun, tool: str, name: str, budget: step_budget.StepBudget
    ) -> str | None:
        """Which budget this sandbox step would exceed, in the wording the run panel uses.

        Per tool, so 30 pixel clicks cannot crowd out the shell and the browser; the total still
        caps the run. A budget is a shape: shell 30, browser 25, screen 8, 40 in all. The
        unlimited preset has no sandbox ceiling either — like every other call, the sandbox
        stops when the model stops asking.
        """
        if budget.unlimited:
            return None
        per_tool = {
            computer_rules.SHELL_TOOL: budget.computer_shell_steps,
            computer_rules.BROWSER_TOOL: budget.computer_browser_steps,
            computer_rules.SCREEN_TOOL: budget.computer_screen_steps,
        }
        label = computer_rules.TOOL_LABELS[tool]
        limit = per_tool.get(tool)
        if limit and active.computer_by_tool.get(tool, 0) >= limit:
            return f"「{name}」的{label}操作用到了 {limit} 步的上限，任务已结束。"
        total = budget.computer_steps
        if total and active.computer_steps >= total:
            return f"「{name}」的电脑操作用到了 {total} 步的上限，任务已结束。"
        return None

    def _reserve_model_call(self, active: ActiveRun) -> None:
        run = self._require_run(active)
        if run["status"] != "running":
            # The row was ended outside this task (Stop's fallback, or another process's
            # restart recovery): the task obeys it instead of working on unseen.
            raise RunStopped("任务已停止。")
        budget = step_budget.budget_for(self._store, self._settings)
        # Each computer step needs one more model call to read its screenshot.
        per_run = (
            budget.workflow_model_calls
            if active.workflow_run_id
            else budget.run_model_calls
        )
        limit = per_run + active.computer_steps
        if per_run and run["model_calls"] >= limit:
            raise RunLimited(f"已达本次任务的模型调用上限（{limit}），任务已结束。")
        turn = active.turn
        if turn is not None and not budget.turn_model_calls:
            # No turn ceiling either: the model calls are free to continue until it answers.
            turn.model_calls += 1
            if turn.wrap_pending:
                # A wrap asked for outside the presets (a reply lane's lookup cap): this call
                # answers with what the turn already found, and the next tool ends the turn.
                turn.wrap_pending, turn.wrapping = False, True
                turn.wrapped = False
        elif turn is not None:
            turn_limit = budget.turn_model_calls + turn.computer_steps
            if turn.wrap_pending:
                # The turn's tools ran out: this call is its last, to answer with what it has.
                turn.wrap_pending, turn.wrapping = False, True
                turn.wrapped = True
            else:
                if turn.model_calls >= turn_limit:
                    # Out of calls: this one still happens — asked for no tools, it is the
                    # answer; asked again, the refusal below ends the turn with what it wrote.
                    turn.wrap_pending, turn.wrapping = False, True
                    turn.wrapped = True
                elif turn.model_calls >= turn_limit - 1:
                    # One call left: make it the answering one, so a bot that was still
                    # searching spends it on the conclusion instead of on another tool round.
                    turn.wrap_pending = True
                    turn.wrapped = True
            turn.model_calls += 1
        self._store.update_run(active.run_id, model_calls=run["model_calls"] + 1)
        self._publish_run(active)

    async def _enforce_token_budget(
        self, active: ActiveRun, conversation: dict, author: dict
    ) -> None:
        """Pause (visible) or end (background) a reply when its bot is over its token budget.

        Runs before each reply model call. A bot already approved this run, one without a
        budget, or one still under its limit passes straight through. Memory extraction is
        counted but never gated: it never reaches this method.
        """
        budget = author.get("token_budget")
        if not isinstance(budget, dict) or author["id"] in active.budget_approved:
            return
        limit = budget.get("limit")
        period = budget.get("period")
        if not isinstance(limit, int) or period not in usage_rules.PERIODS:
            return
        zone = schedules.zone_of(self._settings.timezone)
        used = self._store.budget_used(author["id"], period, datetime.now(timezone.utc), zone)
        if used < limit:
            return
        word = usage_rules.period_word(period)
        error = f"「{author['name']}」已超出{word}用量预算"
        # A run nobody watches never parks on a card it cannot answer: it just ends limited.
        if active.background or active.schedule_id is not None:
            raise RunLimited(error)
        prompt = (
            f"「{author['name']}」{word}已用 {usage_rules.format_tokens(used)} token，"
            f"超过了设定的 {usage_rules.format_tokens(limit)}。继续这次回复吗？"
        )
        action, message = self._store.create_action(
            run_id=active.run_id,
            conversation_id=conversation["id"],
            tool_call_id=new_id("b"),
            tool_name="token_budget",
            arguments="{}",
            summary=f"「{author['name']}」超出用量预算",
            author_id=author["id"],
            author_name=author["name"],
            avatar_seed=author["avatar_seed"],
            status="awaiting",
            confirmation={"prompt": prompt, "confirm_label": "继续", "deny_label": "停下"},
            activity_id=ACTIVITY.get(),
        )
        self._publish_message("message_start", message)
        self._set_step(active, f"等待你确认「{author['name']}」超出预算")
        self.publish_decisions()
        self._refresh_task_progress()
        decision = await self._await_decision(active, action["id"])
        self._refresh_task_progress()
        if decision == "approve":
            self._finish_action(
                active,
                action,
                status="completed",
                summary=f"已同意「{author['name']}」超出预算继续",
                result="",
            )
            active.budget_approved.add(author["id"])
            return
        if decision == "stop":
            raise RunStopped("任务已停止。")
        # Denied or timed out: the run ends limited with the same wording as the card.
        self._finish_action(
            active, action, status="error", summary=error, error=error, result=error_result(error)
        )
        raise RunLimited(error)

    def _set_step(self, active: ActiveRun, step: str) -> None:
        updated = self._store.update_run(active.run_id, current_step=step)
        if updated is not None:
            self._report(active, updated)
        self._note_activity_step(active, step)
        if active.task_id:
            # A thread run's own step (including waiting on the screen) is the card's line.
            task = self._store.get_group_task(active.task_id)
            if task is not None:
                self._emit_task_progress(task)

    def _advance(
        self,
        active: ActiveRun,
        current_agent_id: str | None,
        pending: Sequence[QueuedAgent],
        hops: int,
        step: str | None = None,
    ) -> None:
        """Publish the queue position so clients can show who is next."""
        active.queued = [(queued.agent_id, queued.reply_to_id) for queued in pending]
        updates: dict[str, Any] = {
            "current_agent_id": current_agent_id,
            "pending_agent_ids": [queued.agent_id for queued in pending],
            "hops": hops,
        }
        if step is not None:
            updates["current_step"] = step
        updated = self._store.update_run(active.run_id, **updates)
        if updated is not None:
            self._report(active, updated)

    def _publish_run(self, active: ActiveRun) -> None:
        """Push the run's counters to whoever watches this run."""
        run = self._store.get_run(active.run_id)
        if run is not None:
            self._report(active, run)

    def _report(self, active: ActiveRun, run: dict) -> None:
        """A visible run goes out as ``run``; a scheduled one only moves its panel row.

        The hidden workspace stays hidden: what reaches clients is the ``schedules`` state,
        whose row carries the step and counters. That list is rebuilt per event, so a busy
        run refreshes it at most once a second; a change inside that second is sent when it
        ends, so the row never sits on a step the run has left. Its end always refreshes it
        too (via the scheduler).
        """
        if not active.background:
            self._publish("run", models.run_payload(run))
            return
        wait = active.reported_at + SCHEDULE_PROGRESS_SECONDS - time.monotonic()
        if wait <= 0:
            active.reported_at = time.monotonic()
            self._publish_schedules()
            return
        if active.report_pending is None:
            active.report_pending = asyncio.get_running_loop().call_later(
                wait, self._report_later, active
            )

    def _report_later(self, active: ActiveRun) -> None:
        active.report_pending = None
        if active.task is None or active.task.done():
            return  # the scheduler's own refresh at the end already said everything
        active.reported_at = time.monotonic()
        self._publish_schedules()

    def _publish_workspace(self) -> None:
        self._publish(
            "workspace",
            models.workspace_payload(
                self._store, confirm_timeout_seconds=self._settings.confirm_timeout_seconds
            ),
        )

    def publish_decisions(self) -> None:
        """Tell every window which cards and questions are waiting, without reloading providers."""
        from .decisions import collect_decisions

        self._publish(
            "decisions",
            collect_decisions(self._store, confirm_timeout_seconds=self._settings.confirm_timeout_seconds),
        )

    def _publish_sidebar(self) -> None:
        self._publish("sidebar", models.sidebar_payload(self._store.sidebar_layout()))

    def _publish(self, event: str, payload: Any) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait((event, payload))

    def _publish_message(self, event: str, message: dict) -> None:
        """A message event, unless it belongs to a hidden workspace.

        A scheduled run writes in a conversation no client ever sees, so its own replies and
        steps are stored for the model only. What it writes elsewhere (a private chat, a
        group post, a notice) belongs to that conversation and is published as usual.
        """
        if any(
            active.background and active.conversation_id == str(message["conversation_id"])
            for active in self._runs.values()
        ):
            return
        self._publish(event, models.message_payload(message))

    def publish(self, event: str, payload: Any) -> None:
        """Publish one engine-wide event (used outside a run, e.g. for the sidebar)."""
        self._publish(event, payload)

    def _note_turn_wrap(self, active: ActiveRun, conversation: dict, turn: TurnBudget | None) -> None:
        """One quiet line when a turn stopped at its step budget; the run remembers it.

        Written once per turn (``noticed``), and only when a limit forced the wrap: a bot that
        simply finished answering is not told to ask for 「继续」.
        """
        if turn is None or not turn.wrapped or turn.noticed:
            return
        turn.noticed = True
        active.turn_wrapped = True
        self._append_notice(
            active, conversation, prompts.turn_limited_notice(turn.agent_name, turn.tool_calls)
        )

    def _note_stop_line(self, active: ActiveRun, conversation: dict) -> None:
        """A stopped turn that had tool steps but no answer leaves one short line.

        The user then sees what was found so far instead of a wall of action rows with no
        sentence. The bot is remembered on the conversation, so a following 「继续」 goes
        back to it rather than to whoever the dispatcher would pick next.
        """
        run = self._store.get_run(active.run_id)
        agent_id = str((run or {}).get("current_agent_id") or "")
        rows = [
            message
            for message in self._store.list_messages(conversation["id"])
            if message.get("run_id") == active.run_id
        ]
        actions = [message for message in rows if message["kind"] == "action"]
        if not agent_id and actions:
            agent_id = str(actions[-1].get("author_id") or "")
        if not agent_id:
            return
        agent = self._store.get_agent(agent_id)
        if agent is None:
            return
        if conversation["kind"] == "group":
            self._store.set_interrupted_agent(conversation["id"], agent_id)
        answered = any(
            message["kind"] == "agent"
            and message.get("status") == "complete"
            and not message.get("leads_to_tools")
            for message in rows
        )
        if answered or not actions:
            return
        summaries = [
            " ".join(str((message.get("action") or {}).get("summary") or "").split())
            for message in actions
        ]
        found = next((summary for summary in reversed(summaries) if summary), "")
        if len(found) > 60:
            found = found[:59] + "…"
        message = self._store.append_message(
            conversation_id=conversation["id"],
            author_id=agent["id"],
            author_name=agent["name"],
            avatar_seed=agent["avatar_seed"],
            kind="agent",
            content=prompts.stop_line(found),
            status="complete",
            reply_to_id=active.trigger_message_id,
            run_id=active.run_id,
        )
        active.replies.append((agent["id"], message["id"]))
        self._publish_message("message_start", message)
        self._publish_message("message_end", message)

    def _append_notice(self, active: ActiveRun, conversation: dict, content: str) -> None:
        """A quiet system-authored line in the chat, e.g. why a call switched models."""
        if active.background:
            return
        message = self._store.append_message(
            conversation_id=conversation["id"],
            author_id=None,
            author_name="系统",
            avatar_seed=None,
            kind="notice",
            content=content,
            status="complete",
            run_id=active.run_id,
        )
        self._publish_message("message_start", message)
        self._publish_message("message_end", message)

    def _append_agent_message(
        self,
        active: ActiveRun,
        conversation_id: str,
        author: dict,
        *,
        status: str,
        content: str = "",
        reply_to_id: str | None = None,
    ) -> dict:
        message = self._store.append_message(
            conversation_id=conversation_id,
            author_id=author["id"],
            author_name=author["name"],
            avatar_seed=author["avatar_seed"],
            kind="agent",
            content=content,
            status=status,
            reply_to_id=reply_to_id,
            run_id=active.run_id,
            activity_id=ACTIVITY.get(),
        )
        active.streaming_ids.add(message["id"])
        active.replies.append((author["id"], message["id"]))
        return message

    def _complete_message(
        self,
        active: ActiveRun,
        message: dict,
        content: str,
        mood: str | None,
        roster: Sequence[Mapping[str, str]],
        images: Sequence[str] = (),
        reaction: str | None = None,
        calls: Sequence[Any] = (),
        *,
        tool_started: bool = False,
    ) -> dict | None:
        """Persist a finished reply: mentions come from the body, images from its tag.

        A trailing ``<react>`` tag lands on the message the reply answers. A reply that was
        only a reaction (empty body, no images, no tool calls) deletes its own streaming row
        instead of finishing: the turn is answered with no text and ``None`` comes back.

        ``leads_to_tools`` is written as a final verdict here: this response either called
        tools (so its text was narration, even a CLI's own tools that never reach ``calls``)
        or it did not (so the text is the answer).
        """
        applied = self._apply_reply_reaction(active, message, reaction)
        if not content.strip() and not images and not calls and applied:
            self._remove_reaction_only_reply(active, message)
            return None
        updated = self._store.update_message(
            message["id"],
            content=content,
            status="complete",
            mentions=parse_mentions(content, roster),
            mood=mood,
            leads_to_tools=bool(calls) or tool_started,
        )
        active.streaming_ids.discard(message["id"])
        author_id = message.get("author_id")
        if author_id:
            self._store.record_run_participant(active.run_id, author_id)
        if images and author_id and updated is not None:
            kept = self._store.visible_attachments(author_id, list(images))
            updated = self._store.attach_to_message(message["id"], kept) or updated
        return updated

    def _finish_action(
        self,
        active: ActiveRun,
        action: dict,
        *,
        status: str,
        summary: str,
        result: str,
        error: str | None = None,
        target_agent_id: str | None = None,
        target_conversation_id: str | None = None,
        detail: dict | None = None,
        message_status: str = "complete",
    ) -> None:
        waiting = (self._store.get_action(action["id"]) or {}).get("status") == "awaiting"
        finished = self._store.finish_action(
            action["id"],
            status=status,
            summary=summary,
            message_status=message_status,
            error=error,
            result=result,
            detail=detail,
            target_agent_id=target_agent_id,
            target_conversation_id=target_conversation_id,
        )
        if finished is None:  # pragma: no cover - the action was created moments ago
            return
        _, message = finished
        self._publish_message("message_end", message)
        if waiting:
            self.publish_decisions()
        self._sync_activity_awaiting(message.get("activity_id"))
        self._refresh_task_progress()

    def _record_unexecuted(
        self,
        active: ActiveRun,
        conversation: dict,
        call: ToolCallFragment,
        label: str,
        reason: str,
        author: dict | None = None,
    ) -> None:
        """Persist a refused call (budget or reused identity) as an errored action."""
        who = author or self._supervisor()
        action, message = self._store.create_action(
            run_id=active.run_id,
            conversation_id=conversation["id"],
            tool_call_id=call.id,
            tool_name=call.name or "unknown",
            arguments=canonical_arguments(call.arguments),
            summary=f"{label}未执行：{reason}",
            author_id=who["id"],
            author_name=who["name"],
            avatar_seed=who["avatar_seed"],
            status="error",
            activity_id=ACTIVITY.get(),
        )
        self._publish_message("message_start", message)
        self._finish_action(
            active,
            action,
            status="error",
            summary=f"{label}未执行：{reason}",
            error=reason,
            result=error_result(reason),
        )

    def _assistant_tool_turn(
        self, content: str, calls: list[ToolCallFragment]
    ) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": content if content.strip() else None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments or "{}",
                    },
                }
                for call in calls
            ],
        }

    def _finalize(self, active: ActiveRun, status: str, error: str | None, step: str) -> None:
        """Persist this run's terminal state, interrupt only its own work, then report it."""
        reason = error or {
            "stopped": "任务已停止。",
            "completed": "任务已完成。",
            "limited": "已达本次任务预算上限。",
        }.get(status, "任务已结束。")
        for message in self._store.interrupt_messages(sorted(active.streaming_ids)):
            self._publish_message("message_end", message)
        active.streaming_ids.clear()
        self._store.interrupt_actions(active.run_id, reason)
        conversation = self._store.get_conversation(active.conversation_id)
        if status == "stopped" and not active.background and conversation is not None:
            self._note_stop_line(active, conversation)
        updated = self._store.update_run(
            active.run_id,
            status=status,
            error=error,
            current_step=step,
            current_agent_id=None,
            pending_agent_ids=[],
            wrapped=1 if active.turn_wrapped else 0,
            finished_at=now_iso(),
        )
        if status == "limited" and not active.background and not active.turn_wrapped:
            # A turn's own notice already said it; only the run-level stop needs its own line.
            if conversation is not None:
                steps = int((updated or {}).get("tool_calls") or 0)
                self._append_notice(
                    active, conversation, prompts.run_limited_notice(error, steps)
                )
        if updated is not None and not active.background:
            self._publish("run", models.run_payload(updated))
        self._flush_task_text(active)
        self._settle_workflow(active, status, error)
        self._settle_group_tasks(active, status, error)
        self._settle_activities(active, status, error)
        self._note_reply_context(active)
        if active.background:
            self._deliver_summary(active, status, error)
        if self._runs.get(active.run_id) is active:
            del self._runs[active.run_id]
        self.computer.release(active.run_id)
        for callback in list(self._run_finished):
            try:
                callback(active.run_id)
            except Exception:  # a broken observer never changes a run's outcome
                pass
        self._schedule_memory_extraction(active, status)
        if not active.background and not active.compaction_attempted:
            # The next turn's request must not start above the auto-compaction point just
            # because this run's reply pushed it over: fold now, on a task of its own.
            self.compact_after_run(active.conversation_id)
        if not active.background:
            self._pump_after(active.conversation_id)

    def _note_reply_context(self, active: ActiveRun) -> None:
        """A reply lane ended: tell every live work lane what the group just did.

        This is background for the working turn, not a new requirement: it reaches the
        model at its next call boundary with ``NOTE_CONTEXT_NOTE`` wording.
        """
        if active.lane != "reply" or active.background:
            return
        conversation = self._store.get_conversation(active.conversation_id)
        if conversation is None:
            return
        trigger = (
            self._store.get_message(active.trigger_message_id)
            if active.trigger_message_id
            else None
        )
        if trigger is None:
            return
        answer = ""
        answerer = ""
        for message in reversed(self._store.list_messages(conversation["id"])):
            if (
                message.get("run_id") == active.run_id
                and message["kind"] == "agent"
                and message.get("status") == "complete"
                and str(message.get("content") or "").strip()
            ):
                answer = " ".join(str(message["content"]).split())[:120]
                answerer = str(message.get("author_name") or "")
                break
        if not answer:
            # A status card or a request note already said what the user's message did;
            # there is no answer to summarise for the working lane.
            return
        asker = "用户" if trigger["kind"] == "user" else str(trigger.get("author_name") or "同事")
        who = answerer or "有一路"
        text = f"{asker}问了「{' '.join(str(trigger.get('content') or '').split())[:120]}」，{who}回答：「{answer}」。"
        for activity in self._store.live_work_activities(conversation["id"]):
            if str(activity.get("run_id") or "") == active.run_id:
                continue
            self.note_work_activity(
                str(activity["id"]),
                kind="context",
                text=text,
                source_message_id=str(trigger.get("id") or "") or None,
            )

    def _settle_workflow(self, active: ActiveRun, status: str, error: str | None) -> None:
        """Record how this run ended on the walk it served, unless the walk said so itself.

        The loop parks a run as ``waiting_user`` or ``paused`` before leaving, and those are the
        states the user acts on: the engine's own ``stopped`` must not overwrite them. A run
        that was merely stopped (the stop button, a message that interrupted it, a restart)
        does not end the walk either: the chat belongs to the bot, so the walk is put down as
        paused and continues from the same step. Only the workflow's own stop ends it.
        """
        workflow_run = self._store.get_workflow_run(active.workflow_run_id or "")
        if workflow_run is None:
            return
        if workflow_run["status"] in (
            workflow_rules.STATUS_QUEUED,
            workflow_rules.STATUS_RUNNING,
        ):
            if status == "stopped":
                self._store.park_workflow_run(workflow_run["id"])
            else:
                final = (
                    workflow_rules.STATUS_DONE
                    if status == "completed"
                    else workflow_rules.STATUS_FAILED
                )
                self._store.update_workflow_run(
                    workflow_run["id"],
                    status=final,
                    finished_at=now_iso(),
                    summary=error
                    or ("工作流已完成。" if final == workflow_rules.STATUS_DONE else ""),
                )
        self._publish_workflow_run(workflow_run["id"])
        self._workflow_marker(active, workflow_run["id"])

    def _pump_after(self, conversation_id: str) -> None:
        """A run ended: start what this chat queued."""
        if self.launcher is None or self._streams_closed:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self.launcher.pump(conversation_id)
            )
        except RuntimeError:  # pragma: no cover - finalize always runs inside the loop
            return
        self._pump_tasks.add(task)
        task.add_done_callback(self._pump_tasks.discard)

    def _deliver_summary(self, active: ActiveRun, status: str, error: str | None) -> None:
        """A scheduled run ended: its owner says one thing in the target conversation.

        The workspace itself is invisible, so this message is the whole visible outcome —
        the run's final reply, or a short note saying why there is none. A stopped run
        says nothing. Nothing is delivered when the target conversation is gone.
        """
        if status == "stopped":
            return
        schedule = self._store.get_schedule(active.schedule_id or "")
        if schedule is None:
            return
        target = self._store.get_conversation(schedule["conversation_id"])
        if target is None:
            return
        owner = self._store.get_agent(schedule["owner_agent_id"] or "") or self._supervisor()
        if owner is None:  # pragma: no cover - the supervisor always exists
            return
        mood: str | None
        images: list[str]
        if status == "completed":
            reply = self._store.last_agent_message(active.conversation_id)
            content = (reply["content"] if reply is not None else "").strip()
            if not content:  # pragma: no cover - a completed run ends with a reply
                return
            mood = reply["mood"] if reply is not None else None
            images = [str(image["id"]) for image in (reply or {}).get("attachments") or []]
        else:
            reason = error or "没有完成。"
            content = f"定时任务「{schedule['title']}」这次没有做完：{reason}"
            mood, images = "sorry", []
        message = self._store.append_message(
            conversation_id=target["id"],
            author_id=owner["id"],
            author_name=owner["name"],
            avatar_seed=owner["avatar_seed"],
            kind="agent",
            content=content,
            status="complete",
            schedule={"id": schedule["id"], "title": schedule["title"]},
            mood=mood,
            attachment_ids=images,
            run_id=active.run_id,
        )
        # The workspace is silent, but this message is posted like any other reply.
        self._publish("message_start", models.message_payload(message))
        self._publish("message_end", models.message_payload(message))
