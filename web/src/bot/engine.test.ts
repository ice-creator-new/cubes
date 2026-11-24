import { describe, expect, it } from "vitest";
import { MOODS, SHAPES, type Appearance, type MoodTag } from "./catalog";
import { createBot, type Bot, type GestureKind } from "./engine";
import { eyeOutline, FIT_KEYS, fitOffset } from "./face";
import { CENTER, PROFILES, radiusAtAngle } from "./shapes";
import { clamp, easeOutQuint } from "./math";
import type { Mood } from "../lib/mood";

function appearanceFor(shape: Appearance["shape"]): Appearance {
  return { shape, color: "clay" };
}

const MOOD_LIST: Mood[] = ["idle", "thinking", "speaking", "error", ...MOODS];

/** Worst distance by which an eye sticks out of its silhouette, in viewBox units. */
function overflowOf(bot: Bot, shape: Appearance["shape"], t: number): number {
  const profile = PROFILES[shape];
  let worst = Number.NEGATIVE_INFINITY;
  for (const eye of bot.eyes(t)) {
    if (eye.alpha < 0.02) continue;
    for (const point of eyeOutline(eye)) {
      const dx = point.x - CENTER.x;
      const dy = point.y - CENTER.y;
      const radius = radiusAtAngle(profile, Math.atan2(dy, dx));
      worst = Math.max(worst, Math.hypot(dx, dy) - radius);
    }
  }
  return worst;
}

function worstOverTime(bot: Bot, shape: Appearance["shape"], from: number, to: number, step = 0.05): number {
  let worst = Number.NEGATIVE_INFINITY;
  for (let t = from; t <= to; t += step) worst = Math.max(worst, overflowOf(bot, shape, t));
  return worst;
}

describe("sample", () => {
  it("is a pure function of t", () => {
    const bot = createBot({ appearance: appearanceFor("pebble"), seed: "purity", mood: "thinking" });
    const first = bot.sample(1.2);
    const again = bot.sample(1.2);
    expect(again).toEqual(first);
    bot.sample(0.3);
    bot.sample(7.5);
    expect(bot.sample(1.2)).toEqual(first);
  });

  it("draws a body and two eyes in the 100-unit viewBox", () => {
    const bot = createBot({ appearance: appearanceFor("teardrop"), seed: "inkling", mood: "happy" });
    const frame = bot.sample(0.4);
    expect(frame.body.startsWith("M")).toBe(true);
    expect(frame.body.endsWith("Z")).toBe(true);
    expect(frame.eyes).toHaveLength(2);
    for (const eye of frame.eyes) {
      expect(eye.d.startsWith("M")).toBe(true);
      expect(eye.matrix.startsWith("matrix(")).toBe(true);
      expect(eye.alpha).toBeGreaterThan(0);
    }
    expect(frame.dots).toHaveLength(0);
    expect(frame.notif).toBeNull();
    const thinking = createBot({ appearance: appearanceFor("teardrop"), seed: "inkling", mood: "thinking" });
    expect(thinking.sample(1.5).dots.length).toBeGreaterThan(0);
  });

  it("gives every glyph a finite matrix", () => {
    for (const shape of SHAPES) {
      for (const mood of MOOD_LIST) {
        const bot = createBot({ appearance: appearanceFor(shape), seed: `${shape}|${mood}`, mood });
        for (let t = 0; t < 4; t += 0.37) {
          for (const eye of bot.sample(t).eyes) {
            for (const value of eye.matrix.slice(7, -1).split(",")) expect(Number.isFinite(Number(value))).toBe(true);
          }
        }
      }
    }
  });
});

describe("eye fit", () => {
  it("keeps both eyes inside every silhouette, face and idle frame", () => {
    for (const shape of SHAPES) {
      for (const mood of MOOD_LIST) {
        const bot = createBot({ appearance: appearanceFor(shape), seed: `${shape}|${mood}`, mood });
        expect(worstOverTime(bot, shape, 0, 12), `${shape} ${mood}`).toBeLessThanOrEqual(0.5);
      }
    }
  });

  it("keeps eyes inside while a member speaks, when the nod adds to the drift", () => {
    for (const shape of SHAPES) {
      const bot = createBot({ appearance: appearanceFor(shape), seed: `speak|${shape}`, mood: "speaking" });
      expect(worstOverTime(bot, shape, 0, 8), shape).toBeLessThanOrEqual(0.5);
    }
  });

  it("keeps the chevron error face inside every silhouette", () => {
    for (const shape of SHAPES) {
      const bot = createBot({ appearance: appearanceFor(shape), seed: `error|${shape}`, mood: "error" });
      expect(worstOverTime(bot, shape, 0, 8), shape).toBeLessThanOrEqual(0.5);
    }
  });

  it("keeps eyes inside across the whole pointer envelope, every shape and face", () => {
    for (const shape of SHAPES) {
      for (const mood of MOOD_LIST) {
        for (const [yaw, pitch] of [
          [16, 0],
          [-16, 0],
          [0, 13],
          [0, -13],
          [16, 13],
          [-16, -13],
        ] as const) {
          const bot = createBot({ appearance: appearanceFor(shape), seed: `look|${shape}|${mood}`, mood });
          bot.look({ yaw, pitch, mix: 1, spin: 0, wander: 0.2 }, 0);
          expect(worstOverTime(bot, shape, 0.3, 6, 0.1), `${shape} ${mood} ${yaw},${pitch}`).toBeLessThanOrEqual(0.6);
        }
      }
    }
  });

  it("keeps eyes inside through a shape morph", () => {
    for (const from of SHAPES) {
      for (const to of SHAPES) {
        const bot = createBot({ appearance: appearanceFor(from), seed: `morph|${from}|${to}`, mood: "surprised" });
        bot.sample(0);
        bot.set({ appearance: appearanceFor(to) }, 1);
        // The silhouette being drawn is the two profiles blended on the engine's own easing,
        // so measure against that: it is the body the eyes actually sit on.
        let worst = Number.NEGATIVE_INFINITY;
        for (let step = 0; step <= 60; step += 1) {
          const t = 1 + step / 60;
          const eased = easeOutQuint(clamp((t - 1) / 0.45));
          const drawn = PROFILES[from].map((radius, index) => radius + (PROFILES[to][index]! - radius) * eased);
          for (const eye of bot.eyes(t)) {
            if (eye.alpha < 0.02) continue;
            for (const point of eyeOutline(eye)) {
              const dx = point.x - CENTER.x;
              const dy = point.y - CENTER.y;
              const room = radiusAtAngle(drawn, Math.atan2(dy, dx));
              worst = Math.max(worst, Math.hypot(dx, dy) - room);
            }
          }
        }
        expect(worst, `${from} -> ${to}`).toBeLessThanOrEqual(0.6);
      }
    }
  });

  it("keeps the eyes well clear of the outline at rest, notches included", () => {
    for (const shape of SHAPES) {
      for (const mood of ["idle", "happy", "sorry"] as Mood[]) {
        const bot = createBot({ appearance: appearanceFor(shape), seed: `clear|${shape}`, mood });
        for (const t of [0.4, 1.3, 2.9]) {
          for (const eye of bot.eyes(t)) {
            if (eye.alpha < 0.02) continue;
            for (const point of eyeOutline(eye)) {
              const dx = point.x - CENTER.x;
              const dy = point.y - CENTER.y;
              const clearance = radiusAtAngle(PROFILES[shape], Math.atan2(dy, dx)) - Math.hypot(dx, dy);
              // A face may ride the contour when it is turned away, but a resting eye never
              // sits in a notch: a fifth of the body's radius of air is the floor.
              expect(clearance, `${shape} ${mood} t=${t}`).toBeGreaterThanOrEqual(1.5);
            }
          }
        }
      }
    }
  });

  it("covers every face the engine can ask for", () => {
    for (const shape of SHAPES) {
      for (const key of FIT_KEYS) {
        const [left, right] = [fitOffset(shape, key, 0), fitOffset(shape, key, 1)];
        for (const point of [left, right]) {
          expect(Number.isFinite(point.x) && Number.isFinite(point.y)).toBe(true);
          expect(Math.abs(point.x)).toBeLessThan(30);
          expect(Math.abs(point.y)).toBeLessThan(30);
        }
      }
    }
  });
});

describe("transitions", () => {
  it("glides between moods of the same eye kind", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "switcher" });
    bot.sample(0);
    bot.set({ mood: "curious" }, 1);
    let previous = bot.eyes(1);
    for (let step = 0; step <= 60; step += 1) {
      const t = 1 + step / 60;
      const eyes = bot.eyes(t);
      expect(bot.sample(t).eyeAlpha).toBeCloseTo(1, 6);
      for (const index of [0, 1] as const) {
        expect(Math.hypot(eyes[index].cx - previous[index].cx, eyes[index].cy - previous[index].cy)).toBeLessThanOrEqual(3);
      }
      previous = eyes;
    }
  });

  it("masks a change of eye kind with a blink instead of a crossfade", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "kinds" });
    bot.sample(0);
    bot.set({ mood: "happy" }, 1);
    let previous = bot.eyes(1);
    let squashedWhileSwapping = false;
    let minimumHeight = Number.POSITIVE_INFINITY;
    for (let step = 0; step <= 40; step += 1) {
      const t = 1 + step / 40;
      const eyes = bot.eyes(t);
      const frame = bot.sample(t);
      // The face never fades out: eyes stay opaque, the lid does the masking.
      expect(frame.eyeAlpha).toBeCloseTo(1, 6);
      const jump = Math.max(
        Math.hypot(eyes[0].cx - previous[0].cx, eyes[0].cy - previous[0].cy),
        Math.hypot(eyes[1].cx - previous[1].cx, eyes[1].cy - previous[1].cy),
      );
      minimumHeight = Math.min(minimumHeight, eyeOutline(eyes[0]).reduce((span, point) => Math.max(span, point.y), -Infinity) - eyeOutline(eyes[0]).reduce((span, point) => Math.min(span, point.y), Infinity));
      if (jump > 3) {
        // A jump is only allowed while the lid is shut enough to hide it.
        for (const eye of eyes) {
          const outline = eyeOutline(eye);
          const height = Math.max(...outline.map((point) => point.y)) - Math.min(...outline.map((point) => point.y));
          expect(height).toBeLessThan(eye.h * 0.45);
        }
        squashedWhileSwapping = true;
      }
      previous = eyes;
    }
    // The face swaps behind a shut lid: either the centres jump while the lid hides it, or
    // the whole transition stays put. The lid itself must close either way.
    expect(minimumHeight).toBeLessThan(6);
    expect(squashedWhileSwapping || minimumHeight < 6).toBe(true);
  });

  it("keeps the surprised eyes apart on every silhouette", () => {
    for (const shape of SHAPES) {
      const bot = createBot({ appearance: appearanceFor(shape), seed: "dots", mood: "surprised" });
      const eyes = bot.eyes(0.4);
      const left = eyeOutline(eyes[0]).map((point) => point.x);
      const right = eyeOutline(eyes[1]).map((point) => point.x);
      expect(Math.max(...left), shape).toBeLessThan(Math.min(...right));
    }
  });

  it("settles on the requested mood after the transition window", () => {
    const bot = createBot({ appearance: appearanceFor("hexagon"), seed: "settle" });
    bot.set({ mood: "focused" }, 2);
    const settled = bot.eyes(2.5);
    const direct = createBot({ appearance: appearanceFor("hexagon"), seed: "settle", mood: "focused" }).eyes(2.5);
    expect(settled[0].h).toBeCloseTo(direct[0].h, 6);
    expect(settled[0].cx).toBeCloseTo(direct[0].cx, 6);
  });

  it("stays continuous under a stream of mood changes", () => {
    const moods: Mood[] = ["idle", "happy", "thinking", "error", "speaking", "sorry"];
    const bot = createBot({ appearance: appearanceFor("clover"), seed: "storm" });
    let previous = bot.eyes(0);
    for (let step = 0; step < 240; step += 1) {
      const t = step / 60;
      if (step % 17 === 0) bot.set({ mood: moods[(step / 17) % moods.length] }, t);
      const eyes = bot.eyes(t);
      for (const index of [0, 1] as const) {
        const jump = Math.hypot(eyes[index].cx - previous[index].cx, eyes[index].cy - previous[index].cy);
        // The gesture-free pose only moves by drift, the blob morph and the thinking shrink.
        expect(jump, `t=${t.toFixed(2)}`).toBeLessThan(9);
      }
      previous = eyes;
    }
  });
});

describe("purity with gestures and looks", () => {
  it("replays a whole session identically", () => {
    const script: { at: number; run: (bot: Bot) => void }[] = [
      { at: 0, run: (bot) => bot.gesture("wake" as GestureKind, 0) },
      { at: 2, run: (bot) => bot.look({ yaw: 12, pitch: -6, mix: 1, spin: 0, wander: 0.3 }, 2) },
      { at: 3, run: (bot) => bot.set({ mood: "thinking" }, 3) },
      { at: 5, run: (bot) => bot.gesture("notice", 5) },
      { at: 7, run: (bot) => bot.set({ mood: "happy" }, 7) },
      { at: 8, run: (bot) => bot.gesture("cheer", 8) },
    ];
    const run = () => {
      const bot = createBot({ appearance: appearanceFor("blob"), seed: "replay" });
      const frames: string[] = [];
      for (let t = 0; t <= 10; t += 0.1) {
        for (const entry of script) if (Math.abs(entry.at - t) < 1e-9) entry.run(bot);
        frames.push(JSON.stringify(bot.sample(Number(t.toFixed(1)))));
      }
      return frames;
    };
    expect(run()).toEqual(run());
  });

  it("does not change when a different date is sampled in between", () => {
    const bot = createBot({ appearance: appearanceFor("squircle"), seed: "peek" });
    bot.gesture("notice", 1);
    bot.look({ yaw: -10, pitch: 8, mix: 1, spin: 0, wander: 0.4 }, 1.5);
    const first = bot.sample(2.2);
    bot.sample(9);
    bot.sample(0.2);
    expect(bot.sample(2.2)).toEqual(first);
  });
});

/** The palette must stay readable while the body is shaded: a face is drawn on top of it. */
describe("automatic faces", () => {
  it("never asks the appearance for an expression", () => {
    const appearance: Appearance = { shape: "round", color: "clay" };
    expect(Object.keys(appearance).sort()).toEqual(["color", "shape"]);
    const neutral = createBot({ appearance, seed: "auto" }).eyes(0.5);
    const happy = createBot({ appearance, seed: "auto", mood: "happy" as MoodTag }).eyes(0.5);
    expect(happy[0].kind).not.toBe(neutral[0].kind);
  });
});
