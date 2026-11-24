// A group's tasks, read the way the user asked for them: one entry per request, with who took it
// and the parts they handed on. The server keeps one row per member; the user sent one message.

import type { GroupTask } from "../api/types";

export type RequestStatus = GroupTask["status"];

export interface RequestWork {
  /** The request message, or the task id when the request is gone. */
  key: string;
  requestID: string | null;
  title: string;
  createdAt: string;
  /** Members answering the request itself, in the order they took it. */
  owners: GroupTask[];
  /** Parts handed on, each under the member that handed it. */
  parts: GroupTask[];
  status: RequestStatus;
}

/** The request's state as one word: running, else awaiting, else queued, else stopped, else failed, else done. */
function overall(tasks: GroupTask[]): RequestStatus {
  for (const status of ["running", "awaiting", "queued", "stopped", "failed"] as const) {
    if (tasks.some((task) => task.status === status)) return status;
  }
  return "done";
}

/** One entry per request, newest first; the server answers its rows newest first, parts mixed in. */
export function groupByRequest(tasks: GroupTask[]): RequestWork[] {
  const byKey = new Map<string, GroupTask[]>();
  for (const task of tasks) {
    const key = task.request_message_id ?? task.id;
    byKey.set(key, [...(byKey.get(key) ?? []), task]);
  }
  const work: RequestWork[] = [];
  for (const [key, rows] of byKey) {
    // Rows arrive newest first and timestamps are to the second: reverse, then a stable sort
    // keeps same-second rows in the order they were taken.
    const ordered = [...rows].reverse().sort((a, b) => a.created_at.localeCompare(b.created_at));
    const owners = ordered.filter((task) => task.parent_id === null);
    const first = owners[0];
    if (!first) continue;
    work.push({
      key,
      requestID: first.request_message_id,
      title: first.title,
      createdAt: first.created_at,
      owners,
      parts: ordered.filter((task) => task.parent_id !== null),
      status: overall(ordered),
    });
  }
  return work.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

/**
 * What the header says about the group's work: only open work earns words. Awaiting is counted
 * per task, because a request can be running while one of its parts waits on the user.
 */
export function workSummary(work: RequestWork[]): {
  running: number;
  awaiting: number;
  queued: number;
  stopped: number;
} {
  const tasks = work.flatMap((item) => [...item.owners, ...item.parts]);
  return {
    running: tasks.filter((task) => task.status === "running" && task.awaiting_action_id === null).length,
    awaiting: tasks.filter((task) => task.status === "awaiting" || task.awaiting_action_id !== null).length,
    queued: tasks.filter((task) => task.status === "queued").length,
    stopped: tasks.filter((task) => task.status === "stopped").length,
  };
}
