"""The scheduled-task loop: recovery, due fires, queued fires and outcome recording.

One loop per process, started with the app and cancelled before the engine shuts down.
Every tick is a plain database read of what is due; the loop also wakes immediately when a
run finishes, so a fire that queued behind the previous one starts within milliseconds of
that reply ending rather than on the next tick.

Contract this file enforces:

- due -> start the run now, in the schedule's own hidden workspace, never in the
  conversation the summary will be posted to;
- the previous fire of the same schedule still working there -> mark ``queued`` (one
  pending fire per schedule) and advance the schedule, never touch the running run;
- anything that came due while the process was down is **not** run: recurring rules jump to
  their next future fire, a spent ``once`` becomes disabled with ``last_status`` "missed";
- no configured model -> record ``last_status`` "error" with the same sentence /health uses.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import providers, schedules
from .store import Store

logger = logging.getLogger(__name__)

INVALID_RULE = "定时规则已经无效，请重新设置。"


class Scheduler:
    """Drives scheduled tasks; ``fire`` starts one run and returns it, or ``None`` if busy."""

    def __init__(
        self,
        store: Store,
        *,
        tick_seconds: float,
        default_timezone: str,
        fire: Callable[[dict, str], Awaitable[dict | None]],
        publish: Callable[[str, Any], None],
    ) -> None:
        self._store = store
        self._tick = max(0.01, float(tick_seconds))
        self._default_timezone = default_timezone
        self._fire_run = fire
        self._publish = publish
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Apply the missed-while-down rules once, then start checking."""
        self.recover()
        self._task = asyncio.create_task(self._loop(), name="scheduled-tasks")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @property
    def running(self) -> bool:
        """True while the loop task is alive (tests assert it is cancelled on shutdown)."""
        return self._task is not None and not self._task.done()

    def wake(self) -> None:
        """Ask for a tick right now (a run ended, or a schedule changed over HTTP)."""
        self._wake.set()

    # ------------------------------------------------------------------ bookkeeping

    def recover(self) -> None:
        """Start-up pass: nothing missed while down ever runs, and state is honest again."""
        now = datetime.now(timezone.utc)
        changed = False
        for schedule in self._store.list_schedules():
            try:
                changed = self._recover_one(schedule, now) or changed
            except Exception:  # a hand-edited row must not keep the app from starting
                logger.exception("定时任务 %s 的启动检查失败。", schedule["id"])
        if changed:
            self._changed()

    def _recover_one(self, schedule: dict, now: datetime) -> bool:
        """Apply the start-up rules to one stored schedule; ``True`` when it changed."""
        changed = False
        if schedule["queued"]:
            self._store.update_schedule(schedule["id"], queued=False)
            changed = True
        run_id = schedule["last_run_id"]
        if schedule["last_status"] is None and run_id:
            self._record_outcome(run_id)
        if not schedule["enabled"]:
            return changed
        next_at = (
            schedules.parse_iso(schedule["next_run_at"])
            if schedule["next_run_at"]
            else None
        )
        if next_at is not None and next_at > now:
            return changed
        if schedule["kind"] in ("once", "reminder"):
            # A spent single fire (or one queued when the process died) is missed.
            self._store.update_schedule(
                schedule["id"],
                enabled=False,
                next_run_at=None,
                queued=False,
                last_status="missed",
                last_error=None,
            )
            return True
        try:
            moment = schedules.next_future(schedules.rule_of(schedule), now)
        except Exception:  # an unreadable rule is reported, never retried blindly
            self._store.update_schedule(
                schedule["id"],
                enabled=False,
                next_run_at=None,
                last_status="error",
                last_error=INVALID_RULE,
            )
            return True
        self._store.update_schedule(
            schedule["id"],
            next_run_at=None if moment is None else schedules.utc_iso(moment),
        )
        return True

    def on_run_finished(self, run_id: str) -> None:
        """Engine hook: record this run's outcome, then look for queued fires at once."""
        self._record_outcome(run_id)
        self.wake()

    def _record_outcome(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        if run is None:
            return
        schedule_id = run.get("schedule_id")
        if not schedule_id:
            return
        schedule = self._store.get_schedule(schedule_id)
        if schedule is None or schedule["last_run_id"] != run_id:
            return
        self._store.update_schedule(
            schedule_id, last_status=run["status"], last_error=run["error"]
        )
        self._changed()

    def _changed(self) -> None:
        """Publish ``schedules`` after any mutation, so every window stays in step."""
        self._publish("schedules", self.payload())

    def payload(self) -> dict:
        return schedules.schedules_payload(
            self._store.list_visible_schedules(), self._default_timezone
        )

    # ---------------------------------------------------------------------- loop

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._tick)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # one broken tick must never end the loop
                logger.exception("定时任务检查失败，下一次继续。")

    async def tick(self) -> None:
        """Start every fire that can start now: queued ones first, then newly due ones."""
        now = datetime.now(timezone.utc)
        await self._start_queued(now)
        await self._start_due(now)

    async def _start_queued(self, now: datetime) -> None:
        for schedule in self._store.queued_schedules():
            if self._store.get_conversation(schedule["conversation_id"]) is None:
                self._drop(schedule)
                continue
            if self._busy(schedule):
                continue
            await self._fire(schedule, now)

    def _busy(self, schedule: dict) -> bool:
        """Whether the previous fire of this schedule still works in its hidden workspace.

        The target conversation is never consulted for a scheduled task: a scheduled run does
        not use it. A reminder is the exception — it runs in the chat it was made in, so that
        chat being busy is what makes it queue.
        """
        if schedule["kind"] == "reminder":
            return self._store.active_run(schedule["conversation_id"]) is not None
        workspace_id = schedule["workspace_conversation_id"]
        return workspace_id is not None and self._store.active_run(str(workspace_id)) is not None

    async def _start_due(self, now: datetime) -> None:
        for schedule in self._store.due_schedules(schedules.utc_iso(now)):
            await self._fire(schedule, now)

    async def fire_now(self, schedule_id: str, text: str = "") -> dict | None:
        """``POST /schedules/{id}/run``: start it now, or queue it if its workspace is busy.

        ``text`` is the extra input a webhook call carries; it rides on top of the schedule's
        own prompt, so the same rule can be triggered by hand or by an outside service.
        """
        schedule = self._store.get_schedule(schedule_id)
        if schedule is None:
            return None
        await self._fire(schedule, datetime.now(timezone.utc), text)
        return self._store.get_schedule(schedule_id)

    # --------------------------------------------------------------------- firing

    async def _fire(self, schedule: dict, now: datetime, text: str = "") -> None:
        """Start one scheduled run, or queue/record it; never interrupts what is running."""
        if self._store.get_conversation(schedule["conversation_id"]) is None:
            self._drop(schedule)
            return
        once = schedule["kind"] in ("once", "reminder")
        try:
            moment = None if once else schedules.next_future(schedules.rule_of(schedule), now)
        except Exception:  # a stored rule that no longer computes is disabled, honestly
            self._store.update_schedule(
                schedule["id"],
                enabled=False,
                next_run_at=None,
                queued=False,
                last_status="error",
                last_error=INVALID_RULE,
            )
            self._changed()
            return
        next_run_at = None if moment is None else schedules.utc_iso(moment)
        if providers.resolve_default(self._store) is None:
            self._store.update_schedule(
                schedule["id"],
                enabled=False if once else schedule["enabled"],
                next_run_at=next_run_at,
                queued=False,
                last_run_at=schedules.utc_iso(now),
                last_status="error",
                last_error=providers.NO_MODEL,
            )
            self._changed()
            return
        try:
            run = await self._fire_run(schedule, text)
        except Exception as exc:  # an unexpected fault is recorded, never hidden
            reason = f"定时任务没能启动：{exc.__class__.__name__}。"
            self._store.update_schedule(
                schedule["id"],
                enabled=False if once else schedule["enabled"],
                next_run_at=next_run_at,
                queued=False,
                last_run_at=schedules.utc_iso(now),
                last_status="error",
                last_error=reason,
            )
            self._changed()
            return
        if run is None:
            # The workspace is still busy: one pending fire, no interruption.
            self._store.update_schedule(
                schedule["id"],
                queued=True,
                enabled=schedule["enabled"],
                next_run_at=next_run_at,
            )
            self._changed()
            return
        self._store.update_schedule(
            schedule["id"],
            queued=False,
            enabled=False if once else schedule["enabled"],
            next_run_at=next_run_at,
            last_run_at=schedules.utc_iso(now),
            last_run_id=run["id"],
            last_status=None,
            last_error=None,
            run_count=schedule["run_count"] + 1,
        )
        self._changed()

    def _drop(self, schedule: dict) -> None:
        """The target conversation is gone, so its schedule goes with it."""
        self._store.delete_schedule(schedule["id"])
        self._changed()
