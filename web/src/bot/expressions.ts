// The faces a bot can wear, built from a small vocabulary of eye kinds so each one reads at
// 30px. A face is a gaze direction, an eye spacing (as a fraction of the room the shape has
// at eye height), two independent eye glyphs, a mouth and a blush. Mirrored tilts carry
// subtle moods; kinds and the mouth carry the loud ones. The user never picks one: the mood
// tag and the current gesture do.

import type { MoodTag } from "./catalog";

export type EyeKind = "capsule" | "arcUp" | "arcDown" | "line" | "dot" | "chevron";

export interface EyeShape {
  kind: EyeKind;
  /** Width of the glyph in viewBox units. */
  w: number;
  /** Height of the glyph; `open` is the eyelid and multiplies it. */
  h: number;
  /** Clockwise rotation in radians, in screen coordinates (y down). */
  tilt: number;
  /** 0..1 eyelid. */
  open: number;
  /** Vertical offset from the eye line, positive is down. */
  dy: number;
}

/**
 * A mouth is two curves between the same pair of corners, drawn with a round stroke so no
 * corner is ever a point. `top` and `bottom` are how far each curve's middle sits below the
 * corners (negative is above): equal values are a closed line, a smile when positive and a
 * frown when negative; a `bottom` deeper than `top` opens the mouth.
 */
export interface MouthShape {
  /** Corner to corner, viewBox units. */
  w: number;
  top: number;
  bottom: number;
  /** Clockwise rotation in radians: a lopsided mouth. */
  tilt: number;
}

export interface Gaze {
  /** Radians, positive turns the face to its own right. */
  yaw: number;
  /** Radians, positive looks down. */
  pitch: number;
  /** Radians, added to both eye tilts. */
  roll: number;
}

export interface Expression {
  gaze: Gaze;
  /** Half the distance between eye centres, as a fraction of the shape's half-width. */
  split: number;
  eyes: readonly [EyeShape, EyeShape];
  mouth: MouthShape;
  /** 0..1 strength of the cheeks. */
  blush: number;
}

/** Every face a mood tag can ask for, plus the transient wince. */
export type ExpressionKey = MoodTag | "error";

interface Adjust {
  tilt?: number;
  open?: number;
  dy?: number;
}

function glyph(kind: EyeKind, w: number, h: number, adjust: Adjust = {}): EyeShape {
  return { kind, w, h, tilt: adjust.tilt ?? 0, open: adjust.open ?? 1, dy: adjust.dy ?? 0 };
}

function mouth(w: number, top: number, bottom: number, tilt = 0): MouthShape {
  return { w, top, bottom, tilt };
}

interface Look {
  yaw: number;
  pitch: number;
  split: number;
  roll?: number;
}

function face(look: Look, left: EyeShape, right: EyeShape, lips: MouthShape, blush: number): Expression {
  return { gaze: { yaw: look.yaw, pitch: look.pitch, roll: look.roll ?? 0 }, split: look.split, eyes: [left, right], mouth: lips, blush };
}

export const EXPRESSION_PARAMS: Record<ExpressionKey, Expression> = {
  // Two tall capsules, level gaze, a small easy smile.
  neutral: face({ yaw: 0, pitch: 0, split: 0.32 }, glyph("capsule", 18, 22), glyph("capsule", 18, 22), mouth(10, 1.7, 1.7), 0.3),
  // Round-capped arcs over an open smile, cheeks up.
  happy: face({ yaw: 0, pitch: -0.12, split: 0.34 }, glyph("arcUp", 22, 13), glyph("arcUp", 22, 13), mouth(13, 0.6, 7.2), 0.75),
  // Smaller closed arcs, chin up, a closed smile.
  proud: face({ yaw: 0, pitch: -0.22, split: 0.35 }, glyph("arcUp", 20, 11), glyph("arcUp", 20, 11), mouth(12, 2.4, 2.4), 0.55),
  // One eye wide, one small, looking up and aside, a little "o".
  curious: face(
    { yaw: 0.16, pitch: -0.14, split: 0.3, roll: 0.05 },
    glyph("capsule", 19, 23, { tilt: -0.1 }),
    glyph("capsule", 14, 17, { tilt: 0.08 }),
    mouth(5, -1.8, 2),
    0.3,
  ),
  // Opposite tilts and mismatched eyes, a lopsided mouth.
  confused: face(
    { yaw: -0.14, pitch: -0.06, split: 0.28, roll: 0.05 },
    glyph("capsule", 16, 20, { tilt: 0.24 }),
    glyph("capsule", 18, 14, { tilt: -0.22 }),
    mouth(9, 0.4, 0.4, 0.22),
    0.2,
  ),
  // Heavy lids, gaze low, a small frown.
  sorry: face(
    { yaw: -0.04, pitch: 0.16, split: 0.32 },
    glyph("capsule", 17, 20, { open: 0.72 }),
    glyph("capsule", 17, 20, { open: 0.72 }),
    mouth(9, -2, -2),
    0.25,
  ),
  // Big round dots, wide apart, a round open mouth.
  surprised: face({ yaw: 0, pitch: 0, split: 0.44 }, glyph("dot", 22, 22), glyph("dot", 22, 22), mouth(7, -3.4, 3.6), 0.4),
  // Narrow capsules angled inward, gaze down, lips pressed flat: a hard stare.
  focused: face(
    { yaw: 0, pitch: 0.06, split: 0.3 },
    glyph("capsule", 22, 9, { tilt: 0.18 }),
    glyph("capsule", 22, 9, { tilt: -0.18 }),
    mouth(7, 0, 0),
    0.1,
  ),
  // Thick angular chevrons pointed at each other, gaze down, a grimace: a wince.
  error: face({ yaw: 0, pitch: 0.26, split: 0.4 }, glyph("chevron", 18, 12), glyph("chevron", 18, 12), mouth(9, -2.4, -0.4), 0.15),
};

/**
 * Faces a gesture borrows for a moment. They are full faces rather than per-gesture eye
 * overrides, so the fit table can be solved for them exactly like the moods: each entry is one
 * more row, never a per-frame solve.
 */
export type GestureFaceKey = "wide" | "cheer" | "droop" | "closed";

export const GESTURE_FACES: Record<GestureFaceKey, Expression> = {
  // Eyes popped wide, a small "o": a mention or a private message arriving.
  wide: face({ yaw: 0, pitch: 0, split: 0.46 }, glyph("dot", 24, 25), glyph("dot", 24, 25), mouth(6, -2.6, 2.8), 0.45),
  // Lifted arcs over a wide open grin: a happy reply landing.
  cheer: face({ yaw: 0, pitch: -0.16, split: 0.36 }, glyph("arcUp", 24, 14), glyph("arcUp", 24, 14), mouth(15, 0.4, 8.6), 0.9),
  // Arcs sagging low, a small frown: a sorry reply.
  droop: face(
    { yaw: -0.02, pitch: 0.2, split: 0.33 },
    glyph("arcDown", 24, 11, { dy: 3 }),
    glyph("arcDown", 24, 11, { dy: 3 }),
    mouth(8, -1.8, -1.8),
    0.2,
  ),
  // Two flat bars, sitting low, a tiny round mouth: asleep.
  closed: face(
    { yaw: 0, pitch: 0.1, split: 0.3 },
    glyph("line", 26, 4.5, { dy: 3 }),
    glyph("line", 26, 4.5, { dy: 3 }),
    mouth(4, -1.2, 1.4),
    0.35,
  ),
};

export type FaceKey = ExpressionKey | GestureFaceKey;

/** Every face the fit table and the engine can ask for. */
export const FACES: Record<FaceKey, Expression> = { ...EXPRESSION_PARAMS, ...GESTURE_FACES };
