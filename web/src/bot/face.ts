// Eye glyphs on a spherical head, fitted to each silhouette once at module load.
//
// Two ideas are ported from bloub (MIT, (c) 2026 Jérémy Perret):
//
// - `src/bot/face.ts`: the tangent frame of each eye on the unit sphere, the orthographic
//   projection and the screen-space blink squash. Eyes foreshorten and rotate by themselves
//   near the limb, which is where the volume comes from.
// - `src/bot/eyefit.ts`: the eye offset is TABULATED per (shape, face, eye) and interpolated,
//   never re-solved per frame. Solving per frame reacts to the drift, the pointer and the
//   morph, and jitters; a table entry is a constant and interpolating between two constants
//   is monotone by construction.
//
// The glyph vocabulary (capsule, arc, line, dot, chevron), the silhouettes, the palette and
// every measured constant below are Cubes' own, re-tuned for a 100-unit viewBox with R = 38.

import { FACES, type Expression, type FaceKey, type EyeKind, type MouthShape } from "./expressions";
import { PROFILES, CENTER, R, profilePoint, radiusAtAngle } from "./shapes";
import { SHAPES, type ShapeId } from "./catalog";
import { DEG, clamp } from "./math";
import { blinkScale, eyePoses, surfacePose, type HeadGaze, type SphereEye } from "./sphere";

export type EyeIndex = 0 | 1;

export interface Point {
  x: number;
  y: number;
}

/** Splits are authored as a fraction of the room the shape has; this turns one into degrees. */
export const SPLIT_DEG = 58;

/** Envelope of the pointer look, in degrees. Mirrors `LOOK` in engine.ts. */
export const LOOK_YAW = 16;
export const LOOK_PITCH = 13;
/** Envelope of the idle drift and saccades, in degrees: the exact bounds of `liveliness`. */
const LIFE_YAW = 5.2 + 1.5 + 2.2;
const LIFE_PITCH = 3.8 + 1.2 + 1.6;
/** Pointer tracking keeps a little wander; the fit covers it. */
const HERE_YAW = LOOK_YAW + LIFE_YAW;
const HERE_PITCH = LOOK_PITCH + LIFE_PITCH;

const EYE_LINE = -1;
const MARGIN = 1.2;
/** Vertical half-height of the band the eyes live in, used to measure room per shape. */
const EYE_BAND = 15;

/** Local glyph geometry: `w` across the eye's own axis, `h` along it. */
export interface EyeSpec {
  kind: EyeKind;
  w: number;
  h: number;
  /** In-plane tilt, radians, positive turns the top of the glyph to the right. */
  tilt: number;
  /** 0..1 eyelid the expression asks for. */
  open: number;
}

/** A face before the sphere: gaze in degrees, split in degrees, glyphs in viewBox units. */
export interface FaceFrame {
  gaze: HeadGaze;
  split: number;
  eyes: readonly [EyeSpec, EyeSpec];
  mouth: MouthShape;
  /** 0..1 strength of the cheeks. */
  blush: number;
  alpha: number;
}

export interface EyePose {
  kind: EyeKind;
  index: EyeIndex;
  /** Projected centre in body space, viewBox units. */
  cx: number;
  cy: number;
  /** Glyph size in body space along its own axes. */
  w: number;
  h: number;
  /** In-plane rotation of the glyph, radians, screen convention (y down, positive clockwise). */
  tilt: number;
  /** 1 facing the viewer, 0 on the limb, < 0 behind the head. */
  depth: number;
  /** Layer opacity of this eye, already faded at the limb. */
  alpha: number;
  /** SVG matrix mapping the local glyph to body space. */
  matrix: { a: number; b: number; c: number; d: number; e: number; f: number };
}

export function faceFrameOf(expression: Expression): FaceFrame {
  return {
    gaze: { yaw: expression.gaze.yaw / DEG, pitch: -expression.gaze.pitch / DEG, roll: expression.gaze.roll / DEG },
    split: expression.split * SPLIT_DEG,
    eyes: [
      { kind: expression.eyes[0].kind, w: expression.eyes[0].w, h: expression.eyes[0].h, tilt: expression.eyes[0].tilt, open: expression.eyes[0].open },
      { kind: expression.eyes[1].kind, w: expression.eyes[1].w, h: expression.eyes[1].h, tilt: expression.eyes[1].tilt, open: expression.eyes[1].open },
    ],
    mouth: { ...expression.mouth },
    blush: expression.blush,
    alpha: 1,
  };
}

function rotate2(x: number, y: number, angle: number): { x: number; y: number } {
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  return { x: x * cos - y * sin, y: x * sin + y * cos };
}

/**
 * Half-width of the body at eye height, per shape. Wide blobs spread their eyes into the
 * flanks; a narrow outline keeps them close to the middle.
 */
function bandHalfFor(profile: readonly number[]): number {
  let narrowest = Number.POSITIVE_INFINITY;
  for (let index = 0; index < profile.length; index += 1) {
    const point = profilePoint(profile, index);
    if (Math.abs(point.y - CENTER.y) > EYE_BAND) continue;
    narrowest = Math.min(narrowest, Math.abs(point.x - CENTER.x));
  }
  return narrowest - 1.2;
}

const EYE_BAND_HALF: Record<ShapeId, number> = Object.fromEntries(
  SHAPES.map((shape) => [shape, bandHalfFor(PROFILES[shape])]),
) as Record<ShapeId, number>;

/**
 * How far apart the eyes may sit on this silhouette, as a half split in degrees: the face's
 * own split, kept inside the body and never so tight that the glyphs touch.
 */
export function splitFor(shape: ShapeId, face: FaceFrame): number {
  const band = EYE_BAND_HALF[shape];
  // sin(split) * R is the on-face half distance, measured on the reference circle.
  const wanted = Math.sin(clamp(face.split, 0, 60) * DEG) * 38;
  const tightest = Math.max(face.eyes[0].w, face.eyes[1].w) / 2 + 5;
  const room = Math.min(Math.max(wanted, tightest), Math.max(tightest, band - 4));
  // The eye is glued to the contour, so a narrow silhouette pulls the pair together. Widen
  // the split until the gap survives that, or the two dots of a surprised face touch.
  const profile = PROFILES[shape];
  const across = Math.min(radiusAtAngle(profile, 0), radiusAtAngle(profile, Math.PI));
  let value = Math.asin(clamp(room / 38, 0, 1)) / DEG;
  for (let step = 0; step < 4 && Math.sin(value * DEG) * across < room; step += 1) {
    value = Math.asin(clamp(room / Math.max(across, 1), 0, 1)) / DEG;
    if (value > 60) break;
  }
  return value;
}

/**
 * One eye projected onto the face and glued to the silhouette: `radiusAt` reports the body
 * radius in viewBox units along a direction, so the eye follows the contour instead of
 * assuming a circle. `fit` is the tabulated inward offset.
 */
function placeEye(
  frame: FaceFrame,
  sphere: SphereEye,
  spec: EyeSpec,
  index: EyeIndex,
  radiusAt: (x: number, y: number) => number,
  fit: Point,
  lid: number,
): EyePose {
  const depth = sphere.depth;
  const radius = radiusAt(sphere.x, sphere.y);
  const cx = CENTER.x + sphere.x * radius + fit.x;
  const cy = CENTER.y + EYE_LINE + sphere.y * radius + fit.y;

  const cos = Math.cos(spec.tilt);
  const sin = Math.sin(spec.tilt);
  // Basis × tilt: the glyph's own tilt lives inside the tangent frame, so mirrored tilts
  // (anger, sadness) are reachable even though the head roll leans both eyes the same way.
  const ax = sphere.a * cos + sphere.c * sin;
  const ay = sphere.b * cos + sphere.d * sin;
  const bx = -sphere.a * sin + sphere.c * cos;
  const by = -sphere.b * sin + sphere.d * cos;
  // The blink is a vertical squash in SCREEN space, so it lands after the tangent frame.
  const k = blinkScale(Math.min(lid, spec.open));

  return {
    kind: spec.kind,
    index,
    cx,
    cy,
    w: spec.w,
    h: spec.h,
    tilt: spec.tilt,
    depth,
    alpha: clamp(depth / 0.12, 0, 1) * frame.alpha,
    matrix: { a: ax, b: ay * k, c: bx, d: by * k, e: cx, f: cy },
  };
}

/** Project both eyes. `lid` is the eyelid at this instant, 0..1. */
export function placeEyes(
  frame: FaceFrame,
  radiusAt: (x: number, y: number) => number,
  fit: readonly [Point, Point],
  lid: number,
): [EyePose, EyePose] {
  const spheres = eyePoses(frame.gaze, frame.split);
  return [
    placeEye(frame, spheres[0], frame.eyes[0], 0, radiusAt, fit[0], lid),
    placeEye(frame, spheres[1], frame.eyes[1], 1, radiusAt, fit[1], lid),
  ];
}

/* ------------------------------------------------------- mouth and cheeks */

/** How far below the eye line the mouth sits on the sphere, degrees. */
const MOUTH_DROP = 27;
/** The cheeks: below the eye line, and a little further out than the eyes. */
const CHEEK_DROP = 24;
const CHEEK_OUT = 12;
/** Cheek half-axes, viewBox units; the renderer draws a unit circle through the matrix. */
const CHEEK_RX = 6.4;
const CHEEK_RY = 3.9;
/** Mouth line width, viewBox units: thin enough to stay a line at 30px, and never a hairline. */
export const MOUTH_STROKE = 2.6;

/** A feature glued to the sphere: centre, depth and a matrix from local to body space. */
export interface FeaturePose {
  cx: number;
  cy: number;
  depth: number;
  alpha: number;
  matrix: { a: number; b: number; c: number; d: number; e: number; f: number };
}

export interface FaceFeatures {
  mouth: FeaturePose;
  /** The cheeks' matrices map the unit circle onto the cheek ellipse. */
  cheeks: [FeaturePose, FeaturePose];
}

function placeFeature(
  gaze: HeadGaze,
  across: number,
  drop: number,
  tilt: number,
  radiusAt: (x: number, y: number) => number,
  alpha: number,
  sx = 1,
  sy = 1,
): FeaturePose {
  const sphere = surfacePose(gaze, across, drop);
  // The lower face rides the sphere, not the outline: a lobe or a tip below the eyes must
  // not drag the mouth away from them, and a narrow outline still pulls it in.
  const radius = Math.min(radiusAt(sphere.x, sphere.y), R);
  const cx = CENTER.x + sphere.x * radius;
  const cy = CENTER.y + EYE_LINE + sphere.y * radius;
  const cos = Math.cos(tilt);
  const sin = Math.sin(tilt);
  return {
    cx,
    cy,
    depth: sphere.depth,
    alpha: clamp(sphere.depth / 0.12, 0, 1) * alpha,
    matrix: {
      a: (sphere.a * cos + sphere.c * sin) * sx,
      b: (sphere.b * cos + sphere.d * sin) * sx,
      c: (-sphere.a * sin + sphere.c * cos) * sy,
      d: (-sphere.b * sin + sphere.d * cos) * sy,
      e: cx,
      f: cy,
    },
  };
}

/** The mouth and both cheeks for a face, on the same sphere as its eyes. */
export function placeFeatures(frame: FaceFrame, radiusAt: (x: number, y: number) => number): FaceFeatures {
  const [left, right] = [-1, 1].map((side) =>
    placeFeature(frame.gaze, side * (frame.split + CHEEK_OUT), CHEEK_DROP, 0, radiusAt, frame.alpha * frame.blush, CHEEK_RX, CHEEK_RY),
  ) as [FeaturePose, FeaturePose];
  return {
    mouth: placeFeature(frame.gaze, 0, MOUTH_DROP, frame.mouth.tilt, radiusAt, frame.alpha),
    cheeks: [left, right],
  };
}

/**
 * The mouth in local coordinates: the upper and the lower lip as two quadratics between the
 * same corners. Painted with a round stroke and a fill, so a closed mouth is a soft line with
 * round ends and an open one is a filled shape with the same rounded rim.
 */
export function mouthPath(shape: MouthShape): string {
  const half = roundTo(shape.w / 2);
  return `M${-half} 0Q0 ${roundTo(shape.top * 2)} ${half} 0Q0 ${roundTo(shape.bottom * 2)} ${-half} 0Z`;
}

/** The tongue of an open smile, local to the mouth; `opacity` 0 for anything else. */
export function tongueOf(shape: MouthShape): { cx: number; cy: number; rx: number; ry: number; opacity: number } {
  const open = shape.bottom - shape.top;
  // Only a smile shows it: a raised upper lip is a gasp or a grimace.
  const opacity = clamp((open - 3.5) / 3) * clamp(shape.top + 1);
  const ry = Math.max(0, open * 0.5);
  return { cx: 0, cy: roundTo(shape.bottom - ry * 0.3), rx: roundTo(shape.w * 0.31), ry: roundTo(ry), opacity: roundTo(opacity) };
}

/* ------------------------------------------------------------------ glyphs */

interface Glyph {
  kind: EyeKind;
  index: EyeIndex;
  w: number;
  h: number;
}

function roundTo(value: number): number {
  return Math.round(value * 100) / 100;
}

/** A stadium centred on the local origin, wide or tall. */
function stadiumPath(w: number, h: number): string {
  const halfW = w / 2;
  const halfH = h / 2;
  const at = (x: number, y: number) => `${roundTo(x)} ${roundTo(y)}`;
  if (h >= w) {
    const r = halfW;
    const cap = halfH - halfW;
    return `M${at(-r, -cap)}L${at(-r, cap)}A${r} ${r} 0 0 0 ${at(r, cap)}L${at(r, -cap)}A${r} ${r} 0 0 0 ${at(-r, -cap)}Z`;
  }
  const r = halfH;
  const cap = halfW - halfH;
  return `M${at(-cap, -r)}L${at(cap, -r)}A${r} ${r} 0 0 1 ${at(cap, r)}L${at(-cap, r)}A${r} ${r} 0 0 1 ${at(-cap, -r)}Z`;
}

const ARC_STEPS = 10;
const CAP_STEPS = 6;

/**
 * A curved stroke of even thickness with round caps: a quadratic centre line offset to both
 * sides and closed with a half circle at each end. Pointed tips read as sharp brows and make
 * a smile look sly; round ends read as the soft "^ ^" of a closed, smiling eye.
 */
function arcOutline(glyph: Glyph, up: boolean): Point[] {
  const thickness = Math.max(3.2, glyph.h * 0.42);
  const r = thickness / 2;
  const flip = up ? 1 : -1;
  const ends = flip * (glyph.h / 2 - r);
  const apex = -ends;
  const from = { x: -(glyph.w / 2 - r), y: ends };
  const to = { x: glyph.w / 2 - r, y: ends };
  const control = { x: 0, y: 2 * apex - ends };
  const along = (t: number) => {
    const inverse = 1 - t;
    const point = {
      x: inverse * inverse * from.x + 2 * inverse * t * control.x + t * t * to.x,
      y: inverse * inverse * from.y + 2 * inverse * t * control.y + t * t * to.y,
    };
    const dx = 2 * inverse * (control.x - from.x) + 2 * t * (to.x - control.x);
    const dy = 2 * inverse * (control.y - from.y) + 2 * t * (to.y - control.y);
    const length = Math.hypot(dx, dy) || 1;
    return { point, ux: dx / length, uy: dy / length };
  };
  const outline: Point[] = [];
  for (let step = 0; step <= ARC_STEPS; step += 1) {
    const { point, ux, uy } = along(step / ARC_STEPS);
    outline.push({ x: point.x - uy * r, y: point.y + ux * r });
  }
  const end = along(1);
  for (let step = 1; step < CAP_STEPS; step += 1) {
    const phi = (step / CAP_STEPS) * Math.PI;
    outline.push({
      x: end.point.x + r * (-end.uy * Math.cos(phi) + end.ux * Math.sin(phi)),
      y: end.point.y + r * (end.ux * Math.cos(phi) + end.uy * Math.sin(phi)),
    });
  }
  for (let step = ARC_STEPS; step >= 0; step -= 1) {
    const { point, ux, uy } = along(step / ARC_STEPS);
    outline.push({ x: point.x + uy * r, y: point.y - ux * r });
  }
  const start = along(0);
  for (let step = 1; step < CAP_STEPS; step += 1) {
    const phi = (step / CAP_STEPS) * Math.PI;
    outline.push({
      x: start.point.x + r * (start.uy * Math.cos(phi) - start.ux * Math.sin(phi)),
      y: start.point.y + r * (-start.ux * Math.cos(phi) - start.uy * Math.sin(phi)),
    });
  }
  return outline;
}

function arcPath(glyph: Glyph, up: boolean): string {
  const points = arcOutline(glyph, up).map((point) => `${roundTo(point.x)} ${roundTo(point.y)}`);
  return `M${points.join("L")}Z`;
}

interface Arm {
  from: Point;
  to: Point;
  thickness: number;
}

/** The two inner-pointing arms of a chevron, in local coordinates. */
function chevronArms(glyph: Glyph): [Arm, Arm] {
  const direction = glyph.index === 0 ? 1 : -1;
  const apex = { x: direction * (glyph.w / 2), y: 0 };
  const top = { x: -direction * (glyph.w / 2), y: -glyph.h / 2 };
  const bottom = { x: -direction * (glyph.w / 2), y: glyph.h / 2 };
  const thickness = Math.max(3, glyph.h * 0.34);
  return [
    { from: top, to: apex, thickness },
    { from: apex, to: bottom, thickness },
  ];
}

function armGlyph(arm: Arm): Glyph {
  const dx = arm.to.x - arm.from.x;
  const dy = arm.to.y - arm.from.y;
  return {
    kind: "capsule",
    index: 0,
    w: Math.hypot(dx, dy) + arm.thickness,
    h: arm.thickness,
  };
}

function armAngle(arm: Arm): number {
  return Math.atan2(arm.to.y - arm.from.y, arm.to.x - arm.from.x);
}

/** A stadium rotated about `angle` and moved to (`cx`, `cy`), emitted as one path. */
function rotatedStadiumPath(w: number, h: number, angle: number, cx: number, cy: number): string {
  const halfW = w / 2;
  const halfH = h / 2;
  const at = (x: number, y: number) => {
    const spun = rotate2(x, y, angle);
    return `${roundTo(spun.x + cx)} ${roundTo(spun.y + cy)}`;
  };
  if (h >= w) {
    const r = halfW;
    const cap = halfH - halfW;
    return `M${at(-r, -cap)}L${at(-r, cap)}A${r} ${r} 0 0 0 ${at(r, cap)}L${at(r, -cap)}A${r} ${r} 0 0 0 ${at(-r, -cap)}Z`;
  }
  const r = halfH;
  const cap = halfW - halfH;
  return `M${at(-cap, -r)}L${at(cap, -r)}A${r} ${r} 0 0 1 ${at(cap, r)}L${at(-cap, r)}A${r} ${r} 0 0 1 ${at(-cap, -r)}Z`;
}

/** A chevron is two stadiums, each rotated about its own midpoint, in one compound path. */
function chevronPath(glyph: Glyph): string {
  return chevronArms(glyph)
    .map((arm) => {
      const mid = { x: (arm.from.x + arm.to.x) / 2, y: (arm.from.y + arm.to.y) / 2 };
      const bar = armGlyph(arm);
      return rotatedStadiumPath(bar.w, bar.h, armAngle(arm), mid.x, mid.y);
    })
    .join("");
}

/**
 * The glyph path in local coordinates around the origin; the eye's matrix places, tilts
 * and squashes it. Every kind stays a single `d`, so one `<path>` per eye is enough.
 */
export function eyePath(eye: { kind: EyeKind; index: EyeIndex; w: number; h: number }): string {
  switch (eye.kind) {
    case "arcUp":
      return arcPath(eye, true);
    case "arcDown":
      return arcPath(eye, false);
    case "chevron":
      return chevronPath(eye);
    default:
      return stadiumPath(eye.w, eye.h);
  }
}

/* -------------------------------------------------------------- outline */

function stadiumOutline(w: number, h: number): Point[] {
  const points: Point[] = [];
  const steps = 8;
  if (h >= w) {
    const r = w / 2;
    const cap = h / 2 - r;
    for (let i = 0; i <= steps; i += 1) {
      const angle = -Math.PI / 2 + Math.PI * (i / steps);
      points.push({ x: r * Math.cos(angle), y: -cap + r * Math.sin(angle) });
    }
    for (let i = 0; i <= steps; i += 1) {
      const angle = Math.PI / 2 + Math.PI * (i / steps);
      points.push({ x: r * Math.cos(angle), y: cap + r * Math.sin(angle) });
    }
    return points;
  }
  const r = h / 2;
  const cap = w / 2 - r;
  for (let i = 0; i <= steps; i += 1) {
    const angle = Math.PI / 2 + Math.PI * (i / steps);
    points.push({ x: -cap + r * Math.cos(angle), y: r * Math.sin(angle) });
  }
  for (let i = 0; i <= steps; i += 1) {
    const angle = -Math.PI / 2 + Math.PI * (i / steps);
    points.push({ x: cap + r * Math.cos(angle), y: r * Math.sin(angle) });
  }
  return points;
}

function localOutline(eye: { kind: EyeKind; index: EyeIndex; w: number; h: number }): Point[] {
  if (eye.kind === "arcUp" || eye.kind === "arcDown") {
    return arcOutline(eye, eye.kind === "arcUp");
  }
  if (eye.kind === "chevron") {
    return chevronArms(eye).flatMap((arm) => {
      const mid = { x: (arm.from.x + arm.to.x) / 2, y: (arm.from.y + arm.to.y) / 2 };
      const angle = armAngle(arm);
      return stadiumOutline(armGlyph(arm).w, armGlyph(arm).h).map((point) => {
        const spun = rotate2(point.x, point.y, angle);
        return { x: spun.x + mid.x, y: spun.y + mid.y };
      });
    });
  }
  return stadiumOutline(eye.w, eye.h);
}

/** Collision samples in body space, with the eye's matrix applied. */
export function eyeOutline(eye: EyePose): Point[] {
  const local = localOutline(eye);
  const { a, b, c, d, e, f } = eye.matrix;
  return local.map((point) => ({ x: a * point.x + c * point.y + e, y: b * point.x + d * point.y + f }));
}

/* ------------------------------------------------------------ fit table */

/**
 * Gaze samples the table has to survive: the pointer envelope at both extremes (and its
 * mid), the drift riding on top, plus the face's own resting gaze. The roll is the face's
 * own: it only leans the glyph, and the drift's roll is small enough to be covered by the
 * margin.
 */
function envelopeOf(face: FaceFrame): HeadGaze[] {
  const yaws = [-HERE_YAW, 0, HERE_YAW];
  const pitches = [-HERE_PITCH, 0, HERE_PITCH];
  const out: HeadGaze[] = [{ ...face.gaze }];
  for (const yaw of yaws) {
    for (const pitch of pitches) out.push({ yaw, pitch, roll: face.gaze.roll });
  }
  return out;
}

const FIT_MARGIN_STEPS = 5;
/** Minimum air between the two glyphs at rest, viewBox units. */
const MIN_GAP = 2;

/**
 * Smallest inward offset that keeps one eye inside its silhouette at every gaze of the
 * envelope. Iterative: project at the current offset, measure the worst penetration, move
 * the eye in by that much, repeat. Each step is monotone (the eye moves as a rigid body),
 * so this converges in a few passes; it runs once per (shape, face, eye) at module load.
 */
function solveFit(shape: ShapeId, key: FaceKey, index: EyeIndex, split: number): Point {
  const profile = PROFILES[shape];
  const frame = faceFrameOf(FACES[key]);
  frame.split = split;
  const radiusAt = (x: number, y: number) => radiusAtAngle(profile, Math.atan2(y, x));
  const offset: Point = { x: 0, y: 0 };
  const gazes = envelopeOf(frame);

  for (let step = 0; step < FIT_MARGIN_STEPS; step += 1) {
    let worst = 0;
    let directionX = 0;
    let directionY = 0;
    for (const gaze of gazes) {
      const eye = placeEyes({ ...frame, gaze }, radiusAt, [offset, offset], 1)[index];
      if (eye.depth <= 0.04) continue;
      for (const point of eyeOutline(eye)) {
        const dx = point.x - CENTER.x;
        const dy = point.y - CENTER.y;
        const distance = Math.hypot(dx, dy) || 1;
        const room = radiusAtAngle(profile, Math.atan2(dy, dx)) - MARGIN;
        const penetration = distance - room;
        if (penetration <= worst) continue;
        worst = penetration;
        directionX = -dx / distance;
        directionY = -dy / distance;
      }
    }
    if (worst <= 0) break;
    offset.x += directionX * worst;
    offset.y += directionY * worst;
    // A correction is a nudge, not a rearrangement: an eye that still clips a sliver at
    // the limb is already fading out, and pulling it halfway to the middle to save that
    // sliver is what made a pair of wide eyes merge on the narrow silhouettes.
    const reach = Math.hypot(offset.x, offset.y);
    const cap = 0.5 * Math.min(frame.eyes[index].w, frame.eyes[index].h);
    if (reach > cap) {
      offset.x = (offset.x / reach) * cap;
      offset.y = (offset.y / reach) * cap;
    }
  }
  return offset;
}

export const FIT_KEYS: readonly FaceKey[] = Object.keys(FACES) as FaceKey[];

interface FacePlacement {
  /** Half split the face actually uses on this shape, degrees. */
  split: number;
  fit: readonly [Point, Point];
}

/**
 * Split and eye offsets for one (shape, face). The split is widened until the glyphs keep
 * `MIN_GAP` of air after the fit has pulled them in — the fit is what makes a surprised
 * pair of dots touch on a narrow silhouette, and tabulating the split keeps that decision
 * out of the per-frame path for the same reason the offsets are tabulated.
 */
function solvePlacement(shape: ShapeId, key: FaceKey): FacePlacement {
  const profile = PROFILES[shape];
  const frame = faceFrameOf(FACES[key]);
  const radiusAt = (x: number, y: number) => radiusAtAngle(profile, Math.atan2(y, x));
  const across = Math.min(radiusAtAngle(profile, 0), radiusAtAngle(profile, Math.PI));
  let split = splitFor(shape, frame);
  let fit: readonly [Point, Point] = [solveFit(shape, key, 0, split), solveFit(shape, key, 1, split)];

  for (let pass = 0; pass < 4; pass += 1) {
    const placed = placeEyes({ ...frame, split }, radiusAt, fit, 1);
    const left = Math.max(...eyeOutline(placed[0]).map((point) => point.x));
    const right = Math.min(...eyeOutline(placed[1]).map((point) => point.x));
    const gap = right - left;
    if (gap >= MIN_GAP) break;
    const half = Math.sin(split * DEG) * across + (MIN_GAP - gap) / 2;
    split = Math.asin(clamp(half / Math.max(across, 1), 0, 0.94)) / DEG;
    fit = [solveFit(shape, key, 0, split), solveFit(shape, key, 1, split)];
  }

  return { split, fit };
}

function buildTable(): Record<ShapeId, Record<FaceKey, FacePlacement>> {
  const table = {} as Record<ShapeId, Record<FaceKey, FacePlacement>>;
  for (const shape of SHAPES) {
    const perShape = {} as Record<FaceKey, FacePlacement>;
    for (const key of FIT_KEYS) perShape[key] = solvePlacement(shape, key);
    table[shape] = perShape;
  }
  return table;
}

const PLACEMENT = buildTable();

export function fitOffset(shape: ShapeId, key: FaceKey, index: EyeIndex): Point {
  return PLACEMENT[shape][key].fit[index];
}

/** The half split, in degrees, a face ends up with on a given silhouette. */
export function splitOf(shape: ShapeId, key: FaceKey): number {
  return PLACEMENT[shape][key].split;
}
