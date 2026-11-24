// The eyes live on a sphere, not on a flat sheet. A head orientation (yaw, pitch, roll)
// gives each eye a tangent frame on the unit sphere, projected orthographically: the eye
// slides toward the limb as the head turns, its glyph foreshortens by the depth factor, and
// the pair picks up a rotation "for free". That projection is where the volume comes from.
//
// The maths is ported from bloub's src/bot/face.ts (MIT, (c) 2026 Jérémy Perret), including
// its `spin` composition; the constants (eye split, envelope amplitudes) are re-tuned for
// Cubes' own faces, which are larger glyphs on a smaller body.
//
// Liveliness is a pure function of t: noise drift at incommensurate periods, small saccades,
// a seeded blink calendar with occasional double blinks, and a breath. Nothing here keeps
// state between calls, so pausing, resuming or jumping to any date always renders the same
// picture, and two bots with different seeds never blink together.

import { TAU, clamp, createRng, hash, lerp, loopNoise } from "./math";

export type Vec3 = readonly [number, number, number];

export interface HeadGaze {
  /** Degrees, positive turns the face to its own right. */
  yaw: number;
  /** Degrees, positive looks up (screen y grows down). */
  pitch: number;
  /** Degrees, tilts the head in its own plane. */
  roll: number;
}

/** One eye projected onto the face. `a..d` is the tangent frame in SVG matrix order. */
export interface SphereEye {
  /** Projected centre, in units of the sphere radius. */
  x: number;
  y: number;
  a: number;
  b: number;
  c: number;
  d: number;
  /** Normal's z: 1 = facing the viewer, 0 = on the limb, negative = behind. */
  depth: number;
}

/** Rotates two orthonormal basis vectors inside their common plane. */
function spin(u: Vec3, v: Vec3, angle: number): [Vec3, Vec3] {
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  return [
    [u[0] * cos + v[0] * sin, u[1] * cos + v[1] * sin, u[2] * cos + v[2] * sin],
    [v[0] * cos - u[0] * sin, v[1] * cos - u[1] * sin, v[2] * cos - u[2] * sin],
  ];
}

/**
 * Tangent frame of each eye for a head orientation, in screen space (x right, y down,
 * z toward the viewer). Index 0 is the bot's left eye on screen, index 1 its right.
 * `split` is half the angle between them, in degrees.
 */
export function eyePoses(gaze: HeadGaze, split: number): [SphereEye, SphereEye] {
  return [surfacePose(gaze, -split, 0), surfacePose(gaze, split, 0)];
}

/**
 * Tangent frame of any feature on the face: `across` degrees toward the head's own right,
 * then `drop` degrees down. The eyes sit on the eye line (`drop` 0); the mouth and the
 * cheeks sit below it, so every feature turns, foreshortens and fades at the limb together.
 */
export function surfacePose(gaze: HeadGaze, across: number, drop: number): SphereEye {
  const deg = Math.PI / 180;
  let forward: Vec3 = [0, 0, 1];
  let right: Vec3 = [1, 0, 0];
  let down: Vec3 = [0, 1, 0];

  [forward, right] = spin(forward, right, gaze.yaw * deg);
  [down, forward] = spin(down, forward, gaze.pitch * deg);
  [right, down] = spin(right, down, gaze.roll * deg);

  const [aside, er] = spin(forward, right, across * deg);
  const [ef, ed] = spin(aside, down, drop * deg);
  return { x: ef[0], y: ef[1], a: er[0], b: er[1], c: ed[0], d: ed[1], depth: ef[2] };
}

export interface Liveliness {
  /** Gaze drift and saccades, in degrees. */
  dYaw: number;
  dPitch: number;
  /** The drift alone, without the saccades: what the body follows. A saccade is a flick of
   *  the eyes; a body that copied it would jump with every glance. */
  smoothYaw: number;
  smoothPitch: number;
  dRoll: number;
  /** 1 = lid open, 0 = shut. Applied as a vertical squash on screen. */
  lid: number;
  /** Body drift, in viewBox units: the weight shifting from one side to the other. */
  driftX: number;
  driftY: number;
  /** A slow sway of the whole body in depth, degrees: it turns a little left and right and
   *  nods a little up and down, on periods of its own. Never an in-plane roll. */
  swayYaw: number;
  swayPitch: number;
  /** Breath swing: add to 1 for the height, and `breathWidth` to 1 for the width. */
  breath: number;
  breathWidth: number;
}

/** Everything about a bot's idle motion that only its seed decides. */
export interface Rhythm {
  blinks: readonly number[];
  saccades: readonly { at: number; yaw: number; pitch: number }[];
  /** `saccades`' dates alone, the only thing the per-frame lookup bisects. */
  saccadeTimes: readonly number[];
  phases: readonly number[];
}

const BLINK_DUR = 0.18;
const SACCADE_DUR = 0.14;

/**
 * A pre-drawn blink calendar: 1.9 to 4.6 s apart, with a ~18 % chance of a double blink.
 * Drawn per seed, so a wall of avatars never blinks in unison. Generous horizon so a bot
 * left on screen for hours keeps blinking.
 */
export function buildRhythm(seed: string): Rhythm {
  const rng = createRng(hash(seed) ^ 0x5eed);
  const blinks: number[] = [];
  let clock = 1.1 + rng() * 2.4;
  while (clock < 7200) {
    blinks.push(clock);
    clock += 1.9 + rng() * 2.7;
    if (rng() < 0.18) {
      blinks.push(clock);
      clock += 0.24;
    }
  }

  const saccades: Rhythm["saccades"][number][] = [];
  let jump = 0.8 + rng() * 1.6;
  while (jump < 3600) {
    saccades.push({ at: jump, yaw: (rng() - 0.5) * 4.4, pitch: (rng() - 0.5) * 3.2 });
    jump += 1.4 + rng() * 2.6;
  }

  return {
    blinks,
    saccades,
    saccadeTimes: saccades.map((entry) => entry.at),
    phases: [rng() * TAU, rng() * TAU, rng() * TAU, rng() * TAU, rng() * TAU],
  };
}

/** Index of the last entry at or before `t`, or -1. */
function lastIndexAt(times: readonly number[], t: number): number {
  let low = 0;
  let high = times.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (times[middle]! <= t) {
      found = middle;
      low = middle + 1;
    } else high = middle - 1;
  }
  return found;
}

/**
 * 1 with the lid open, dipping to 0 through a blink: a fast close (40 % of the blink) and a
 * slower open, which is what makes a blink read as a blink instead of a wink.
 */
export function blinkLid(blinks: readonly number[], t: number): number {
  // Blinks are sorted, but a calendar of 2400 entries is cheap to binary-search.
  const index = lastIndexAt(blinks, t);
  if (index < 0) return 1;
  const k = (t - blinks[index]!) / BLINK_DUR;
  if (k >= 1) return 1;
  if (k <= 0.4) return 1 - k / 0.4;
  return (k - 0.4) / 0.6;
}

/** A skittish little jump that lands with an ease-out and holds until the next one. */
function saccadeAt(rhythm: Rhythm, t: number): { yaw: number; pitch: number } {
  const index = lastIndexAt(rhythm.saccadeTimes, t);
  if (index < 0) return { yaw: 0, pitch: 0 };
  const current = rhythm.saccades[index]!;
  const previous = rhythm.saccades[index - 1];
  const k = clamp((t - current.at) / SACCADE_DUR, 0, 1);
  const eased = 1 - (1 - k) ** 3;
  const fromYaw = previous?.yaw ?? 0;
  const fromPitch = previous?.pitch ?? 0;
  return { yaw: lerp(fromYaw, current.yaw, eased), pitch: lerp(fromPitch, current.pitch, eased) };
}

export interface LifeOptions {
  /** 1 = full drift, 0 = the head is held still by an external target. */
  wander?: number;
  blink?: boolean;
  /** Low detail keeps the blink and a gentler sway, which is all that reads at 24px. */
  subtle?: boolean;
  /** Seconds per breath. Slower while the bot sleeps. */
  breathPeriod?: number;
}

/**
 * The idle life of a face. `t` is seconds since the bot appeared; the same `t` always gives
 * the same result, whatever was sampled in between.
 */
export function liveliness(rhythm: Rhythm, t: number, options: LifeOptions = {}): Liveliness {
  const wander = options.wander ?? 1;
  const blink = options.blink ?? true;
  const subtle = options.subtle ?? false;
  const [p0, p1, p2, p3, p4] = rhythm.phases as [number, number, number, number, number];

  // Periods that do not divide each other: the drift never repeats in a way the eye can see.
  const noiseYaw = loopNoise(t, 11.3, p0) * 5.2 + loopNoise(t, 3.7, p1) * 1.5;
  const noisePitch = loopNoise(t, 9.1, p2) * 3.8 + loopNoise(t, 4.3, p3) * 1.2;
  const jump = subtle ? { yaw: 0, pitch: 0 } : saccadeAt(rhythm, t);

  // A slow sway in depth on periods of its own: the body never quite settles, so it reads as
  // alive even while the face holds still. Low detail keeps a third of it.
  const swayScale = subtle ? 0.35 : 1;
  const breathPeriod = options.breathPeriod ?? 3.4;
  const swell = Math.sin((t / breathPeriod) * TAU);

  return {
    dYaw: (noiseYaw + jump.yaw) * wander,
    dPitch: (noisePitch + jump.pitch) * wander,
    smoothYaw: noiseYaw * wander,
    smoothPitch: noisePitch * wander,
    dRoll: loopNoise(t, 13.7, p4) * 2.0 * wander,
    lid: blink ? blinkLid(rhythm.blinks, t) : 1,
    driftX: (subtle ? 0.2 : 1) * loopNoise(t, 7.9, p1) * 0.7,
    driftY: (subtle ? 0.2 : 1) * loopNoise(t, 5.3, p0) * 0.6,
    swayYaw: loopNoise(t, 8.3, p4) * 10 * swayScale,
    swayPitch: loopNoise(t, 10.1, p2) * 7 * swayScale,
    // A little more than one percent, with the width giving way as the height grows: a blob
    // cannot gain mass from nowhere, and the counter-change is what makes it read as breath.
    breath: (subtle ? 0.004 : 0.012) * swell,
    breathWidth: -(subtle ? 0.002 : 0.006) * swell,
  };
}

/** 1 = open, 0.06 = shut. The lid never collapses to a line of zero height. */
export function blinkScale(lid: number): number {
  return 0.06 + 0.94 * clamp(lid);
}

/**
 * A blink forced by an event (a masked state change): one full close-and-open over `duration`.
 * Returns 1 outside the window.
 */
export function forcedLid(since: number, duration: number): number {
  const k = since / duration;
  if (!(k >= 0 && k <= 1)) return 1;
  return Math.abs(k * 2 - 1);
}
