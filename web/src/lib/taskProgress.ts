// Live task state, kept as pure helpers: a `task_progress` event merges into the rows already on
// screen, the sidebar filters and orders a group's threads, and both the card and the rail read
// one status vocabulary. No React and no store here, so each rule is testable by itself.

import type { GroupTask, GroupThread, TaskProgress } from "../api/types";

export type TaskStatus = GroupTask["status"];

export interface TaskStatusMeta {
  /** What the card and the rail call this state. */
  label: string;
  /** The tone class both surfaces share: running/queued/awaiting/waiting/done/failed/stopped. */
  tone: "running" | "queued" | "awaiting" | "waiting" | "done" | "failed" | "stopped";
}

/**
 * A status, with the amber 「等你确认」 that only the action id knows about, and the muted
 * 「等待中」 of a running task that is blocked on the screen or another holder.
 */
export function taskStatusMeta(status: TaskStatus, awaiting = false, waiting = false): TaskStatusMeta {
  if (awaiting) return { label: "等你确认", tone: "awaiting" };
  if (waiting && status === "running") return { label: "等待中", tone: "waiting" };
  switch (status) {
    case "queued":
      return { label: "排队中", tone: "queued" };
    case "running":
      return { label: "进行中", tone: "running" };
    case "awaiting":
      return { label: "等你确认", tone: "awaiting" };
    case "done":
      return { label: "完成", tone: "done" };
    case "failed":
      return { label: "没做成", tone: "failed" };
    case "stopped":
      return { label: "停下了", tone: "stopped" };
  }
}

/**
 * Why a queued task is still waiting: its own member's running task when there is one, else the
 * team-wide cap (no same-owner running task means the queue is global). Without the task list,
 * the quiet generic line is all we can honestly say.
 */
export function queuedWaitLine(
  task: Pick<GroupTask, "id" | "owner_id">,
  tasks: readonly GroupTask[] | undefined,
  ownerName?: string,
): string {
  const running = tasks?.find(
    (row) => row.id !== task.id && row.owner_id === task.owner_id && row.status === "running",
  );
  if (running) return `等「${ownerName ?? "成员"}」做完「${running.title}」再开始`;
  if (tasks) return "同时在做的任务满了，空出来就开始";
  return "等前面的任务做完再开始";
}

/** The one-line progress the card shows: steps so far and what it is doing now. */
export function taskStepLine(task: Pick<GroupTask, "status" | "steps" | "current_step">): string {
  const current = task.current_step.trim();
  if (task.status === "queued") return "排队中，等前面的任务做完";
  if (task.status === "running" || task.status === "awaiting") {
    return `第 ${task.steps + 1} 步${current ? ` · 当前：${current}` : ""}`;
  }
  if (task.status === "done") return task.steps > 0 ? `共 ${task.steps} 步` : "已完成";
  return task.steps > 0 ? `${task.status === "failed" ? "失败前" : "停下时"}做了 ${task.steps} 步` : "";
}

/**
 * Merge one throttled progress event into a group's rows by task id. The row stays where it was
 * (the list's order is structural), and every live field moves together so the card never shows
 * a step count from one beat and a step name from another.
 */
export function mergeTaskProgress(tasks: readonly GroupTask[], progress: TaskProgress): GroupTask[] {
  let found = false;
  const next = tasks.map((task) => {
    if (task.id !== progress.task_id) return task;
    found = true;
    return {
      ...task,
      status: progress.status,
      steps: progress.steps,
      current_step: progress.current_step,
      latest_text: progress.latest_text,
      recent_steps: progress.recent_steps,
      awaiting_action_id: progress.awaiting_action_id,
      waiting_for: progress.waiting_for,
    };
  });
  // A task the store has not loaded yet (a page opened mid-stream) stays absent; the next
  // `group_tasks` event brings it whole.
  return found ? next : [...tasks];
}

/** Which statuses the sidebar keeps: open work, plus the failures the user has not cleared. */
const SIDEBAR_STATUS: Record<TaskStatus, boolean> = {
  running: true,
  awaiting: true,
  queued: true,
  failed: true,
  stopped: true,
  done: false,
};

/** The sidebar shows a live line for open work; done threads disappear. */
export function isLiveGroupThread(thread: GroupThread): boolean {
  return SIDEBAR_STATUS[thread.status];
}

/** Awaiting first, then running, then queued, then the failures; ties settle by title. */
const THREAD_RANK: Record<TaskStatus, number> = {
  awaiting: 0,
  running: 1,
  queued: 2,
  failed: 3,
  stopped: 4,
  done: 9,
};

export function visibleGroupThreads(threads: readonly GroupThread[] | undefined): GroupThread[] {
  return (threads ?? []).filter(isLiveGroupThread).sort((a, b) => {
    const rank =
      (a.awaiting_action_id ? 0 : THREAD_RANK[a.status]) - (b.awaiting_action_id ? 0 : THREAD_RANK[b.status]);
    return rank !== 0 ? rank : a.title.localeCompare(b.title, "zh-CN");
  });
}

/** One group's live threads, already filtered and ordered. */
export function threadsOfGroup(threads: readonly GroupThread[] | undefined, groupID: string): GroupThread[] {
  return visibleGroupThreads((threads ?? []).filter((thread) => thread.group_id === groupID));
}

/**
 * Fold a progress event into the workspace's sidebar list: replace that thread's row, or add it
 * when the group's work just became live. Done rows drop out.
 */
export function mergeGroupThreadProgress(
  threads: readonly GroupThread[] | undefined,
  groupID: string,
  task: Pick<
    GroupTask,
    "id" | "thread_id" | "owner_id" | "title" | "status" | "steps" | "current_step" | "awaiting_action_id"
  > & {
    dismissed_at?: string | null;
    seen_at?: string | null;
  },
): GroupThread[] {
  const rows = threads ?? [];
  if (!task.thread_id) return [...rows];
  if (task.dismissed_at) return rows.filter((thread) => thread.task_id !== task.id);
  const row: GroupThread = {
    group_id: groupID,
    task_id: task.id,
    thread_id: task.thread_id,
    owner_id: task.owner_id,
    title: task.title,
    status: task.status,
    steps: task.steps,
    current_step: task.current_step,
    awaiting_action_id: task.awaiting_action_id,
  };
  const index = rows.findIndex((thread) => thread.task_id === task.id);
  const next = index < 0 ? [...rows, row] : rows.map((thread) => (thread.task_id === task.id ? row : thread));
  return next.filter(
    (thread) => thread.status !== "done" || (thread.task_id === task.id && !task.seen_at),
  );
}

/**
 * Move an existing sidebar row from a progress event alone, for a group whose task list this
 * window has not read (the sidebar still has its thread). An unknown task adds nothing: the
 * owner and title it would need are not in a progress event.
 */
export function patchGroupThreadProgress(
  threads: readonly GroupThread[] | undefined,
  progress: TaskProgress,
): GroupThread[] {
  const rows = threads ?? [];
  if (!rows.some((thread) => thread.task_id === progress.task_id)) return [...rows];
  const next = rows.map((thread) =>
    thread.task_id === progress.task_id
      ? {
          ...thread,
          status: progress.status,
          steps: progress.steps,
          current_step: progress.current_step,
          awaiting_action_id: progress.awaiting_action_id,
        }
      : thread,
  );
  return next;
}

/**
 * Replace one group's sidebar rows from the group's whole task list (a `group_tasks` event). Rows
 * of other groups are untouched, and the same ordering and filtering apply as everywhere else.
 */
export function groupThreadsFromTasks(
  threads: readonly GroupThread[] | undefined,
  groupID: string,
  tasks: readonly GroupTask[],
): GroupThread[] {
  const others = (threads ?? []).filter((thread) => thread.group_id !== groupID);
  const mine: GroupThread[] = tasks
    .filter((task) => task.thread_id !== null && !task.dismissed_at && (task.status !== "done" || !task.seen_at))
    .map((task) => ({
      group_id: groupID,
      task_id: task.id,
      thread_id: task.thread_id!,
      owner_id: task.owner_id,
      title: task.title,
      status: task.status,
      steps: task.steps,
      current_step: task.current_step,
      awaiting_action_id: task.awaiting_action_id,
    }));
  return [...others, ...mine];
}

/** Milliseconds between two instants, clamped at zero so a clock skew never reads negative. */
export function elapsedMs(startedAt: string | null | undefined, now: number, endedAt?: string | null): number {
  if (!startedAt) return 0;
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return 0;
  const end = endedAt ? Date.parse(endedAt) : now;
  return Math.max(0, (Number.isNaN(end) ? now : end) - start);
}

/**
 * Elapsed time in words: seconds under a minute, then whole minutes, then hours and minutes.
 * Short enough for the card's header line and stable as it ticks.
 */
export function formatElapsed(ms: number): string {
  const seconds = Math.floor(Math.max(0, ms) / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

/** Whether a status still counts time: only a task that has a live run does. */
export function isTicking(status: TaskStatus): boolean {
  return status === "running";
}

/** The thread page's task, found from the loaded rows of every group. */
export function taskOfThread(
  tasksByGroup: Record<string, GroupTask[]>,
  threadID: string,
): GroupTask | undefined {
  for (const tasks of Object.values(tasksByGroup)) {
    const task = tasks.find((row) => row.thread_id === threadID);
    if (task) return task;
  }
  return undefined;
}

/** One button the task card offers; each maps to a store call or a local view toggle. */
export type TaskCardAction = "expand" | "thread" | "stop" | "cancel" | "merge" | "resume" | "dismiss";

/** The buttons a card of this status shows, in the order they sit in its action row. */
export function taskCardActions(status: TaskStatus): TaskCardAction[] {
  switch (status) {
    case "queued":
      return ["cancel", "merge"];
    case "running":
      return ["expand", "thread", "stop"];
    case "awaiting":
      return ["thread", "stop"];
    case "stopped":
    case "failed":
      return ["resume", "dismiss", "thread"];
    case "done":
      return ["thread"];
  }
}

/** A stopped/failed card's reason, in one line the user can read without opening the thread. */
export function taskReasonLine(task: Pick<GroupTask, "status" | "end_reason" | "stopped_by">): string {
  if (task.status === "failed") return (task.end_reason ?? "").trim() || "出错了";
  if (task.status !== "stopped") return "";
  const by = (task.stopped_by ?? "").trim();
  if (!by) return "停下来了";
  if (by.endsWith("停下的") || by.endsWith("中断")) return by;
  return `${by}停下的`;
}

/** A row from before task threads, kept for the record: no thread, and archived by migration. */
export function isTaskArchived(task: Pick<GroupTask, "thread_id" | "dismissed_at">): boolean {
  return task.thread_id === null && task.dismissed_at !== null;
}

/** Put down by the user (archived included): gone from every list, still drawn in the timeline. */
export function isTaskDismissed(task: Pick<GroupTask, "dismissed_at">): boolean {
  return task.dismissed_at !== null;
}
