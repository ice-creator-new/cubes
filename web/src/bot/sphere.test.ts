import { describe, expect, it } from "vitest";
import { buildRhythm, blinkLid, eyePoses, forcedLid, liveliness } from "./sphere";

const columnNorm = (a: number, b: number) => Math.hypot(a, b);

describe("sphere projection", () => {
  it("leaves centre-facing eyes uncompressed", () => {
    const [left, right] = eyePoses({ yaw: 0, pitch: 0, roll: 0 }, 0);
    for (const eye of [left, right]) {
      expect(eye.x).toBeCloseTo(0, 6);
      expect(eye.y).toBeCloseTo(0, 6);
      expect(eye.depth).toBeCloseTo(1, 6);
      expect(columnNorm(eye.a, eye.b)).toBeCloseTo(1, 6);
      expect(columnNorm(eye.c, eye.d)).toBeCloseTo(1, 6);
    }
  });

  it("foreshortens the glyph across the split and the eyes stay on the sphere", () => {
    const split = 60;
    const [left, right] = eyePoses({ yaw: 0, pitch: 0, roll: 0 }, split);
    const cosine = Math.cos((split * Math.PI) / 180);
    for (const eye of [left, right]) {
      // Unit vector on the sphere: |x| = sin(split), depth = cos(split).
      expect(Math.abs(eye.x)).toBeCloseTo(Math.sin((split * Math.PI) / 180), 6);
      expect(eye.depth).toBeCloseTo(cosine, 6);
      // The tangent frame leans away from the viewer, so its screen width shrinks.
      expect(columnNorm(eye.a, eye.b)).toBeCloseTo(cosine, 6);
      expect(columnNorm(eye.c, eye.d)).toBeCloseTo(1, 6);
    }
  });

  it("pushes the near-limb eye deeper when the head turns", () => {
    const split = 20;
    const [left, right] = eyePoses({ yaw: 30, pitch: 0, roll: 0 }, split);
    // Positive yaw turns the face to its own right: index 1 is the eye nearer the limb.
    expect(right.depth).toBeLessThan(left.depth);
    expect(right.depth).toBeLessThan(Math.cos(((split + 30) * Math.PI) / 180) + 1e-6);
    expect(columnNorm(right.a, right.b)).toBeLessThan(columnNorm(left.a, left.b));
  });

  it("rolls the head in its own plane without moving the pair off centre", () => {
    const straight = eyePoses({ yaw: 0, pitch: 0, roll: 0 }, 20);
    const rolled = eyePoses({ yaw: 0, pitch: 0, roll: 25 }, 20);
    for (const index of [0, 1] as const) {
      expect(rolled[index].depth).toBeCloseTo(straight[index].depth, 6);
      expect(Math.hypot(rolled[index].x, rolled[index].y)).toBeCloseTo(
        Math.hypot(straight[index].x, straight[index].y),
        6,
      );
    }
    expect(rolled[0].y).toBeLessThan(straight[0].y);
  });
});

describe("blink calendar", () => {
  const rhythm = buildRhythm("member-7");

  it("is deterministic", () => {
    expect(buildRhythm("member-7")).toEqual(rhythm);
    const other = buildRhythm("member-8");
    expect(other.blinks).not.toEqual(rhythm.blinks);
  });

  it("draws blinks between 1.9 and 4.7 seconds apart", () => {
    for (let index = 1; index < 200; index += 1) {
      const gap = rhythm.blinks[index]! - rhythm.blinks[index - 1]!;
      // A double blink is the only short gap there is, and it is 0.24 s.
      if (gap < 0.3) {
        expect(gap).toBeCloseTo(0.24, 2);
        continue;
      }
      expect(gap).toBeGreaterThanOrEqual(1.89);
      expect(gap).toBeLessThanOrEqual(4.7);
    }
  });

  it("includes double blinks", () => {
    let doubles = 0;
    for (let index = 1; index < rhythm.blinks.length; index += 1) {
      if (rhythm.blinks[index]! - rhythm.blinks[index - 1]! < 0.3) doubles += 1;
    }
    expect(doubles).toBeGreaterThan(20);
    expect(doubles / rhythm.blinks.length).toBeLessThan(0.25);
  });

  it("closes fast and opens slower", () => {
    const start = rhythm.blinks[3]!;
    expect(blinkLid(rhythm.blinks, start - 0.01)).toBe(1);
    expect(blinkLid(rhythm.blinks, start + 0.02)).toBeLessThan(0.95);
    expect(blinkLid(rhythm.blinks, start + 0.072)).toBeLessThan(0.02);
    // Half open again later on the way back up.
    const rising = blinkLid(rhythm.blinks, start + 0.15);
    expect(rising).toBeGreaterThan(0.3);
    expect(rising).toBeLessThan(1);
    expect(blinkLid(rhythm.blinks, start + 0.23)).toBe(1);
  });
});

describe("liveliness", () => {
  const rhythm = buildRhythm("drift");

  it("is a pure function of time", () => {
    expect(liveliness(rhythm, 12.34)).toEqual(liveliness(rhythm, 12.34));
    expect(liveliness(rhythm, 3)).not.toEqual(liveliness(rhythm, 3.5));
  });

  it("keeps drift inside the envelope the fit table solves for", () => {
    for (let t = 0; t < 60; t += 0.05) {
      const life = liveliness(rhythm, t);
      expect(Math.abs(life.dYaw)).toBeLessThanOrEqual(8.95);
      expect(Math.abs(life.dPitch)).toBeLessThanOrEqual(6.65);
      expect(Math.abs(life.dRoll)).toBeLessThanOrEqual(2.05);
      expect(Math.abs(life.driftX)).toBeLessThanOrEqual(0.71);
      expect(Math.abs(life.driftY)).toBeLessThanOrEqual(0.61);
      // A slow sway in depth: a few degrees, or a row of avatars would rock.
      expect(Math.abs(life.swayYaw)).toBeLessThanOrEqual(11);
      expect(Math.abs(life.swayPitch)).toBeLessThanOrEqual(8);
      // The breath is a swing around 1, and the width gives way as the height grows.
      expect(Math.abs(life.breath)).toBeLessThanOrEqual(0.0125);
      expect(Math.abs(life.breathWidth)).toBeLessThanOrEqual(0.0065);
      expect(Math.sign(life.breathWidth)).toBe(-Math.sign(life.breath));
    }
  });

  it("holds still when wander is off and drops the extras in low detail", () => {
    const held = liveliness(rhythm, 5, { wander: 0 });
    expect(held.dYaw).toBeCloseTo(0, 9);
    expect(held.dPitch).toBeCloseTo(0, 9);
    expect(held.lid).toBeLessThanOrEqual(1);
    // Low detail keeps a gentler sway and breath, and drops the saccades entirely.
    const low = liveliness(rhythm, 5, { subtle: true });
    expect(Math.abs(low.driftX)).toBeLessThanOrEqual(0.15);
    expect(Math.abs(low.driftY)).toBeLessThanOrEqual(0.13);
    expect(Math.abs(low.breath)).toBeLessThanOrEqual(0.0045);
    expect(Math.abs(low.swayYaw)).toBeLessThan(4);
    expect(low.lid).toBe(held.lid);
  });

  it("saccades: the gaze sits still for a while, then jumps", () => {
    const jumps = rhythm.saccades.filter((entry) => Math.abs(entry.yaw) > 0.8);
    expect(jumps.length).toBeGreaterThan(10);
    const at = rhythm.saccades[5]!;
    const before = liveliness(rhythm, at.at - 0.2, { wander: 1, blink: false });
    const after = liveliness(rhythm, at.at + 0.2, { wander: 1, blink: false });
    expect(Math.abs(after.dYaw - before.dYaw)).toBeGreaterThan(0.2);
  });

  it("masks a state change with one full blink", () => {
    expect(forcedLid(-1, 0.3)).toBe(1);
    expect(forcedLid(0, 0.3)).toBe(1);
    expect(forcedLid(0.15, 0.3)).toBe(0);
    expect(forcedLid(0.3, 0.3)).toBe(1);
  });
});
