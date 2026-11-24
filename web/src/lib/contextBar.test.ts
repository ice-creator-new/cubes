import { describe, expect, it } from "vitest";
import type { ContextCategory } from "../api/types";
import {
  contextLayout,
  fillTone,
  percentLabel,
  remainingLabel,
  shareLabel,
  tokenLabel,
} from "./contextBar";

function category(id: ContextCategory["id"], tokens: number): ContextCategory {
  return { id, label: id, tokens };
}

describe("tokenLabel", () => {
  it("writes small counts plainly and large ones as K and M", () => {
    expect(tokenLabel(0)).toBe("0");
    expect(tokenLabel(999)).toBe("999");
    expect(tokenLabel(1_000)).toBe("1K");
    expect(tokenLabel(8_600)).toBe("8.6K");
    expect(tokenLabel(293_000)).toBe("293K");
    expect(tokenLabel(557_000)).toBe("557K");
    expect(tokenLabel(1_000_000)).toBe("1M");
    expect(tokenLabel(1_500_000)).toBe("1.5M");
  });
});

describe("shareLabel", () => {
  it("keeps one decimal for small shares and rounds large ones", () => {
    expect(shareLabel(8_600, 1_000_000)).toBe("0.9%");
    expect(shareLabel(30_000, 1_000_000)).toBe("3%");
    expect(shareLabel(560_000, 1_000_000)).toBe("56%");
    expect(shareLabel(20, 1_000_000)).toBe("<0.1%");
    expect(shareLabel(100, 0)).toBe("0%");
  });
});

describe("fillTone", () => {
  it("steps at 60 and at the threshold", () => {
    expect(fillTone(59, 67)).toBe("accent");
    expect(fillTone(60, 67)).toBe("warn");
    expect(fillTone(66.9, 67)).toBe("warn");
    expect(fillTone(67, 67)).toBe("danger");
    expect(fillTone(80, 67)).toBe("danger");
  });
});

describe("contextLayout", () => {
  it("lays categories out by their share of the window", () => {
    const layout = contextLayout(
      [category("system", 100), category("user", 200)],
      1_000,
      700,
    );
    expect(layout.segments.map((segment) => [segment.id, segment.left, segment.width])).toEqual([
      ["system", 0, 10],
      ["user", 10, 20],
    ]);
    expect(layout.free).toEqual({ left: 30, width: 40 });
    expect(layout.buffer).toEqual({ left: 70, width: 30 });
    expect(layout.marker).toBe(70);
  });

  it("clips a request already past the window and leaves no free room", () => {
    const layout = contextLayout(
      [category("system", 600), category("reply", 600)],
      1_000,
      700,
    );
    expect(layout.segments.map((segment) => segment.width)).toEqual([60, 40]);
    expect(layout.free).toEqual({ left: 70, width: 0 });
    expect(layout.buffer).toEqual({ left: 70, width: 30 });
  });

  it("ignores empty categories", () => {
    const layout = contextLayout(
      [category("system", 0), category("tools", 500)],
      1_000,
      700,
    );
    expect(layout.segments.map((segment) => segment.id)).toEqual(["tools"]);
  });
});

describe("labels", () => {
  it("names the room left before the threshold, or says it was reached", () => {
    expect(percentLabel(55.7)).toBe("56%");
    expect(remainingLabel(557_000, 850_000)).toBe("还剩 293K 后自动压缩");
    expect(remainingLabel(850_000, 850_000)).toBe("已到自动压缩阈值");
  });
});
