"""Hidden task threads: the queue, the global cap and the one-thread-per-member rule.

A member's ``start_task`` ends its group turn and asks this scheduler to run the work as a
normal run in the task's own thread. Thread runs are real runs (own stop, budget, consent,
steps); this module only decides *when* one may execute and starts it through the engine.

The scheduler is the only writer of ``group_tasks.status`` transitions queued <-> running,
so a group run and a thread run can finish in either order without losing a waiting task.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import models, prompts
from .agent.run_state import LANES_PER_GROUP
from .errors import EngineBusy, TaskBusy
from .store import group_tasks as task_rules
from .store import now_iso

logger = logging.getLogger(__name__)

# A task's progress event reaches its card at most this often, in seconds (<= 4/s).
PROGRESS_SECONDS = 0.25


class TaskScheduler:
    """Queue task threads, start what fits, and hand every finished part back."""

    def __init__(self, store: Any, engine: Any) -> None:
        self._store = store
        self._engine = engine
        self._lock = asyncio.Lock()
        # The opening a queued run needs, when it is more than its task row can say. It is
        # transient on purpose: a task queued across a restart is settled by the startup heal.
        self._instructions: dict[str, str] = {}
        # Futures a ``group_assign`` caller is waiting on, by child task id.
        self._parts: dict[str, asyncio.Future] = {}
        # Parent tasks parked on a part: they hold no execution slot (they are waiting, not
        # working), so the cap still describes how many runs are really busy.
        self._waiting_on_part: set[str] = set()
        # Tasks a reply lane opened: they wait for the group's work lane to end first.
        self._behind_lane: set[str] = set()
        self._wake_task: asyncio.Task | None = None
        # When each task's live payload last reached its card (monotonic seconds).
        self._progress_at: dict[str, float] = {}

    # ------------------------------------------------------------------ enqueue

    async def enqueue(
        self,
        task_id: str,
        *,
        instruction: str | None = None,
        another_round: bool = False,
        behind_lane: bool = False,
    ) -> dict | None:
        """Put one task in the queue, then start whatever the caps allow right now.

        ``behind_lane`` marks a task a reply lane opened: it waits until the group's live
        work lane ends, so answering a question never starts the same bot twice at once.
        """
        async with self._lock:
            updated = self._store.update_group_task(
                task_id,
                status=task_rules.QUEUED,
                run_id=None,
                current_step="排队中",
                started_at=None,
                latest_text="",
                end_reason=None,
                stopped_by=None,
                seen_at=None,
                another_round=another_round,
            )
            if updated is None:
                return None
            if instruction:
                self._instructions[task_id] = instruction
            if behind_lane:
                self._behind_lane.add(task_id)
                # The card says what it waits for: the chat's live lane.
                updated = self._store.update_group_task(
                    task_id, current_step=self._behind_lane_step(updated)
                ) or updated
            self._touch_card(updated, status="streaming", content=updated["title"])
            self._publish_group_tasks(updated["conversation_id"])
            self._publish_workspace()
            await self._pump_locked()
            return self._store.get_group_task(task_id)

    async def pump(self) -> None:
        async with self._lock:
            await self._pump_locked()

    def _behind_lane_step(self, task: dict) -> str:
        """The card's wait line: this bot's own reply, or the other bot whose work holds it.

        A task opened by a reply lane waits for the chat's work lane. When that lane is the
        same bot still answering, naming it would read as waiting for itself, so it says
        「这一轮回复」 instead; a group's other bot is named as before.
        """
        owner_id = str(task.get("owner_id") or "")
        for run in self._store.active_runs_in(str(task["conversation_id"])):
            if str(run.get("lane") or "work") == "reply":
                continue
            holder_id = str(run.get("current_agent_id") or "")
            if holder_id and holder_id == owner_id:
                return "排队中，等这一轮回复结束"
            holder = self._store.get_agent(holder_id) if holder_id else None
            if holder is not None:
                return f"排队中，等「{holder['name']}」做完"
        owner = self._store.get_agent(owner_id) if owner_id else None
        return f"排队中，等「{owner['name']}」做完" if owner else "排队中，等前面的回复结束"

    def on_run_finished(self, run_id: str) -> None:
        """Engine hook: a run (group or thread) ended; settle any part, then refill slots."""
        task = self._store.task_of_run(run_id)
        if task is None:
            # A group run ending frees no thread slot, but it may release a task a reply
            # lane opened (``behind_lane``). A thread run may have ended before it could
            # bind, so its page still gets a pump too.
            run = self._store.get_run(run_id)
            conversation = (
                self._store.get_conversation(run["conversation_id"]) if run is not None else None
            )
            if conversation is None:
                return
            if conversation["kind"] != "thread":
                if self._behind_lane:
                    try:
                        asyncio.get_running_loop().create_task(self._wake())
                    except RuntimeError:  # pragma: no cover - finalize runs inside the loop
                        pass
                return
        else:
            self._resolve_part(task)
            self._waiting_on_part.discard(task["id"])
            if task["status"] in (task_rules.DONE, task_rules.STOPPED, task_rules.FAILED):
                self._instructions.pop(task["id"], None)
        try:
            asyncio.get_running_loop().create_task(self._wake())
        except RuntimeError:  # pragma: no cover - finalize always runs inside the loop
            pass

    async def _wake(self) -> None:
        if self._wake_task is not None and not self._wake_task.done():
            return
        self._wake_task = asyncio.current_task()
        try:
            await self.pump()
        except Exception:  # one broken wake never stops the next
            logger.exception("任务排队检查失败，下一次继续。")
        finally:
            self._wake_task = None

    # ------------------------------------------------------------------ starting

    def _active_count(self) -> int:
        running = [
            task
            for task in self._store.running_thread_tasks()
            if task["id"] not in self._waiting_on_part
        ]
        return len(running)

    def _max_parallel(self) -> int:
        return int(self._store.team_settings().get("max_parallel_tasks") or 3)

    async def _pump_locked(self) -> None:
        """Start every queued task whose member is free and that fits under the cap."""
        cap = max(1, min(8, self._max_parallel()))
        started_any = False
        for task in self._store.queued_tasks():
            current = self._store.get_group_task(task["id"])
            if current is None or current["status"] != task_rules.QUEUED:
                continue
            owner_id = str(current["owner_id"])
            if self._store.running_thread_task(owner_id, exclude=current["id"]) is not None:
                # This member already has one task; its end wakes the queue for the next.
                continue
            if self._active_count() >= cap:
                break
            thread_id = current.get("thread_id")
            if not thread_id:
                continue
            if self._store.active_run(thread_id) is not None or self._engine.is_running(thread_id):
                # A message the user posted into that thread holds it right now.
                continue
            if task["id"] in self._behind_lane:
                if self._engine.is_running(str(current["conversation_id"])):
                    continue
                self._behind_lane.discard(task["id"])
            if await self._start(current):
                started_any = True
        if started_any:
            self._publish_workspace()

    async def _start(self, task: dict) -> bool:
        owner = self._store.get_agent(str(task["owner_id"]))
        if owner is None:
            failed = self._store.update_group_task(
                task["id"], status=task_rules.FAILED, end_reason="负责的成员不在了"
            )
            if failed is not None:
                self._touch_card(
                    failed,
                    status="interrupted",
                    content=prompts.task_stopped_line(failed["title"], failed["status"]),
                )
            self._publish_group_tasks(task["conversation_id"])
            return False
        thread_id = str(task["thread_id"])
        instruction = self._instructions.pop(task["id"], None)
        if instruction is None:
            instruction = self._default_instruction(task)
        run = self._store.begin_bare_run(thread_id, f"任务：{task['title']}")
        self._store.update_run(run["id"], current_agent_id=owner["id"])
        started = self._store.update_group_task(
            task["id"],
            status=task_rules.RUNNING,
            run_id=run["id"],
            started_at=now_iso(),
            current_step="准备开始",
            latest_text="",
        ) or task
        try:
            self._engine.start(
                run["id"],
                thread_id,
                task_id=task["id"],
                task_instruction=instruction,
            )
        except EngineBusy:
            # The thread got a run in the same instant (a manual post): leave it queued.
            self._store.update_run(
                run["id"], status="stopped", current_step="任务已停止", finished_at=now_iso()
            )
            self._store.update_group_task(
                task["id"], status=task_rules.QUEUED, run_id=None, current_step="排队中"
            )
            return False
        self._touch_card(started, status="streaming", content=started["title"])
        self._publish_group_tasks(task["conversation_id"])
        return True

    def _default_instruction(self, task: dict) -> str | None:
        """What a queued run is told when its exact opening was not kept in memory."""
        rounds = int(task.get("rounds") or 1)
        instruction = task.get("instruction") or task.get("title") or ""
        if task.get("parent_id") is not None and task.get("assigned_by"):
            giver = self._engine._agent_name(str(task["assigned_by"]))
            return prompts.task_instruction(instruction, giver)
        if task.get("assigned_by") and rounds > 1:
            giver = self._engine._agent_name(str(task["assigned_by"]))
            return prompts.transfer_instruction(instruction, giver)
        if rounds > 1:
            return prompts.resume_instruction(instruction, rounds)
        return None

    # ------------------------------------------------------------------ assign

    def part_future(self, child_id: str, parent_task_id: str) -> asyncio.Future:
        """The future one ``group_assign`` caller awaits for its part's result."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._parts[child_id] = future
        self._waiting_on_part.add(parent_task_id)
        return future

    def _resolve_part(self, task: dict) -> None:
        future = self._parts.pop(task["id"], None)
        if future is None or future.done():
            return
        message = (
            self._store.get_message(task["result_message_id"])
            if task.get("result_message_id")
            else None
        )
        future.set_result(
            {
                "status": task["status"],
                "text": str((message or {}).get("content") or ""),
                "message_id": (message or {}).get("id"),
            }
        )

    async def abandon_part(self, child_id: str, parent_task_id: str) -> None:
        """The assigner stopped waiting: leave nothing running for a result nobody reads."""
        self._waiting_on_part.discard(parent_task_id)
        future = self._parts.pop(child_id, None)
        if future is not None and not future.done():
            future.cancel()
        task = self._store.get_group_task(child_id)
        if task is None:
            return
        # The assigner gave up on it: the row says the group, not the user, stopped this part.
        parent = self._store.get_group_task(parent_task_id)
        giver = (
            self._engine._agent_name(str(parent["owner_id"])) if parent is not None else "同事"
        )
        if task["status"] == task_rules.QUEUED:
            stopped = self._store.update_group_task(
                child_id, status=task_rules.STOPPED, stopped_by=f"群里的「{giver}」"
            )
            if stopped is not None:
                self._touch_card(
                    stopped,
                    status="interrupted",
                    content=prompts.task_stopped_line(stopped["title"], stopped["status"]),
                )
            self._publish_group_tasks(task["conversation_id"])
        elif task["status"] in (task_rules.RUNNING, task_rules.AWAITING) and task.get("run_id"):
            active = self._engine.live_run(str(task["run_id"]))
            if active is not None:
                active.stopped_by = f"群里的「{giver}」"
            await self._engine.stop(str(task["run_id"]))
        self._publish_workspace()

    def note_instruction(self, task_id: str, text: str) -> dict | None:
        """A queued task's opening gains the user's addition; the run reads it when it starts.

        The task has not started, so there is no lane to tell: appending to its stored
        instruction (and to the opening kept for it in memory) is what the thread will be told.
        """
        task = self._store.get_group_task(task_id)
        if task is None or task["status"] != task_rules.QUEUED:
            return None
        addition = f"\n\n用户的补充：{text}"
        queued = self._instructions.get(task_id)
        if queued is not None:
            self._instructions[task_id] = f"{queued}{addition}"
        instruction = f"{task.get('instruction') or task['title']}{addition}"
        updated = self._store.update_group_task(task_id, instruction=instruction)
        if updated is not None:
            self._touch_card(updated, status="streaming", content=updated["title"])
        return updated

    # ------------------------------------------------------------------ user actions

    async def merge(self, group_id: str, task_id: str) -> dict | None:
        """「并入正在做的那件」: a queued task becomes a note on its owner's live work."""
        async with self._lock:
            task = self._store.get_group_task(task_id)
            if task is None or task["conversation_id"] != group_id:
                return None
            if task["status"] != task_rules.QUEUED:
                return None
            running = self._store.running_thread_task(str(task["owner_id"]), exclude=task_id)
            if running is None or not running.get("thread_id"):
                return None
            active = self._engine.live_run(str(running.get("run_id") or ""))
            if active is None:
                return None
            owner = self._store.get_agent(str(task["owner_id"]))
            if owner is None:  # pragma: no cover - the task's owner existed moments ago
                return None
            text = prompts.merge_note(
                str(task["title"]), str(task.get("instruction") or "")
            )
            noted = False
            for activity in self._store.activities_of_run(
                str(running.get("run_id") or ""), unfinished=True
            ):
                if str(activity.get("agent_id") or "") != str(owner["id"]):
                    continue
                noted = (
                    self._engine.note_work_activity(
                        str(activity["id"]), kind="request", text=text
                    )
                    or noted
                )
            if not noted:
                self._engine.add_note(active, kind="request", text=text)
            self._instructions.pop(task_id, None)
            merged = self._store.update_group_task(
                task_id,
                status=task_rules.DONE,
                run_id=None,
                current_step=f"已并入「{running['title']}」",
            )
            if merged is not None:
                # The card says where the request went instead of looking unfinished.
                self._touch_card(
                    merged, status="complete", content=f"已并入「{running['title']}」"
                )
            self._publish_group_tasks(group_id)
            self._publish_workspace()
            return self._store.task_payload(merged) if merged is not None else None

    async def stop(self, group_id: str, task_id: str) -> dict | None:
        """Stop one task: dequeue it, or stop only its own thread run."""
        async with self._lock:
            task = self._store.get_group_task(task_id)
            if task is None or task["conversation_id"] != group_id:
                return None
            if task["status"] == task_rules.QUEUED:
                self._instructions.pop(task_id, None)
                stopped = self._store.update_group_task(
                    task_id,
                    status=task_rules.STOPPED,
                    run_id=None,
                    current_step="已停下",
                    stopped_by="你",
                )
                if stopped is not None:
                    self._touch_card(
                        stopped,
                        status="interrupted",
                        content=prompts.task_stopped_line(stopped["title"], stopped["status"]),
                    )
                self._publish_group_tasks(group_id)
                self._publish_workspace()
                await self._pump_locked()
                return self._store.task_payload(stopped) if stopped is not None else None
            if task["status"] in (task_rules.RUNNING, task_rules.AWAITING) and task.get("run_id"):
                active = self._engine.live_run(str(task["run_id"]))
                if active is not None:
                    # The card's own button is the user stopping this one task.
                    active.stopped_by = "你"
                await self._engine.stop(str(task["run_id"]))
                current = self._store.get_group_task(task_id)
                self._publish_group_tasks(group_id)
                self._publish_workspace()
                return self._store.task_payload(current) if current is not None else None
            return None

    async def resume(self, group_id: str, task_id: str) -> dict | None:
        """「接着做」: resume this exact stopped/failed task in its own thread.

        The user pressed a card's button, so this never posts a message into the group and
        never depends on a dispatcher call; the thread continues from where it stopped.
        """
        instruction: str | None = None
        async with self._lock:
            task = self._store.get_group_task(task_id)
            if task is None or task["conversation_id"] != group_id:
                return None
            if task.get("dismissed_at") or task["status"] not in (
                task_rules.STOPPED,
                task_rules.FAILED,
            ):
                return None
            owner = self._store.get_agent(str(task["owner_id"]))
            if owner is None:
                return None
            self._engine._thread_of(task, owner)
            instruction = prompts.resume_instruction(
                task["instruction"] or task["title"], int(task["rounds"] or 1) + 1
            )
        # ``enqueue`` takes this same lock, so it must run after the block above.
        await self.enqueue(task_id, instruction=instruction, another_round=True)
        return self._store.task_payload(self._store.get_group_task(task_id) or task)

    async def start_now(self, conversation_id: str, task_id: str) -> dict | None:
        """「现在就做」: start a queued task beside its owner's live work.

        The user asked for this one now, so it bypasses the per-member rule (a member may
        have a second task running) and the wait for that chat's live lane. The caps that
        protect the machine stay: the team-wide parallel limit and the chat's own lane cap.
        """
        async with self._lock:
            task = self._store.get_group_task(task_id)
            if task is None or task["conversation_id"] != conversation_id:
                return None
            if task["status"] != task_rules.QUEUED or task.get("dismissed_at"):
                raise TaskBusy("这个任务已经不在排队，不能现在开始。")
            thread_id = task.get("thread_id")
            if not thread_id:
                raise TaskBusy("这个任务还没有自己的工作线程。")
            if len(self._store.active_runs_in(conversation_id)) >= LANES_PER_GROUP:
                raise TaskBusy("这个聊天同时在做的事已经满了，等一件做完再开始。")
            if self._active_count() >= max(1, min(8, self._max_parallel())):
                raise TaskBusy("同时在做的任务满了，等一件做完再开始。")
            if self._store.active_run(thread_id) is not None or self._engine.is_running(thread_id):
                raise TaskBusy("这个任务自己的线程正在处理一条消息，等它说完再开始。")
            self._behind_lane.discard(task_id)
            if not await self._start(task):
                raise TaskBusy("这个任务刚刚被别的运行占住了，稍后再试。")
            return self._store.task_payload(self._store.get_group_task(task_id) or task)

    def dismiss(self, group_id: str, task_id: str) -> dict | None:
        """「不做了」: put a stopped/failed task down for good (its card stays, greyed)."""
        task = self._store.get_group_task(task_id)
        if task is None or task["conversation_id"] != group_id:
            return None
        if task["status"] not in (task_rules.STOPPED, task_rules.FAILED):
            return None
        updated = self._store.mark_task_dismissed(task_id)
        if updated is None:
            return None
        self._touch_card(updated, status="interrupted", content=updated["title"])
        self._publish_group_tasks(group_id)
        self._publish_workspace()
        return self._store.task_payload(updated)

    def seen(self, group_id: str, task_id: str) -> dict | None:
        """The user has seen a finished task's result: it leaves the sidebar."""
        task = self._store.get_group_task(task_id)
        if task is None or task["conversation_id"] != group_id:
            return None
        if task["status"] != task_rules.DONE:
            return None
        updated = self._store.mark_task_seen(task_id)
        if updated is None:
            return None
        self._publish_group_tasks(group_id)
        self._publish_workspace()
        return self._store.task_payload(updated)

    # ------------------------------------------------------------------ publishing

    def _touch_card(self, task: dict, *, status: str, content: str | None = None) -> None:
        card_id = task.get("card_message_id")
        if not card_id:
            return
        updated = self._store.update_message(
            card_id, status=status, **({"content": content} if content is not None else {})
        )
        if updated is not None:
            self._engine.publish("message_end", models.message_payload(updated))

    def _publish_group_tasks(self, conversation_id: str) -> None:
        self._engine.publish(
            "group_tasks",
            {
                "conversation_id": conversation_id,
                "tasks": self._store.group_tasks_payload(conversation_id),
            },
        )

    def _publish_workspace(self) -> None:
        self._engine.publish(
            "workspace",
            models.workspace_payload(
                self._store,
                confirm_timeout_seconds=self._engine._settings.confirm_timeout_seconds,
            ),
        )

    # ------------------------------------------------------------------ progress

    def task_progress(self, task: dict) -> dict:
        """The small payload the card's live window reads."""
        view = self._store.task_payload(task)
        return {
            "conversation_id": view["conversation_id"],
            "task_id": view["id"],
            "latest_text": view.get("latest_text") or "",
            "recent_steps": view.get("recent_steps") or [],
            "steps": int(view.get("steps") or 0),
            "current_step": view.get("current_step") or "",
            "awaiting_action_id": view.get("awaiting_action_id"),
            "waiting_for": view.get("waiting_for"),
            "status": view.get("status"),
        }

    def publish_progress(self, task: dict, *, force: bool = False) -> None:
        """Push one task's live fields, throttled to :data:`PROGRESS_SECONDS`."""
        now = time.monotonic()
        last = self._progress_at.get(task["id"], float("-inf"))
        if not force and now - last < PROGRESS_SECONDS:
            return
        self._progress_at[task["id"]] = now
        self._engine.publish("task_progress", self.task_progress(task))
