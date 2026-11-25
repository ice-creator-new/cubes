import { describe, expect, it } from "vitest";
import type { StatusCard } from "../api/types";
import { statusCardRows, statusStepLine } from "./statusCard";

const names: Record<string, string> = { a1: "匠师", a2: "乙" };
const nameOf = (id: string) => names[id];

function card(patch: Partial<StatusCard> = {}): StatusCard {
  return {
    tasks: [
      { owner: "a1", title: "接真 OCR", status: "running", step: 2, current: "跑测试", latest: "在核对发票号", waiting_for: null },
      { owner: "a2", title: "整理合同", status: "done", step: 4, current: "", latest: "整理完了", waiting_for: null },
    ],
    idle: ["a2"],
    ...patch,
  };
}

describe("status card rows", () => {
  it("resolves owner ids to names and keeps payload order", () => {
    const { rows } = statusCardRows(card(), nameOf);
    expect(rows.map((row) => row.ownerName)).toEqual(["匠师", "乙"]);
    expect(rows[0]).toMatchObject({
      ownerID: "a1",
      title: "接真 OCR",
      label: "进行中",
      tone: "running",
      stepLine: "第 3 步 · 当前：跑测试",
      latest: "在核对发票号",
    });
  });

  it("shows an unresolved owner as itself, not as a blank face", () => {
    const { rows } = statusCardRows(
      card({ tasks: [{ owner: "新来的", title: "x", status: "queued", step: 0, current: "", latest: "", waiting_for: null }] }),
      nameOf,
    );
    expect(rows[0]).toMatchObject({ ownerID: null, ownerName: "新来的", stepLine: "排队中" });
  });

  it("resolves idle names and passes through unknown ones", () => {
    expect(statusCardRows(card(), nameOf).idleNames).toEqual(["乙"]);
    expect(statusCardRows(card({ idle: ["a1", "外部"] }), nameOf).idleNames).toEqual(["匠师", "外部"]);
  });
});

describe("live work on the card", () => {
  it("lists work running in the group with its board, so the card is not empty", () => {
    const view = statusCardRows(
      {
        tasks: [],
        idle: ["主簿", "匠师"],
        activities: [
          {
            activity_id: "ac1",
            agent: "军师",
            title: "出技术方案",
            current_step: "「军师」正在思考（10 秒）",
            latest: "",
            todos: [
              { text: "读代码", status: "done" },
              { text: "列风险", status: "doing" },
              { text: "写方案", status: "pending" },
            ],
            todo_done: 1,
            todo_total: 3,
          },
        ],
      },
      () => undefined,
      (name) => (name === "军师" ? "a_js" : undefined),
    );
    expect(view.live).toHaveLength(1);
    expect(view.live[0]).toMatchObject({ ownerID: "a_js", current: "正在思考" });
    expect(view.live[0]!.todos.map((todo) => todo.text)).toEqual(["读代码", "列风险", "写方案"]);
  });
});

describe("status step line", () => {
  it("counts live steps from one and totals a finished task", () => {
    expect(statusStepLine({ owner: "a1", title: "t", status: "running", step: 0, current: "查资料", latest: "", waiting_for: null })).toBe(
      "第 1 步 · 当前：查资料",
    );
    expect(statusStepLine({ owner: "a1", title: "t", status: "done", step: 4, current: "", latest: "", waiting_for: null })).toBe(
      "共 4 步",
    );
  });

  it("says where a failure stopped, and nothing when it never started", () => {
    expect(statusStepLine({ owner: "a1", title: "t", status: "failed", step: 2, current: "", latest: "", waiting_for: null })).toBe(
      "停在第 3 步",
    );
    expect(statusStepLine({ owner: "a1", title: "t", status: "stopped", step: 0, current: "", latest: "", waiting_for: null })).toBe(
      "",
    );
  });
});
