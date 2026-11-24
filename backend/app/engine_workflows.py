"""Walking a workflow graph: every branch that may run does, a few at a time.

Split out of :class:`~app.engine.SupervisorEngine` as a mixin because it is a different loop,
not a step of the chat loop. The engine calls :meth:`WorkflowWalking._workflow_loop` for a
workflow conversation; everything it needs to report is published as a ``workflow_run`` event,
and everything it needs to resume is read back from the store.

The forward edges are a DAG and one walk drives it with a handful of workers: the loop starts
every node whose parents have all settled and whose edge into it was live, in plan order, up
to ``workflow_rules.MAX_PARALLEL_STEPS`` of them. Each started node runs in its own task and
is one bounded bot turn in the page, so two arms of one fan-out really overlap in time. A join
waits until every parent has settled and runs if at least one edge into it was live; a branch
decides which of its outgoing edges are live, and everything left unreachable is skipped —
never silently ignored.

What is shared and what is not, because it decides what concurrency may touch:

- Budgets are the walk's, not a branch's. Model and tool calls spend the run's own counters in
  the run row, so the cap holds across whichever branches happen to be running.
- A bot turn's own budget (model calls, tool calls, the wrap-up call) lives in a context
  variable in the branch task, so two branches working as the same bot cannot read or restore
  each other's turn.
- The page's messages stay ordered the way they were written: each branch opens its own message
  at its first token and streams into it, and the node row records its ``message_id``, which is
  what labels that reply as this step's. Branches interleave on the page exactly as two people
  typing at once would.
- The cursor in the run row is the last node started, for the panel's benefit only; nothing
  the walk decides reads it back. A node control (skip/retry) names its node, because with
  several nodes live at once "the cursor's node" would be a guess.

A question (``ask_user``) or a confirmation card parks only its own node: the other branches
keep running and the walk parks on the question once nothing else can move, so one branch
asking does not hold up the work beside it. A pause pressed mid-flight starts nothing new and
lets the running branches finish; a stop cancels every branch; a branch that fails marks its
own downstream unreachable as before, and the walk ends failed once the branches already
running have settled.

A loop edge, once its source settles and takes it, rewinds the nodes between its target and
its source to pending and counts one round; the frontier then finds the target ready again.
Loops are taken only when no branch is running, since a round rewrites whole arms. Loop edges
are not dependencies (a node never waits for one), and one that has used up its rounds is
dead, so the walk leaves through the source's other edges.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import models
from . import workflows as workflow_rules
from .agent.run_state import ActiveRun
from .errors import RunLimited, RunStopped, UpstreamError, WorkflowPaused, WorkflowWaiting
from .store import new_id, now_iso

# How long a scheduler pass waits for a branch before looking at the user's controls again.
CONTROL_POLL_SECONDS = 0.2


class _Park:
    """A branch saying "I am done, but the walk should park at the next boundary"."""


PARK = _Park()


@dataclass
class _NodeFailure:
    """How one node failed: the line the walk's summary carries, and whether the run ends
    with an error (an upstream fault, as before) or merely completes with a failed walk."""

    error: Exception
    summary: str
    raise_it: bool


class WorkflowWalking:
    """The node driver: the frontier, the gates, questions, and the user's controls."""

    async def _workflow_loop(self, active: ActiveRun, conversation: dict) -> None:
        """Drive the graph: start every node that may start, then wait for one to settle.

        Each pass reads the run's rows, starts what the frontier allows — up to
        ``workflow_rules.MAX_PARALLEL_STEPS`` branches at once, in plan order — and then waits
        for the next branch to settle, so a stop, a pause or a restart always leaves a graph
        the next pass can pick up. Four things end the walk without failing it: an ``ask_user``
        node parks it once nothing else can move, a pause request parks it once the branches
        already running have settled, a confirmation gate waits inside its own branch, and a
        restart finds the rows exactly as the last pass wrote them.
        """
        store = self._store
        workflow_run = store.get_workflow_run(active.workflow_run_id or "")
        if workflow_run is None:
            raise UpstreamError("这次运行的节点记录不见了，工作流无法继续。")
        workflow_run_id = workflow_run["id"]
        workflow = store.get_workflow(workflow_run["workflow_id"])
        name = workflow["name"] if workflow is not None else conversation["title"]
        description = workflow["description"] if workflow is not None else ""
        owner = self._workflow_owner(conversation, workflow)
        # The graph never changes mid-run: the run copied it when it began.
        nodes = workflow_run["steps"]
        edges = workflow_run["edges"]
        store.update_workflow_run(
            workflow_run_id, status=workflow_rules.STATUS_RUNNING, run_id=active.run_id
        )
        self._publish_workflow_run(workflow_run_id)
        self._workflow_started_marker(active, workflow_run_id, name)

        branches: dict[str, asyncio.Task] = {}
        # Questions this run posted. Their answers arrive in a later run; the message that
        # started this one must never be read as the answer to a question asked mid-flight.
        asked: set[str] = set()
        failure: _NodeFailure | None = None
        parked = False
        try:
            while True:
                if active.stop_requested:
                    raise RunStopped("任务已停止。")
                if await self._apply_workflow_control(active, workflow_run_id, branches):
                    parked = True
                states = self._workflow_states(workflow_run_id)
                loops = self._workflow_loops(workflow_run_id)
                # A settled node that took a loop edge sends the walk round again before
                # anything else is decided: the nodes it rewinds must not be skipped as
                # unreachable first. Never while a branch runs: a round rewrites whole arms.
                if (
                    not branches
                    and self._take_loop(workflow_run_id, nodes, edges, states, loops)
                ):
                    continue
                # A branch that went the other way makes whole arms unreachable: say so now, so
                # the frontier never waits for a node nothing can ever feed.
                if self._settle_unreachable(workflow_run_id, nodes, edges, states, loops):
                    states = self._workflow_states(workflow_run_id)
                if self._answer_waiting(active, conversation, workflow_run_id, states, asked):
                    continue
                if (
                    failure is None
                    and not branches
                    and all(
                        state["status"] in workflow_rules.SETTLED
                        for state in states.values()
                    )
                ):
                    return
                if failure is None and not parked:
                    self._start_ready(
                        active,
                        conversation,
                        owner,
                        workflow_run_id=workflow_run_id,
                        name=name,
                        description=description,
                        nodes=nodes,
                        edges=edges,
                        states=states,
                        loops=loops,
                        branches=branches,
                        asked=asked,
                    )
                    # A question posted a moment ago is in the rows, not in the snapshot the
                    # pass began with: the park decision below has to see it.
                    states = self._workflow_states(workflow_run_id)
                if branches:
                    for outcome in await self._await_branches(branches):
                        if outcome is PARK:
                            parked = True
                        elif outcome is not None and failure is None:
                            failure = outcome
                    continue
                # Nothing runs any more: decide why the walk stopped where it did.
                if failure is not None:
                    # Every branch has settled; the walk ends failed on the first failure, and
                    # nothing that never ran is left looking like it is still queued.
                    self._settle_leftovers(workflow_run_id)
                    store.update_workflow_run(
                        workflow_run_id,
                        status=workflow_rules.STATUS_FAILED,
                        summary=failure.summary,
                        finished_at=now_iso(),
                    )
                    self._publish_workflow_run(workflow_run_id)
                    if failure.raise_it:
                        raise failure.error
                    return
                if parked:
                    store.update_workflow_run(
                        workflow_run_id, status=workflow_rules.STATUS_PAUSED
                    )
                    self._publish_workflow_run(workflow_run_id)
                    raise WorkflowPaused()
                if any(
                    state["status"] == workflow_rules.STEP_WAITING
                    for state in states.values()
                ):
                    store.update_workflow_run(
                        workflow_run_id, status=workflow_rules.STATUS_WAITING
                    )
                    self._publish_workflow_run(workflow_run_id)
                    raise WorkflowWaiting()
                # Nothing running, nothing ready and nothing parked: only a graph the validator
                # let through could do this, so fail loudly instead of spinning.
                store.update_workflow_run(
                    workflow_run_id,
                    status=workflow_rules.STATUS_FAILED,
                    summary="这条工作流的连线让流程停住了，没有节点可以继续。",
                    finished_at=now_iso(),
                )
                self._publish_workflow_run(workflow_run_id)
                return
        finally:
            await self._stop_branches(workflow_run_id, branches)

    def _start_ready(
        self,
        active: ActiveRun,
        conversation: dict,
        owner: dict,
        *,
        workflow_run_id: str,
        name: str,
        description: str,
        nodes: Sequence[dict],
        edges: Sequence[dict],
        states: Mapping[str, dict],
        loops: Mapping[str, int],
        branches: dict[str, asyncio.Task],
        asked: set[str],
    ) -> None:
        """Start whatever may start now, in plan order, up to the walk's worker count.

        A question takes no worker: it is posted and left for the user while the branches
        beside it keep going. Only one question is open at a time, because one page has one
        input line to answer it with; a second ``ask_user`` waits for the first answer.
        """
        free = workflow_rules.MAX_PARALLEL_STEPS - len(branches)
        if free <= 0:
            return
        open_question = any(
            state["status"] == workflow_rules.STEP_WAITING for state in states.values()
        )
        for node in self._ready_nodes(nodes, edges, states, loops):
            if node["kind"] == workflow_rules.ASK_USER:
                if open_question:
                    continue
                self._ask_workflow_question(
                    active, conversation, owner, node, workflow_run_id, nodes
                )
                asked.add(node["id"])
                open_question = True
                continue
            if free <= 0:
                return
            state = states[node["id"]]
            # The panel reads the cursor to say where the walk is; nothing decides from it.
            self._store.update_workflow_run(
                workflow_run_id, cursor=int(state["step_index"])
            )
            branches[node["id"]] = asyncio.create_task(
                self._workflow_node(
                    active,
                    conversation,
                    owner,
                    workflow_run_id=workflow_run_id,
                    name=name,
                    description=description,
                    nodes=nodes,
                    edges=edges,
                    loops=loops,
                    node=node,
                    state=state,
                )
            )
            free -= 1

    def _answer_waiting(
        self,
        active: ActiveRun,
        conversation: dict,
        workflow_run_id: str,
        states: Mapping[str, dict],
        asked: set[str],
    ) -> bool:
        """Take the message that started this run as the answer to the open question.

        Only a question an earlier run posted is answered here: the run that asks a question
        while its other branches work still has the message that started it, and that message
        is not the answer to a question it asked itself. ``True`` when a node was settled.
        """
        if active.trigger_message_id is None:
            return False
        for node_id, state in states.items():
            if state["status"] != workflow_rules.STEP_WAITING or node_id in asked:
                continue
            self._finish_workflow_node(
                workflow_run_id,
                node_id,
                status=workflow_rules.STEP_DONE,
                output=self._workflow_answer(active, conversation),
                message_id=state.get("message_id"),
            )
            return True
        return False

    async def _await_branches(self, branches: dict[str, asyncio.Task]) -> list[Any]:
        """Wait for the next branch to settle, or for the user to press something.

        The short timeout is what lets pause and stop land while a long model call is still
        streaming: every pass looks at the controls again, and a stopped branch stops itself
        on its next chunk. A control fault raised by a branch (stop, spent run budget) is
        re-raised here, so the walk ends the way the run loop expects it to.
        """
        done, _pending = await asyncio.wait(
            set(branches.values()),
            timeout=CONTROL_POLL_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        outcomes: list[Any] = []
        for node_id, task in list(branches.items()):
            if task not in done:
                continue
            del branches[node_id]
            outcomes.append(task.result())
        return outcomes

    async def _stop_branches(
        self, workflow_run_id: str, branches: dict[str, asyncio.Task]
    ) -> None:
        """No branch outlives its walk, and none is left marked as running.

        A branch that settled its own row keeps it; one that was cancelled or raised mid-turn
        goes back to pending, so a paused or failed walk never shows a node that is not there.
        """
        for node_id, task in branches.items():
            if not task.done():
                task.cancel()
            state = self._store.step_state(workflow_run_id, node_id) or {}
            if state.get("status") == workflow_rules.STEP_RUNNING:
                self._store.update_workflow_step(
                    workflow_run_id,
                    node_id,
                    status=workflow_rules.STEP_PENDING,
                    started_at=None,
                )
        if branches:
            await asyncio.gather(*branches.values(), return_exceptions=True)
            self._publish_workflow_run(workflow_run_id)

    async def _workflow_node(
        self,
        active: ActiveRun,
        conversation: dict,
        owner: dict,
        *,
        workflow_run_id: str,
        name: str,
        description: str,
        nodes: Sequence[dict],
        edges: Sequence[dict],
        loops: Mapping[str, int],
        node: dict,
        state: Mapping[str, Any],
    ) -> "_NodeFailure | _Park | None":
        """One node as one bounded bot turn, in a task of its own so it overlaps its siblings.

        It returns ``None`` when the node settled itself, a :class:`_NodeFailure` when the walk
        must end failed once the branches already running have settled, and :data:`PARK` when
        the walk should park at the next boundary. Stop and a spent run budget are raised: they
        end the whole run, not just this branch.
        """
        store = self._store
        index = int(state["step_index"])
        attempts = int(state["attempts"])
        # The snapshot the loop handed over; the gate flips it, so a retry never re-asks.
        approved = bool(int(state["approved"] or 0))
        while True:
            # A gate is asked for once per round and settles nothing by itself: a pause pressed
            # while the card was open must leave the node exactly where it is.
            if node["gate"] == workflow_rules.GATE_CONFIRM and not approved:
                decision = await self._workflow_card(active, conversation, owner, node, nodes)
                current = store.step_state(workflow_run_id, node["id"]) or {}
                if current.get("status") in workflow_rules.SETTLED:
                    return None
                if decision == "stop":
                    raise RunStopped("任务已停止。")
                if decision != "approve":
                    self._finish_workflow_node(
                        workflow_run_id,
                        node["id"],
                        status=workflow_rules.STEP_SKIPPED,
                        output="你跳过了这一步。",
                    )
                    # Nobody answered the card: park instead of deciding for them.
                    return PARK if decision == "timeout" else None
                # Recorded on the node: the approval must not be asked for a second time.
                store.update_workflow_step(workflow_run_id, node["id"], approved=1)
                approved = True
                if active.stop_requested or self._control_queued(active, "pause"):
                    return PARK

            store.update_workflow_step(
                workflow_run_id,
                node["id"],
                status=workflow_rules.STEP_RUNNING,
                attempts=attempts + 1,
                started_at=now_iso(),
            )
            self._publish_workflow_run(workflow_run_id)
            self._set_step(
                active, f"第 {index + 1}/{len(nodes)} 个节点：{node['title']}"
            )
            prompt = workflow_rules.node_prompt(
                name=name,
                nodes=nodes,
                node=node,
                round_=int(state.get("rounds") or 0),
                choices=(
                    workflow_rules.available_choices(node, edges, loops)
                    if node["kind"] == workflow_rules.BRANCH
                    else None
                ),
            )
            block = workflow_rules.plan_block(
                name=name,
                description=description,
                nodes=nodes,
                edges=edges,
                states=self._workflow_states(workflow_run_id),
                current=node["id"],
                loops=loops,
            )
            # This branch's own turn limit, kept apart from its siblings': both run as the
            # same bot, so ``turn_limited[owner id]`` cannot say which branch spent what.
            limited: dict[str, str] = {}
            try:
                text, message = await self._workflow_step_turn(
                    active, conversation, owner, prompt, block, limited=limited
                )
            except (RunStopped, RunLimited):
                raise
            except UpstreamError as exc:
                if node["on_fail"] == workflow_rules.FAIL_RETRY and attempts < 1:
                    attempts += 1
                    continue
                self._finish_workflow_node(
                    workflow_run_id, node["id"], status=workflow_rules.STEP_FAILED, output=str(exc)
                )
                return _NodeFailure(exc, str(exc), raise_it=True)

            if not text.strip():
                # An empty answer is a node that produced nothing: it follows ``on_fail``. A
                # turn that ran out of its own budget is said so, not blamed on the step.
                spent = limited.get("reason")
                if node["on_fail"] == workflow_rules.FAIL_RETRY and attempts < 1:
                    attempts += 1
                    continue
                if node["on_fail"] == workflow_rules.FAIL_SKIP:
                    self._finish_workflow_node(
                        workflow_run_id,
                        node["id"],
                        status=workflow_rules.STEP_SKIPPED,
                        output="这一步没有产出内容，按设置跳过。",
                    )
                    return None
                self._finish_workflow_node(
                    workflow_run_id,
                    node["id"],
                    status=workflow_rules.STEP_FAILED,
                    output=(
                        f"这一步的调用预算用完了，没有产出内容（{spent}）。"
                        if spent
                        else "这一步没有产出内容。"
                    ),
                )
                summary = (
                    f"「{node['title']}」这一轮的调用预算用完了，没有产出内容。"
                    if spent
                    else f"「{node['title']}」没有产出内容。"
                )
                return _NodeFailure(UpstreamError(summary), summary, raise_it=False)

            choice = None
            if node["kind"] == workflow_rules.BRANCH:
                # Read the answer against every choice, then keep it only if it is still open:
                # 「不合格」 must not be read as the open 「合格」 it contains once its loop is spent.
                offered, _closed = workflow_rules.available_choices(node, edges, loops)
                named = workflow_rules.parse_choice(text, node.get("choices") or [])
                choice = named if named in offered else None
                if choice is None:
                    # A branch that names no option takes no edge: the arms it fed are dead.
                    self._finish_workflow_node(
                        workflow_run_id,
                        node["id"],
                        status=workflow_rules.STEP_FAILED,
                        output=text,
                    )
                    summary = f"「{node['title']}」没有给出可用的选项。"
                    return _NodeFailure(UpstreamError(summary), summary, raise_it=False)
            self._finish_workflow_node(
                workflow_run_id,
                node["id"],
                status=workflow_rules.STEP_DONE,
                output=text,
                choice=choice,
                message_id=message["id"] if message is not None else None,
            )
            return None

    # ------------------------------------------------------------------ frontier

    def _workflow_states(self, workflow_run_id: str) -> dict[str, dict]:
        return {
            state["node_id"]: state
            for state in self._store.workflow_step_states(workflow_run_id)
        }

    def _workflow_loops(self, workflow_run_id: str) -> dict[str, int]:
        workflow_run = self._store.get_workflow_run(workflow_run_id)
        return dict(workflow_run["loops"]) if workflow_run is not None else {}

    def _take_loop(
        self,
        workflow_run_id: str,
        nodes: Sequence[dict],
        edges: Sequence[dict],
        states: Mapping[str, dict],
        loops: Mapping[str, int],
    ) -> bool:
        """Take the first loop edge a settled source chose and its target is not yet rewound.

        A loop is taken when its source is done, the edge is live from it (a branch picked its
        choice, or a plain node simply finished) and its target has settled — a target still
        pending means this round was already rewound. ``True`` when the walk went round.
        """
        for edge in edges:
            if not workflow_rules.is_loop(edge):
                continue
            source = self._node_by_id(nodes, edge["from"])
            if edge not in workflow_rules.reachable_edges(source, edges, states, loops):
                continue
            target_state = states.get(edge["to"]) or {}
            if target_state.get("status") not in workflow_rules.SETTLED:
                continue
            body = workflow_rules.loop_body(nodes, edges, edge)
            self._store.take_workflow_loop(workflow_run_id, workflow_rules.edge_key(edge), body)
            self._publish_workflow_run(workflow_run_id)
            return True
        return False

    def _ready_nodes(
        self,
        nodes: Sequence[dict],
        edges: Sequence[dict],
        states: Mapping[str, dict],
        loops: Mapping[str, int],
    ) -> list[dict]:
        """Every node whose parents have all settled and whose edge into it was live.

        Plan order decides the order they are started in, which is what makes two branches of
        one fan-out begin in the order the user drew them. Only forward edges are dependencies:
        a loop edge brings its target back by rewinding it, never by blocking it. A node parked
        on a question is not ready here: its answer is what settles it.
        """
        forward = workflow_rules.forward_edges(edges)
        parents = workflow_rules.parents_of(nodes, forward)
        ready: list[dict] = []
        for node in nodes:
            state = states.get(node["id"]) or {}
            if state.get("status") != workflow_rules.STEP_PENDING:
                continue
            incoming = parents.get(node["id"], [])
            if any(
                (states.get(edge["from"]) or {}).get("status") not in workflow_rules.SETTLED
                for edge in incoming
            ):
                continue
            if incoming and not any(
                edge in workflow_rules.reachable_edges(
                    self._node_by_id(nodes, edge["from"]), forward, states, loops
                )
                for edge in incoming
            ):
                continue
            ready.append(node)
        return ready

    @staticmethod
    def _control_queued(active: ActiveRun, command: str) -> bool:
        """Whether one of the user's controls is waiting for the next boundary."""
        return any(queued == command for queued, _node in active.workflow_controls)

    def _settle_leftovers(self, workflow_run_id: str) -> None:
        """A failed walk will not run anything else: its pending nodes say so.

        A node the frontier could still have reached on the other side of the failure is not
        unreachable, it is simply never run — and leaving it 「未开始」 on a finished walk
        would read as "about to start".
        """
        moved = False
        for state in self._store.workflow_step_states(workflow_run_id):
            if state["status"] != workflow_rules.STEP_PENDING:
                continue
            self._store.update_workflow_step(
                workflow_run_id,
                state["node_id"],
                status=workflow_rules.STEP_SKIPPED,
                output="这一步没有跑到：工作流在别处失败了。",
                finished_at=now_iso(),
            )
            moved = True
        if moved:
            self._publish_workflow_run(workflow_run_id)

    def _settle_unreachable(
        self,
        workflow_run_id: str,
        nodes: Sequence[dict],
        edges: Sequence[dict],
        states: Mapping[str, dict],
        loops: Mapping[str, int],
    ) -> bool:
        """Skip every pending node whose every incoming edge is dead; ``True`` when one moved.

        Runs to a fixed point: skipping a node kills its own outgoing edges, which may strand
        the node after it. ``reachable_edges`` says which edges a settled node really took, so
        this needs no special case for a failed or skipped parent. Forward edges only: a node
        a loop could bring back is still decided by the edges that lead into it this round.
        """
        forward = workflow_rules.forward_edges(edges)
        parents = workflow_rules.parents_of(nodes, forward)
        moved = False
        changed = True
        while changed:
            changed = False
            for node in nodes:
                state = states.get(node["id"]) or {}
                if state.get("status") != workflow_rules.STEP_PENDING:
                    continue
                incoming = parents.get(node["id"], [])
                if not incoming:
                    continue
                if any(
                    (states.get(edge["from"]) or {}).get("status") not in workflow_rules.SETTLED
                    for edge in incoming
                ):
                    continue
                live = [
                    edge
                    for edge in incoming
                    if edge
                    in workflow_rules.reachable_edges(
                        self._node_by_id(nodes, edge["from"]), forward, states, loops
                    )
                ]
                if live:
                    continue
                self._store.update_workflow_step(
                    workflow_run_id,
                    node["id"],
                    status=workflow_rules.STEP_SKIPPED,
                    output="这一支没有走到。",
                    finished_at=now_iso(),
                )
                states[node["id"]] = {
                    **state,
                    "status": workflow_rules.STEP_SKIPPED,
                }
                moved = True
                changed = True
        if moved:
            self._publish_workflow_run(workflow_run_id)
        return moved

    @staticmethod
    def _node_by_id(nodes: Sequence[dict], node_id: str) -> dict:
        for node in nodes:
            if node["id"] == node_id:
                return node
        return {"id": node_id, "kind": workflow_rules.DO, "title": node_id}

    def _workflow_owner(self, conversation: dict, workflow: dict | None) -> dict:
        """Whose plan this is: the workflow's owner, who is also the one the chat is with."""
        for agent_id in (
            (workflow or {}).get("owner_agent_id"),
            *conversation["member_ids"],
        ):
            agent = self._store.get_agent(agent_id) if agent_id else None
            if agent is not None:
                return agent
        return self._supervisor()

    async def _workflow_step_turn(
        self,
        active: ActiveRun,
        conversation: dict,
        agent: dict,
        prompt: str,
        block: str,
        *,
        limited: dict[str, str] | None = None,
    ) -> tuple[str, dict | None]:
        """One node as one bounded bot turn, with the graph in the system prompt.

        ``limited`` collects this turn's own budget limit: sibling branches run as the same
        bot, so the run-wide ``turn_limited`` map cannot say which of them ran out.
        """
        roster = self._roster(conversation)
        role = "supervisor" if agent["kind"] == "supervisor" else "member"
        if role == "supervisor":
            messages = self._supervisor_context(conversation, cue=prompt, workflow=block)
        else:
            messages = self._member_context(
                conversation, roster, agent, task=prompt, workflow=block
            )
        return await self._member_call(
            active,
            conversation,
            agent,
            messages,
            None,
            roster,
            role=role,
            limited_into=limited,
        )

    def _finish_workflow_node(
        self,
        workflow_run_id: str,
        node_id: str,
        *,
        status: str,
        output: str,
        choice: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Close one node; the cursor moves on the next pass, from the graph itself."""
        self._store.update_workflow_step(
            workflow_run_id,
            node_id,
            status=status,
            output=output,
            choice=choice,
            message_id=message_id,
            finished_at=now_iso(),
        )
        self._publish_workflow_run(workflow_run_id)

    def _workflow_answer(self, active: ActiveRun, conversation: dict) -> str:
        """The user's message that answers an ``ask_user`` node."""
        trigger = self._trigger_message(active, conversation)
        return str(trigger["content"] or "").strip() if trigger is not None else ""

    def _ask_workflow_question(
        self,
        active: ActiveRun,
        conversation: dict,
        owner: dict,
        node: dict,
        workflow_run_id: str,
        nodes: Sequence[dict],
    ) -> None:
        """Post the question as the owner's own message and park the walk on the answer."""
        message = self._store.append_message(
            conversation_id=conversation["id"],
            author_id=owner["id"],
            author_name=owner["name"],
            avatar_seed=owner["avatar_seed"],
            kind="agent",
            content=workflow_rules.question_text(node),
            status="complete",
            run_id=active.run_id,
        )
        self._store.update_workflow_step(
            workflow_run_id,
            node["id"],
            status=workflow_rules.STEP_WAITING,
            message_id=message["id"],
            started_at=now_iso(),
        )
        self._publish_message("message_start", message)
        self._publish_message("message_end", message)
        index = next(
            (position for position, item in enumerate(nodes) if item["id"] == node["id"]), 0
        )
        self._set_step(active, f"第 {index + 1}/{len(nodes)} 个节点：等你回答")

    async def _workflow_card(
        self,
        active: ActiveRun,
        conversation: dict,
        owner: dict,
        node: dict,
        nodes: Sequence[dict],
    ) -> str:
        """Ask before a ``confirm`` node runs; the answer is ``approve``/``deny``/``stop``/…"""
        index = next(
            (position for position, item in enumerate(nodes) if item["id"] == node["id"]), 0
        )
        action, message = self._store.create_action(
            run_id=active.run_id,
            conversation_id=conversation["id"],
            tool_call_id=new_id("b"),
            tool_name="workflow_step",
            arguments="{}",
            summary=f"准备开始第 {index + 1} 个节点：{node['title']}",
            author_id=owner["id"],
            author_name=owner["name"],
            avatar_seed=owner["avatar_seed"],
            status="awaiting",
            confirmation={
                "prompt": f"工作流要开始「{node['title']}」，现在开始吗？",
                "confirm_label": "开始",
                "deny_label": "跳过",
            },
        )
        self._publish_message("message_start", message)
        self._set_step(active, f"等你确认「{node['title']}」")
        self.publish_decisions()
        decision = await self._await_decision(active, action["id"])
        self._finish_action(
            active,
            action,
            status="completed" if decision == "approve" else "error",
            summary=(
                f"开始「{node['title']}」"
                if decision == "approve"
                else f"跳过了「{node['title']}」"
            ),
            result="",
        )
        return decision

    # ------------------------------------------------------------------ the way in

    def _workflow_owner_agent(self, walk: dict) -> dict:
        workflow = self._store.get_workflow(walk["workflow_id"]) or {}
        owner = self._store.get_agent(workflow.get("owner_agent_id") or "")
        return owner or self._supervisor()

    def _workflow_entry(self, active: ActiveRun, walk: dict, name: str) -> dict | None:
        """The row in the owner's own chat that leads into this walk's page (once per walk)."""
        existing = self._store.action_for_target("workflow_run", walk["conversation_id"])
        if existing is not None:
            return existing
        owner = self._workflow_owner_agent(walk)
        chat = self._store.ensure_direct_conversation(owner["id"])
        if chat is None:  # pragma: no cover - bootstrap always makes the supervisor's chat
            return None
        total = len(walk["steps"])
        summary = f"开始工作流「{name}」（共 {total} 步）"
        action, message = self._store.create_action(
            run_id=active.run_id,
            conversation_id=chat["id"],
            tool_call_id=new_id("e"),
            tool_name="workflow_run",
            arguments="{}",
            summary=summary,
            author_id=owner["id"],
            author_name=owner["name"],
            avatar_seed=owner["avatar_seed"],
            status="completed",
        )
        pointed = self._store.set_action_target(
            action["id"], target_agent_id=None, target_conversation_id=walk["conversation_id"]
        )
        self._publish_message("message_start", message)
        if pointed is not None:
            self._publish_message("message_end", pointed[1])
        return action

    def _workflow_marker(self, active: ActiveRun, workflow_run_id: str) -> None:
        """Rewrite the way-in row once the walk ends or is put down: how it left off."""
        walk = self._store.get_workflow_run(workflow_run_id)
        if walk is None or walk["status"] not in (
            workflow_rules.STATUS_DONE,
            workflow_rules.STATUS_FAILED,
            workflow_rules.STATUS_STOPPED,
            workflow_rules.STATUS_PAUSED,
        ):
            return
        name = (self._store.get_workflow(walk["workflow_id"]) or {}).get("name", "工作流")
        states = self._store.workflow_step_states(walk["id"])
        done = sum(1 for state in states if state["status"] in workflow_rules.SETTLED)
        total = len(walk["steps"])
        summary = {
            workflow_rules.STATUS_DONE: f"工作流「{name}」跑完了（{done}/{total}）",
            workflow_rules.STATUS_FAILED: f"工作流「{name}」失败了：{walk['summary'] or '没有说明'}",
            workflow_rules.STATUS_STOPPED: f"工作流「{name}」已停止（{done}/{total}）",
            workflow_rules.STATUS_PAUSED: f"工作流「{name}」暂停在第 {min(done + 1, total)}/{total} 步",
        }[walk["status"]]
        entry = self._store.action_for_target("workflow_run", walk["conversation_id"])
        if entry is None:  # pragma: no cover - the row is written when the walk starts
            return
        updated = self._store.update_action_summary(entry["id"], summary=summary)
        if updated is not None:
            self._publish_message("message_end", updated[1])

    def _workflow_started_marker(self, active: ActiveRun, workflow_run_id: str, name: str) -> None:
        walk = self._store.get_workflow_run(workflow_run_id)
        if walk is None:
            return
        self._workflow_entry(active, walk, name)

    def _publish_workflow_run(self, workflow_run_id: str) -> None:
        """Push one run's payload to every window watching the panel or its chat."""
        workflow_run = self._store.get_workflow_run(workflow_run_id)
        if workflow_run is None:
            return
        self._publish("workflow_run", models.workflow_run_payload(self._store, workflow_run))
        self.publish_decisions()

    def workflow_control(self, run_id: str, command: str, node_id: str | None = None) -> bool:
        """Queue one control for a live workflow run; ``False`` when it is not running.

        Controls land at the next boundary, never inside a node's own work: a pause that
        interrupted a tool call halfway would leave a node with no result at all. ``node_id``
        names the node a skip or a retry is for — with several nodes live at once, the cursor
        can no longer say which one the user meant.
        """
        active = next((run for run in self._runs.values() if run.run_id == run_id), None)
        if active is None or active.task is None or active.task.done():
            return False
        active.workflow_controls.append((command, node_id))
        return True

    async def _apply_workflow_control(
        self, active: ActiveRun, workflow_run_id: str, branches: Mapping[str, Any]
    ) -> bool:
        """Act on whatever the user pressed since the last boundary.

        Returns ``True`` when the walk must not start anything new: a pause queued while
        branches are still running waits for them, because the button promises to stop at a
        node boundary, not in the middle of a step. Skip and retry cancel the branch they name
        first, so the row the user set is the row the node keeps.
        """
        while active.workflow_controls:
            command, node_id = active.workflow_controls[0]
            if command == "pause":
                if branches:
                    return True
                active.workflow_controls.pop(0)
                self._store.update_workflow_run(
                    workflow_run_id, status=workflow_rules.STATUS_PAUSED
                )
                self._publish_workflow_run(workflow_run_id)
                raise WorkflowPaused()
            active.workflow_controls.pop(0)
            target = node_id or self._cursored_node(workflow_run_id)
            if target is None:
                continue
            state = self._store.step_state(workflow_run_id, target) or {}
            if state.get("status") in workflow_rules.SETTLED:
                continue
            task = branches.get(target)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._publish_workflow_run(workflow_run_id)
            if command == "skip":
                self._finish_workflow_node(
                    workflow_run_id,
                    target,
                    status=workflow_rules.STEP_SKIPPED,
                    output="你跳过了这一步。",
                )
            elif command == "retry":
                # Put the node back to pending so the next pass runs it again.
                self._store.update_workflow_step(
                    workflow_run_id, target, status=workflow_rules.STEP_PENDING
                )
                self._publish_workflow_run(workflow_run_id)
        return False

    def _cursored_node(self, workflow_run_id: str) -> str | None:
        """The node a control with no name of its own falls back to: the cursor's."""
        workflow_run = self._store.get_workflow_run(workflow_run_id)
        if workflow_run is None:  # pragma: no cover - deleted mid-run
            return None
        nodes = workflow_run["steps"]
        cursor = int(workflow_run["cursor"])
        node = nodes[cursor] if cursor < len(nodes) else None
        return node["id"] if node is not None else None
