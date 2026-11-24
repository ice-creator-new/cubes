// Fourteen radial silhouettes sampled on 64 evenly spaced angles. Every profile is a radial
// function r(θ) from one centre, so the body stays star-shaped and eyes can be fitted by
// angle.
//
// Each silhouette is described once as an implicit field in a unit space — a rounded polygon
// (a smooth-minimum of half-planes, so a corner is a fillet rather than a cusp), a lobed
// radial curve, a capsule or a superellipse. The profile is that field's distance along each
// sampled ray, found by bisection at module load. Rotations, leans and stretches are applied
// to the field's coordinates, so the lean is part of the profile and nothing has to be
// transformed twice.
//
// Every shape is normalised to the same visual weight (area) and then capped, by lowering the
// scale rather than clipping a tip, so a long silhouette still fits the viewBox.

import type { ShapeId } from "./catalog";
import { TAU } from "./math";

/** Radius of the reference circle, in the 0 0 100 100 viewBox. */
export const R = 38;
export const CENTER = { x: 50, y: 52 };
/**
 * Samples per silhouette. 120 is divisible by 3, 4, 5 and 6, so every fold of every
 * silhouette lands on identical sample phases: a six-point star is sampled the same way on
 * all six arms and cannot come out lopsided, and a five-petal shape is as even as a four-lobe
 * one.
 */
export const PROFILE_SAMPLES = 120;
/** The furthest any profile sample may sit from the centre. */
export const MAX_RADIUS = 46;

const DEG = Math.PI / 180;

/** A signed field: negative inside, zero on the outline, positive outside. */
type Field = (x: number, y: number) => number;

function mod(value: number, period: number): number {
  return ((value % period) + period) % period;
}

/** Evaluate `field` in a frame rotated by `angle`, so the silhouette leans. */
function rotated(field: Field, angle: number): Field {
  const cos = Math.cos(angle * DEG);
  const sin = Math.sin(angle * DEG);
  return (x, y) => field(x * cos + y * sin, -x * sin + y * cos);
}

/** Evaluate `field` with the axes stretched: wider or taller without changing its character. */
function stretched(field: Field, kx: number, ky: number): Field {
  return (x, y) => field(x / kx, y / ky);
}

/**
 * A regular n-gon with filleted corners: every point within `round` of the polygon inset by
 * the same distance. The rounding is exact — each corner is an arc of radius `round` and the
 * sides between them stay flat — so a hexagon keeps a flat top and no corner is a cusp.
 * `phase` places the first vertex, and with it where a point or a flat side sits.
 */
function roundedPolygon(sides: number, phase: number, round: number): Field {
  const corners = Array.from({ length: sides }, (_, index) => {
    const angle = phase * DEG + (index / sides) * TAU;
    return { x: Math.cos(angle), y: Math.sin(angle) };
  });
  return (x, y) => {
    let inside = true;
    let nearest = Number.POSITIVE_INFINITY;
    for (let index = 0; index < sides; index += 1) {
      const from = corners[index]!;
      const to = corners[(index + 1) % sides]!;
      const ex = to.x - from.x;
      const ey = to.y - from.y;
      if (ex * (y - from.y) - ey * (x - from.x) < 0) inside = false;
      const along = Math.max(
        0,
        Math.min(1, ((x - from.x) * ex + (y - from.y) * ey) / (ex * ex + ey * ey)),
      );
      nearest = Math.min(nearest, Math.hypot(x - (from.x + ex * along), y - (from.y + ey * along)));
    }
    return (inside ? -nearest : nearest) - round;
  };
}

interface Lobes {
  /** How many lobes. A point count for the star, a petal count for the flower. */
  count: number;
  /** Lobe amplitude as a fraction of the base radius. */
  amp: number;
  /** A second harmonic, positive to squeeze the notches narrow and keep the tips broad. */
  pinch?: number;
  /** Where a lobe points, degrees. 0 = right, -90 = up. */
  phase?: number;
}

/** `1 + a·cos(kθ)`, optionally narrowed by a 2k harmonic: a flower, a clover, a star. */
function lobed({ count, amp, pinch = 0, phase = 0 }: Lobes): Field {
  const turn = phase * DEG;
  return (x, y) => {
    const angle = Math.atan2(y, x);
    const radius = 1 + amp * Math.cos(count * (angle - turn)) + pinch * Math.cos(2 * count * (angle - turn));
    return Math.hypot(x, y) - radius;
  };
}

/** A horizontal capsule: a segment of half length `half`, swept by a circle of `cap`. */
function capsule(half: number, cap: number): Field {
  return (x, y) => Math.hypot(Math.max(Math.abs(x) - half, 0), y) - cap;
}

/** A superellipse |x|^n + |y|^n = 1; n = 2 is a circle, larger n squares it off. */
function superellipse(power: number): Field {
  return (x, y) => (Math.abs(x) ** power + Math.abs(y) ** power) ** (1 / power) - 1;
}

/** A dome of radius 1 standing on straight sides, with a flat, softly cornered base: a
 *  touch taller than wide. Above the dome's equator it is the circle; below, the sides run
 *  straight down, so the two meet tangentially with no seam. */
function arch(): Field {
  const top = -0.15;
  const base = 0.95;
  const corner = 0.32;
  return (x, y) => {
    if (y <= top) return Math.hypot(x, y - top) - 1;
    const ax = Math.abs(x);
    if (y > base - corner && ax > 1 - corner) return Math.hypot(ax - (1 - corner), y - (base - corner)) - corner;
    return Math.max(ax - 1, y - base);
  };
}

/** `field` moved by (`dx`, `dy`) and scaled by `k`: a part to union onto a head. */
function placed(field: Field, dx: number, dy: number, k: number): Field {
  return (x, y) => field((x - dx) / k, (y - dy) / k);
}

/** The union of several fields: a head with ears. Only the sign is read, so scales may differ. */
function union(...fields: Field[]): Field {
  return (x, y) => Math.min(...fields.map((field) => field(x, y)));
}

const circle: Field = (x, y) => Math.hypot(x, y) - 1;

const FIELDS: Record<ShapeId, Field> = {
  // A circle. The reference of the family.
  round: superellipse(2),
  // An organic circle with five soft bulges, wider than it is tall. Two harmonics out of
  // step with each other, so the bulges are not a regular pattern; the distinction from the
  // pebble is that the lumps are deep enough to read at 30px.
  blob: stretched(
    (x, y) => {
      const angle = Math.atan2(y, x);
      const radius = 1 + 0.085 * Math.cos(5 * angle + 0.55) + 0.042 * Math.cos(3 * angle + 2.2);
      return Math.hypot(x, y) - radius;
    },
    1.1,
    1,
  ),
  // A rounded square, square-on.
  squircle: roundedPolygon(4, 45, 0.3),
  // Four broad points with concave notches, a touch taller than wide. A wide fillet keeps
  // the junctions soft instead of pinching into cusps.
  sparkle: stretched(lobed({ count: 4, amp: 0.2, pinch: 0.05 }), 1, 1.02),
  // Four fat equal lobes: a puffy clover. The notches are filleted generously, so they stay
  // open at 30px without turning into creases.
  clover: lobed({ count: 4, amp: 0.19, phase: 45 }),
  // A wide rounded top tapering to a soft point at the lower left: a pear, with a third
  // harmonic so it reads as a soft triangle rather than an egg.
  teardrop: (x, y) => {
    const angle = Math.atan2(y, x);
    const radius =
      1 - 0.27 * Math.cos(angle - 140 * DEG) + 0.11 * Math.cos(3 * angle - 200 * DEG);
    return Math.hypot(x, y) - radius;
  },
  // Six even petals, deep enough to read as petals at 30px rather than as a lumpy circle.
  flower: lobed({ count: 6, amp: 0.115, pinch: -0.03, phase: -90 }),
  // A triangle on a flat-ish base, pointing up, corners heavily filleted.
  peak: roundedPolygon(3, -90, 0.52),
  // A horizontal capsule, tilted a few degrees off level.
  capsule: rotated(capsule(0.55, 1), -4),
  // An arch: a round dome on straight sides and a flat, softly cornered base.
  arch: arch(),
  // A pentagon, point up.
  pentagon: roundedPolygon(5, -90, 0.3),
  // Six soft points at N, NE, SE, S, SW, NW with concave notches: a regular six-point
  // skeleton in polar form, deeply indented and then filleted with one width.
  star: lobed({ count: 6, amp: 0.2, pinch: 0.03, phase: -90 }),
  // A hexagon with a flat top and a flat bottom.
  hexagon: roundedPolygon(6, 0, 0.28),
  // A smooth stone, flattened and never symmetric: no feature big enough to look like a
  // fault, and no lumps, which is what separates it from the blob.
  pebble: stretched(
    (x, y) => {
      const angle = Math.atan2(y, x);
      const radius =
        1 +
        0.012 * Math.cos(angle + 1.1) +
        0.022 * Math.cos(2 * angle + 2.2) +
        0.014 * Math.cos(3 * angle + 0.5) +
        0.006 * Math.cos(5 * angle + 3.4);
      return Math.hypot(x, y) - radius;
    },
    1,
    0.85,
  ),
  // The classic implicit heart, (x² + y² − 1)³ = x²y³ with y up: two lobes and a point.
  heart: (x, y) => {
    const up = -y * 1.08 + 0.12;
    return (x * x + up * up - 1) ** 3 - x * x * up ** 3;
  },
  // An egg: rounder and fuller at the bottom, narrower at the top, standing upright.
  egg: stretched(
    (x, y) => {
      const angle = Math.atan2(y, x);
      return Math.hypot(x, y) - (1 + 0.13 * Math.sin(angle));
    },
    0.86,
    1.08,
  ),
  // A rhombus standing on a point, taller than wide, every corner softened.
  diamond: stretched(roundedPolygon(4, -90, 0.32), 0.84, 1.08),
  // A regular octagon with a flat top, barely rounded so the eight sides stay readable.
  octagon: roundedPolygon(8, 22.5, 0.1),
  // A round head with two pointed ears.
  cat: union(
    circle,
    placed(roundedPolygon(3, -90, 0.22), -0.6, -0.72, 0.42),
    placed(roundedPolygon(3, -90, 0.22), 0.6, -0.72, 0.42),
  ),
  // A round head with two round ears.
  bear: union(circle, placed(circle, -0.72, -0.72, 0.42), placed(circle, 0.72, -0.72, 0.42)),
  // A dome on straight sides with a hem of three scallops along the bottom.
  ghost: (x, y) => {
    if (y <= 0) return Math.hypot(x, y) - 1;
    const hem = 0.86 + 0.13 * Math.cos(x * Math.PI * 1.5);
    return Math.max(Math.abs(x) - 1, y - hem);
  },
};

/**
 * The distance from the origin to the outline along `angle`, in the field's own units. Every
 * silhouette is star-shaped, so the ray meets the outline exactly once and a bisection
 * between the inside origin and a far-out sample finds it.
 */
function rayDistance(field: Field, angle: number): number {
  const ux = Math.cos(angle);
  const uy = Math.sin(angle);
  let low = 0;
  let high = 3;
  for (let step = 0; step < 40; step += 1) {
    const middle = (low + high) / 2;
    if (field(middle * ux, middle * uy) <= 0) low = middle;
    else high = middle;
  }
  return (low + high) / 2;
}

function polygonArea(points: { x: number; y: number }[]): number {
  let total = 0;
  for (let i = 0; i < points.length; i += 1) {
    const here = points[i]!;
    const next = points[(i + 1) % points.length]!;
    total += here.x * next.y - next.x * here.y;
  }
  return Math.abs(total) / 2;
}

/**
 * Circular smoothing with a triangular kernel: a fillet `width` degrees wide, applied to the
 * radius. It rounds a lobed tip and a concave notch alike, and because the kernel is a fixed
 * width in angle it never moves a flat side of a polygon (those shapes carry no fillet here;
 * their fillets are exact arcs in the field).
 */
function filetRadii(raw: readonly number[], width: number): number[] {
  const radius = Math.max(0, Math.round((width / 360) * raw.length));
  if (radius === 0) return [...raw];
  const weights = Array.from({ length: radius * 2 + 1 }, (_, index) => radius + 1 - Math.abs(index - radius));
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  return raw.map((_, index) => {
    let sum = 0;
    for (let offset = -radius; offset <= radius; offset += 1) {
      sum += raw[(index + offset + raw.length) % raw.length]! * weights[offset + radius]!;
    }
    return sum / total;
  });
}

/**
 * Equal visual weight: every profile is scaled to the area of the reference circle, then
 * capped. The cap lowers the scale, so a tip is never sliced off flat at the viewBox edge.
 */
function normalize(field: Field, fillet = 0): readonly number[] {
  let raw: number[] = [];
  for (let index = 0; index < PROFILE_SAMPLES; index += 1) {
    raw.push(rayDistance(field, (index / PROFILE_SAMPLES) * TAU));
  }
  raw = filetRadii(raw, fillet);
  const unitPoints = raw.map((value, index) => {
    const angle = (index / PROFILE_SAMPLES) * TAU;
    return { x: Math.cos(angle) * value, y: Math.sin(angle) * value };
  });
  const areaScale = Math.sqrt((Math.PI * R * R) / polygonArea(unitPoints));
  const widest = Math.max(...raw);
  const scale = Math.min(areaScale, MAX_RADIUS / widest);
  return raw.map((value) => value * scale);
}

export const PROFILES: Record<ShapeId, readonly number[]> = {
  round: normalize(FIELDS.round),
  blob: normalize(FIELDS.blob, 8),
  squircle: normalize(FIELDS.squircle),
  sparkle: normalize(FIELDS.sparkle, 14),
  clover: normalize(FIELDS.clover, 20),
  teardrop: normalize(FIELDS.teardrop, 6),
  flower: normalize(FIELDS.flower, 12),
  peak: normalize(FIELDS.peak),
  capsule: normalize(FIELDS.capsule),
  arch: normalize(FIELDS.arch),
  pentagon: normalize(FIELDS.pentagon),
  star: normalize(FIELDS.star, 16),
  hexagon: normalize(FIELDS.hexagon),
  pebble: normalize(FIELDS.pebble, 6),
  heart: normalize(FIELDS.heart, 22),
  egg: normalize(FIELDS.egg),
  diamond: normalize(FIELDS.diamond),
  octagon: normalize(FIELDS.octagon),
  cat: normalize(FIELDS.cat, 16),
  bear: normalize(FIELDS.bear, 18),
  ghost: normalize(FIELDS.ghost, 10),
};

/** Linear lookup between the stored samples. */
export function radiusAtAngle(profile: readonly number[], angle: number): number {
  const turns = (mod(angle, TAU) / TAU) * PROFILE_SAMPLES;
  const index = Math.floor(turns) % PROFILE_SAMPLES;
  const next = (index + 1) % PROFILE_SAMPLES;
  const fraction = turns - Math.floor(turns);
  const from = profile[index] ?? R;
  const to = profile[next] ?? R;
  return from + (to - from) * fraction;
}

export function profilePoint(profile: readonly number[], index: number, scale = 1): { x: number; y: number } {
  const theta = (index / PROFILE_SAMPLES) * TAU;
  const radius = (profile[index] ?? R) * scale;
  return { x: CENTER.x + Math.cos(theta) * radius, y: CENTER.y + Math.sin(theta) * radius };
}

/** Closed Catmull-Rom spline through the samples, emitted as cubic beziers. */
export function profilePath(profile: readonly number[], scale = 1): string {
  const points = profile.map((_, index) => profilePoint(profile, index, scale));
  const last = points.length - 1;
  const at = (index: number) => points[((index % points.length) + points.length) % points.length]!;
  const round = (value: number) => Math.round(value * 100) / 100;
  let path = `M${round(at(0).x)} ${round(at(0).y)}`;
  for (let index = 0; index <= last; index += 1) {
    const previous = at(index - 1);
    const here = at(index);
    const next = at(index + 1);
    const after = at(index + 2);
    const control1 = { x: here.x + (next.x - previous.x) / 6, y: here.y + (next.y - previous.y) / 6 };
    const control2 = { x: next.x - (after.x - here.x) / 6, y: next.y - (after.y - here.y) / 6 };
    path += `C${round(control1.x)} ${round(control1.y)} ${round(control2.x)} ${round(control2.y)} ${round(next.x)} ${round(next.y)}`;
  }
  return `${path}Z`;
}
