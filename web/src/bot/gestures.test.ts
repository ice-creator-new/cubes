import { describe, expect, it } from "vitest";
import { SHAPES, type Appearance } from "./catalog";
import { createBot, type Frame, type GestureKind } from "./engine";
import { eyeOutline, type EyePose } from "./face";
import { CENTER, PROFILES, radiusAtAngle } from "./shapes";

const KINDS: GestureKind[] = ["wake", "notice", "nod", "cheer", "wince", "sigh", "think"];

function appearanceFor(shape: Appearance["shape"]): Appearance {
  return { shape, color: "clay" };
}

function transformOf(frame: Frame): number[] {
  return frame.transform
    .slice(7, -1)
    .split(",")
    .map((value) => Number(value));
}

/** Largest single-frame jump in eye centres, ignoring eyes that are faded out. */
function eyeJump(before: [EyePose, EyePose], after: [EyePose, EyePose]): number {
  let jump = 0;
  for (const index of [0, 1] as const) {
    if (after[index].alpha < 0.2 || before[index].alpha < 0.2) continue;
    jump = Math.max(jump, Math.hypot(after[index].cx - before[index].cx, after[index].cy - before[index].cy));
  }
  return jump;
}

function eyeBoxY(eye: EyePose): number {
  const points = eyeOutline(eye);
  return Math.max(...points.map((point) => point.y)) - Math.min(...points.map((point) => point.y));
}

describe("gestures", () => {
  it("starts and ends continuous, and lands back on the mood pose", () => {
    for (const kind of KINDS) {
      const bot = createBot({ appearance: appearanceFor("round"), seed: `gesture-${kind}` });
      const control = createBot({ appearance: appearanceFor("round"), seed: `gesture-${kind}` });
      // The first frame without the gesture, so the layering starts from the on-screen pose.
      const frames: Frame[] = [];
      for (let step = 0; step < 40; step += 1) frames.push(bot.sample(0.5 + step / 60));
      bot.gesture(kind, 1.2);
      let previousEyes = bot.eyes(1.2);
      let previousFrame = bot.sample(1.2);
      for (let step = 1; step <= 150; step += 1) {
        const t = 1.2 + step / 60;
        const frame = bot.sample(t);
        const eyes = bot.eyes(t);
        expect(eyeJump(previousEyes, eyes), `${kind} at ${t.toFixed(2)} eyes`).toBeLessThan(12);
        const now = transformOf(frame);
        const was = transformOf(previousFrame);
        for (let index = 0; index < 6; index += 1) {
          expect(Math.abs(now[index]! - was[index]!), `${kind} at ${t.toFixed(2)} body`).toBeLessThan(4);
        }
        previousEyes = eyes;
        previousFrame = frame;
      }
      // Well after the gesture, the bot is exactly the one that never gestured.
      const settled = 1.2 + 2.2;
      expect(bot.sample(settled)).toEqual(control.sample(settled));
    }
  });

  it("keeps the eyes inside the body through every gesture", () => {
    for (const shape of SHAPES) {
      for (const kind of KINDS) {
        const bot = createBot({ appearance: appearanceFor(shape), seed: `fit-${kind}-${shape}` });
        bot.gesture(kind, 0.5);
        let worst = Number.NEGATIVE_INFINITY;
        for (let step = 0; step <= 160; step += 1) {
          const t = 0.5 + step / 60;
          for (const eye of bot.eyes(t)) {
            if (eye.alpha < 0.02) continue;
            for (const point of eyeOutline(eye)) {
              const dx = point.x - CENTER.x;
              const dy = point.y - CENTER.y;
              const radius = radiusAtAngle(PROFILES[shape], Math.atan2(dy, dx));
              worst = Math.max(worst, Math.hypot(dx, dy) - radius);
            }
          }
        }
        expect(worst, `${kind} ${shape}`).toBeLessThanOrEqual(0.6);
      }
    }
  });

  it("wake glances around and settles", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "wake" });
    bot.gesture("wake", 0);
    const seen = new Set<number>();
    for (let step = 0; step <= 90; step += 1) {
      seen.add(Math.round(bot.eyes(step / 60)[0].cx));
    }
    // The pair sweeps across a wide band of the face rather than drifting in place.
    const spread = Math.max(...seen) - Math.min(...seen);
    expect(spread).toBeGreaterThan(20);
  });

  it("notice widens the eyes and pops a pastille on the body edge", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "notice" });
    const before = bot.sample(0.1);
    expect(before.notif).toBeNull();
    bot.gesture("notice", 1);
    let widest = 0;
    let pastille = 0;
    for (let step = 0; step <= 70; step += 1) {
      const frame = bot.sample(1 + step / 60);
      widest = Math.max(widest, eyeBoxY(bot.eyes(1 + step / 60)[0]));
      if (frame.notif) pastille = Math.max(pastille, frame.notif.r);
    }
    expect(widest).toBeGreaterThan(20);
    expect(pastille).toBeGreaterThan(7);
  });

  it("cheer hops the body up and then lands back on the base line", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "cheer" });
    bot.gesture("cheer", 0);
    const offsets: number[] = [];
    for (let step = 0; step <= 70; step += 1) offsets.push(transformOf(bot.sample(step / 60))[5]!);
    const lowest = Math.min(...offsets);
    const highest = Math.max(...offsets);
    expect(highest - lowest).toBeGreaterThan(4);
    // Back on the base line: the transform's offset returns to (almost) nothing.
    expect(Math.abs(offsets[offsets.length - 1]!)).toBeLessThan(1.5);
    expect(lowest).toBeLessThan(-4);
  });

  it("sleep closes the eyes, stays, and waking restores the pose", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "sleepy" });
    const control = createBot({ appearance: appearanceFor("round"), seed: "sleepy" });
    bot.gesture("sleep", 0);
    const asleep = bot.eyes(4);
    expect(asleep[0].kind).toBe("line");
    expect(eyeBoxY(asleep[0])).toBeLessThan(6);
    // A sleeping bot sinks a little and breathes.
    expect(transformOf(bot.sample(4))[5]!).toBeGreaterThan(0.8);
    bot.gesture("wake", 6);
    const awake = bot.eyes(8);
    expect(awake[0].kind).toBe("capsule");
    expect(bot.sample(9)).toEqual(control.sample(9));
  });

  it("a pointer look wakes a sleeping bot", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "doze" });
    bot.gesture("sleep", 0);
    expect(bot.eyes(2)[0].kind).toBe("line");
    bot.look({ yaw: 10, pitch: 4, mix: 1, spin: 0, wander: 0.4 }, 2);
    expect(bot.eyes(3)[0].kind).toBe("capsule");
  });

  it("drops the particles at low detail but keeps the blink", () => {
    const full = createBot({ appearance: appearanceFor("round"), seed: "cheer", detail: "full" });
    const low = createBot({ appearance: appearanceFor("round"), seed: "cheer", detail: "low" });
    full.gesture("cheer", 0);
    low.gesture("cheer", 0);
    let fullDots = 0;
    let lowDots = 0;
    for (let step = 0; step <= 60; step += 1) {
      fullDots = Math.max(fullDots, full.sample(step / 60).dots.length);
      lowDots = Math.max(lowDots, low.sample(step / 60).dots.length);
    }
    expect(fullDots).toBeGreaterThan(0);
    expect(lowDots).toBe(0);
    expect(low.sample(2).eyes[0].matrix).toEqual(full.sample(2).eyes[0].matrix);
  });
});
