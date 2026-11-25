import { describe, expect, it } from "vitest";
import type { Activity, Message } from "../api/types";
import {
  ACTIVITY_PILL,
  ACTIVITY_STATUS_META,
  activityChipText,
  activityGlyph,
  activityProgressLine,
  activityStepLine,
  activityThinking,
  groupActivitiesByDay,
  groupPlan,
  isLiveActivity,
} from "./activities";

let seq = 0;

function message(patch: Partial<Message>): Message {
  seq += 1;
  return {
    id: `m${seq}`,
    conversation_id: "c1",
    author_id: "a1",
    author_name: "匠师",
    avatar_seed: null,
    kind: "agent",
    content: "",
    status: "complete",
    seq,
    created_at: "2026-11-25T10:00:00Z",
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: "r1",
    ...patch,
  };
}

function activity(patch: Partial<Activity> = {}): Activity {
  return {
    id: "ac1",
    conversation_id: "c1",
    agent_id: "a1",
    agent_name: "匠师",
    run_id: "r1",
    task_id: null,
    parent_id: null,
    title: "核对发布页数据",
    summary: "",
    status: "running",
    current_step: "正在读取 pricing.csv",
    latest_text: "",
    steps: 3,
    current_tool: "file_read",
    started_at: "2026-11-25T10:00:00Z",
    finished_at: null,
    request_message_id: null,
    answer_message_id: null,
    ...patch,
  };
}

describe("groupPlan", () => {
  it("hides a running activity's rows and leaves one card where they were", () => {
    const user = message({ kind: "user", author_id: null, content: "查一下发布页数据" });
    const action = message({
      kind: "action",
      activity_id: "ac1",
      action: {
        id: "act1",
        run_id: "r1",
        tool_call_id: "t1",
        tool_name: "web_search",
        status: "completed",
        summary: "搜索「发布页」",
        target_agent_id: null,
        target_conversation_id: null,
        error: null,
      },
    });
    const note = message({ activity_id: "ac1", content: "我先看数据。" });
    const plan = groupPlan([user, action, note], { ac1: activity() }, "c1");
    expect(plan.messages.map((row) => row.id)).toEqual([user.id]);
    expect(plan.slots).toEqual([{ at: 1, activityID: "ac1" }]);
  });

  it("keeps the answer and puts the chip right above it once the activity is done", () => {
    const user = message({ kind: "user", author_id: null, content: "查一下" });
    const action = message({ kind: "action", activity_id: "ac1" });
    const answer = message({ activity_id: "ac1", content: "数据改成 +0.8 个百分点。", leads_to_tools: false });
    const done = activity({ status: "completed", summary: "数据改成 +0.8 个百分点…", answer_message_id: answer.id, finished_at: "2026-11-25T10:01:12Z" });
    const plan = groupPlan([user, action, answer], { ac1: done }, "c1");
    expect(plan.messages.map((row) => row.id)).toEqual([user.id, answer.id]);
    // The chip sits before the answer, so it reads above the bubble.
    expect(plan.slots).toEqual([{ at: 1, activityID: "ac1" }]);
  });

  it("keeps a consent card in place while every other row of the activity hides", () => {
    const awaiting = message({
      kind: "action",
      activity_id: "ac1",
      action: {
        id: "act1",
        run_id: "r1",
        tool_call_id: "t1",
        tool_name: "file_write",
        status: "awaiting",
        summary: "写入 pricing.csv",
        target_agent_id: null,
        target_conversation_id: null,
        error: null,
      },
    });
    const note = message({ activity_id: "ac1", content: "准备写入。" });
    const plan = groupPlan([awaiting, note], { ac1: activity({ status: "awaiting" }) }, "c1");
    expect(plan.messages.map((row) => row.id)).toEqual([awaiting.id]);
    expect(plan.slots).toEqual([{ at: 0, activityID: "ac1" }]);
  });

  it("keeps every row of old history, where no activity id was written", () => {
    const rows = [message({ content: "第一句" }), message({ content: "第二句", leads_to_tools: false })];
    const plan = groupPlan(rows, {}, "c1");
    expect(plan.messages.map((row) => row.id)).toEqual(rows.map((row) => row.id));
    expect(plan.slots).toEqual([]);
  });

  it("keeps a live activity with no rows yet as a card at the end", () => {
    const user = message({ kind: "user", author_id: null, content: "做一下" });
    const plan = groupPlan([user], { ac1: activity() }, "c1");
    expect(plan.slots).toEqual([{ at: 1, activityID: "ac1" }]);
  });

  it("ignores another conversation's live activities", () => {
    const user = message({ kind: "user", author_id: null, content: "做一下" });
    const plan = groupPlan([user], { ac1: activity({ conversation_id: "c2" }) }, "c1");
    expect(plan.slots).toEqual([]);
  });

  it("still finds the answer by its own shape before the payload lands", () => {
    const answer = message({ activity_id: "ac1", content: "好了。", leads_to_tools: false });
    const plan = groupPlan([answer], {}, "c1");
    expect(plan.messages.map((row) => row.id)).toEqual([answer.id]);
    expect(plan.slots).toEqual([{ at: 1, activityID: "ac1" }]);
  });
});

describe("card wording", () => {
  it("reads a running activity as its current step and its counts", () => {
    const running = activity();
    expect(activityStepLine(running)).toBe("正在读取 pricing.csv");
    // A ticking counter would change the line (and restart its shimmer) every few seconds.
    expect(activityStepLine(activity({ current_step: "「匠师」正在思考（12 秒）" }))).toBe("正在思考");
    expect(activityProgressLine(running, Date.parse("2026-11-25T10:00:42Z"))).toBe("3 步 · 42 秒");
    expect(isLiveActivity(running.status)).toBe(true);
  });

  it("falls back to its newest sentence, then to a quiet wait", () => {
    expect(activityStepLine(activity({ current_step: "", latest_text: "我看到两个表。" }))).toBe("我看到两个表。");
    expect(activityStepLine(activity({ current_step: "", latest_text: "", steps: 0 }))).toBe("正在开始");
    expect(activityStepLine(activity({ status: "awaiting", current_step: "", latest_text: "" }))).toBe(
      "等你确认后继续",
    );
  });

  it("collapses a finished activity to its steps and time", () => {
    const done = activity({ status: "completed", finished_at: "2026-11-25T10:01:12Z", steps: 6 });
    expect(activityChipText(done, 0)).toBe("用了 6 步 · 1 分 12 秒");
    expect(isLiveActivity(done.status)).toBe(false);
  });

  it("says what happened instead of how long a stop or an error took", () => {
    expect(activityChipText(activity({ status: "stopped", summary: "已停下：你点了停止" }), 0)).toBe(
      "已停下：你点了停止",
    );
    expect(activityChipText(activity({ status: "stopped", summary: "" }), 0)).toBe("已停下");
    expect(activityChipText(activity({ status: "error", summary: "模型连接断了" }), 0)).toBe("出错：模型连接断了");
    expect(activityChipText(activity({ status: "error", summary: "" }), 0)).toBe("出错了");
  });

  it("names each status and gives it a pill colour", () => {
    expect(ACTIVITY_STATUS_META.completed.label).toBe("已完成");
    expect(ACTIVITY_PILL[ACTIVITY_STATUS_META.completed.tone]).toBe("ok");
    expect(ACTIVITY_STATUS_META.awaiting.label).toBe("等你确认");
    expect(ACTIVITY_STATUS_META.stopped.label).toBe("已停下");
    expect(ACTIVITY_STATUS_META.error.label).toBe("出错");
    expect(ACTIVITY_STATUS_META.running.label).toBe("进行中");
  });

  it("picks a work log glyph from the row's own words, title first", () => {
    expect(activityGlyph(activity({ title: "核对发布页数据" }))).toBe("browser");
    expect(activityGlyph(activity({ title: "整理竞品截图" }))).toBe("browser");
    expect(activityGlyph(activity({ title: "补充更新日志" }))).toBe("file");
    expect(activityGlyph(activity({ title: "验证回滚脚本" }))).toBe("terminal");
    expect(activityGlyph(activity({ title: "整理会议结论" }))).toBe("chat");
    // A generic title falls back to what the row says it is doing.
    expect(activityGlyph(activity({ title: "继续", summary: "正在搜索资料" }))).toBe("search");
    expect(activityGlyph(activity({ title: "继续", summary: "", steps: 2 }))).toBe("work");
  });

});

describe("activityThinking", () => {
  it("thinks before any call and during a model call, else shows the newest call's tool", () => {
    expect(activityThinking(activity({ current_tool: "", current_step: "" }))).toBe(true);
    expect(activityThinking(activity({ current_tool: "web_search", current_step: "「匠师」正在思考（5 秒）" }))).toBe(true);
    expect(activityThinking(activity({ current_tool: "computer_shell", current_step: "运行「npm init -y」" }))).toBe(false);
  });
});

describe("groupActivitiesByDay", () => {
  const now = new Date("2026-11-25T12:00:00");

  it("splits rows into 今天, 昨天 and one header per older day", () => {
    const rows = [
      activity({ id: "a", started_at: new Date("2026-11-25T09:00:00").toISOString() }),
      activity({ id: "b", started_at: new Date("2026-11-24T20:00:00").toISOString() }),
      activity({ id: "c", started_at: new Date("2026-11-20T08:00:00").toISOString() }),
      activity({ id: "d", started_at: new Date("2026-11-25T11:30:00").toISOString() }),
      activity({ id: "e", started_at: new Date("2026-11-23T08:00:00").toISOString() }),
      activity({ id: "f", started_at: new Date("2025-12-31T08:00:00").toISOString() }),
    ];
    const groups = groupActivitiesByDay(rows, now);
    expect(groups.map((group) => group.label)).toEqual([
      "今天",
      "昨天",
      "11月23日",
      "11月20日",
      "2025年12月31日",
    ]);
    expect(groups[0]!.activities.map((row) => row.id)).toEqual(["a", "d"]);
    expect(groups[1]!.activities.map((row) => row.id)).toEqual(["b"]);
    expect(groups[2]!.activities.map((row) => row.id)).toEqual(["e"]);
    expect(groups[3]!.activities.map((row) => row.id)).toEqual(["c"]);
    expect(groups[4]!.activities.map((row) => row.id)).toEqual(["f"]);
  });

  it("leaves out a day with nothing in it", () => {
    const groups = groupActivitiesByDay([activity({ started_at: new Date("2026-11-25T09:00:00").toISOString() })], now);
    expect(groups.map((group) => group.label)).toEqual(["今天"]);
    expect(groupActivitiesByDay([], now)).toEqual([]);
  });

  it("uses local calendar days, not a fixed 24 hours, at a midnight boundary", () => {
    // 00:30 today and 23:30 yesterday: only the calendar day decides which header they join.
    const local = (day: number, hour: number, minute: number) =>
      new Date(2026, 10, day, hour, minute).toISOString();
    const groups = groupActivitiesByDay(
      [activity({ id: "today", started_at: local(25, 0, 30) }), activity({ id: "yesterday", started_at: local(24, 23, 30) })],
      new Date(2026, 10, 25, 12, 0),
    );
    expect(groups.map((group) => group.label)).toEqual(["今天", "昨天"]);
  });
});
