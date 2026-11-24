import { describe, expect, it } from "vitest";
import { SHAPES } from "./catalog";
import {
  CENTER,
  MAX_RADIUS,
  PROFILES,
  PROFILE_SAMPLES,
  R,
  profilePath,
  profilePoint,
  radiusAtAngle,
} from "./shapes";
import { DEG, TAU } from "./math";

const STEPS = PROFILE_SAMPLES;

function pointsOf(profile: readonly number[]) {
  return profile.map((_, index) => profilePoint(profile, index));
}

function areaOf(profile: readonly number[]) {
  const points = pointsOf(profile);
  let total = 0;
  for (let index = 0; index < points.length; index += 1) {
    const here = points[index]!;
    const next = points[(index + 1) % points.length]!;
    total += here.x * next.y - next.x * here.y;
  }
  return Math.abs(total) / 2;
}

/** Directions (degrees) of the local maxima of a profile, one per lobe or vertex. */
function peakAngles(profile: readonly number[]): number[] {
  const out: number[] = [];
  const widest = Math.max(...profile);
  for (let index = 0; index < profile.length; index += 1) {
    const here = profile[index]!;
    const before = profile[(index - 1 + profile.length) % profile.length]!;
    const after = profile[(index + 1) % profile.length]!;
    if (here >= before && here > after && here > widest - 0.9) {
      out.push(Math.round((index * 360) / STEPS));
    }
  }
  return out;
}

/** Points along the emitted path: the start, then six samples of every cubic. */
function splineSamples(profile: readonly number[]): { x: number; y: number }[] {
  const numbers = profilePath(profile).match(/-?\d+(?:\.\d+)?/g)?.map(Number) ?? [];
  const segments = (numbers.length - 2) / 6;
  let from = { x: numbers[0]!, y: numbers[1]! };
  const out = [from];
  for (let segment = 0; segment < segments; segment += 1) {
    const base = 2 + segment * 6;
    const control1 = { x: numbers[base]!, y: numbers[base + 1]! };
    const control2 = { x: numbers[base + 2]!, y: numbers[base + 3]! };
    const to = { x: numbers[base + 4]!, y: numbers[base + 5]! };
    for (let step = 1; step <= 6; step += 1) {
      const t = step / 6;
      const inverse = 1 - t;
      out.push({
        x:
          inverse ** 3 * from.x +
          3 * inverse ** 2 * t * control1.x +
          3 * inverse * t ** 2 * control2.x +
          t ** 3 * to.x,
        y:
          inverse ** 3 * from.y +
          3 * inverse ** 2 * t * control1.y +
          3 * inverse * t ** 2 * control2.y +
          t ** 3 * to.y,
      });
    }
    from = to;
  }
  return out;
}

describe("silhouettes", () => {
  it("samples every silhouette on exactly 64 positive angles", () => {
    for (const shape of SHAPES) {
      const profile = PROFILES[shape];
      expect(profile).toHaveLength(PROFILE_SAMPLES);
      for (const radius of profile) expect(Number.isFinite(radius) && radius > 0).toBe(true);
    }
  });

  it("normalises every silhouette to roughly the same visual weight", () => {
    const reference = Math.PI * R * R;
    for (const shape of SHAPES) {
      const area = areaOf(PROFILES[shape]);
      // A silhouette whose tip would leave the viewBox is scaled down instead of clipped, so
      // it carries a little less area; nothing may be much heavier than the reference circle.
      expect(area / reference, shape).toBeGreaterThan(0.78);
      expect(area / reference, shape).toBeLessThan(1.02);
    }
  });

  it("never flattens a tip against the radius cap", () => {
    for (const shape of SHAPES) {
      const profile = PROFILES[shape];
      expect(Math.max(...profile), shape).toBeLessThanOrEqual(MAX_RADIUS + 1e-9);
      let run = 0;
      let longest = 0;
      for (let index = 0; index < profile.length * 2; index += 1) {
        if (profile[index % profile.length]! > MAX_RADIUS - 0.02) {
          run += 1;
          longest = Math.max(longest, run);
        } else run = 0;
      }
      // Two samples at the very top of a rounded tip is a curve; a third would be a flat cut.
      expect(Math.min(longest, 3), shape).toBeLessThanOrEqual(2);
    }
  });

  it("has no cusps: the curvature and the turning angle stay bounded", () => {
    for (const shape of SHAPES) {
      const profile = PROFILES[shape];
      const widest = Math.max(...profile);
      for (let index = 0; index < profile.length; index += 1) {
        const before = profile[(index - 1 + profile.length) % profile.length]!;
        const here = profile[index]!;
        const after = profile[(index + 1) % profile.length]!;
        expect(Math.abs(after - 2 * here + before), `${shape} at ${index}`).toBeLessThan(0.09 * widest);
      }
      const points = pointsOf(profile);
      for (let index = 0; index < points.length; index += 1) {
        const from = points[(index - 1 + points.length) % points.length]!;
        const here = points[index]!;
        const to = points[(index + 1) % points.length]!;
        const first = { x: here.x - from.x, y: here.y - from.y };
        const second = { x: to.x - here.x, y: to.y - here.y };
        const turn = Math.atan2(first.x * second.y - first.y * second.x, first.x * second.x + first.y * second.y);
        // The bean's cusp read as a near-reversal; a soft notch turns by about 50° at most.
        expect(Math.abs(turn), `${shape} at ${index}`).toBeLessThan(1);
      }
    }
  });

  it("draws the spline through the profile, never more than a unit away from it", () => {
    for (const shape of SHAPES) {
      const profile = PROFILES[shape];
      for (const point of splineSamples(profile)) {
        const dx = point.x - CENTER.x;
        const dy = point.y - CENTER.y;
        const distance = Math.hypot(dx, dy);
        const sampled = radiusAtAngle(profile, Math.atan2(dy, dx));
        expect(Math.abs(distance - sampled), `${shape} at ${(Math.atan2(dy, dx) * 57.3).toFixed(0)}°`).toBeLessThan(1);
      }
    }
  });

  it("keeps every silhouette distinct from the others", () => {
    for (let left = 0; left < SHAPES.length; left += 1) {
      for (let right = left + 1; right < SHAPES.length; right += 1) {
        const a = PROFILES[SHAPES[left]!]!;
        const b = PROFILES[SHAPES[right]!]!;
        let total = 0;
        for (let index = 0; index < a.length; index += 1) total += Math.abs(a[index]! - b[index]!);
        expect(total / a.length, `${SHAPES[left]} vs ${SHAPES[right]}`).toBeGreaterThan(0.35);
      }
    }
  });
});

describe("shape character", () => {
  it("keeps the capsule long and level-ish", () => {
    const points = pointsOf(PROFILES.capsule);
    const xs = points.map((point) => point.x - CENTER.x);
    const ys = points.map((point) => point.y - CENTER.y);
    const ratio = (Math.max(...xs) - Math.min(...xs)) / (Math.max(...ys) - Math.min(...ys));
    expect(ratio).toBeGreaterThan(1.4);
    expect(ratio).toBeLessThan(1.65);
  });

  it("keeps a flat top and a flat bottom on the hexagon", () => {
    const profile = PROFILES.hexagon;
    for (const top of [-90, 90]) {
      const middle = radiusAtAngle(profile, top * DEG);
      expect(Math.abs(radiusAtAngle(profile, (top - 10) * DEG) - middle), `top ${top}`).toBeLessThan(1.1);
      expect(Math.abs(radiusAtAngle(profile, (top + 10) * DEG) - middle), `top ${top}`).toBeLessThan(1.1);
    }
  });

  it("is built on a regular skeleton, so no arm is longer or sharper than another", () => {
    const folds: [keyof typeof PROFILES, number][] = [
      ["star", 6],
      ["flower", 6],
      ["clover", 4],
      ["pentagon", 5],
    ];
    for (const [shape, fold] of folds) {
      const profile = PROFILES[shape];
      const shift = profile.length / fold;
      expect(Number.isInteger(shift), shape).toBe(true);
      for (let index = 0; index < profile.length; index += 1) {
        // Every arm is sampled the same way, so the drawn spline cannot come out lopsided.
        expect(Math.abs(profile[index]! - profile[(index + shift) % profile.length]!), `${shape} at ${index}`).toBeLessThan(0.1);
      }
    }
    // The sparkle is deliberately a touch taller than wide, so it mirrors instead of rotating.
    const sparkle = PROFILES.sparkle;
    for (let index = 0; index < sparkle.length; index += 1) {
      const mirrored = sparkle[(sparkle.length / 2 - index + sparkle.length) % sparkle.length]!;
      expect(Math.abs(sparkle[index]! - mirrored), `sparkle at ${index}`).toBeLessThan(0.12);
    }
  });

  it("indents the lobed silhouettes deeply enough to read at 30px", () => {
    const indent = (shape: keyof typeof PROFILES) => {
      const profile = PROFILES[shape];
      const widest = Math.max(...profile);
      return (widest - Math.min(...profile)) / widest;
    };
    // A star and a sparkle need real points; a flower and a clover need a visible notch
    // between fat lobes. Under fifteen percent is what blurs into a lumpy circle.
    expect(indent("star")).toBeGreaterThan(0.23);
    expect(indent("star")).toBeLessThan(0.31);
    expect(indent("sparkle")).toBeGreaterThan(0.26);
    expect(indent("sparkle")).toBeLessThan(0.36);
    expect(indent("flower")).toBeGreaterThan(0.14);
    expect(indent("flower")).toBeLessThan(0.19);
    expect(indent("clover")).toBeGreaterThan(0.2);
  });

  it("keeps the blob lumpy and the pebble a smooth stone", () => {
    // A bulge has to stand out from the shape's own range to count, so this counts lumps
    // rather than the two ends of an ellipse.
    const lumps = (profile: readonly number[]) => {
      const widest = Math.max(...profile);
      const narrowest = Math.min(...profile);
      const floor = narrowest + (widest - narrowest) * 0.1;
      let count = 0;
      for (let index = 0; index < profile.length; index += 1) {
        const here = profile[index]!;
        const before = profile[(index - 1 + profile.length) % profile.length]!;
        const after = profile[(index + 1) % profile.length]!;
        if (here >= before && here > after && here > floor) count += 1;
      }
      return count;
    };
    expect(lumps(PROFILES.blob)).toBe(5);
    expect(lumps(PROFILES.pebble)).toBe(2);

    const aspect = (profile: readonly number[]) => {
      const points = pointsOf(profile);
      const xs = points.map((point) => point.x);
      const ys = points.map((point) => point.y);
      return (Math.max(...xs) - Math.min(...xs)) / (Math.max(...ys) - Math.min(...ys));
    };
    // The stone is the flatter of the two; the blob is wider than it is tall but rounder.
    expect(aspect(PROFILES.pebble)).toBeGreaterThan(1.1);
    expect(aspect(PROFILES.pebble) - aspect(PROFILES.blob)).toBeGreaterThan(0.03);

    let apart = 0;
    for (let index = 0; index < PROFILES.blob.length; index += 1) {
      apart += Math.abs(PROFILES.blob[index]! - PROFILES.pebble[index]!);
    }
    expect(apart / PROFILES.blob.length).toBeGreaterThan(2);
  });

  it("points where the reference points", () => {
    const near = (degrees: number, target: number, spread = 20) => {
      const delta = Math.abs((((degrees - target + 180) % 360) + 360) % 360 - 180);
      return delta <= spread;
    };
    const peak = peakAngles(PROFILES.peak);
    expect(peak.some((angle) => near(angle, -90, 14)), "peak apex up").toBe(true);
    // The arch is a dome on a flat base: level along the bottom, round and taller above.
    const arch = PROFILES.arch;
    const below = radiusAtAngle(arch, 90 * DEG);
    expect(Math.abs(radiusAtAngle(arch, 80 * DEG) * Math.sin(80 * DEG) - below)).toBeLessThan(0.6);
    expect(Math.abs(radiusAtAngle(arch, 100 * DEG) * Math.sin(100 * DEG) - below)).toBeLessThan(0.6);
    expect(radiusAtAngle(arch, -90 * DEG)).toBeGreaterThan(below);
    const pentagon = peakAngles(PROFILES.pentagon);
    expect(pentagon).toHaveLength(5);
    expect(pentagon.some((angle) => near(angle, -90, 14)), "pentagon point up").toBe(true);
    const star = peakAngles(PROFILES.star);
    expect(star).toHaveLength(6);
    for (const [index, target] of [-90, -30, 30, 90, 150, 210].entries()) {
      expect(star.some((angle) => near(angle, target, 16)), `star point ${index}`).toBe(true);
    }
    const sparkle = peakAngles(PROFILES.sparkle);
    expect(sparkle).toHaveLength(4);
    const clover = peakAngles(PROFILES.clover);
    expect(clover).toHaveLength(4);
    const flower = peakAngles(PROFILES.flower);
    expect(flower).toHaveLength(6);
    // The square sits square-on: its corners on the diagonals, its sides level.
    const square = peakAngles(PROFILES.squircle);
    expect(square).toHaveLength(4);
    for (const [index, target] of [45, 135, 225, 315].entries()) {
      expect(square.some((angle) => near(angle, target, 4)), `square corner ${index}`).toBe(true);
    }
  });

  it("keeps the teardrop's tip at its lower left", () => {
    const profile = PROFILES.teardrop;
    const narrowest = profile.reduce((best, radius, index) => (radius < profile[best]! ? index : best), 0);
    const tip = (narrowest * 360) / STEPS;
    const delta = Math.abs((((tip - 140 + 180) % 360) + 360) % 360 - 180);
    expect(delta).toBeLessThan(22);
    expect(Math.min(...profile) / Math.max(...profile)).toBeLessThan(0.6);
  });

  it("keeps the blob lopsided rather than merely round", () => {
    const profile = PROFILES.blob;
    let lopsided = 0;
    for (let index = 0; index < profile.length / 2; index += 1) {
      lopsided = Math.max(lopsided, Math.abs(profile[index]! - profile[index + profile.length / 2]!));
    }
    expect(lopsided).toBeGreaterThan(1.5);
    // Wider than it is tall, as in the reference sheet.
    const points = pointsOf(profile);
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan(Math.max(...ys) - Math.min(...ys));
  });

  it("keeps every non-round silhouette away from a circle", () => {
    for (const shape of SHAPES) {
      if (shape === "round" || shape === "pebble") continue;
      const profile = PROFILES[shape];
      const widest = Math.max(...profile);
      const narrowest = Math.min(...profile);
      expect(widest - narrowest, shape).toBeGreaterThan(1.5);
    }
    // The pebble is meant to be the near-round one: uneven, but never lumpy.
    const pebble = PROFILES.pebble;
    expect(Math.max(...pebble) - Math.min(...pebble)).toBeLessThan(7);
  });
});

describe("radius lookup", () => {
  it("wraps around and interpolates between samples", () => {
    const profile = PROFILES.star;
    expect(radiusAtAngle(profile, 0)).toBeCloseTo(profile[0]!, 6);
    expect(radiusAtAngle(profile, TAU)).toBeCloseTo(profile[0]!, 6);
    expect(radiusAtAngle(profile, TAU / STEPS / 2)).toBeCloseTo((profile[0]! + profile[1]!) / 2, 6);
    expect(radiusAtAngle(profile, -TAU / STEPS / 2)).toBeCloseTo((profile[0]! + profile[STEPS - 1]!) / 2, 6);
  });
});
