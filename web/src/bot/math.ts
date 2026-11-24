// Small numeric helpers shared by the avatar engine. No React, no DOM.

export const TAU = Math.PI * 2;
export const DEG = Math.PI / 180;

export function clamp(value: number, low = 0, high = 1): number {
  return value < low ? low : value > high ? high : value;
}

export function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

export function easeOutQuint(t: number): number {
  const p = clamp(t, 0, 1);
  const inverse = 1 - p;
  return 1 - inverse * inverse * inverse * inverse * inverse;
}

export function easeOutCubic(t: number): number {
  const p = clamp(t, 0, 1);
  return 1 - (1 - p) ** 3;
}

/**
 * Looped 1D noise: three sines at 1x, 2x and 3x, so the curve repeats exactly after
 * `period` but never reads as a single sine. The gaze drift is built from a few of these
 * at incommensurate periods (11.3s, 3.7s, …), so it never visibly repeats.
 */
export function loopNoise(t: number, period: number, seed = 0): number {
  const p = (t / period) * TAU;
  return (
    0.55 * Math.sin(p + seed) +
    0.3 * Math.sin(2 * p + seed * 1.7 + 1.1) +
    0.15 * Math.sin(3 * p + seed * 2.3 + 2.4)
  );
}

/** Deterministic PRNG (mulberry32): the same seed always draws the same sequence. */
export function createRng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let value = Math.imul(a ^ (a >>> 15), 1 | a);
    value = (value + Math.imul(value ^ (value >>> 7), 61 | value)) ^ value;
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Piecewise smooth interpolation through `[u, value]` keys, both ends included. Used by the
 * gesture scripts, which are written as a few poses and eased between them.
 */
export function keyframes(u: number, keys: readonly (readonly [number, number])[]): number {
  const first = keys[0];
  const last = keys[keys.length - 1];
  if (!first || !last) return 0;
  if (u <= first[0]) return first[1];
  if (u >= last[0]) return last[1];
  for (let index = 0; index < keys.length - 1; index += 1) {
    const from = keys[index]!;
    const to = keys[index + 1]!;
    if (u > to[0]) continue;
    const span = to[0] - from[0];
    const k = span <= 0 ? 1 : (u - from[0]) / span;
    return lerp(from[1], to[1], k * k * (3 - 2 * k));
  }
  return last[1];
}

/** FNV-1a over UTF-16 code units; stable across runs so a seed always draws the same face. */
export function hash(text: string): number {
  let value = 2166136261;
  for (let i = 0; i < text.length; i += 1) value = Math.imul(value ^ text.charCodeAt(i), 16777619);
  return value >>> 0;
}

/** A deterministic 0..1 value from a hash and a salt, so several streams can share one seed. */
export function unit(hashValue: number, salt: number): number {
  let value = Math.imul(hashValue ^ Math.imul(salt + 1, 2654435761), 2246822519) >>> 0;
  value = Math.imul(value ^ (value >>> 15), 3266489917) >>> 0;
  return (value >>> 8) / 0x1000000;
}

function hexToRGB(hex: string): [number, number, number] {
  const text = hex.replace("#", "");
  const value = Number.parseInt(text, 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function channel(value: number): string {
  return Math.round(clamp(value, 0, 255)).toString(16).padStart(2, "0");
}

function rgbToHex(rgb: [number, number, number]): string {
  return `#${channel(rgb[0])}${channel(rgb[1])}${channel(rgb[2])}`;
}

export function lerpHex(a: string, b: string, t: number): string {
  if (t <= 0) return a;
  if (t >= 1) return b;
  const left = hexToRGB(a);
  const right = hexToRGB(b);
  return rgbToHex([lerp(left[0], right[0], t), lerp(left[1], right[1], t), lerp(left[2], right[2], t)]);
}

/** WCAG relative luminance of an sRGB hex colour. */
export function luminance(hex: string): number {
  const [r, g, b] = hexToRGB(hex).map((value) => {
    const channelValue = value / 255;
    return channelValue <= 0.04045 ? channelValue / 12.92 : ((channelValue + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(a: string, b: string): number {
  const left = luminance(a);
  const right = luminance(b);
  const high = Math.max(left, right);
  const low = Math.min(left, right);
  return (high + 0.05) / (low + 0.05);
}
