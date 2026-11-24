import { describe, expect, it } from "vitest";
import { RAIL_DEFAULT, RAIL_FOLDED, RAIL_MAX, RAIL_MIN, clampRailWidth } from "./railWidth";

describe("rail width", () => {
  it("keeps a dragged width between the floor and the ceiling", () => {
    expect(clampRailWidth(100, 1400)).toBe(RAIL_MIN);
    expect(clampRailWidth(900, 1400)).toBe(RAIL_MAX);
    expect(clampRailWidth(300.4, 1400)).toBe(300);
  });

  it("gives way to the chat column in a narrow window, but never below the floor", () => {
    // 720px window: at most 720 - 420 = 300 for the rail.
    expect(clampRailWidth(400, 720)).toBe(300);
    expect(clampRailWidth(400, 500)).toBe(RAIL_MIN);
  });

  it("falls back to the default for a missing or broken stored value", () => {
    expect(clampRailWidth(Number.NaN, 1400)).toBe(RAIL_DEFAULT);
  });

  it("folds wide enough to leave clear room right of the macOS traffic lights", () => {
    // Lights start at x = 18 and span about 52px: the folded rail must leave clear room after them.
    expect(RAIL_FOLDED - (18 + 52)).toBeGreaterThanOrEqual(24);
  });
});
