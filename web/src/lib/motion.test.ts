import { describe, expect, it } from "vitest";
import { T_BASE, flipOffset, heightMotion } from "./motion";

describe("heightMotion", () => {
  it("snaps on the first measurement and when nothing moved", () => {
    expect(heightMotion({ from: null, to: 36, elapsed: 500, line: 22 })).toEqual({
      animate: false,
      duration: 0,
    });
    expect(heightMotion({ from: 36, to: 36, elapsed: 500, line: 22 })).toEqual({
      animate: false,
      duration: 0,
    });
  });

  it("glides a deliberate change", () => {
    expect(heightMotion({ from: 36, to: 102, elapsed: 400, line: 22 })).toEqual({
      animate: true,
      duration: T_BASE,
    });
    // One line added after a pause is still deliberate.
    expect(heightMotion({ from: 36, to: 58, elapsed: 300, line: 22 })).toEqual({
      animate: true,
      duration: T_BASE,
    });
  });

  it("snaps one line of growth inside a key repeat, so typing never trails the caret", () => {
    expect(heightMotion({ from: 36, to: 58, elapsed: 30, line: 22 })).toEqual({
      animate: false,
      duration: 0,
    });
    expect(heightMotion({ from: 36, to: 66, elapsed: 30, line: 22 })).toEqual({
      animate: false,
      duration: 0,
    });
  });

  it("still glides a jump inside a key repeat: a paste is not a repeat", () => {
    expect(heightMotion({ from: 36, to: 150, elapsed: 20, line: 22 })).toEqual({
      animate: true,
      duration: T_BASE,
    });
  });

  it("never animates when the system asks for less motion", () => {
    expect(heightMotion({ from: 36, to: 150, elapsed: 500, line: 22, reduced: true })).toEqual({
      animate: false,
      duration: 0,
    });
  });
});

describe("flipOffset", () => {
  it("ignores a box that did not really move", () => {
    expect(flipOffset({ left: 10, top: 20 }, { left: 10.2, top: 19.8 })).toBeNull();
  });

  it("returns the distance back to the old spot", () => {
    expect(flipOffset({ left: 10, top: 120 }, { left: 10, top: 40 })).toEqual({ dx: 0, dy: 80 });
    expect(flipOffset({ left: 64, top: 40 }, { left: 10, top: 40 })).toEqual({ dx: 54, dy: 0 });
  });
});
