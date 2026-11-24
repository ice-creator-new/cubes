// The public appearance catalogue. These lists must stay identical to the backend's
// constants; a test pins them. Nothing here touches React or the DOM.
//
// An appearance is a silhouette, a body colour and, optionally, an eye colour. The body is a
// catalogue colour or any `#rrggbb`; the eye is any `#rrggbb`, or absent for the automatic
// high-contrast eye. Faces are automatic: the reply's mood tag and what the bot is doing pick
// the expression, so there is nothing for a user to choose there.

import { hash, unit } from "./math";

export const SHAPES = [
  "round",
  "blob",
  "squircle",
  "sparkle",
  "clover",
  "teardrop",
  "flower",
  "peak",
  "capsule",
  "arch",
  "pentagon",
  "star",
  "hexagon",
  "pebble",
  "heart",
  "egg",
  "diamond",
  "octagon",
  "cat",
  "bear",
  "ghost",
] as const;
export type ShapeId = (typeof SHAPES)[number];
/** The shapes a seed may pick: the original fourteen, so a member without a stored
 *  appearance keeps the face it always had when shapes are added to the end of the set. */
const SEEDED_SHAPES = SHAPES.slice(0, 14);

/** Silhouettes retired from the set, mapped to what replaced them. */
const LEGACY_SHAPES: Record<string, ShapeId> = { bean: "blob", cloud: "clover", drop: "teardrop", lean: "arch" };

export const COLORS = [
  "ink",
  "cream",
  "clay",
  "coral",
  "tangerine",
  "honey",
  "moss",
  "jade",
  "sky",
  "iris",
  "plum",
  "slate",
] as const;
export type ColorId = (typeof COLORS)[number];
/** A free colour, always stored as lowercase `#rrggbb`. */
export type HexColor = `#${string}`;
/** A body colour: a catalogue swatch or a free colour. */
export type ColorValue = ColorId | HexColor;

/** The subset a model may tag a reply with. Faces follow it; the user picks nothing here. */
export const MOODS = ["neutral", "happy", "proud", "curious", "confused", "sorry", "surprised", "focused"] as const;
export type MoodTag = (typeof MOODS)[number];

export interface Appearance {
  shape: ShapeId;
  color: ColorValue;
  /** The eye colour; absent or null picks a high-contrast eye for the body automatically. */
  eye?: HexColor | null;
}

/** A stored shape id, with a retired id mapped to the shape that replaced it. */
export function resolveShape(value: unknown): ShapeId | null {
  if (typeof value !== "string") return null;
  const shape = LEGACY_SHAPES[value] ?? value;
  return (SHAPES as readonly string[]).includes(shape) ? (shape as ShapeId) : null;
}

export function isShapeId(value: unknown): value is ShapeId {
  return resolveShape(value) !== null;
}

export function isColorId(value: unknown): value is ColorId {
  return typeof value === "string" && (COLORS as readonly string[]).includes(value);
}

/** `value` as a lowercase `#rrggbb`, or null when it is not one. */
export function asHexColor(value: unknown): HexColor | null {
  if (typeof value !== "string" || !/^#[0-9a-f]{6}$/i.test(value)) return null;
  return value.toLowerCase() as HexColor;
}

export function isMoodTag(value: unknown): value is MoodTag {
  return typeof value === "string" && (MOODS as readonly string[]).includes(value);
}

/** Model mood tags; anything else is dropped. */
export function asMoodTag(value: string | null | undefined): MoodTag | null {
  return isMoodTag(value) ? value : null;
}

/**
 * A face derived from a seed. Ink is the one colour a seed never picks: it is the darkest
 * swatch and would make newly created members look like flat holes.
 */
export function appearanceFromSeed(seed: string | null | undefined): Appearance {
  const value = hash(seed ?? "");
  const shape = SEEDED_SHAPES[Math.floor(unit(value, 1) * SEEDED_SHAPES.length)] ?? "round";
  const colorOffset = Math.floor(unit(value, 2) * (COLORS.length - 1));
  const color = COLORS[1 + colorOffset] ?? "clay";
  return { shape, color };
}

/** Stored appearance when valid, the seed-derived face otherwise. An unusable eye colour
 *  falls back to the automatic eye; it never costs the body its shape or colour. */
export function resolveAppearance(
  appearance: { shape?: unknown; color?: unknown; eye?: unknown } | null | undefined,
  seed: string | null | undefined,
): Appearance {
  if (!appearance) return appearanceFromSeed(seed);
  const fallback = appearanceFromSeed(seed);
  const shape = resolveShape(appearance.shape) ?? fallback.shape;
  const color = isColorId(appearance.color) ? appearance.color : (asHexColor(appearance.color) ?? fallback.color);
  const eye = asHexColor(appearance.eye);
  return eye ? { shape, color, eye } : { shape, color };
}
