import { describe, expect, it } from "vitest";
import { BOTTOM_EPS, atBottom } from "./scroll";

describe("atBottom", () => {
  it("counts the exact end as the end", () => {
    expect(atBottom(1000, 600, 400)).toBe(true);
  });

  it("counts a sub-pixel or rounding gap as the end", () => {
    // Fractional scrollHeight against a rounded scrollTop: the real case that showed the button.
    expect(atBottom(1000.4, 600, 400)).toBe(true);
    expect(atBottom(1000, 600 - BOTTOM_EPS, 400)).toBe(true);
  });

  it("counts a real scroll away as away", () => {
    expect(atBottom(1000, 600 - BOTTOM_EPS - 1, 400)).toBe(false);
    expect(atBottom(1000, 100, 400)).toBe(false);
  });

  it("counts a view shorter than its box as the end", () => {
    // No scrolling possible: the reader is at the bottom of everything there is.
    expect(atBottom(200, 0, 400)).toBe(true);
  });
});
