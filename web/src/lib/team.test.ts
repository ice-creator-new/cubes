import { describe, expect, it } from "vitest";
import type { Conversation, Run, Workspace } from "../api/types";
import { activityOf, bucketLabel, budgetState, budgetTone, deltaText, formatTokens, presetsFor, relativeTime } from "./team";

function run(overrides: Partial<Run> = {}): Run {
  return {
    id: "r1",
    conversation_id: "c1",
    status: "running",
    current_step: "「小南」正在思考",
    error: null,
    model_calls: 0,
    tool_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    created_at: "2026-10-28T10:00:00.000Z",
    updated_at: "2026-10-28T10:00:00.000Z",
    finished_at: null,
    current_agent_id: null,
    pending_agent_ids: [],
    hops: 0,
    ...overrides,
  };
}

function conversation(id: string, title: string): Conversation {
  return { id, kind: "group", title, purpose: "", member_ids: [], last_message: null, created_at: "", updated_at: "" };
}

function workspace(active: Run[]): Workspace {
  return {
    agents: [],
    conversations: [conversation("c1", "市场调研"), conversation("c2", "写周报")],
    supervisor_id: "super",
    direct_conversation_id: "direct",
    onboarded: true,
    active_runs: active,
  };
}

describe("activityOf", () => {
  it("reports the member's own turn as working and not waiting", () => {
    const active = run({ current_agent_id: "a1", pending_agent_ids: ["a2"] });
    expect(activityOf("a1", workspace([active]))).toEqual({
      kind: "working",
      conversationID: "c1",
      conversationTitle: "市场调研",
      step: "「小南」正在思考",
      waitingTurn: false,
    });
  });

  it("reports a queued member as working and waiting", () => {
    const active = run({ current_agent_id: "a1", pending_agent_ids: ["a2"] });
    expect(activityOf("a2", workspace([active]))).toEqual({
      kind: "working",
      conversationID: "c1",
      conversationTitle: "市场调研",
      step: "「小南」正在思考",
      waitingTurn: true,
    });
  });

  it("prefers the member's current turn over a queued slot in another run", () => {
    const active = [
      run({ id: "r2", conversation_id: "c2", current_agent_id: "a2", pending_agent_ids: [] }),
      run({ id: "r1", conversation_id: "c1", current_agent_id: "a1", pending_agent_ids: ["a2"] }),
    ];
    expect(activityOf("a2", workspace(active))).toEqual({
      kind: "working",
      conversationID: "c2",
      conversationTitle: "写周报",
      step: "「小南」正在思考",
      waitingTurn: false,
    });
  });

  it("reports idle when the member is in no run", () => {
    expect(activityOf("a3", workspace([run({ current_agent_id: "a1" })]))).toEqual({ kind: "idle" });
  });
});

describe("formatTokens", () => {
  it("keeps counts below 1 万 as plain integers", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(9999)).toBe("9999");
  });

  it("switches to 万 at 1 万, dropping a trailing .0", () => {
    expect(formatTokens(10000)).toBe("1 万");
    expect(formatTokens(12000)).toBe("1.2 万");
    expect(formatTokens(20000)).toBe("2 万");
    expect(formatTokens(100000)).toBe("10 万");
  });

  it("switches to 亿 at 1 亿", () => {
    expect(formatTokens(100000000)).toBe("1 亿");
    expect(formatTokens(120000000)).toBe("1.2 亿");
    expect(formatTokens(250000000)).toBe("2.5 亿");
  });
});

describe("budgetState", () => {
  it("reports no budget as under", () => {
    expect(budgetState(12000, null)).toEqual({ ratio: 0, over: false });
  });

  it("marks under when used is below the limit", () => {
    expect(budgetState(5000, { limit: 10000, period: "week" })).toEqual({ ratio: 0.5, over: false });
  });

  it("marks over once used reaches the limit", () => {
    expect(budgetState(10000, { limit: 10000, period: "week" })).toEqual({ ratio: 1, over: true });
    expect(budgetState(12000, { limit: 10000, period: "week" })).toEqual({ ratio: 1.2, over: true });
  });
});

describe("relativeTime", () => {
  const now = new Date(2026, 9, 28, 12, 0, 0);
  const at = (day: number, hour: number, minute = 0, second = 0) =>
    new Date(2026, 9, day, hour, minute, second).toISOString();

  it("says 刚刚 under a minute, then minutes, then hours", () => {
    expect(relativeTime(at(28, 11, 59, 30), now)).toBe("刚刚");
    expect(relativeTime(at(28, 11, 55), now)).toBe("5分钟前");
    expect(relativeTime(at(28, 9, 0), now)).toBe("3小时前");
  });

  it("says 昨天 for the previous calendar day, then M月D日 before that", () => {
    expect(relativeTime(at(27, 23, 0), now)).toBe("昨天");
    expect(relativeTime(at(26, 23, 0), now)).toBe("10月26日");
  });
});

describe("deltaText", () => {
  it("reports none when the previous period had no usage", () => {
    expect(deltaText(0, 0, "week")).toEqual({ text: "上周没有用量", tone: "none" });
    expect(deltaText(12000, 0, "day")).toEqual({ text: "昨天没有用量", tone: "none" });
  });

  it("reports flat under a 1% change", () => {
    expect(deltaText(10050, 10000, "week")).toEqual({ text: "与上周持平", tone: "flat" });
    expect(deltaText(9950, 10000, "month")).toEqual({ text: "与上月持平", tone: "flat" });
  });

  it("reports up and down with the rounded percent", () => {
    expect(deltaText(12000, 10000, "week")).toEqual({ text: "比上周 +20%", tone: "up" });
    expect(deltaText(7000, 10000, "day")).toEqual({ text: "比昨天 −30%", tone: "down" });
  });

  it("treats exactly 1% as a real move, not flat", () => {
    expect(deltaText(10100, 10000, "week")).toEqual({ text: "比上周 +1%", tone: "up" });
    expect(deltaText(9900, 10000, "week")).toEqual({ text: "比上周 −1%", tone: "down" });
  });
});

describe("budgetTone", () => {
  it("reports none without a budget or with a zero limit", () => {
    expect(budgetTone(5000, null)).toBe("none");
    expect(budgetTone(5000, { limit: 0, period: "week" })).toBe("none");
  });

  it("reports ok below 80%", () => {
    expect(budgetTone(0, { limit: 10000, period: "week" })).toBe("ok");
    expect(budgetTone(7999, { limit: 10000, period: "week" })).toBe("ok");
  });

  it("reports warn at 80% and up to the limit", () => {
    expect(budgetTone(8000, { limit: 10000, period: "week" })).toBe("warn");
    expect(budgetTone(9999, { limit: 10000, period: "week" })).toBe("warn");
  });

  it("reports over once used reaches the limit", () => {
    expect(budgetTone(10000, { limit: 10000, period: "week" })).toBe("over");
    expect(budgetTone(12000, { limit: 10000, period: "week" })).toBe("over");
  });
});

describe("bucketLabel", () => {
  it("formats an hourly bucket as 14:00–15:00", () => {
    const start = new Date(2026, 9, 28, 14, 0).toISOString();
    expect(bucketLabel(start, "day")).toBe("14:00–15:00");
  });

  it("formats a daily bucket with the weekday", () => {
    // 2026-10-28 is a Wednesday.
    const start = new Date(2026, 9, 28, 0, 0).toISOString();
    expect(bucketLabel(start, "week")).toBe("10月28日 周三");
  });

  it("formats a monthly bucket as just the date", () => {
    const start = new Date(2026, 9, 28, 0, 0).toISOString();
    expect(bucketLabel(start, "month")).toBe("10月28日");
  });
});

describe("presetsFor", () => {
  it("sizes the quick-fill amounts to the period", () => {
    expect(presetsFor("day")).toEqual([5, 20, 50]);
    expect(presetsFor("week")).toEqual([20, 100, 300]);
    expect(presetsFor("month")).toEqual([100, 500, 1000]);
  });
});
