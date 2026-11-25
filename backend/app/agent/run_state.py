"""Per-run state, and the launch/stop coordination every entry point shares.

One run per conversation is the invariant: a run reserves its conversation under a lock,
interrupts whatever reply was running there, and the scheduler's fires go through the same
path so they can never touch a run the user started. One run may still serve several bot turns
at once — a workflow's parallel branches — so the turn being served is a context variable: it
belongs to the task that set it, and a sibling task never sees it.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from .. import attachments as attachment_rules
from .. import models
from .. import workflows as workflow_rules
from ..errors import EngineBusy, WorkflowBusy
from ..constants import STEP_QUEUED, STEP_STOPPED
from ..store import Store, now_iso

# A new message waits this long for the reply it interrupts to wind down, then cancels it.
INTERRUPT_STOP_TIMEOUT = 3.0


def resolve_send_mode(store: Store, conversation: dict, running: dict | None) -> str:
    """The mode a send carries when the request named none.

    The bot the send will meet decides: a direct chat's member, or in a group the member
    speaking right now (``running``). A member's own pick wins; without one the team setting
    applies. Between turns nobody is speaking, so the team setting answers.
    """
    agent_id: str | None = None
    if conversation.get("kind") == "direct" and len(conversation["member_ids"]) == 1:
        agent_id = conversation["member_ids"][0]
    elif running is not None:
        agent_id = running.get("current_agent_id")
    agent = store.get_agent(agent_id) if agent_id else None
    if agent is not None and agent.get("send_mode"):
        return str(agent["send_mode"])
    return str(store.team_settings()["send_mode"])


# The bot turn the current task is serving (see ``ActiveRun.turn``). Each task — and each
# workflow branch started with ``create_task`` — gets its own context, so nothing here leaks
# from one branch to another run or another branch.
_TURN: ContextVar["TurnBudget | None"] = ContextVar("cubes_turn_budget", default=None)
# The group task the current bot turn is working on; a context variable for the same reason:
# side-by-side opinions each work on their own task.
GROUP_TASK: ContextVar[str | None] = ContextVar("cubes_group_task", default=None)
# The activity the current bot turn is writing (see ``app.store.activities``). Nested turns —
# an ``ask_agent`` answer, a private chat opened by ``direct_message`` — read the outer value,
# which is exactly the ``parent_id`` of the turn they open.
ACTIVITY: ContextVar[str | None] = ContextVar("cubes_activity", default=None)


@dataclass
class QueuedAgent:
    """One pending slot in a run's queue: who replies next, what they answer, and why.

    ``cue`` is how the slot was earned: ``mention`` (someone @-ed it, or the user's message
    named it), ``turn`` (@所有人 or a bot's group question), ``opinion`` (side by side) or
    ``closing`` (the supervisor speaking last after the members).
    """

    agent_id: str
    reply_to_id: str | None = None
    cue: str = "mention"
    # The group task this slot works on, when it continues one (resume, reply, transfer).
    task_id: str | None = None
    # What the previous owner said when it transferred the task here.
    note: str = ""
    # This turn's slice of a multi-ask user message (already a prompt line), when it has one.
    focus: str = ""
    # An invite slot: the turn runs only once these agents are in the group.
    needs: tuple[str, ...] = ()


@dataclass
class TurnBudget:
    """One bot's turn: its own model and tool calls, apart from the bots that spoke before it.

    A turn whose tools ran out is told so in the refused calls' results; its next model call
    (``wrap_pending``) is its last, to answer from what it already found instead of losing the
    work. A tool asked for during that call (``wrapping``) ends the run.
    """

    agent_id: str
    agent_name: str
    model_calls: int = 0
    tool_calls: int = 0
    computer_steps: int = 0
    # ``ask_user`` ran in this turn: a reply that stops on a lead-in needs no nudge then.
    asked: bool = False
    wrap_pending: bool = False
    wrapping: bool = False
    # Set when a limit forced the wrap (not when the bot chose to answer): the run remembers
    # it, and the chat gets one quiet notice saying 「继续」 picks up from here.
    wrapped: bool = False
    # The notice was already written for this turn; one line per turn.
    noticed: bool = False

    @property
    def label(self) -> str:
        return f"「{self.agent_name}」这一轮"


@dataclass
class ActiveRun:
    run_id: str
    conversation_id: str
    trigger_message_id: str | None = None
    task: asyncio.Task[None] | None = None
    stop_requested: bool = False
    streaming_ids: set[str] = field(default_factory=set)
    # How many private chats deep the current turn is; nested turns share this run.
    dm_depth: int = 0
    # Non-zero while this run is doing subtasks: those turns may not split again.
    subtask_depth: int = 0
    # (conversation_id, agent_id) pairs that already notified the user in this turn.
    notified: set[tuple[str, str]] = field(default_factory=set)
    # The schedule that started this run, or ``None`` for a run the user started.
    schedule_id: str | None = None
    # A scheduled run works in a hidden conversation: none of its events reach a client.
    background: bool = False
    # When this hidden run last refreshed its row in the schedules panel (monotonic seconds).
    reported_at: float = float("-inf")
    # The refresh that sends a step change that arrived inside the last second, if one is due.
    report_pending: asyncio.TimerHandle | None = None
    # The user allows the sandbox once per run; a refusal holds for the rest of it.
    computer_allowed: bool = False
    computer_denied: bool = False
    # 「继续」 in a direct chat, when the chat's previous run stopped at a budget: the cue this
    # run tells every bot turn, so it picks up the unfinished work instead of starting over.
    continue_cue: str = ""
    # Some bot's turn ran into a budget this run; persisted so the next 「继续」 knows.
    turn_wrapped: bool = False
    # This run already tried to fold its history before a request. A second fold right after
    # the run would only nibble at the turns the first one kept, so the next run's own
    # pre-turn check is left to handle growth.
    compaction_attempted: bool = False
    # Computer steps have their own budget, and each one buys the model call that reads it.
    # Each sandbox tool is counted separately as well: pixel steps are the expensive ones.
    computer_steps: int = 0
    computer_by_tool: dict[str, int] = field(default_factory=dict)
    # What the sandbox tools remember between steps of this run: the last step and the page it
    # left (the repeat guard), the page or screen as the backend last saw it, and whether the
    # browser is the window in front (protocol 4 does not report the focused window yet).
    computer_last: dict | None = None
    computer_page: str | None = None
    computer_browser_front: bool = False
    # The files this run has read or written, as ``path -> (fingerprint, epoch seconds)`` of
    # what it last saw. A write whose current fingerprint differs, and that the working set
    # says another run changed since, is refused: two runs must not undo each other's work.
    file_ledger: dict[str, tuple[str, float]] = field(default_factory=dict)
    # (agent_id, message_id) of every agent reply this run wrote, in order. Memory writes
    # point at them, and the run's extraction names them as their source.
    replies: list[tuple[str, str]] = field(default_factory=list)
    # Agent ids whose over-budget reply the user approved this run; each prompts only once.
    budget_approved: set[str] = field(default_factory=set)
    # The workflow walk this run is serving, when it serves one: it owns the cursor, and the
    # run's own budget is the wider workflow one.
    workflow_run_id: str | None = None
    # Pause/skip/retry pressed while the walk was busy, with the node it names (``None`` for
    # pause): applied at the next step boundary.
    workflow_controls: list[tuple[str, str | None]] = field(default_factory=list)
    # Bots whose turn ran out of its own budget this run, by agent id: said out loud in the
    # result that asked for them, so "no reply" never reads as "it chose not to answer".
    turn_limited: dict[str, str] = field(default_factory=dict)
    # Messages the user steered into this run, oldest first. Each is already saved in the
    # transcript; the loop hands it to the model at its next call boundary, never mid-stream.
    steers: list[dict] = field(default_factory=list)
    # ``group_transfer`` calls waiting to be queued: (task_id, new owner id, note).
    transfers: list[tuple[str, str, str]] = field(default_factory=list)
    # The slots still waiting their turn, as last published: (agent_id, reply_to_id).
    queued: list[tuple[str, str | None]] = field(default_factory=list)
    # When a group task's progress last reached the card, in monotonic seconds (SSE throttle).
    task_progress_at: float = float("-inf")
    # The group task this run is a hidden thread run for, and the opening it must give the
    # thread. They are set by the task scheduler; a normal thread post leaves them ``None``.
    task_id: str | None = None
    task_instruction: str | None = None
    # Who answers this run's message, when the user re-dispatched it themselves: the
    # dispatcher is skipped and the route is already stored on the message.
    route_override: list[str] = field(default_factory=list)
    # The run failed because the model service did, not because the work did: the reason a
    # group task keeps says so, instead of a generic failure line.
    upstream_error: bool = False
    # Who stopped this run, for the tasks it leaves behind: 你, 群里的「X」, or a restart.
    stopped_by: str = "你"
    # The activity whose turn started this run (a task thread opened by ``start_task`` or
    # ``group_assign``). Read when the run is started, so a queued thread still knows who
    # caused it after the group turn has long finished.
    activity_parent: str | None = None

    def take_steers(self) -> list[dict]:
        """Hand over every steer waiting now, so each reaches the model exactly once."""
        taken, self.steers = self.steers, []
        return taken

    def keep_steers_after(self, seq: int) -> None:
        """Drop steers a just-built request already contains; keep only newer ones."""
        self.steers = [message for message in self.steers if int(message["seq"]) > seq]

    @property
    def turn(self) -> TurnBudget | None:
        """The bot turn the current task is serving.

        A context variable rather than a field: a workflow's branches run as separate tasks,
        and each one must spend and wrap its own turn budget without seeing — or restoring —
        the budget of the branch that happens to be in the middle of a model call.
        """
        return _TURN.get()

    @turn.setter
    def turn(self, value: TurnBudget | None) -> None:
        _TURN.set(value)


class RunLauncher:
    """Reserve, start and stop one conversation's run; scheduler fires come through here."""

    def __init__(self, store: Store, engine: Any) -> None:
        self._store = store
        self._engine = engine
        # One lock per conversation: interrupting the old run and starting the new one is a
        # single step, so two quick sends can never interleave.
        self._locks: dict[str, asyncio.Lock] = {}

    def is_live(self, run_id: str) -> bool:
        return self._engine.live_run(run_id) is not None

    async def finish_stop(
        self, run_id: str, *, timeout: float | None = None
    ) -> None:
        """Stop one running run and make sure it ends up stopped, owned or not."""
        stopped = await self._engine.stop(run_id, timeout=timeout)
        current = self._store.get_run(run_id)
        if stopped or current is None or current["status"] != "running":
            return
        # No in-process task owns this run any more: fail it explicitly instead of
        # leaving the conversation permanently busy.
        self._store.interrupt_messages(
            self._store.streaming_message_ids(current["conversation_id"])
        )
        self._store.interrupt_actions(run_id, "任务已停止。")
        self._store.update_run(
            run_id,
            status="stopped",
            error=None,
            current_step=STEP_STOPPED,
            current_agent_id=None,
            pending_agent_ids=[],
            finished_at=now_iso(),
        )
        # No turn owns this run any more, so its open activities close here or never.
        for activity in self._store.activities_of_run(run_id, unfinished=True):
            self._store.finish_activity(
                str(activity["id"]), status="stopped", summary="已停下：任务已停止。"
            )

    async def _heal(self, conversation_id: str) -> dict | None:
        """Make the engine and the database agree on this chat's run; answer the live row.

        Two ways they drift apart, and either one used to lock a chat for good: a row still
        marked running with no task behind it (every send queued behind a run that would
        never end), and a task still working whose row was ended elsewhere (every send
        refused as busy, and Stop did nothing because the row already said stopped).
        """
        running = self._store.active_run(conversation_id)
        if running is not None and self._engine.live_run(running["id"]) is None:
            await self.finish_stop(running["id"], timeout=INTERRUPT_STOP_TIMEOUT)
            running = None
        stray = self._engine.live_run_in(conversation_id)
        if stray is not None and (running is None or running["id"] != stray.run_id):
            await self._engine.stop(stray.run_id, timeout=INTERRUPT_STOP_TIMEOUT)
        return running

    async def launch(
        self,
        conversation: dict,
        *,
        content: str,
        mentions: list[str],
        reply_to_id: str | None = None,
        attachment_ids: list[str] | None = None,
        schedule: dict | None = None,
        interrupt: bool = True,
        use_computer: bool = False,
        mode: str = "interrupt",
        addresses: list[str] | None = None,
    ) -> dict | None:
        """Reserve and start one run; the HTTP endpoint and scheduled fires share this.

        ``mode`` is how a user send meets a reply already running here: ``steer`` joins that
        turn — the message is saved and handed to the model at its next call boundary, never
        mid-stream — while ``queue`` waits for a run of its own once this one ends. Any other
        mode is the old behaviour: the running reply is stopped first (``interrupt``).
        ``interrupt=False`` is a scheduled fire: it never touches a running run, and returns
        ``None`` when the conversation is busy so the scheduler queues that fire instead.
        """
        lock = self._locks.setdefault(conversation["id"], asyncio.Lock())
        async with lock:
            running = await self._heal(conversation["id"])
            # Checked under the lock: two sends of one upload can never both bind it.
            if self._store.unbound_attachments(attachment_ids or []) is None:
                raise HTTPException(status_code=422, detail=attachment_rules.STALE)
            if running is not None:
                if mode == "steer" and interrupt:
                    joined = self._join_running(
                        conversation,
                        running,
                        content=content,
                        mentions=mentions,
                        reply_to_id=reply_to_id,
                        attachment_ids=attachment_ids,
                    )
                    if joined is not None:
                        return joined
                    # This bot drives its own CLI session: a message cannot join its turn,
                    # so it waits like any other.
                    mode = "queue"
                if mode == "queue":
                    return self._queue_send(
                        conversation,
                        content=content,
                        mentions=mentions,
                        reply_to_id=reply_to_id,
                        attachment_ids=attachment_ids,
                        addresses=addresses,
                    )
                if not interrupt:
                    return None
                await self.finish_stop(running["id"], timeout=INTERRUPT_STOP_TIMEOUT)
            # A send already waiting keeps its place: a later one queues behind it rather than
            # overtaking it. An explicit interrupt is the exception — the user asked to speak now.
            if mode != "interrupt" and self._store.queued_run(conversation["id"]) is not None:
                return self._queue_send(
                    conversation,
                    content=content,
                    mentions=mentions,
                    reply_to_id=reply_to_id,
                    attachment_ids=attachment_ids,
                    addresses=addresses,
                )
            # No await between here and reserving the run: nothing else can start one.
            run, message = self._store.begin_run(
                conversation["id"],
                content,
                mentions,
                reply_to_id=reply_to_id,
                interrupts=running is not None,
                attachment_ids=attachment_ids,
                schedule=schedule,
                addresses=addresses,
            )
            # A walk parked on a question takes this message as its answer: the run picks the
            # walk up from its rows, so asking on the page and answering on the page are one
            # path. A paused walk is left alone, and so is a walk this message interrupted
            # (the engine parked it as paused): talking there is not resuming it.
            workflow_run = None
            if schedule is None:
                parked = self._store.live_workflow_run_by_conversation(conversation["id"])
                if parked is not None and parked["status"] == workflow_rules.STATUS_WAITING:
                    workflow_run = parked
                    self._store.update_workflow_run(
                        parked["id"], status=workflow_rules.STATUS_RUNNING, run_id=run["id"]
                    )
                    self._publish_workflow_run(parked["id"])
            # Name who answers before anything is published: the run's first payload must
            # not leave clients guessing (and showing the supervisor in a member's chat).
            owner_id = (
                conversation["member_ids"][0]
                if workflow_run is not None and conversation["member_ids"]
                else None
            )
            self._store.update_run(
                run["id"],
                current_agent_id=owner_id
                or self._engine.first_responder(conversation, message),
            )
            try:
                self._engine.start(
                    run["id"],
                    conversation["id"],
                    message_id=message["id"],
                    schedule_id=(schedule or {}).get("id"),
                    # A schedule the user marked may use the sandbox skips the consent card.
                    computer_allowed=bool(use_computer) and schedule is not None,
                    workflow_run_id=workflow_run["id"] if workflow_run is not None else None,
                )
            except EngineBusy as exc:  # pragma: no cover - healed under the lock above
                # Nothing started: take the message back, so a retry never leaves a copy.
                removed = self._store.discard_run(run["id"])
                for message_id in removed:
                    self._engine.publish(
                        "message_removed",
                        {"conversation_id": conversation["id"], "message_id": message_id},
                    )
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return self._store.get_run(run["id"]) or run

    # ------------------------------------------------------------------ steers

    def _owner_of(self, conversation: dict | None) -> dict | None:
        """The bot a direct chat or a task thread belongs to; ``None`` for a group or walk."""
        if conversation is None or conversation["kind"] not in ("direct", "thread"):
            return None
        if len(conversation["member_ids"]) != 1:
            return None
        return self._store.get_agent(conversation["member_ids"][0])


    def _queue_send(
        self,
        conversation: dict,
        *,
        content: str,
        mentions: list[str],
        reply_to_id: str | None,
        attachment_ids: list[str] | None,
        addresses: list[str] | None = None,
    ) -> dict:
        """Write the message as queued and tell every window, without starting anything."""
        queued, message = self._store.begin_queued_run(
            conversation["id"],
            content,
            mentions,
            reply_to_id=reply_to_id,
            attachment_ids=attachment_ids,
            addresses=addresses,
        )
        self._engine.publish("message_end", models.message_payload(message))
        return queued

    def _steerable(self, agent: dict) -> bool:
        """Whether a message can join this bot's running turn mid-reply.

        A CLI engine runs its own session around a fixed prompt: the only place to add a
        message is a new turn, so such a bot queues the steer instead.
        """
        from .. import cli_engines
        from ..providers import resolve_model

        try:
            protocol = resolve_model(self._store, agent, self._engine._settings).protocol
        except Exception:
            # No resolvable model this instant: worse to lose the message than to queue it.
            return False
        return protocol not in cli_engines.CLI_PROTOCOLS

    def _join_running(
        self,
        conversation: dict,
        running: dict,
        *,
        content: str,
        mentions: list[str],
        reply_to_id: str | None,
        attachment_ids: list[str] | None,
    ) -> dict | None:
        """Save the message and hand it to the live run; ``None`` when it cannot join.

        Nothing awaits between the liveness check and the hand-off, so a run that ends here
        can never swallow the message: it either joins the turn or the caller queues it.
        """
        owner = self._owner_of(conversation)
        if owner is None:
            # A group has no single owner: the bot taking the turn right now is the one that
            # reads the steer. Between turns there is nobody to tell, so it waits instead.
            owner = (
                self._store.get_agent(running["current_agent_id"])
                if running.get("current_agent_id")
                else None
            )
        if owner is None or not self._steerable(owner):
            return None
        active = self._engine.live_run(running["id"])
        if active is None:
            return None
        message = self._store.append_user_message(
            conversation["id"],
            content,
            mentions,
            reply_to_id=reply_to_id,
            attachment_ids=attachment_ids,
        )
        self._engine.steer(active, message)
        return self._store.get_run(running["id"]) or running

    # ------------------------------------------------------------------ workflows

    def _publish_workflow_run(self, workflow_run_id: str) -> None:
        walk = self._store.get_workflow_run(workflow_run_id)
        if walk is not None:
            self._engine.publish("workflow_run", models.workflow_run_payload(self._store, walk))

    def _walk_owner(self, workflow: dict) -> dict | None:
        return self._store.get_agent(workflow["owner_agent_id"]) or self._store.supervisor()

    async def start_workflow(
        self, workflow: dict, *, input: str, started_by: str
    ) -> tuple[str, str]:
        """Begin one walk in a page of its own: ``(conversation_id, workflow_run_id)``.

        The walk is the owner bot at work, but the chat the user already talks to that bot in
        stays a chat: the steps happen on a page of their own — its own thread, its own flow
        beside it — and the owner's chat keeps one row that leads there. A workflow that is
        already live is not started twice; a paused one of the same plan continues.
        """
        owner = self._walk_owner(workflow)
        if owner is None:  # pragma: no cover - bootstrap always creates the supervisor
            raise WorkflowBusy("找不到这个工作流的负责人，暂时无法开始。")
        for live in self._store.live_workflow_runs():
            if live["workflow_id"] != workflow["id"]:
                continue
            if live["status"] == workflow_rules.STATUS_PAUSED:
                return await self.resume_workflow(live)
            raise WorkflowBusy("这个工作流已经在跑了。")
        conversation = self._store.create_conversation(
            kind="workflow",
            title=f"工作流 · {workflow['name']}",
            purpose=workflow["description"] or "按步骤执行的工作流",
            member_ids=[owner["id"]],
        )
        walk = self._store.begin_workflow_run(
            workflow, conversation_id=conversation["id"], input=input, started_by=started_by
        )
        self._store.bump_workflow_run_count(workflow["id"])
        # The page is new, so every window needs to know it before the first step speaks.
        self._engine.publish(
            "workspace",
            models.workspace_payload(
                self._store,
                confirm_timeout_seconds=self._engine._settings.confirm_timeout_seconds,
            ),
        )
        self._engine.publish("sidebar", models.sidebar_payload(self._store.sidebar_layout()))
        self._publish_workflow_run(walk["id"])
        await self.pump(conversation["id"])
        return conversation["id"], walk["id"]

    async def resume_workflow(self, workflow_run: dict) -> tuple[str, str]:
        """Continue a paused walk: it queues for its page and starts once the page is free."""
        self._store.update_workflow_run(
            workflow_run["id"], status=workflow_rules.STATUS_QUEUED, run_id=None
        )
        self._publish_workflow_run(workflow_run["id"])
        await self.pump(workflow_run["conversation_id"])
        return workflow_run["conversation_id"], workflow_run["id"]

    async def answer_workflow(self, workflow_run: dict, text: str) -> None:
        """The user's answer to a question the walk is parked on, as a message in its page."""
        conversation = self._store.get_conversation(workflow_run["conversation_id"])
        walk_workflow = self._store.get_workflow(workflow_run["workflow_id"])
        owner = self._walk_owner(walk_workflow) if walk_workflow is not None else None
        if conversation is None or owner is None:
            raise WorkflowBusy("找不到这次运行所在的页面。")
        run = await self.launch(
            conversation, content=text, mentions=[owner["id"]], interrupt=False
        )
        if run is None:
            raise WorkflowBusy("它正在回复别的消息，等它说完再回答。")

    async def resume_queued(self) -> None:
        """Start every send left queued by a previous process; called once at startup.

        Read from the runs themselves, not the visible chat list: a queued send in a task's
        hidden thread waits for its slot just like any other.
        """
        for conversation_id in self._store.queued_conversation_ids():
            await self.pump(conversation_id)

    async def pump(self, conversation_id: str) -> None:
        """Start whatever waits for this conversation's run slot, if it is free right now.

        A send the user queued goes first: it was said before anything else waiting. Then a
        walk asked for while the page was busy begins after it instead of interrupting it.
        """
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            if await self._heal(conversation_id) is not None:
                return
            conversation = self._store.get_conversation(conversation_id)
            if conversation is None:
                return
            queued = self._store.queued_run(conversation_id)
            if queued is not None:
                await self._start_queued(conversation, queued)
                return
            walk = self._store.queued_workflow_run(conversation_id)
            if walk is None:
                return
            workflow = self._store.get_workflow(walk["workflow_id"])
            owner = self._walk_owner(workflow) if workflow is not None else None
            if owner is None:  # pragma: no cover - a walk dies with its workflow
                return
            run = self._store.begin_bare_run(conversation_id, f"工作流「{workflow['name']}」")
            self._store.update_workflow_run(
                walk["id"], status=workflow_rules.STATUS_RUNNING, run_id=run["id"]
            )
            self._store.update_run(run["id"], current_agent_id=owner["id"])
            try:
                self._engine.start(run["id"], conversation_id, workflow_run_id=walk["id"])
            except EngineBusy as exc:  # pragma: no cover - guarded by the lock above
                self._store.update_run(
                    run["id"],
                    status="stopped",
                    error=str(exc),
                    current_step=STEP_STOPPED,
                    finished_at=now_iso(),
                )
                self._store.update_workflow_run(
                    walk["id"], status=workflow_rules.STATUS_QUEUED, run_id=None
                )
                return
            self._publish_workflow_run(walk["id"])

    async def _start_queued(self, conversation: dict, queued: dict) -> None:
        """Promote one queued send into a normal run answering the message it already wrote."""
        promoted = self._store.promote_queued_run(queued["id"])
        if promoted is None:  # pragma: no cover - looked up under this conversation's lock
            return
        run, message = promoted
        self._store.update_run(
            run["id"], current_agent_id=self._engine.first_responder(conversation, message)
        )
        try:
            self._engine.start(run["id"], conversation["id"], message_id=message["id"])
        except EngineBusy:  # pragma: no cover - the slot was free under the lock
            self._store.update_run(run["id"], status="queued", current_step=STEP_QUEUED)
            return
        self._engine.publish("message_end", models.message_payload(message))

    async def fire_scheduled(self, schedule: dict, text: str = "") -> dict | None:
        """Start one scheduled run in its hidden workspace; ``None`` while that is busy.

        The target conversation is never used for the work: the owner posts one summary
        there when the run ends, and the user can keep talking in it meanwhile. ``text`` is
        a webhook caller's extra input, added under the schedule's own prompt. A reminder is
        different: it restarts its bot in the chat the reminder was made in, from a quiet
        trigger row, and the bot continues the conversation there.
        """
        if schedule["kind"] == "reminder":
            return await self._fire_reminder(schedule)
        target = self._store.get_conversation(schedule["conversation_id"])
        if target is None:
            return None
        owner = self._store.get_agent(schedule["owner_agent_id"] or "") or self._store.supervisor()
        if owner is None:  # pragma: no cover - bootstrap always creates the supervisor
            return None
        workspace = self._store.ensure_background_conversation(
            schedule, owner_id=owner["id"], purpose=target["title"]
        )
        if self._store.active_run(workspace["id"]) is not None:
            return None
        # Every fire starts from a blank page: the previous one's history is not context.
        self._store.reset_conversation(workspace["id"])
        prompt = schedule["prompt"]
        extra = text.strip()
        if extra:
            prompt = f"{prompt}\n\n这次运行的额外输入：\n{extra}"
        return await self.launch(
            workspace,
            content=prompt,
            mentions=[owner["id"]],
            schedule={"id": schedule["id"], "title": schedule["title"]},
            interrupt=False,
            use_computer=bool(schedule["use_computer"]),
        )

    async def _fire_reminder(self, schedule: dict) -> dict | None:
        """Wake the reminder's bot in the chat the reminder was made in.

        The trigger is stored as a user message flagged ``reminder``: the model reads it like
        any other instruction, the client draws it as a quiet system row. Nothing posts a
        second summary — the run's own reply is the answer. ``None`` while that chat is busy,
        so the scheduler queues the reminder and fires it after the running reply ends.
        """
        target = self._store.get_conversation(schedule["conversation_id"])
        if target is None or target["kind"] == "background":
            return None
        owner = self._store.get_agent(schedule["owner_agent_id"] or "") or self._store.supervisor()
        if owner is None:  # pragma: no cover - bootstrap always creates the supervisor
            return None
        lock = self._locks.setdefault(target["id"], asyncio.Lock())
        async with lock:
            if await self._heal(target["id"]) is not None:
                return None
            note = schedule["prompt"].strip()
            run, message = self._store.begin_run(
                target["id"],
                f"提醒：{note}",
                [owner["id"]],
                schedule={"id": schedule["id"], "title": schedule["title"]},
                reminder=True,
            )
            self._store.update_run(run["id"], current_agent_id=owner["id"])
            # The trigger is visible in every window, unlike a hidden workspace's rows.
            payload = models.message_payload(message)
            self._engine.publish("message_start", payload)
            self._engine.publish("message_end", payload)
            try:
                self._engine.start(
                    run["id"],
                    target["id"],
                    message_id=message["id"],
                    schedule_id=schedule["id"],
                )
            except EngineBusy as exc:  # pragma: no cover - guarded by the lock above
                self._store.update_run(
                    run["id"],
                    status="stopped",
                    error=str(exc),
                    current_step=STEP_STOPPED,
                    finished_at=now_iso(),
                )
                return None
        return self._store.get_run(run["id"]) or run
