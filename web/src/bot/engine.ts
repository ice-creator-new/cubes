// The face engine: a pure `sample(t)` over a little state that only the setters change.
//
// The model is bloub's (MIT, (c) 2026 Jérémy Perret): eyes on a sphere, idle life as a pure
// function of time, one-shot gestures layered over the pose, and an eye-fit offset read from
// a table. See ThirdPartyNotices.txt. The look — silhouettes, palette, eye glyph vocabulary
// and expressions — is Cubes' own.
//
// Setters (`set`, `gesture`, `look`) are dated and never run during sampling, so `sample(t)`
// stays replayable: reading any date twice gives the same frame.

import type { Appearance } from "./catalog";
import { FACES, type FaceKey } from "./expressions";
import {
  eyePath,
  mouthPath,
  placeFeatures,
  tongueOf,
  faceFrameOf,
  fitOffset,
  MOUTH_STROKE,
  placeEyes,
  splitOf,
  type EyeIndex,
  type EyePose,
  type FaceFrame,
  type Point,
} from "./face";
import { swatchOf, type Swatch } from "./palette";
import { CENTER, PROFILES, R, profilePath, profilePoint, radiusAtAngle } from "./shapes";
import {
  DEG,
  TAU,
  clamp,
  contrast,
  easeOutCubic,
  easeOutQuint,
  keyframes,
  lerp,
  lerpHex,
} from "./math";
import { buildRhythm, forcedLid, liveliness, type Rhythm } from "./sphere";
import type { Mood } from "../lib/mood";

export type Detail = "full" | "low";

/** One-shot animations, fired by team events. */
export type GestureKind = "wake" | "notice" | "nod" | "cheer" | "wince" | "sigh" | "think" | "sleep";

/** Where something outside the bot (the pointer) wants it to look. Absolute, in degrees. */
export interface Look {
  yaw: number;
  pitch: number;
  /** 0 = the pose decides, 1 = the target decides. Eased by the engine. */
  mix: number;
  /** Degrees travelled on the way in, fading to 0 — the eyes go round the sphere. */
  spin: number;
  /** 1 = idle drift alive, 0 = held on the target. */
  wander: number;
}

export interface EyeRender {
  /** Glyph path in local coordinates; `matrix` places, tilts and squashes it. */
  d: string;
  matrix: string;
  alpha: number;
}

export interface Dot {
  cx: number;
  cy: number;
  r: number;
  opacity: number;
  /** Thinking dots are the body split in three, so they wear the body colour; sparkles
   *  around a cheer wear the eye colour. */
  tone: "body" | "eye";
  /**
   * `row` keeps a dot at a fixed distance from the body's centre — the three thinking dots
   * stay the same size and spacing however far the body has shrunk. `body` rides the group
   * transform, which is what a spark thrown off by a hop does.
   */
  anchor: "body" | "row";
}

/** The notify pastille: a disc centred on the body's edge; the renderer cuts its gap out. */
export interface Notif {
  x: number;
  y: number;
  r: number;
}

/**
 * The matte light on the body: one linear gradient from a lit rim to a shaded rim, in body
 * space, so the face keeps its flat colour and only the edges pick up volume. See
 * `colourAtShade` for the exact interpolation the renderer's gradient performs.
 */
export interface Shade {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  stops: readonly { offset: number; color: string }[];
}

export interface Frame {
  body: string;
  /**
   * Group transform carrying breath, squash, hop, the thinking shrink, the head-turn
   * parallax and a gesture's roll. Body and eyes share it, so a face fitted inside the
   * silhouette stays inside however the group moves.
   */
  transform: string;
  /**
   * The body's turn in depth, degrees: `x` is a rotation about the horizontal axis (positive
   * tips the top away), `y` about the vertical one (positive turns the right side away). It
   * is already in `transform` and `layers`; 0 and 0 is a square-on portrait.
   */
  tilt: { x: number; y: number };
  /**
   * The body's thickness: transforms of the silhouette at planes behind the face, each turned
   * and projected like the face itself, all painted `sideFill`. One colour, so the slices
   * merge into a single solid side with no seams between them. Empty while the body faces
   * the viewer: the side would hide behind the face and only bleed through its edge.
   */
  layers: string[];
  sideFill: string;
  /** Opacity of the side: 0 square-on, rising with the turn so it never pops in. */
  sideAlpha: number;
  /** Local glyph paths plus their transforms; the body clip still applies. */
  eyes: [EyeRender, EyeRender];
  /** Layer opacity: 0 while the body is a dot. */
  eyeAlpha: number;
  dots: Dot[];
  notif: Notif | null;
  fill: string;
  eyeFill: string;
  /** Rim light, already in body space. */
  shade: Shade;
  /** The mouth in local coordinates, placed by `matrix`; it wears the eye colour. */
  mouth: MouthRender;
  /** Unit circles mapped onto the two cheeks; the body clip applies. */
  cheeks: [FeatureRender, FeatureRender];
  blushFill: string;
  tongueFill: string;
  /** The contact shadow under the body, outside the group: it stays on the ground. */
  shadow: Shadow | null;
}

export interface FeatureRender {
  matrix: string;
  alpha: number;
}

export interface MouthRender extends FeatureRender {
  d: string;
  /** Lip line width, viewBox units: thicker at small sizes so it survives the downscale. */
  stroke: number;
  /** The tongue of an open smile, local to the mouth, clipped by it. */
  tongue: { cx: number; cy: number; rx: number; ry: number; opacity: number };
}

export interface Shadow {
  cx: number;
  cy: number;
  rx: number;
  ry: number;
  opacity: number;
}

export interface BotConfig {
  appearance: Appearance;
  seed: string;
  detail?: Detail;
  mood?: Mood;
  /** `false` starts at rest: a still, flat portrait that comes alive when it is woken. */
  awake?: boolean;
}

export interface Bot {
  set(patch: { appearance?: Appearance; mood?: Mood }, t: number): void;
  /** Start a one-shot gesture at `t`. `sleep` stays on until something wakes it. */
  gesture(kind: GestureKind, t: number): void;
  /** Aim at something, or `null` to hand the direction back to the pose. */
  look(target: Look | null, t: number): void;
  /**
   * Wake or settle at `t`. The change ramps: the bot eases up to life, and back down to the
   * rest pose, so nothing jumps. Dated like every other setter, so sampling stays pure.
   */
  setAwake(on: boolean, t: number): void;
  /** Whether a one-shot gesture is on screen at `t` (sleep does not count: it is a state). */
  gestureActive(t: number): boolean;
  /** Whether the bot has settled to the rest pose at `t`, so a renderer can stop painting. */
  resting(t: number): boolean;
  sample(t: number): Frame;
  /** Eye geometry at t, in body space (before the group transform). */
  eyes(t: number): [EyePose, EyePose];
}

const TRANSITION = 0.45;
/** A forced blink that masks a glyph-kind change, seconds. */
const MASK = 0.3;
/** When the incoming glyphs take over, from `set` — easeOutQuint reaching 0.5. */
const SWAP_DELAY = 0.06;
/** How long a waking bot takes to open its eyes again. */
const WAKE_TIME = 0.3;
/** How long the whole body takes to come alive, and to settle back to a still portrait. */
const AWAKE_RAMP = 0.35;
const REST_RAMP = 0.3;

const THINK_SCALE = 0.26;
const DOT_X = 0.55 * 38;
const DOT_R = 7.2;
const DOT_PERIOD = 1.1;

const NO_LOOK: Look = { yaw: 0, pitch: 0, mix: 0, spin: 0, wander: 1 };
const LOOK_MORPH = 0.24;

/** The body follows the head: this fraction of the eye travel, on top of the idle drift. */
const BODY_FOLLOW = 0.26;
/** Envelope of the pointer look, in degrees; the body's turn in depth is scaled by it. */
const YAW_RANGE = 16;
/** The body turns in depth with the head, by this fraction of the head's turn, plus a share of
 *  its own seeded sway. The pointer turns it too, on a slower ease than the eyes: the head
 *  leads and the body follows behind it. */
const BODY_TURN = 0.85;
const SWAY_SHARE = 0.6;
const BODY_LOOK_MORPH = 0.45;
/** Front to back, viewBox units: a soft pebble, not a coin and not a ball. */
const THICKNESS = 14;
/** Slices drawn behind the face; enough that their union reads as one solid side. */
const LAYERS_FULL = 8;
const LAYERS_LOW = 3;
/** Half the length of the light's axis, in viewBox units: past the body, so the rim is soft. */
const SHADE_AXIS = 56;
/** How much lighter the lit rim is, and how much darker the shaded one. */
const SHADE_LIT = 0.28;
const SHADE_DARK = 0.1;
/** Where the gradient's flat middle begins and ends: the face sits there, uncovered. The
 *  shaded band is the narrower of the two, because a dark rim behind an eye costs contrast
 *  while a light one does not. */
const SHADE_LIT_EDGE = 0.32;
const SHADE_DARK_EDGE = 0.86;
/** The light comes from up and to the left of the room, not of the bot. */
const LIGHT = Math.atan2(-0.76, -0.65);
/** The dark theme's canvas (`--canvas` in styles.css): the shade must never swallow the body's
 *  edge against it. Keep the two in step. */
export const DARK_CANVAS = "#17181c";
/** What the cheeks and the tongue are tinted toward. */
const BLUSH = "#ff5f7e";
const TONGUE = "#ff6f86";

/** The bottom of a silhouette, where its contact shadow goes: lowest point and half width. */
function floorOf(profile: readonly number[]): { y: number; half: number } {
  const points = profile.map((_, index) => profilePoint(profile, index));
  const y = Math.max(...points.map((point) => point.y));
  const base = points.filter((point) => point.y > y - 6).map((point) => point.x);
  return { y, half: Math.max(10, (Math.max(...base) - Math.min(...base)) / 2) };
}

function matrixString(m: { a: number; b: number; c: number; d: number; e: number; f: number }): string {
  return `matrix(${r2(m.a)},${r2(m.b)},${r2(m.c)},${r2(m.d)},${r2(m.e)},${r2(m.f)})`;
}

/** `outer ∘ inner`, both affine maps in SVG matrix order. */
function compose(outer: readonly number[], inner: readonly number[]): number[] {
  const [a, b, c, d, e, f] = outer as [number, number, number, number, number, number];
  const [p, q, r, s, u, v] = inner as [number, number, number, number, number, number];
  return [a * p + c * q, b * p + d * q, a * r + c * s, b * r + d * s, a * u + c * v + e, b * u + d * v + f];
}

/**
 * The plane of the body `z` units in front of its middle, turned in depth and projected
 * orthographically: the silhouette narrows by the cosine of the turn and the plane slides
 * toward the side the face turns to.
 */
function planeMatrix(yaw: number, pitch: number, z: number): number[] {
  const cy = Math.cos(yaw * DEG);
  const sy = Math.sin(yaw * DEG);
  const cp = Math.cos(pitch * DEG);
  const sp = Math.sin(pitch * DEG);
  return [cy, 0, 0, cp, CENTER.x * (1 - cy) + z * sy, CENTER.y * (1 - cp) - z * sp];
}

/**
 * The shaded rim for one fill. A dark swatch — ink, slate, iris — cannot take the full shade
 * without its edge merging into the dark canvas at small sizes, so the amount backs off until
 * the rim still separates from the page.
 */
function shadeDarkFor(fill: string, strength = 1): string {
  // The palette already promises a body 1.4:1 against the dark canvas; the shaded rim keeps
  // that same separation, with headroom, so a very dark swatch simply takes less of it.
  const floor = 1.5;
  let amount = SHADE_DARK * strength;
  let shaded = lerpHex(fill, "#000000", amount);
  for (let step = 0; step < 6 && contrast(shaded, DARK_CANVAS) < floor; step += 1) {
    amount /= 2;
    shaded = lerpHex(fill, "#000000", amount);
  }
  return shaded;
}

/** Per-gesture length in seconds. `sleep` is open-ended. */
const GESTURE_TIME: Record<Exclude<GestureKind, "sleep">, number> = {
  wake: 1.5,
  notice: 1.15,
  nod: 0.85,
  cheer: 1.15,
  wince: 0.9,
  sigh: 1.6,
  think: 1.3,
};

interface GesturePose {
  face: FaceKey | null;
  faceMix: number;
  /** Body multipliers around 1 and offsets in viewBox units. */
  sx: number;
  sy: number;
  ox: number;
  oy: number;
  /** Body roll about the centre, radians: a lean, a slump, a shiver. */
  roll: number;
  /** Absolute gaze to mix in, degrees. */
  aimYaw: number;
  aimPitch: number;
  aimMix: number;
  /** Degrees the body turns about its vertical axis on top of the head: a swivel, a shake. */
  spin: number;
  /** Degrees the body tips in depth on top of the head, positive tips the top away. */
  tip: number;
  dots: Dot[];
  notif: { angle: number; r: number; opacity: number } | null;
  /** Multiplies the face's opacity: a gesture can hide the eyes (the thinking dot). */
  eyeAlpha: number;
  /** Forces a blink at the start, the way bloub masks a state entry. */
  mask: boolean;
}

const IDENTITY_GESTURE: GesturePose = {
  face: null,
  faceMix: 0,
  sx: 1,
  sy: 1,
  ox: 0,
  oy: 0,
  roll: 0,
  aimYaw: 0,
  aimPitch: 0,
  aimMix: 0,
  spin: 0,
  tip: 0,
  dots: [],
  notif: null,
  eyeAlpha: 1,
  mask: false,
};

/** Static per-seed motion. Built once per seed and shared by every bot using it. */
const RHYTHMS = new Map<string, Rhythm>();

function rhythmFor(seed: string): Rhythm {
  const cached = RHYTHMS.get(seed);
  if (cached) return cached;
  const rhythm = buildRhythm(seed);
  RHYTHMS.set(seed, rhythm);
  return rhythm;
}

/** Cached face frames: a mood only ever asks for a dozen of them. */
const FRAMES = new Map<FaceKey, FaceFrame>();

function frameForKey(key: FaceKey): FaceFrame {
  const cached = FRAMES.get(key);
  if (cached) return cached;
  const frame = faceFrameOf(FACES[key]);
  FRAMES.set(key, frame);
  return frame;
}

/** The face a mood asks for. Idle, thinking and speaking all rest on the neutral face. */
function expressionKeyFor(mood: Mood): FaceKey {
  if (mood === "idle" || mood === "thinking" || mood === "speaking") return "neutral";
  return mood;
}

/** The face a mood shows, before gestures. Thinking closes the eyes: the body is the face. */
function moodFrame(key: FaceKey, mood: Mood): FaceFrame {
  const frame = frameForKey(key);
  return mood === "thinking" ? { ...frame, alpha: 0 } : frame;
}

function lerpEye(a: FaceFrame["eyes"][0], b: FaceFrame["eyes"][0], t: number) {
  return {
    kind: a.kind,
    w: lerp(a.w, b.w, t),
    h: lerp(a.h, b.h, t),
    tilt: lerp(a.tilt, b.tilt, t),
    open: lerp(a.open, b.open, t),
  };
}

/**
 * Two faces crossed. Same kinds: every parameter glides. Different kinds: the outgoing pair
 * holds until the midpoint, then the incoming pair takes over — the engine masks that swap
 * with a blink, so nothing can be seen jumping.
 */
function blendFrames(a: FaceFrame, b: FaceFrame, t: number): FaceFrame {
  if (t <= 0) return a;
  if (t >= 1) return b;
  const sameKinds = a.eyes[0].kind === b.eyes[0].kind && a.eyes[1].kind === b.eyes[1].kind;
  const eyes: FaceFrame["eyes"] = sameKinds
    ? [lerpEye(a.eyes[0], b.eyes[0], t), lerpEye(a.eyes[1], b.eyes[1], t)]
    : (t < 0.5 ? a : b).eyes;
  return {
    gaze: {
      yaw: lerp(a.gaze.yaw, b.gaze.yaw, t),
      pitch: lerp(a.gaze.pitch, b.gaze.pitch, t),
      roll: lerp(a.gaze.roll, b.gaze.roll, t),
    },
    split: lerp(a.split, b.split, t),
    eyes,
    // The mouth has no kinds: it always glides, so a smile opens and a frown sets smoothly.
    mouth: {
      w: lerp(a.mouth.w, b.mouth.w, t),
      top: lerp(a.mouth.top, b.mouth.top, t),
      bottom: lerp(a.mouth.bottom, b.mouth.bottom, t),
      tilt: lerp(a.mouth.tilt, b.mouth.tilt, t),
    },
    blush: lerp(a.blush, b.blush, t),
    alpha: lerp(a.alpha, b.alpha, t),
  };
}

/**
 * One dot's beat in the thinking row: a full raised cosine over the whole period, offset per dot,
 * so the three never sit still together and none of them rests flat and then pops.
 */
function pulse(t: number, index: number): number {
  const phase = t / DOT_PERIOD - index * 0.17;
  return 0.5 - 0.5 * Math.cos(phase * TAU);
}

/** Three dots in a row, the middle one being the body itself; all three share its colour. */
function thinkingDots(t: number, weight: number): Dot[] {
  const emerge = 0.34 + 0.66 * weight;
  return [-1, 1].map((side) => {
    const beat = pulse(t, side < 0 ? 0 : 2);
    return {
      cx: CENTER.x + side * DOT_X * emerge,
      cy: CENTER.y,
      r: DOT_R * (1 + 0.22 * beat),
      opacity: weight * (0.55 + 0.45 * beat),
      tone: "body",
      anchor: "row",
    };
  });
}

/** Gesture scripts, `u` from 0 to 1. Each is identity at both ends, so it can be layered. */
function gestureScript(kind: Exclude<GestureKind, "sleep">, u: number, thinking: boolean): GesturePose {
  const weight = Math.min(clamp(u / 0.14, 0, 1), clamp((1 - u) / 0.2, 0, 1));

  switch (kind) {
    case "wake": {
      // A glance around the head: the eyes travel past the limb, vanish, and come back.
      return {
        ...IDENTITY_GESTURE,
        aimYaw: keyframes(u, [
          [0, -34],
          [0.22, -24],
          [0.52, 26],
          [0.74, 10],
          [1, 0],
        ]),
        aimPitch: keyframes(u, [
          [0, -7],
          [0.3, 5],
          [0.62, -3],
          [1, 0],
        ]),
        aimMix: keyframes(u, [
          [0, 0],
          [0.1, 0.95],
          [0.7, 0.95],
          [1, 0],
        ]),
        // A waking stretch: the body lifts and narrows a touch, then settles.
        sx: 1 - 0.05 * Math.sin(u * Math.PI) ** 2,
        sy: 1 + 0.05 * Math.sin(u * Math.PI) ** 2,
        roll: 0.035 * Math.sin(u * Math.PI * 2),
        // The body swivels after the glance, past the head, so its side shows both ways.
        spin: keyframes(u, [
          [0, 0],
          [0.28, -22],
          [0.6, 24],
          [0.84, -6],
          [1, 0],
        ]),
        tip: 6 * Math.sin(u * Math.PI),
        mask: true,
      };
    }

    case "notice": {
      // Wide eyes, a small pop upward, and a pastille on the shoulder.
      const rise = easeOutCubic(clamp(u / 0.1)) * (1 - clamp((u - 0.55) / 0.45));
      const pop = keyframes(u, [
        [0, 0],
        [0.22, 1.14],
        [0.42, 1],
        [0.82, 1],
        [1, 0],
      ]);
      return {
        ...IDENTITY_GESTURE,
        face: "wide",
        faceMix: Math.min(clamp(u / 0.12), clamp((1 - u) / 0.18)),
        sx: 1 - 0.035 * rise,
        sy: 1 + 0.07 * rise,
        oy: -2.2 * rise,
        roll: -0.09 * rise,
        aimYaw: -15 * weight,
        aimPitch: -7 * weight,
        aimMix: 0.75 * weight,
        // Turns toward the pastille on its shoulder and rears back a little.
        spin: -20 * rise,
        tip: 10 * rise,
        notif: { angle: -42, r: 8.4 * pop, opacity: clamp(pop * 1.6) },
        mask: true,
      };
    }

    case "nod": {
      const swing = Math.sin(u * TAU * 2) * (1 - u) ** 1.4;
      return {
        ...IDENTITY_GESTURE,
        aimYaw: 3 * weight,
        aimPitch: 8.5 * swing,
        aimMix: 0.9 * weight,
        oy: 2 * swing,
        roll: 0.035 * swing,
        // A nod in depth: the top of the body tips toward the viewer and back.
        tip: -16 * swing,
        sy: 1 - 0.025 * Math.abs(swing),
      };
    }

    case "cheer": {
      // A hop with squash and stretch, and the eyes as lifted arcs while it is airborne.
      const hop = keyframes(u, [
        [0, 0],
        [0.22, 1],
        [0.38, 1],
        [0.56, 0],
        [1, 0],
      ]);
      // Anticipation (a short crouch), then a stretch as it leaves the ground, then the
      // landing squash: a jump that reads without a shadow to point at.
      const squash = keyframes(u, [
        [0, 1],
        [0.1, 0.93],
        [0.2, 1.07],
        [0.38, 1.02],
        [0.54, 0.9],
        [0.7, 1.03],
        [0.88, 0.99],
        [1, 1],
      ]);
      const tilt = keyframes(u, [
        [0, 0],
        [0.3, 1],
        [0.56, -0.8],
        [1, 0],
      ]);
      const particles: Dot[] = [];
      for (let index = 0; index < 4; index += 1) {
        const phase = clamp((u - 0.05 - index * 0.06) / 0.5);
        if (phase <= 0 || phase >= 1) continue;
        const angle = -0.6 - index * 0.62;
        const radius = 46 + 12 * phase;
        particles.push({
          cx: CENTER.x + Math.cos(angle) * radius,
          cy: CENTER.y + Math.sin(angle) * radius,
          r: 2.6 * (1 - phase),
          opacity: 0.9 * (1 - phase) * weight,
          tone: "eye",
          anchor: "body",
        });
      }
      return {
        ...IDENTITY_GESTURE,
        face: "cheer",
        faceMix: Math.min(clamp(u / 0.1), clamp((1 - u) / 0.25)),
        sx: 1 / Math.sqrt(squash),
        sy: squash,
        oy: -7.5 * hop,
        roll: 0.05 * tilt * weight,
        // A swivel while airborne, and the top thrown back on the way up.
        spin: keyframes(u, [
          [0, 0],
          [0.2, -12],
          [0.42, 34],
          [0.62, -10],
          [0.82, 3],
          [1, 0],
        ]),
        tip: 12 * hop,
        aimPitch: -4 * hop,
        aimMix: 0.6 * weight,
        dots: particles,
      };
    }

    case "wince": {
      const shiver = Math.sin(u * TAU * 5) * (1 - u) ** 1.2;
      return {
        ...IDENTITY_GESTURE,
        face: "error",
        faceMix: Math.min(clamp(u / 0.08), clamp((1 - u) / 0.2)),
        ox: 2.2 * shiver,
        oy: 0.9 * weight,
        roll: 0.04 * shiver,
        // A head shake in depth, not a flat wobble.
        spin: 16 * shiver,
        sx: 1 + 0.025 * shiver,
        aimPitch: 7 * weight,
        aimMix: 0.7 * weight,
      };
    }

    case "sigh": {
      const sink = keyframes(u, [
        [0, 0],
        [0.32, 1],
        [0.66, 1],
        [1, 0],
      ]);
      return {
        ...IDENTITY_GESTURE,
        face: "droop",
        faceMix: Math.min(clamp(u / 0.2), clamp((1 - u) / 0.28)),
        oy: 3.6 * sink,
        roll: 0.06 * sink,
        // Slumps forward: the top falls toward the viewer.
        tip: -14 * sink,
        spin: -8 * sink,
        sy: 1 - 0.05 * sink,
        sx: 1 + 0.03 * sink,
        aimPitch: 9 * sink,
        aimMix: 0.8 * sink,
      };
    }

    case "think": {
      if (thinking) {
        // The steady three dots are the thinking mood, and the mood change already morphs
        // into them. The gesture is its accent: a masked blink and one gulp as it lands.
        const squeeze = Math.sin(clamp(u / 0.6) * Math.PI);
        return {
          ...IDENTITY_GESTURE,
          sx: 1 + 0.06 * squeeze,
          sy: 1 - 0.12 * squeeze,
          oy: 1.2 * squeeze,
          mask: true,
        };
      }
      // Fired on its own: the body becomes the middle dot and the two side dots emerge from
      // its flanks, the way bloub's thinking state does.
      const emerge = 0.3 + 0.7 * easeOutCubic(clamp(u / 0.28));
      const shrink = keyframes(u, [
        [0, 1],
        [0.3, THINK_SCALE],
        [0.74, THINK_SCALE],
        [1, 1],
      ]);
      const dotsWeight = Math.min(clamp(u / 0.12), clamp((1 - u) / 0.16));
      return {
        ...IDENTITY_GESTURE,
        sx: shrink,
        sy: shrink,
        eyeAlpha: 1 - clamp(u / 0.1),
        dots: [-1, 1].map((side) => ({
          cx: CENTER.x + side * DOT_X * emerge,
          cy: CENTER.y,
          r: DOT_R * 0.92 * (1 + 0.2 * pulse(u * 2, side < 0 ? 0 : 2)),
          opacity: dotsWeight * (0.55 + 0.45 * pulse(u * 2, side < 0 ? 0 : 2)),
          tone: "body" as const,
          anchor: "row" as const,
        })),
        mask: true,
      };
    }
  }
}

/** A sleeping face: eyes shut, a slow breath, sunk a touch. */
function sleepPose(since: number, t: number): GesturePose {
  const settle = easeOutCubic(clamp(since / 1.2));
  const breath = Math.sin((t / 2.6) * TAU);
  return {
    ...IDENTITY_GESTURE,
    face: "closed",
    faceMix: settle,
    sy: 1 + 0.014 * breath,
    oy: 1.8 * settle + 0.6 * breath,
    roll: -0.12 * settle + 0.014 * breath * settle,
    // Dozing: the top tips forward and turns aside, rocking with each breath.
    tip: -10 * settle + 2 * breath * settle,
    spin: 12 * settle,
  };
}

function lerpPoint(a: Point, b: Point, t: number): Point {
  return { x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) };
}

function r2(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * The body's affine map: rotate and stretch about the centre, then shift. Body and eyes are
 * drawn inside the same map, so containment in body space survives it unchanged. Returned in
 * SVG matrix order ``[a, b, c, d, e, f]``.
 */
function bodyMatrix(sx: number, sy: number, roll: number, ox: number, oy: number): number[] {
  const cos = Math.cos(roll);
  const sin = Math.sin(roll);
  const a = sx * cos;
  const b = sx * sin;
  const c = -sy * sin;
  const d = sy * cos;
  return [a, b, c, d, CENTER.x - (CENTER.x * a + CENTER.y * c) + ox, CENTER.y - (CENTER.x * b + CENTER.y * d) + oy];
}

/**
 * The rim light for one frame: a single gradient across the body from the lit rim to the
 * shaded one. The light belongs to the room, so its direction turns against the body's roll
 * and swings as the head turns; the middle of the gradient is the flat fill, which is what
 * keeps the face's own contrast untouched.
 */
function shadeOf(fill: string, roll: number, turn: number, strength: number): Shade {
  const angle = LIGHT - roll + turn * 0.2;
  const dx = Math.cos(angle) * SHADE_AXIS;
  const dy = Math.sin(angle) * SHADE_AXIS;
  return {
    x1: CENTER.x - dx,
    y1: CENTER.y - dy,
    x2: CENTER.x + dx,
    y2: CENTER.y + dy,
    stops: [
      { offset: 0, color: lerpHex(fill, "#ffffff", SHADE_LIT * strength) },
      { offset: SHADE_LIT_EDGE, color: fill },
      { offset: SHADE_DARK_EDGE, color: fill },
      { offset: 1, color: shadeDarkFor(fill, strength) },
    ],
  };
}

/**
 * The colour a shade paints at one point of the body, interpolated exactly the way the
 * renderer's linear gradient does. Exported so the contrast of the eyes against the shaded
 * body can be checked where it is actually drawn.
 */
export function colourAtShade(shade: Shade, x: number, y: number): string {
  const dx = shade.x2 - shade.x1;
  const dy = shade.y2 - shade.y1;
  const along = clamp(((x - shade.x1) * dx + (y - shade.y1) * dy) / (dx * dx + dy * dy), 0, 1);
  for (let index = 0; index < shade.stops.length - 1; index += 1) {
    const from = shade.stops[index]!;
    const to = shade.stops[index + 1]!;
    if (along > to.offset) continue;
    const span = to.offset - from.offset;
    return lerpHex(from.color, to.color, span <= 0 ? 0 : (along - from.offset) / span);
  }
  return shade.stops[shade.stops.length - 1]?.color ?? "#000000";
}

export function createBot(config: BotConfig): Bot {
  const rhythm = rhythmFor(config.seed);
  const detail: Detail = config.detail ?? "full";
  let spec = {
    appearance: config.appearance,
    mood: config.mood ?? "idle",
  };

  // Mood faces glide; the snapshot is the blended face on screen when `set` landed.
  let faceFrom: FaceFrame | null = null;
  let faceFromKey: FaceKey | null = null;
  let faceFromAt = Number.NEGATIVE_INFINITY;
  let maskAt = Number.NEGATIVE_INFINITY;

  // Body shape and colour morph on their own axes.
  let shapePrev = spec.appearance.shape;
  let profilePrev: readonly number[] = PROFILES[shapePrev];
  let shapeAt = Number.NEGATIVE_INFINITY;
  // The target swatch is resolved once per appearance change, not per frame: a free body
  // colour costs a contrast search for its automatic eye.
  let swatchTarget: Swatch = swatchOf(spec.appearance);
  let colorFrom = swatchTarget;
  let colorAt = Number.NEGATIVE_INFINITY;

  // The mood's body: scale for the thinking dot and the weight of its side dots.
  let bodyFrom = { scale: 1, dots: 0 };
  let bodyFromAt = Number.NEGATIVE_INFINITY;

  let gestureState: { kind: Exclude<GestureKind, "sleep">; at: number } | null = null;
  let sleepSince: number | null = null;
  let wakeAt: number | null = null;

  let lookFrom: Look = NO_LOOK;
  // The body eases toward the same target on its own, slower clock, from where the body was:
  // starting it from the eyes' snapshot would snap the body to the eyes on every new look.
  let bodyLookFrom: Look = NO_LOOK;
  let lookTo: Look = NO_LOOK;
  let lookAt = Number.NEGATIVE_INFINITY;

  // The wake weight: 1 fully alive, 0 the canonical rest pose. Toggling records the weight
  // it was at, so a ramp interrupted halfway turns around from there instead of snapping.
  let awake = config.awake ?? true;
  let awakeFrom = awake ? 1 : 0;
  let awakeAt = Number.NEGATIVE_INFINITY;
  const weightAt = (t: number): number => {
    const target = awake ? 1 : 0;
    if (target === awakeFrom) return target;
    const span = awake ? AWAKE_RAMP : REST_RAMP;
    return lerp(awakeFrom, target, easeOutCubic(clamp((t - awakeAt) / span, 0, 1)));
  };

  // Per-bot caches: a frame costs two matrix multiplies, not 64 spline segments per avatar.
  let bodyKey = "";
  let bodyPath = "";
  let bodyFloor = { y: CENTER.y + R, half: R };
  const glyphs = new Map<string, string>();

  const shapeProgress = (t: number) => easeOutQuint(clamp((t - shapeAt) / TRANSITION, 0, 1));
  const moodProgress = (t: number) => easeOutQuint(clamp((t - faceFromAt) / TRANSITION, 0, 1));

  const targetKey = () => expressionKeyFor(spec.mood);
  const targetMoodFrame = () => moodFrame(targetKey(), spec.mood);

  const moodFaceAt = (t: number): FaceFrame => {
    if (!faceFrom) return targetMoodFrame();
    const k = moodProgress(t);
    if (k >= 1) return targetMoodFrame();
    return blendFrames(faceFrom, targetMoodFrame(), k);
  };

  const profileAt = (t: number): readonly number[] => {
    const current = PROFILES[spec.appearance.shape];
    const k = shapeProgress(t);
    if (k >= 1 || profilePrev === current) return current;
    return current.map((radius, index) => lerp(profilePrev[index] ?? radius, radius, k));
  };

  const targetMoodBody = () =>
    spec.mood === "thinking" ? { scale: THINK_SCALE, dots: 1 } : { scale: 1, dots: 0 };

  const moodBodyAt = (t: number) => {
    const k = easeOutQuint(clamp((t - bodyFromAt) / TRANSITION, 0, 1));
    const target = targetMoodBody();
    if (k >= 1) return target;
    return {
      scale: lerp(bodyFrom.scale, target.scale, k),
      dots: lerp(bodyFrom.dots, target.dots, k),
    };
  };

  const swatchAt = (t: number) => {
    const target = swatchTarget;
    const k = easeOutQuint(clamp((t - colorAt) / TRANSITION, 0, 1));
    if (k >= 1) return target;
    return {
      fill: lerpHex(colorFrom.fill, target.fill, k),
      eyeFill: lerpHex(colorFrom.eyeFill, target.eyeFill, k),
    };
  };

  /** The look on screen at `t`; the body reads it from its own snapshot, on a slower ease. */
  const lookAtTime = (t: number, body = false): Look => {
    const from = body ? bodyLookFrom : lookFrom;
    const k = easeOutQuint(clamp((t - lookAt) / (body ? BODY_LOOK_MORPH : LOOK_MORPH), 0, 1));
    if (k >= 1) return lookTo;
    return {
      yaw: lerp(from.yaw, lookTo.yaw, k),
      pitch: lerp(from.pitch, lookTo.pitch, k),
      mix: lerp(from.mix, lookTo.mix, k),
      spin: lerp(from.spin, lookTo.spin, k),
      wander: lerp(from.wander, lookTo.wander, k),
    };
  };

  const oneShotAt = (t: number): boolean => {
    // Sleep is a visible state, not an event: it settles and wakes with the bot rather than
    // pinning it awake for as long as it lasts.
    if (sleepSince !== null || !gestureState) return false;
    return t - gestureState.at < GESTURE_TIME[gestureState.kind];
  };

  const activeGesture = (t: number): GesturePose | null => {
    if (!gestureState) return null;
    const { kind, at } = gestureState;
    const u = (t - at) / GESTURE_TIME[kind];
    if (u >= 1) return null;
    return gestureScript(kind, clamp(u, 0, 1), spec.mood === "thinking");
  };

  /**
   * The gesture layer at `t`. Sleep overrides everything until a wake, and then fades out
   * over `WAKE_TIME` while the gesture that woke it starts underneath.
   */
  const gestureAt = (t: number): GesturePose | null => {
    if (sleepSince === null) return activeGesture(t);
    const asleep = sleepPose(t - sleepSince, t);
    const fade = wakeAt === null ? 1 : 1 - clamp((t - wakeAt) / WAKE_TIME, 0, 1);
    if (fade >= 1) return asleep;
    const base = activeGesture(t) ?? IDENTITY_GESTURE;
    if (fade <= 0) return activeGesture(t);
    return {
      ...asleep,
      faceMix: asleep.faceMix * fade,
      sx: lerp(base.sx, asleep.sx, fade),
      sy: lerp(base.sy, asleep.sy, fade),
      ox: lerp(base.ox, asleep.ox, fade),
      oy: lerp(base.oy, asleep.oy, fade),
      roll: lerp(base.roll, asleep.roll, fade),
      spin: lerp(base.spin, asleep.spin, fade),
      tip: lerp(base.tip, asleep.tip, fade),
      aimYaw: base.aimYaw,
      aimPitch: base.aimPitch,
      aimMix: base.aimMix * (1 - fade),
      dots: base.dots,
      notif: base.notif,
      mask: base.mask,
    };
  };

  /** The split the table gives each face, morphed across the shape and the face blends. */
  const splitAt = (t: number, gesture: GesturePose | null): number => {
    const key = targetKey();
    const shape = shapeProgress(t);
    const mood = moodProgress(t);
    const shapeNow = spec.appearance.shape;
    const acrossShape = (face: FaceKey) => lerp(splitOf(shapePrev, face), splitOf(shapeNow, face), shape);
    const resting = lerp(acrossShape(faceFromKey ?? key), acrossShape(key), mood);
    if (!gesture?.face || gesture.faceMix <= 0) return resting;
    return lerp(resting, splitOf(shapeNow, gesture.face), gesture.faceMix);
  };

  /** The fit offset of one eye, morphed across the shape and the face blends. */
  const fitAt = (t: number, index: EyeIndex, key: FaceKey, gesture: GesturePose | null): Point => {
    const shape = shapeProgress(t);
    const mood = moodProgress(t);
    const shapeNow = spec.appearance.shape;
    const acrossShape = (face: FaceKey) =>
      lerpPoint(fitOffset(shapePrev, face, index), fitOffset(shapeNow, face, index), shape);
    const moodFit = lerpPoint(acrossShape(faceFromKey ?? key), acrossShape(key), mood);
    if (!gesture?.face || gesture.faceMix <= 0) return moodFit;
    return lerpPoint(moodFit, fitOffset(shapeNow, gesture.face, index), gesture.faceMix);
  };

  /** Everything the frame needs, in body space. */
  const computePose = (t: number) => {
    const wake = weightAt(t);
    const raw = gestureAt(t);
    // Everything a gesture does is faded by the wake weight, so a resting bot stays the rest
    // pose even with a gesture in flight, and the hand-over is continuous both ways.
    const gesture: GesturePose | null = raw
      ? {
          ...raw,
          faceMix: raw.faceMix * wake,
          sx: lerp(1, raw.sx, wake),
          sy: lerp(1, raw.sy, wake),
          ox: raw.ox * wake,
          oy: raw.oy * wake,
          roll: raw.roll * wake,
          eyeAlpha: lerp(1, raw.eyeAlpha, wake),
          aimMix: raw.aimMix * wake,
          spin: raw.spin * wake,
          tip: raw.tip * wake,
          dots: raw.dots.map((dot) => ({ ...dot, opacity: dot.opacity * wake })),
          notif: raw.notif ? { ...raw.notif, opacity: raw.notif.opacity * wake, r: raw.notif.r * wake } : null,
        }
      : null;
    const key = targetKey();
    let face = moodFaceAt(t);
    const aim = gesture ?? IDENTITY_GESTURE;
    if (gesture?.face && gesture.faceMix > 0) {
      face = blendFrames(face, frameForKey(gesture.face), gesture.faceMix);
    }

    const split = splitAt(t, gesture);

    const look = lookAtTime(t);
    const body = moodBodyAt(t);
    const asleep = sleepSince !== null && wakeAt === null;
    const alive = face.alpha > 0.01;
    const life = liveliness(rhythm, t, {
      wander: asleep ? 0.12 : alive ? look.wander : 0,
      blink: alive && !asleep,
      subtle: detail === "low",
      breathPeriod: asleep ? 5.2 : 3.4,
    });

    // At rest the eyes are open and the face keeps its mood; only the liveliness is gone.
    const eyesVisible = lerp(1, face.alpha, wake) * aim.eyeAlpha;
    const gaze = {
      yaw:
        lerp(lerp(face.gaze.yaw, look.yaw, look.mix), aim.aimYaw, aim.aimMix) +
        (life.dYaw - look.spin) * wake,
      pitch: lerp(lerp(face.gaze.pitch, look.pitch, look.mix), aim.aimPitch, aim.aimMix) + life.dPitch * wake,
      roll: face.gaze.roll + life.dRoll * wake,
    };
    const blinkLid = lerp(1, life.lid, wake);
    const maskLid = lerp(1, forcedLid(t - maskAt, MASK), wake);
    const lid = Math.min(blinkLid, maskLid);

    // The body follows the head: it shifts a fraction of the way the eyes travel and turns in
    // depth behind it. The head leads: the body reads the pointer on a slower ease, so a look
    // lands in the eyes first and the body swings round after them. A gesture's spin and tip
    // turn the body on top of that.
    const follow = detail === "low" ? BODY_FOLLOW * 0.4 : BODY_FOLLOW;
    const lag = lookAtTime(t, true);
    // The body follows the head's slow drift, never its saccades: only the eyes flick.
    const headYaw = lerp(lerp(face.gaze.yaw, lag.yaw, lag.mix), aim.aimYaw, aim.aimMix) + life.smoothYaw * wake;
    const headPitch = lerp(lerp(face.gaze.pitch, lag.pitch, lag.mix), aim.aimPitch, aim.aimMix) + life.smoothPitch * wake;
    const bodyYaw = (BODY_TURN * headYaw + SWAY_SHARE * life.swayYaw + aim.spin) * wake;
    // Positive head pitch looks up, which tips the top of the body away.
    const bodyPitch = (BODY_TURN * headPitch + SWAY_SHARE * life.swayPitch + aim.tip) * wake;
    // The mood's own body (the thinking dot) fades in with the wake weight, so the rest pose
    // is a plain, untransformed body.
    const moodScale = lerp(1, body.scale, wake);
    const sx = moodScale * aim.sx * (1 + life.breathWidth * wake);
    const sy = moodScale * aim.sy * (1 + life.breath * wake);
    // A speaking bot moves its mouth: syllables on two incommensurate beats, so it never reads
    // as a metronome, and a pause now and then. Pure in t, like everything else here.
    const talk =
      spec.mood === "speaking" && alive
        ? clamp(0.5 + 0.5 * Math.sin(t * TAU * 3.1) + 0.35 * Math.sin(t * TAU * 1.7 + 1.3)) *
          clamp(0.6 + Math.sin(t * TAU * 0.37 + 0.8)) *
          wake
        : 0;
    const mouth = { ...face.mouth, top: face.mouth.top - 0.6 * talk, bottom: face.mouth.bottom + 4.2 * talk };
    return {
      face: { ...face, gaze, split, mouth, alpha: eyesVisible },
      gesture: aim,
      key,
      body,
      wake,
      sx,
      sy,
      // The shift follows the head without its saccades too, for the same reason as the turn.
      ox: (aim.ox + life.driftX + follow * R * Math.sin((gaze.yaw - (life.dYaw - life.smoothYaw) * wake) * DEG)) * wake,
      oy: (aim.oy + life.driftY - follow * R * Math.sin((gaze.pitch - (life.dPitch - life.smoothPitch) * wake) * DEG)) * wake,
      roll: aim.roll * wake,
      // Turning right sends the right side away; looking up tips the top away.
      tiltY: bodyYaw,
      tiltX: bodyPitch,
      lid,
    };
  };

  const renderEyes = (t: number) => {
    const state = computePose(t);
    const profile = profileAt(t);
    const radiusAt = (x: number, y: number) => radiusAtAngle(profile, Math.atan2(y, x));
    const fit: [Point, Point] = [
      fitAt(t, 0, state.key, state.gesture),
      fitAt(t, 1, state.key, state.gesture),
    ];
    return {
      eyes: placeEyes(state.face, radiusAt, fit, state.lid),
      features: placeFeatures(state.face, radiusAt),
      state,
      profile,
    };
  };

  const renderFrame = (t: number): Frame => {
    const { eyes, features, state, profile } = renderEyes(t);
    const key = `${spec.appearance.shape}|${shapeProgress(t).toFixed(3)}`;
    if (key !== bodyKey) {
      bodyKey = key;
      bodyPath = profilePath(profile as number[]);
      bodyFloor = floorOf(profile);
    }
    const swatch = swatchAt(t);
    const flat = bodyMatrix(state.sx, state.sy, state.roll, state.ox, state.oy);
    // The face is the front plane of a body `THICKNESS` deep; the turns never pass edge-on,
    // so the front is always the plane nearest the viewer.
    const near = THICKNESS / 2;
    const matrix = compose(planeMatrix(state.tiltY, state.tiltX, near), flat);
    // How far the back plane sits from the front one on screen. Nearly square-on the side is
    // hidden behind the face and would only darken its edge, so it fades in with the turn
    // rather than switching on at a threshold: a switch flickers as the sway crosses it.
    const reveal = THICKNESS * Math.hypot(Math.sin(state.tiltY * DEG), Math.sin(state.tiltX * DEG));
    const sideAlpha = r2(clamp((reveal - 0.3) / 1.5));
    const slices = sideAlpha <= 0 ? 0 : detail === "low" ? LAYERS_LOW : LAYERS_FULL;
    const layers: string[] = [];
    for (let index = 0; index < slices; index += 1) {
      const z = -near + 2 * near * (index / slices);
      layers.push(`matrix(${compose(planeMatrix(state.tiltY, state.tiltX, z), flat).map(r2).join(",")})`);
    }
    // Everything drawn outside the group — the dots and the pastille — is mapped through the
    // same matrix, so none of it drifts when the body turns.
    const place = (x: number, y: number) => ({
      x: matrix[0]! * x + matrix[2]! * y + matrix[4]!,
      y: matrix[1]! * x + matrix[3]! * y + matrix[5]!,
    });
    const centre = place(CENTER.x, CENTER.y);

    const renders = eyes.map((eye) => {
      const glyphKey = `${eye.kind}|${eye.w.toFixed(2)}|${eye.h.toFixed(2)}|${eye.index}`;
      let d = glyphs.get(glyphKey);
      if (d === undefined) {
        d = eyePath(eye);
        if (glyphs.size < 64) glyphs.set(glyphKey, d);
      }
      return {
        d,
        matrix: matrixString(eye.matrix),
        alpha: eye.alpha,
      };
    }) as [EyeRender, EyeRender];

    const dots: Dot[] = [];
    if (state.body.dots * state.wake > 0.01) dots.push(...thinkingDots(t, state.body.dots * state.wake));
    if (state.gesture.dots.length && detail === "full") dots.push(...state.gesture.dots);
    for (const dot of dots) {
      if (dot.anchor === "row") {
        // The row keeps its own spacing around the body's centre: a thinking bot shrinks to
        // the middle dot, and the two others sit a constant distance away.
        dot.cx += centre.x - CENTER.x;
        dot.cy += centre.y - CENTER.y;
      } else {
        const moved = place(dot.cx, dot.cy);
        dot.cx = moved.x;
        dot.cy = moved.y;
        dot.r *= (state.sx + state.sy) / 2;
      }
    }

    let notif: Notif | null = null;
    if (state.gesture.notif && state.gesture.notif.opacity > 0.02) {
      const { angle, r } = state.gesture.notif;
      const radians = (angle * Math.PI) / 180;
      const radius = radiusAtAngle(profile, radians);
      // Centred on the outline, as in bloub: half the pastille sits over the shoulder and
      // the notch around it is carved from the body by the renderer.
      const at = place(CENTER.x + Math.cos(radians) * radius, CENTER.y + Math.sin(radians) * radius);
      notif = { x: at.x, y: at.y, r };
    }

    // The light belongs to the room: inside the group it turns against the body's roll.
    const turn = clamp(state.face.gaze.yaw / YAW_RANGE, -1.5, 1.5);
    const low = detail === "low";

    const lips = state.face.mouth;
    const { mouth, cheeks } = features;
    // The shadow stays on the ground: a hop lifts the body away from it, so it shrinks and
    // fades; the thinking dot casts a dot of a shadow.
    let shadow: Shadow | null = null;
    if (!low) {
      const lift = Math.max(0, -state.oy);
      const away = clamp(1 - lift / 16, 0.35, 1);
      shadow = {
        cx: r2(CENTER.x + state.ox * 0.6),
        // The ground is under the body's own bottom: a sink or a shrink moves it, a hop does not.
        cy: r2(Math.min(CENTER.y + (bodyFloor.y - CENTER.y) * state.sy + Math.max(0, state.oy) + 1.2, 97)),
        // Turned edge-on the body is narrower, but its thickness still stands on the ground.
        rx: r2(bodyFloor.half * 0.78 * state.sx * away * (0.35 + 0.65 * Math.abs(Math.cos(state.tiltY * DEG)))),
        ry: r2(3.4 * Math.min(1, state.sy) * away),
        // Only a bot that is awake stands on the ground: a resting portrait has no smudge under it.
        opacity: r2(0.2 * away * clamp(state.sx * 1.4) * state.wake),
      };
    }

    return {
      body: bodyPath,
      transform: `matrix(${matrix.map(r2).join(",")})`,
      // `+ 0` folds -0 into 0, so a still frame is the same value whichever side it settled from.
      tilt: { x: r2(state.tiltX) + 0, y: r2(state.tiltY) + 0 },
      layers,
      sideFill: lerpHex(shadeDarkFor(swatch.fill), swatch.fill, 0.35),
      sideAlpha,
      eyes: renders,
      eyeAlpha: state.face.alpha,
      dots,
      notif,
      fill: swatch.fill,
      eyeFill: swatch.eyeFill,
      // The rim light is there at rest too: a resting bot is a still portrait, not a flat one.
      shade: shadeOf(swatch.fill, state.roll, turn, low ? 0.7 : 1),
      mouth: {
        d: mouthPath(lips),
        matrix: matrixString(mouth.matrix),
        alpha: r2(mouth.alpha),
        stroke: low ? 3.4 : MOUTH_STROKE,
        tongue: tongueOf(lips),
      },
      cheeks: cheeks.map((cheek) => ({ matrix: matrixString(cheek.matrix), alpha: r2(cheek.alpha * 0.62) })) as [
        FeatureRender,
        FeatureRender,
      ],
      blushFill: lerpHex(swatch.fill, BLUSH, 0.6),
      tongueFill: lerpHex(swatch.eyeFill, TONGUE, 0.62),
      shadow,
    };
  };

  return {
    set(patch, t) {
      const next = {
        appearance: patch.appearance ?? spec.appearance,
        mood: patch.mood ?? spec.mood,
      };
      const sameColour =
        next.appearance.color === spec.appearance.color && (next.appearance.eye ?? null) === (spec.appearance.eye ?? null);
      const sameAppearance = next.appearance.shape === spec.appearance.shape && sameColour;
      if (sameAppearance && next.mood === spec.mood) return;

      const previousKey = targetKey();
      faceFrom = moodFaceAt(t);
      faceFromKey = previousKey;
      faceFromAt = t;
      bodyFrom = moodBodyAt(t);
      bodyFromAt = t;
      if (next.appearance.shape !== spec.appearance.shape) {
        profilePrev = profileAt(t);
        shapePrev = spec.appearance.shape;
        shapeAt = t;
      }
      if (!sameColour) {
        colorFrom = swatchAt(t);
        colorAt = t;
        swatchTarget = swatchOf(next.appearance);
      }
      // Centre the blink on the swap, not on the call: the glyphs change 60 ms after `set`.
      if (kindsDiffer(previousKey, expressionKeyFor(next.mood))) maskAt = t + SWAP_DELAY - MASK / 2;
      spec = { appearance: next.appearance, mood: next.mood };
    },

    gesture(kind, t) {
      if (kind === "sleep") {
        sleepSince = t;
        wakeAt = null;
        gestureState = null;
        return;
      }
      if (sleepSince !== null) wakeAt = wakeAt ?? t;
      gestureState = { kind, at: t };
      if (gestureScript(kind, 0, spec.mood === "thinking").mask) maskAt = t + SWAP_DELAY - MASK / 2;
    },

    setAwake(on, t) {
      if (on === awake) return;
      awakeFrom = weightAt(t);
      awake = on;
      awakeAt = t;
    },

    gestureActive(t) {
      return oneShotAt(t);
    },

    resting(t) {
      // A face is only still once every morph has landed: a colour, shape or mood change on
      // a resting bot must play to the end before the renderer may stop painting it.
      const morphing =
        t - colorAt < TRANSITION ||
        t - shapeAt < TRANSITION ||
        t - faceFromAt < TRANSITION ||
        t - bodyFromAt < TRANSITION ||
        t - maskAt < MASK;
      return weightAt(t) <= 0 && !oneShotAt(t) && !morphing;
    },

    look(target, t) {
      if (target && !Number.isFinite(target.yaw + target.pitch + target.mix + target.spin + target.wander)) return;
      bodyLookFrom = lookAtTime(t, true);
      lookFrom = lookAtTime(t);
      lookTo = target ?? NO_LOOK;
      lookAt = t;
      if (target && target.mix > 0.05 && sleepSince !== null && wakeAt === null) {
        wakeAt = t;
        maskAt = t + SWAP_DELAY - MASK / 2;
      }
    },

    sample(t) {
      return renderFrame(t);
    },

    eyes(t) {
      return renderEyes(t).eyes;
    },
  };
}

function kindsDiffer(a: FaceKey, b: FaceKey): boolean {
  return FACES[a].eyes[0].kind !== FACES[b].eyes[0].kind || FACES[a].eyes[1].kind !== FACES[b].eyes[1].kind;
}

/** Eye geometry of a configuration that has never transitioned, at time t. */
export function eyePosesAt(config: BotConfig, t: number): [EyePose, EyePose] {
  return createBot(config).eyes(t);
}
