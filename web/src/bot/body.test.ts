// The body is not a backdrop: it leans, follows the head, breathes and catches the light.
// Everything here is a pure function of time and a seed, and the eyes are fitted in body
// space, so these tests check the body's own motion and that the face's contrast survives
// the shading painted under it.

import { describe, expect, it } from "vitest";
import { COLORS, SHAPES, type Appearance } from "./catalog";
import { DARK_CANVAS, colourAtShade, createBot, type Bot, type Frame, type GestureKind } from "./engine";
import { eyeOutline } from "./face";
import { CENTER, PROFILES, radiusAtAngle } from "./shapes";
import { DEG, contrast, luminance } from "./math";
import type { Mood } from "../lib/mood";

const GESTURES: GestureKind[] = ["wake", "notice", "nod", "cheer", "wince", "sigh", "think"];

function appearanceFor(shape: Appearance["shape"], color: Appearance["color"] = "clay"): Appearance {
  return { shape, color };
}

function matrixOf(frame: Frame): number[] {
  return frame.transform.slice(7, -1).split(",").map(Number);
}

function place(matrix: readonly number[], x: number, y: number): { x: number; y: number } {
  return { x: matrix[0]! * x + matrix[2]! * y + matrix[4]!, y: matrix[1]! * x + matrix[3]! * y + matrix[5]! };
}

/** Body scale and roll, read back out of the group transform. */
function bodyOf(frame: Frame) {
  const [a, b, c, d] = matrixOf(frame) as [number, number, number, number];
  return {
    sx: Math.hypot(a, b),
    sy: Math.hypot(c, d),
    roll: Math.atan2(b, a),
    centre: place(matrixOf(frame), CENTER.x, CENTER.y),
  };
}

function meanEyeX(bot: Bot, t: number): number {
  const eyes = bot.eyes(t);
  return (eyes[0].cx + eyes[1].cx) / 2;
}

describe("head turn", () => {
  it("moves the body toward the pointer, less than the eyes travel", () => {
    const still = createBot({ appearance: appearanceFor("round"), seed: "follow" });
    const looking = createBot({ appearance: appearanceFor("round"), seed: "follow" });
    looking.look({ yaw: 16, pitch: 0, mix: 1, spin: 0, wander: 0.35 }, 0);
    const t = 2.5;
    const eyeTravel = meanEyeX(looking, t) - meanEyeX(still, t);
    const bodyTravel = bodyOf(looking.sample(t)).centre.x - bodyOf(still.sample(t)).centre.x;
    expect(eyeTravel).toBeGreaterThan(6);
    expect(bodyTravel).toBeGreaterThan(0.15 * eyeTravel);
    expect(bodyTravel).toBeLessThan(0.6 * eyeTravel);
  });

  it("follows the gaze vertically as well", () => {
    const still = createBot({ appearance: appearanceFor("pebble"), seed: "updown" });
    const up = createBot({ appearance: appearanceFor("pebble"), seed: "updown" });
    up.look({ yaw: 0, pitch: 13, mix: 1, spin: 0, wander: 0.35 }, 0);
    const t = 1.7;
    const eyeTravel = (up.eyes(t)[0].cy + up.eyes(t)[1].cy) / 2 - (still.eyes(t)[0].cy + still.eyes(t)[1].cy) / 2;
    const bodyTravel = bodyOf(up.sample(t)).centre.y - bodyOf(still.sample(t)).centre.y;
    expect(eyeTravel).toBeLessThan(-4);
    expect(bodyTravel).toBeLessThan(-0.6);
  });

  it("turns the body in depth toward the pointer, and its side shows", () => {
    const right = createBot({ appearance: appearanceFor("squircle"), seed: "turn" });
    const left = createBot({ appearance: appearanceFor("squircle"), seed: "turn" });
    right.look({ yaw: 16, pitch: 0, mix: 1, spin: 0, wander: 0 }, 0);
    left.look({ yaw: -16, pitch: 0, mix: 1, spin: 0, wander: 0 }, 0);
    const t = 3.1;
    const a = right.sample(t);
    const b = left.sample(t);
    expect(a.tilt.y).toBeGreaterThan(8);
    expect(b.tilt.y).toBeLessThan(-8);
    // The front plane narrows with the turn: a real turn in depth, not a slide.
    expect(bodyOf(a).sx).toBeLessThan(0.99);
    // The back slices sit off to the side the face turns away from, so the side shows.
    const back = (frame: Frame) => frame.layers[0]!.slice(7, -1).split(",").map(Number)[4]!;
    expect(back(a)).toBeLessThan(matrixOf(a)[4]! - 2);
    expect(back(b)).toBeGreaterThan(matrixOf(b)[4]! + 2);
  });

  it("lets the eyes lead and the body follow behind them", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "lead", detail: "full" });
    const rest = createBot({ appearance: appearanceFor("round"), seed: "lead", detail: "full" });
    // Dated every frame, the way Avatar.tsx calls it: the body's slower ease must survive that.
    const look = { yaw: 16, pitch: 0, mix: 1, spin: 0, wander: 0 };
    const travel: { eye: number; turn: number }[] = [];
    for (let step = 0; step <= 72; step += 1) {
      const t = 1 + step / 60;
      bot.look(look, t);
      travel.push({ eye: meanEyeX(bot, t) - meanEyeX(rest, t), turn: bot.sample(t).tilt.y - rest.sample(t).tilt.y });
    }
    const early = travel[7]!;
    const late = travel[72]!;
    expect(early.eye / late.eye).toBeGreaterThan(early.turn / late.turn + 0.15);
  });

  it("turns toward a new pointer position without a jump", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "square-up" });
    let previous = bot.sample(2).tilt;
    bot.look({ yaw: -10, pitch: 4, mix: 1, spin: 0, wander: 0.35 }, 2);
    for (let step = 1; step <= 60; step += 1) {
      const now = bot.sample(2 + step / 60).tilt;
      expect(Math.abs(now.x - previous.x) + Math.abs(now.y - previous.y), `step ${step}`).toBeLessThan(2.5);
      previous = now;
    }
  });
});

describe("steadiness", () => {
  it("never jerks the body or flickers its side, idle or tracking a resting pointer", () => {
    for (const pointer of [null, { yaw: 6, pitch: 3, mix: 1, spin: 0, wander: 0.35 }]) {
      const bot = createBot({ appearance: appearanceFor("squircle", "sky"), seed: "steady" });
      let before = bot.sample(0);
      let previous = before;
      for (let step = 1; step <= 60 * 30; step += 1) {
        const t = step / 60;
        // Dated every frame, the way Avatar.tsx calls it.
        bot.look(pointer, t);
        const frame = bot.sample(t);
        // Shaking is a change of speed from one frame to the next, not motion itself: the
        // body may glide, but it may not kick. Saccades are the eyes' business alone.
        const kick = Math.hypot(
          frame.tilt.x - 2 * previous.tilt.x + before.tilt.x,
          frame.tilt.y - 2 * previous.tilt.y + before.tilt.y,
        );
        // The pointer arriving starts a turn from rest; the steady state after it must not kick.
        if (t > 0.5) expect(kick, `turn at ${t.toFixed(2)}`).toBeLessThan(0.08);
        expect(Math.abs(frame.sideAlpha - previous.sideAlpha), `side at ${t.toFixed(2)}`).toBeLessThan(0.08);
        before = previous;
        previous = frame;
      }
    }
  });
});

describe("idle body", () => {
  it("sways in depth on its own, seeded, never as a flat in-plane roll", () => {
    const seedA = createBot({ appearance: appearanceFor("blob"), seed: "sway-a" });
    const seedB = createBot({ appearance: appearanceFor("blob"), seed: "sway-b" });
    const yaws: number[] = [];
    const others: number[] = [];
    for (let t = 0; t <= 40; t += 0.2) {
      const frame = seedA.sample(t);
      expect(Math.abs(bodyOf(frame).roll)).toBeLessThan(1e-6);
      expect(Math.abs(frame.tilt.x)).toBeLessThan(16);
      expect(Math.abs(frame.tilt.y)).toBeLessThan(22);
      yaws.push(frame.tilt.y);
      others.push(seedB.sample(t).tilt.y);
    }
    const swing = Math.max(...yaws) - Math.min(...yaws);
    expect(swing).toBeGreaterThan(8);
    const apart = Math.max(...yaws.map((yaw, index) => Math.abs(yaw - others[index]!)));
    expect(apart).toBeGreaterThan(1);
  });

  it("breathes: the height swings by about a percent and the width gives way", () => {
    const bot = createBot({ appearance: appearanceFor("clover"), seed: "breath" });
    const heights: number[] = [];
    const widths: number[] = [];
    for (let t = 0; t <= 12; t += 0.1) {
      const body = bodyOf(bot.sample(t));
      heights.push(body.sy);
      widths.push(body.sx);
    }
    expect(Math.max(...heights) - Math.min(...heights)).toBeGreaterThan(0.015);
    expect(Math.max(...widths) - Math.min(...widths)).toBeGreaterThan(0.004);
  });

  it("keeps the eyes and the pastille riding the same transform as the body", () => {
    const bot = createBot({ appearance: appearanceFor("arch"), seed: "ride", mood: "thinking" });
    const frame = bot.sample(1.4);
    const matrix = matrixOf(frame);
    const centre = place(matrix, CENTER.x, CENTER.y);
    expect(frame.dots.length).toBeGreaterThan(0);
    // Thinking splits the body in three: the outer dots straddle the body's centre.
    const xs = frame.dots.map((dot) => dot.cx);
    expect(Math.abs((Math.min(...xs) + Math.max(...xs)) / 2 - centre.x)).toBeLessThan(0.3);
    for (const dot of frame.dots) expect(Math.abs(dot.cy - centre.y)).toBeLessThan(0.6);
  });

  it("keeps the thinking dots a row of three, however small the body shrinks", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "think-row" });
    bot.gesture("think", 0);
    let smallestBody = 1;
    for (const t of [0.2, 0.45, 0.7, 0.95]) {
      const frame = bot.sample(t);
      const centre = bodyOf(frame).centre;
      smallestBody = Math.min(smallestBody, bodyOf(frame).sy);
      expect(frame.dots).toHaveLength(2);
      const xs = frame.dots.map((dot) => dot.cx);
      // The row keeps its own spacing: the body is a dot in the middle, and the two others
      // sit a fixed distance either side instead of collapsing into it.
      expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan(30);
      for (const dot of frame.dots) {
        expect(dot.r).toBeGreaterThan(5);
        expect(Math.abs(dot.cy - centre.y)).toBeLessThan(0.6);
      }
    }
    expect(smallestBody).toBeLessThan(0.4);
  });

  it("hangs the notice pastille on the transformed contour", () => {
    const bot = createBot({ appearance: appearanceFor("peak"), seed: "pastille" });
    bot.look({ yaw: 12, pitch: -8, mix: 1, spin: 0, wander: 0.3 }, 0);
    bot.gesture("notice", 0.4);
    const frame = bot.sample(0.72);
    expect(frame.notif).not.toBeNull();
    const angle = (-42 * Math.PI) / 180;
    const radius = radiusAtAngle(PROFILES.peak, angle);
    const local = { x: CENTER.x + Math.cos(angle) * radius, y: CENTER.y + Math.sin(angle) * radius };
    const mapped = place(matrixOf(frame), local.x, local.y);
    // The transform string is rounded to 0.01 per element, which is worth up to ~0.5 units
    // at the contour; the pastille is placed with the exact matrix.
    expect(Math.abs(frame.notif!.x - mapped.x)).toBeLessThan(0.6);
    expect(Math.abs(frame.notif!.y - mapped.y)).toBeLessThan(0.6);
  });
});

describe("rim light", () => {
  it("is a light rim on one side and a shade on the other, never on the face", () => {
    for (const color of COLORS) {
      const bot = createBot({ appearance: appearanceFor("blob", color), seed: `rim-${color}`, mood: "happy" });
      const frame = bot.sample(2.2);
      expect(frame.shade).not.toBeNull();
      const stops = frame.shade!.stops;
      const lit = stops[0]!.color;
      const dark = stops[stops.length - 1]!.color;
      expect(luminance(lit)).toBeGreaterThan(luminance(frame.fill));
      expect(luminance(dark)).toBeLessThan(luminance(frame.fill));
      // The plateaus in the middle are the flat fill: that is the face's own ground.
      expect(stops[1]!.color).toBe(frame.fill);
      expect(stops[2]!.color).toBe(frame.fill);
    }
  });

  it("turns with the body and softens at low detail", () => {
    const right = createBot({ appearance: appearanceFor("star", "sky"), seed: "rim-turn" });
    const left = createBot({ appearance: appearanceFor("star", "sky"), seed: "rim-turn" });
    right.look({ yaw: 16, pitch: 0, mix: 1, spin: 0, wander: 0.35 }, 0);
    left.look({ yaw: -16, pitch: 0, mix: 1, spin: 0, wander: 0.35 }, 0);
    const a = right.sample(2.6).shade!;
    const b = left.sample(2.6).shade!;
    const angleOf = (shade: typeof a) => Math.atan2(shade.y2 - shade.y1, shade.x2 - shade.x1);
    expect(Math.abs(angleOf(a) - angleOf(b))).toBeGreaterThan(2 * DEG);

    // A small face keeps its volume, at less strength: a full rim would only blur at 24px.
    const low = createBot({ appearance: appearanceFor("star", "sky"), seed: "rim-turn", detail: "low" });
    const full = createBot({ appearance: appearanceFor("star", "sky"), seed: "rim-turn" });
    const lift = (frame: Frame) => luminance(frame.shade.stops[0]!.color) - luminance(frame.fill);
    expect(lift(low.sample(2.6))).toBeGreaterThan(0);
    expect(lift(low.sample(2.6))).toBeLessThan(lift(full.sample(2.6)));
  });

  it("keeps the shaded edge separate from the dark canvas", () => {
    // At 30px the rim is off, but the shade still tints the edge of a full-size bot in the
    // dark theme; a navy or ink body must not merge into the dark canvas.
    for (const color of COLORS) {
      const bot = createBot({ appearance: appearanceFor("blob", color), seed: `edge-${color}` });
      const shade = bot.sample(1.6).shade!;
      for (let t = 0.8; t <= 1.0001; t += 0.04) {
        const x = shade.x1 + (shade.x2 - shade.x1) * t;
        const y = shade.y1 + (shade.y2 - shade.y1) * t;
        expect(contrast(colourAtShade(shade, x, y), DARK_CANVAS), `${color} at ${t.toFixed(2)}`).toBeGreaterThanOrEqual(1.15);
      }
    }
  });

  it("never takes the eyes below 4.5:1 against the body it paints", () => {
    let worst = Number.POSITIVE_INFINITY;
    let where = "";
    for (const color of COLORS) {
      for (const shape of SHAPES) {
        for (const mood of ["idle", "happy", "thinking", "surprised", "happy", "sorry"] as Mood[]) {
          const bot = createBot({ appearance: appearanceFor(shape, color), seed: `contrast|${shape}|${color}`, mood });
          for (const t of [0.4, 1.3, 2.9, 4.4]) {
            const frame = bot.sample(t);
            if (!frame.shade) continue;
            for (const eye of bot.eyes(t)) {
              if (eye.alpha < 0.02) continue;
              for (const point of eyeOutline(eye)) {
                const ratio = contrast(colourAtShade(frame.shade, point.x, point.y), frame.eyeFill);
                if (ratio < worst) {
                  worst = ratio;
                  where = `${shape} ${color} ${mood} t=${t}`;
                }
              }
            }
          }
        }
      }
    }
    expect(worst, where).toBeGreaterThanOrEqual(4.5);
  });
});

describe("gesture body motion", () => {
  it("moves the body, not only the face", () => {
    for (const kind of GESTURES) {
      const bot = createBot({ appearance: appearanceFor("clover"), seed: `body-${kind}` });
      const control = createBot({ appearance: appearanceFor("clover"), seed: `body-${kind}` });
      bot.gesture(kind, 0.3);
      let widestRoll = 0;
      let widestOffset = 0;
      let flattest = 1;
      for (let step = 0; step <= 120; step += 1) {
        const t = 0.3 + step / 60;
        const body = bodyOf(bot.sample(t));
        const rest = bodyOf(control.sample(t));
        widestRoll = Math.max(widestRoll, Math.abs(body.roll - rest.roll));
        widestOffset = Math.max(widestOffset, Math.hypot(body.centre.x - rest.centre.x, body.centre.y - rest.centre.y));
        flattest = Math.min(flattest, body.sy / rest.sy);
      }
      if (kind === "think") {
        // Thinking turns the body into the middle of three dots: it shrinks, it does not lean.
        expect(flattest, kind).toBeLessThan(0.5);
        continue;
      }
      expect(widestRoll * (180 / Math.PI), kind).toBeGreaterThan(1.6);
      expect(widestOffset, kind).toBeGreaterThan(1.2);
    }
  });

  it("crouches before it hops", () => {
    const bot = createBot({ appearance: appearanceFor("round"), seed: "anticipation" });
    bot.gesture("cheer", 0);
    let crouched = false;
    let top = 0;
    for (let step = 0; step <= 60; step += 1) {
      const t = step / 60;
      const body = bodyOf(bot.sample(t));
      if (t < 0.2 && body.sy < 0.97) crouched = true;
      top = Math.min(top, body.centre.y - CENTER.y);
    }
    expect(crouched).toBe(true);
    expect(top).toBeLessThan(-4);
  });

  it("tips sideways while asleep and rights itself on waking", () => {
    const bot = createBot({ appearance: appearanceFor("pebble"), seed: "doze-body" });
    bot.gesture("sleep", 0);
    const asleep = bodyOf(bot.sample(4));
    expect(Math.abs(asleep.roll)).toBeGreaterThan(4 * DEG);
    bot.gesture("wake", 6);
    const awake = bodyOf(bot.sample(9));
    expect(Math.abs(awake.roll)).toBeLessThan(3.5 * DEG);
  });
});

describe("body motion stays continuous", () => {
  it("never jumps when the pointer changes or a gesture starts", () => {
    const bot = createBot({ appearance: appearanceFor("teardrop"), seed: "smooth-body" });
    let previous = matrixOf(bot.sample(0));
    for (let step = 0; step <= 240; step += 1) {
      const t = step / 60;
      if (step === 50) bot.look({ yaw: -16, pitch: 0, mix: 1, spin: 0, wander: 0.3 }, t);
      if (step === 90) bot.look({ yaw: 16, pitch: 10, mix: 1, spin: 0, wander: 0.3 }, t);
      if (step === 130) bot.gesture("nod", t);
      if (step === 180) bot.look(null, t);
      const now = matrixOf(bot.sample(t));
      for (let index = 0; index < 6; index += 1) {
        // The body rides the same eased morph as the gaze, which starts fast: most of its
        // travel lands in the first frames of the 240 ms change.
        expect(Math.abs(now[index]! - previous[index]!), `step ${step} element ${index}`).toBeLessThan(2.6);
      }
      previous = now;
    }
  });
});
