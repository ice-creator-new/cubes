// The rest pose: a sidebar bot at rest is a still, flat portrait — no drift, no breath, no
// lean, no blink, no rim, an untransformed body — and it comes alive on a dated ramp when it
// is hovered, selected or busy, so nothing jumps on the way in or out.

import { describe, expect, it } from "vitest";
import type { Appearance } from "./catalog";
import { createBot, type Bot, type Frame } from "./engine";
import { eyeOutline } from "./face";

function appearanceFor(shape: Appearance["shape"] = "squircle"): Appearance {
  return { shape, color: "coral" };
}

function matrixOf(frame: Frame): number[] {
  return frame.transform.slice(7, -1).split(",").map(Number);
}

function eyeCentres(bot: Bot, t: number): number[] {
  return bot.eyes(t).flatMap((eye) => [eye.cx, eye.cy]);
}

describe("rest pose", () => {
  it("is the same frame at every date: a still portrait", () => {
    const bot = createBot({ appearance: appearanceFor(), seed: "rest", awake: false });
    const first = bot.sample(0);
    for (const t of [0.2, 1, 7.5, 60, 601.3]) expect(bot.sample(t)).toEqual(first);
    // Lit like a still photo, with no dots, no pastille and an identity group. The light
    // is part of the portrait, so it never changes from one date to the next.
    expect(first.shade.stops[0]!.color).not.toBe(first.fill);
    expect(first.dots).toHaveLength(0);
    expect(first.notif).toBeNull();
    expect(matrixOf(first)).toEqual([1, 0, 0, 1, 0, 0]);
    expect(first.tilt).toEqual({ x: 0, y: 0 });
    expect(first.eyeAlpha).toBe(1);
  });

  it("keeps the mood's face but none of its movement", () => {
    const happy = createBot({ appearance: appearanceFor("round"), seed: "rest-mood", awake: false, mood: "happy" });
    const excited = happy.eyes(0.4);
    expect(excited[0].kind).toBe("arcUp");
    // Same eyes at any date, and no blink ever closes them.
    expect(happy.eyes(9.7)).toEqual(excited);
    for (let t = 0; t < 12; t += 0.05) {
      const eyes = happy.eyes(t);
      const height =
        Math.max(...eyeOutline(eyes[0]).map((point) => point.y)) -
        Math.min(...eyeOutline(eyes[0]).map((point) => point.y));
      expect(height).toBeGreaterThan(8);
    }
  });

  it("stays pure: a look or a stray sample never disturbs the rest frame", () => {
    const bot = createBot({ appearance: appearanceFor("blob"), seed: "rest-pure", awake: false });
    const first = bot.sample(3);
    bot.look({ yaw: 16, pitch: -13, mix: 1, spin: 4, wander: 1 }, 3);
    bot.sample(11);
    bot.sample(0.1);
    expect(bot.sample(3)).toEqual(first);
    expect(bot.resting(3)).toBe(true);
  });

  it("keeps painting a colour change on a resting bot until it lands", () => {
    const bot = createBot({ appearance: appearanceFor(), seed: "rest-recolour", awake: false });
    expect(bot.resting(5)).toBe(true);
    bot.set({ appearance: { shape: "squircle", color: "#2e6be6", eye: "#ffd84a" } }, 5);
    expect(bot.resting(5.01)).toBe(false);
    expect(bot.resting(5.6)).toBe(true);
    const settled = bot.sample(5.6);
    expect(settled.fill).toBe("#2e6be6");
    expect(settled.eyeFill).toBe("#ffd84a");
    expect(settled.shade.stops[1]!.color).toBe("#2e6be6");
  });
});

describe("waking", () => {
  it("ramps up and down without a jump, and lands exactly on the rest pose", () => {
    const bot = createBot({ appearance: appearanceFor("star"), seed: "wake-ramp", awake: false });
    const rest = createBot({ appearance: appearanceFor("star"), seed: "wake-ramp", awake: false });
    expect(bot.resting(1)).toBe(true);

    bot.setAwake(true, 1);
    let previous = matrixOf(bot.sample(1));
    let previousEyes = eyeCentres(bot, 1);
    let alive = 0;
    for (let step = 1; step <= 90; step += 1) {
      const t = 1 + step / 60;
      const frame = bot.sample(t);
      const now = matrixOf(frame);
      for (let index = 0; index < 6; index += 1) {
        expect(Math.abs(now[index]! - previous[index]!), `up ${step} element ${index}`).toBeLessThan(0.6);
      }
      const eyes = eyeCentres(bot, t);
      for (let index = 0; index < eyes.length; index += 1) {
        expect(Math.abs(eyes[index]! - previousEyes[index]!), `up ${step} eye ${index}`).toBeLessThan(2);
      }
      if (step > 30) alive = Math.max(alive, Math.abs(now[5]!));
      previous = now;
      previousEyes = eyes;
      expect(frame.shade).not.toBeNull();
    }
    // Fully awake: the body moves and catches the light.
    expect(alive).toBeGreaterThan(0.2);
    expect(bot.resting(2.5)).toBe(false);

    bot.setAwake(false, 2.5);
    previous = matrixOf(bot.sample(2.5));
    for (let step = 1; step <= 90; step += 1) {
      const t = 2.5 + step / 60;
      const now = matrixOf(bot.sample(t));
      for (let index = 0; index < 6; index += 1) {
        expect(Math.abs(now[index]! - previous[index]!), `down ${step} element ${index}`).toBeLessThan(0.6);
      }
      previous = now;
    }
    expect(bot.resting(3.5)).toBe(true);
    // Settled: exactly the still portrait, whatever the date.
    expect(bot.sample(40)).toEqual(rest.sample(40));
  });

  it("turns around from halfway instead of snapping", () => {
    const bot = createBot({ appearance: appearanceFor("pebble"), seed: "wake-mid", awake: false });
    bot.setAwake(true, 0);
    // Interrupt the ramp a tenth of a second in, then resume it.
    const half = matrixOf(bot.sample(0.1));
    bot.setAwake(false, 0.1);
    const next = matrixOf(bot.sample(0.1 + 1 / 60));
    for (let index = 0; index < 6; index += 1) {
      expect(Math.abs(next[index]! - half[index]!)).toBeLessThan(0.4);
    }
    bot.setAwake(true, 0.2);
    const again = matrixOf(bot.sample(0.2 + 1 / 60));
    for (let index = 0; index < 6; index += 1) {
      expect(Math.abs(again[index]! - matrixOf(bot.sample(0.2))[index]!)).toBeLessThan(0.4);
    }
  });

  it("wakes for a gesture and settles again when it ends", () => {
    const bot = createBot({ appearance: appearanceFor("clover"), seed: "wake-gesture", awake: false });
    const restFrame = bot.sample(0);
    bot.gesture("notice", 2);
    expect(bot.gestureActive(2.1)).toBe(true);
    expect(bot.resting(2.1)).toBe(false);

    // What the renderer does: keep it awake for as long as a one-shot gesture is on screen.
    let moved = 0;
    for (let step = 0; step <= 140; step += 1) {
      const t = 2 + step / 60;
      bot.setAwake(bot.gestureActive(t), t);
      const frame = bot.sample(t);
      moved = Math.max(moved, Math.hypot(frame.notif?.x ?? 0, 0), Math.abs(matrixOf(frame)[5]!));
    }
    expect(moved).toBeGreaterThan(1);
    const after = 2 + 1.2 + 1;
    expect(bot.gestureActive(after)).toBe(false);
    bot.setAwake(false, after);
    expect(bot.resting(after + 0.4)).toBe(true);
    expect(bot.sample(after + 0.5)).toEqual(restFrame);
  });

  it("does not treat sleep as an event", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "wake-sleep", awake: false });
    bot.gesture("sleep", 0);
    // Asleep is a pose the bot settles into; it must not pin the bot awake for ever.
    expect(bot.gestureActive(5)).toBe(false);
    expect(bot.resting(5)).toBe(true);
    expect(bot.sample(5)).toEqual(bot.sample(20));
  });
});
