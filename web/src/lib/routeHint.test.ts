import { describe, expect, it } from "vitest";
import type { RouteDecision } from "../api/types";
import { routeHint, routeTitle } from "./routeHint";

const names: Record<string, string> = { a1: "匠师", a2: "乙" };
const nameOf = (id: string) => names[id];
const titles: Record<string, string> = { t1: "接真 OCR" };
const taskTitle = (id: string) => titles[id];

function route(patch: Partial<RouteDecision> = {}): RouteDecision {
  return { speakers: [], kind: "answer", reason: "问你点名的人", by: "dispatcher", ...patch };
}

describe("route hint text", () => {
  it("lists the speakers of an answer in order", () => {
    expect(routeHint(route({ speakers: ["a1", "a2"] }), nameOf, taskTitle).text).toBe("→ 匠师、乙");
  });

  it("drops unknown speaker ids rather than showing raw ids", () => {
    expect(routeHint(route({ speakers: ["a1", "gone"] }), nameOf, taskTitle).text).toBe("→ 匠师");
    expect(routeHint(route({ speakers: ["gone"] }), nameOf, taskTitle).text).toBe("→ 等成员回答");
  });

  it("names the task for a steer and a resume", () => {
    expect(routeHint(route({ kind: "steer", task_id: "t1" }), nameOf, taskTitle).text).toBe("→ 并入「接真 OCR」");
    expect(routeHint(route({ kind: "resume", task_id: "t1" }), nameOf, taskTitle).text).toBe(
      "→ 接着做「接真 OCR」",
    );
    expect(routeHint(route({ kind: "steer", task_id: "missing" }), nameOf, taskTitle).text).toBe("→ 并入「那件事」");
  });

  it("reads status and silence as their own lines", () => {
    expect(routeHint(route({ kind: "status" }), nameOf).text).toBe("→ 进度");
    expect(routeHint(route({ kind: "none" }), nameOf).text).toBe("→ 不需要回复");
  });

  it("says routing is still running instead of naming anyone", () => {
    const hint = routeHint(route({ pending: true, speakers: ["a1"] }), nameOf, taskTitle);
    expect(hint.text).toBe("正在决定谁来接");
    expect(hint.title).toBe("正在判断这条消息该交给谁");
  });
});

describe("route hint tooltip", () => {
  it("puts the dispatcher's reason after its own prefix", () => {
    expect(routeTitle(route({ reason: "问的是方案对比" }))).toBe("自动判断：问的是方案对比");
  });

  it("names the fallback and the two user-driven deciders", () => {
    expect(routeTitle(route({ by: "fallback", reason: "" }))).toBe("没判断出来，交给了上一位说话的人");
    expect(routeTitle(route({ by: "fallback", reason: "没有可用的分发" }))).toBe(
      "没判断出来，交给了上一位说话的人：没有可用的分发",
    );
    expect(routeTitle(route({ by: "mention", reason: "你 @ 了匠师" }))).toBe("你点了名：你 @ 了匠师");
    expect(routeTitle(route({ by: "user", reason: "你改成了乙" }))).toBe("你改过：你改成了乙");
  });

  it("keeps a bare prefix when there is no reason", () => {
    expect(routeTitle(route({ by: "dispatcher", reason: "" }))).toBe("自动判断");
    expect(routeTitle(route({ by: "mention", reason: "  " }))).toBe("你点了名");
  });
});

describe("route hint with several intents", () => {
  it("writes one line per ask, in order, and an invite line", () => {
    const hint = routeHint(
      route({
        speakers: ["a1", "a2"],
        intents: [
          { text: "不要用 flutter 了，改 react", kind: "answer", speakers: ["a1"], invite: [] },
          { text: "让画师辅助匠师", kind: "invite", speakers: ["a2"], invite: ["a2"] },
        ],
      }),
      nameOf,
      taskTitle,
    );
    expect(hint.lines).toEqual([
      "“不要用 flutter 了，改 react” → 匠师",
      "邀请 乙 → 乙",
    ]);
    expect(hint.text).toBe("“不要用 flutter 了，改 react” → 匠师 · 邀请 乙 → 乙");
  });

  it("keeps the single-line look when there is only one intent", () => {
    expect(
      routeHint(route({ speakers: ["a1"], intents: [{ text: "改 react", kind: "answer", speakers: ["a1"], invite: [] }] }), nameOf)
        .lines,
    ).toBeUndefined();
  });
});
