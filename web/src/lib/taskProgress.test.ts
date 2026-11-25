import { describe, expect, it } from "vitest";
import type { GroupTask, GroupThread, TaskProgress } from "../api/types";
import {
  elapsedMs,
  formatElapsed,
  groupThreadsFromTasks,
  mergeGroupThreadProgress,
  mergeTaskProgress,
  patchGroupThreadProgress,
  queuedWaitLine,
  isTaskArchived,
  isTaskDismissed,
  showsStatusWord,
  taskCardActions,
  taskReasonLine,
  taskStatusMeta,
  taskStepLine,
  visibleGroupThreads,
} from "./taskProgress";

function task(id: string, overrides: Partial<GroupTask> = {}): GroupTask {
  return {
    id,
    conversation_id: "g1",
    request_message_id: "req",
    parent_id: null,
    owner_id: "a1",
    assigned_by: null,
    title: id,
    instruction: "",
    status: "running",
    run_id: null,
    result_message_id: null,
    rounds: 1,
    created_at: "2026-11-24T10:00:00Z",
    updated_at: "2026-11-24T10:00:00Z",
    card_message_id: null,
    started_at: "2026-11-24T10:00:00Z",
    thread_id: `t-${id}`,
    steps: 2,
    current_step: "跑测试",
    latest_text: "",
    recent_steps: [],
    awaiting_action_id: null,
    waiting_for: null,
    end_reason: null,
    stopped_by: null,
    dismissed_at: null,
    seen_at: null,
    ...overrides,
  };
}

function thread(taskID: string, overrides: Partial<GroupThread> = {}): GroupThread {
  return {
    group_id: "g1",
    task_id: taskID,
    thread_id: `t-${taskID}`,
    owner_id: "a1",
    title: taskID,
    status: "running",
    steps: 1,
    current_step: "查资料",
    awaiting_action_id: null,
    ...overrides,
  };
}

const progress: TaskProgress = {
  conversation_id: "g1",
  task_id: "b",
  latest_text: "第 11 步：在核对发票号",
  recent_steps: ["读了合同", "跑了 OCR"],
  steps: 10,
  current_step: "接真 OCR",
  awaiting_action_id: null,
  waiting_for: "等「匠师」用完屏幕",
  status: "running",
};

describe("task progress merge", () => {
  it("moves every live field of the named row and leaves the rest alone", () => {
    const rows = [task("a"), task("b")];
    const merged = mergeTaskProgress(rows, progress);
    expect(merged.map((row) => row.id)).toEqual(["a", "b"]);
    expect(merged[1]).toMatchObject({
      steps: 10,
      current_step: "接真 OCR",
      latest_text: progress.latest_text,
      recent_steps: ["读了合同", "跑了 OCR"],
      waiting_for: "等「匠师」用完屏幕",
    });
    expect(merged[0]).toBe(rows[0]);
  });

  it("does not invent a row the store has not loaded", () => {
    expect(mergeTaskProgress([task("a")], progress)).toEqual([task("a")]);
  });

  it("adds, updates and drops sidebar rows by status", () => {
    const added = mergeGroupThreadProgress([thread("a")], "g1", task("b"));
    expect(added.map((row) => row.task_id)).toEqual(["a", "b"]);
    const done = mergeGroupThreadProgress(added, "g1", task("b", { status: "done" }));
    expect(done.map((row) => row.task_id)).toEqual(["a", "b"]);
    const seen = mergeGroupThreadProgress(done, "g1", task("b", { status: "done", seen_at: "2026-11-24T11:00:00Z" }));
    expect(seen.map((row) => row.task_id)).toEqual(["a"]);
  });

  it("moves a sidebar row from a progress event alone, keeping it until seen", () => {
    const rows = [thread("b"), thread("x")];
    const moved = patchGroupThreadProgress(rows, progress);
    expect(moved[0]).toMatchObject({ task_id: "b", steps: 10, current_step: "接真 OCR" });
    expect(moved[1]).toBe(rows[1]);
    expect(patchGroupThreadProgress(moved, { ...progress, status: "done" }).map((row) => row.task_id)).toEqual([
      "b",
      "x",
    ]);
  });

  it("rebuilds one group's rows from its whole task list", () => {
    const rows = [thread("old"), thread("x", { group_id: "g2" })];
    const rebuilt = groupThreadsFromTasks(rows, "g1", [
      task("a", { status: "queued" }),
      task("b", { status: "done" }),
      task("c", { status: "running", awaiting_action_id: "act" }),
    ]);
    expect(rebuilt.filter((row) => row.group_id === "g1").map((row) => row.task_id)).toEqual(["a", "b", "c"]);
    expect(rebuilt.some((row) => row.group_id === "g2")).toBe(true);
  });
});

describe("sidebar thread order and visibility", () => {
  it("shows awaiting first, then running, queued and the failures; done rows disappear", () => {
    const rows = [
      thread("failed", { status: "failed" }),
      thread("queued", { status: "queued" }),
      thread("done", { status: "done" }),
      thread("running"),
      thread("awaiting", { awaiting_action_id: "act" }),
      thread("stopped", { status: "stopped" }),
    ];
    expect(visibleGroupThreads(rows).map((row) => row.task_id)).toEqual([
      "awaiting",
      "running",
      "queued",
      "failed",
      "stopped",
    ]);
  });
});

describe("elapsed time", () => {
  const start = "2026-11-24T10:00:00Z";
  const at = (seconds: number) => Date.parse(start) + seconds * 1000;

  it("counts from the start and clamps a clock skew at zero", () => {
    expect(elapsedMs(start, at(30))).toBe(30_000);
    expect(elapsedMs(start, at(-5))).toBe(0);
    expect(elapsedMs(null, at(30))).toBe(0);
  });

  it("freezes at the end time once the task is over", () => {
    expect(elapsedMs(start, at(900), "2026-11-24T10:05:00Z")).toBe(300_000);
  });

  it("reads as seconds, minutes, then hours and minutes", () => {
    expect(formatElapsed(0)).toBe("0 秒");
    expect(formatElapsed(45_000)).toBe("45 秒");
    expect(formatElapsed(90_000)).toBe("1 分");
    expect(formatElapsed(3_700_000)).toBe("1 小时 1 分");
  });
});

describe("task status words", () => {
  it("gives an awaiting action its own amber state over any status", () => {
    expect(taskStatusMeta("running", true)).toEqual({ label: "等你确认", tone: "awaiting" });
  });

  it("treats awaiting as a real status, not only an overlay", () => {
    expect(taskStatusMeta("awaiting")).toEqual({ label: "等你确认", tone: "awaiting" });
  });

  it("names every status the card can show", () => {
    expect(taskStatusMeta("queued").label).toBe("排队中");
    expect(taskStatusMeta("running").label).toBe("进行中");
    expect(taskStatusMeta("done").label).toBe("完成");
    expect(taskStatusMeta("failed").label).toBe("没做成");
    expect(taskStatusMeta("stopped").label).toBe("停下了");
  });

  it("says 等待中 when a running task waits for the screen, muted not running-blue", () => {
    expect(taskStatusMeta("running", false, true)).toEqual({ label: "等待中", tone: "waiting" });
    expect(taskStatusMeta("running", false, false)).toEqual({ label: "进行中", tone: "running" });
  });

  it("counts the current step while running, and totals once done", () => {
    expect(taskStepLine({ status: "running", steps: 10, current_step: "接真 OCR" })).toBe(
      "第 11 步 · 当前：接真 OCR",
    );
    expect(taskStepLine({ status: "done", steps: 4, current_step: "" })).toBe("共 4 步");
    expect(taskStepLine({ status: "queued", steps: 0, current_step: "" })).toBe("排队中，等前面的任务做完");
  });
});

describe("queued wait sentence", () => {
  it("names the owner's running task when there is one", () => {
    const queued = task("q", { status: "queued" });
    const running = task("r", { owner_id: "a1", title: "接真 OCR" });
    expect(queuedWaitLine(queued, [queued, running], "匠师")).toBe("等「匠师」做完「接真 OCR」再开始");
  });

  it("reads as the team cap when no same-owner task is running", () => {
    const queued = task("q", { status: "queued" });
    const other = task("r", { owner_id: "a2", title: "别的事" });
    expect(queuedWaitLine(queued, [queued, other], "匠师")).toBe("同时在做的任务满了，空出来就开始");
  });

  it("stays generic when the task list is not loaded", () => {
    expect(queuedWaitLine(task("q", { status: "queued" }), undefined, "匠师")).toBe("等前面的任务做完再开始");
  });

  it("keeps the scheduler's own reason when it queued behind a live lane", () => {
    const queued = task("q", { status: "queued", current_step: "排队中，等「匠师」做完" });
    expect(queuedWaitLine(queued, [queued], "匠师")).toBe("排队中，等「匠师」做完");
  });
});

describe("queued card status word", () => {
  it("drops the top-right word when the wait line already says 排队中", () => {
    expect(showsStatusWord("queued", "排队中，等这一轮回复结束")).toBe(false);
    expect(showsStatusWord("queued", "排队中，等「主管」做完")).toBe(false);
  });

  it("keeps it when the card waits on the cap, and on every other status", () => {
    expect(showsStatusWord("queued", "同时在做的任务满了，空出来就开始")).toBe(true);
    expect(showsStatusWord("running", "排队中，等前面的任务做完")).toBe(true);
  });
});

describe("task card buttons", () => {
  it("offers only the moves a status can use", () => {
    expect(taskCardActions("queued")).toEqual(["cancel", "merge"]);
    expect(taskCardActions("running")).toEqual(["expand", "thread", "stop"]);
    expect(taskCardActions("awaiting")).toEqual(["thread", "stop"]);
    expect(taskCardActions("stopped")).toEqual(["resume", "dismiss", "thread"]);
    expect(taskCardActions("failed")).toEqual(["resume", "dismiss", "thread"]);
    expect(taskCardActions("done")).toEqual(["thread"]);
  });

  it("lets a queued card in the user's own chat start right away", () => {
    expect(taskCardActions("queued", true)).toEqual(["start", "cancel"]);
    expect(taskCardActions("running", true)).toEqual(["expand", "thread", "stop"]);
  });
});

describe("task reason line", () => {
  it("shows a failed task's end reason as-is, with a fallback", () => {
    expect(taskReasonLine(task("a", { status: "failed", end_reason: "步数用完了" }))).toBe("步数用完了");
    expect(taskReasonLine(task("a", { status: "failed", end_reason: "  " }))).toBe("出错了");
  });

  it("turns stopped_by into a sentence without doubling it", () => {
    expect(taskReasonLine(task("a", { status: "stopped", stopped_by: "你" }))).toBe("你停下的");
    expect(taskReasonLine(task("a", { status: "stopped", stopped_by: "群里的「匠师」" }))).toBe(
      "群里的「匠师」停下的",
    );
    expect(taskReasonLine(task("a", { status: "stopped", stopped_by: "重启后中断" }))).toBe("重启后中断");
    expect(taskReasonLine(task("a", { status: "stopped", stopped_by: "你停下的" }))).toBe("你停下的");
    expect(taskReasonLine(task("a", { status: "stopped", stopped_by: null }))).toBe("停下来了");
  });

  it("says nothing for a live or finished task", () => {
    expect(taskReasonLine(task("a"))).toBe("");
    expect(taskReasonLine(task("a", { status: "done" }))).toBe("");
  });
});

describe("archived and dismissed rows", () => {
  it("calls a threadless, put-down row archived and any put-down row dismissed", () => {
    const old = task("old", { thread_id: null, dismissed_at: "2026-11-24T10:00:00Z" });
    expect(isTaskArchived(old)).toBe(true);
    expect(isTaskDismissed(old)).toBe(true);
    const live = task("live");
    expect(isTaskArchived(live)).toBe(false);
    expect(isTaskDismissed(live)).toBe(false);
  });

  it("keeps dismissed and seen done rows out of the sidebar rebuild", () => {
    const rows = groupThreadsFromTasks([], "g1", [
      task("open"),
      task("seen", { status: "done", seen_at: "2026-11-24T11:00:00Z" }),
      task("unseen", { status: "done" }),
      task("dropped", { status: "failed", dismissed_at: "2026-11-24T11:00:00Z" }),
    ]);
    expect(rows.map((row) => row.task_id)).toEqual(["open", "unseen"]);
  });

  it("drops a dismissed row from the sidebar when its task event arrives", () => {
    const rows = groupThreadsFromTasks([], "g1", [task("a")]);
    expect(mergeGroupThreadProgress(rows, "g1", task("a", { dismissed_at: "2026-11-24T11:00:00Z" }))).toEqual([]);
  });
});
