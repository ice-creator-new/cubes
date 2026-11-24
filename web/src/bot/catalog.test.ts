import { describe, expect, it } from "vitest";
import { COLORS, MOODS, SHAPES, appearanceFromSeed, resolveAppearance } from "./catalog";
import { EXPRESSION_PARAMS } from "./expressions";
import { contrast } from "./math";
import { DARK_CANVAS } from "./engine";
import { SWATCHES, swatchOf } from "./palette";

describe("catalog", () => {
  it("matches the shared appearance catalogue item for item", () => {
    expect([...SHAPES]).toEqual([
      "round",
      "blob",
      "squircle",
      "sparkle",
      "clover",
      "teardrop",
      "flower",
      "peak",
      "capsule",
      "arch",
      "pentagon",
      "star",
      "hexagon",
      "pebble",
      "heart",
      "egg",
      "diamond",
      "octagon",
      "cat",
      "bear",
      "ghost",
    ]);
    expect([...COLORS]).toEqual([
      "ink",
      "cream",
      "clay",
      "coral",
      "tangerine",
      "honey",
      "moss",
      "jade",
      "sky",
      "iris",
      "plum",
      "slate",
    ]);
    expect([...MOODS]).toEqual(["neutral", "happy", "proud", "curious", "confused", "sorry", "surprised", "focused"]);
  });
});

describe("appearanceFromSeed", () => {
  it("is deterministic", () => {
    for (const seed of ["a", "member-1", "主管", ""]) {
      expect(appearanceFromSeed(seed)).toEqual(appearanceFromSeed(seed));
    }
  });

  it("never lands on ink, and only uses values from the catalogue", () => {
    for (let index = 0; index < 500; index += 1) {
      const appearance = appearanceFromSeed(`seed-${index}`);
      expect(appearance.color).not.toBe("ink");
      expect(COLORS).toContain(appearance.color);
      expect(SHAPES).toContain(appearance.shape);
    }
  });

  it("keeps stored values and falls back per field when they are missing", () => {
    expect(resolveAppearance({ shape: "clover", color: "jade" }, "x")).toEqual({ shape: "clover", color: "jade" });
    expect(resolveAppearance(null, "x")).toEqual(appearanceFromSeed("x"));
    expect(resolveAppearance({ shape: "clover", color: "jade" }, undefined).shape).toBe("clover");
    expect(resolveAppearance({ shape: "nope", color: "mustard" }, "x")).toEqual(appearanceFromSeed("x"));
  });

  it("keeps a free body colour and a chosen eye, and drops only what is unusable", () => {
    expect(resolveAppearance({ shape: "round", color: "#12AB9F", eye: "#FFF0E0" }, "x")).toEqual({
      shape: "round",
      color: "#12ab9f",
      eye: "#fff0e0",
    });
    // A broken eye keeps the body and falls back to the automatic eye.
    expect(resolveAppearance({ shape: "round", color: "jade", eye: "red" }, "x")).toEqual({ shape: "round", color: "jade" });
    // A broken body colour falls back to the seed's colour, not the whole face.
    expect(resolveAppearance({ shape: "round", color: "#abc" }, "x")).toEqual({
      shape: "round",
      color: appearanceFromSeed("x").color,
    });
  });

  it("maps the retired silhouettes and ignores a retired expression", () => {
    for (const [legacy, current] of [
      ["bean", "blob"],
      ["cloud", "clover"],
      ["drop", "teardrop"],
      ["lean", "arch"],
    ] as const) {
      expect(resolveAppearance({ shape: legacy, color: "jade" }, "x")).toEqual({ shape: current, color: "jade" });
    }
    // A stored row from an older build carries an expression; it is simply not read.
    const stored = resolveAppearance({ shape: "hexagon", color: "plum", expression: "sleepy" } as never, "x");
    expect(stored).toEqual({ shape: "hexagon", color: "plum" });
  });
});

describe("palette", () => {
  it("gives every body colour an eye colour at 4.5:1 or better", () => {
    for (const color of COLORS) {
      expect(contrast(SWATCHES[color].fill, SWATCHES[color].eyeFill)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("paints a chosen eye as is and finds a readable eye for any free body", () => {
    expect(swatchOf({ color: "jade", eye: "#ff0000" })).toEqual({ fill: SWATCHES.jade.fill, eyeFill: "#ff0000" });
    expect(swatchOf({ color: "jade" })).toEqual(SWATCHES.jade);
    for (const fill of ["#000000", "#ffffff", "#808080", "#ff00ff", "#00ff00", "#2244aa"] as const) {
      const swatch = swatchOf({ color: fill });
      expect(swatch.fill).toBe(fill);
      expect(contrast(swatch.fill, swatch.eyeFill), fill).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("keeps every body readable against both canvases", () => {
    for (const color of [
      "clay",
      "coral",
      "tangerine",
      "honey",
      "moss",
      "jade",
      "sky",
      "iris",
      "plum",
      "slate",
      "ink",
      "cream",
    ] as const) {
      expect(contrast(SWATCHES[color].fill, DARK_CANVAS)).toBeGreaterThan(1.4);
      expect(contrast(SWATCHES[color].fill, "#f3f2f0")).toBeGreaterThan(1.3);
    }
  });
});

describe("expressions", () => {
  it("never tilts a capsule whose aspect ratio is close to square", () => {
    for (const [key, expression] of Object.entries(EXPRESSION_PARAMS)) {
      for (const eye of expression.eyes) {
        const tilt = Math.abs(eye.tilt + expression.gaze.roll);
        if (eye.kind !== "capsule" || tilt <= 0.03) continue;
        const ratio = eye.w / (eye.h * Math.max(0.2, eye.open));
        expect(Math.abs(ratio - 1), `${key} eye ${eye.w}x${eye.h}`).toBeGreaterThanOrEqual(0.15);
      }
    }
  });

  it("keeps every pair of expressions distinguishable by kind or by large geometry", () => {
    const keys = Object.keys(EXPRESSION_PARAMS) as (keyof typeof EXPRESSION_PARAMS)[];
    const differs = (a: (typeof EXPRESSION_PARAMS)[keyof typeof EXPRESSION_PARAMS], b: typeof a): boolean => {
      if (a.eyes[0].kind !== b.eyes[0].kind || a.eyes[1].kind !== b.eyes[1].kind) return true;
      if (Math.abs(a.split - b.split) >= 0.05) return true;
      if (Math.abs(a.gaze.yaw - b.gaze.yaw) >= 0.1 || Math.abs(a.gaze.pitch - b.gaze.pitch) >= 0.1) return true;
      return a.eyes.some((eye, index) => {
        const other = b.eyes[index as 0 | 1];
        return Math.abs(eye.w - other.w) >= 6 || Math.abs(eye.h - other.h) >= 6 || Math.abs(eye.dy - other.dy) >= 2;
      });
    };
    for (let i = 0; i < keys.length; i += 1) {
      for (let j = i + 1; j < keys.length; j += 1) {
        const left = EXPRESSION_PARAMS[keys[i]!];
        expect(differs(left, EXPRESSION_PARAMS[keys[j]!]), `${keys[i]} vs ${keys[j]}`).toBe(true);
      }
    }
  });
});
