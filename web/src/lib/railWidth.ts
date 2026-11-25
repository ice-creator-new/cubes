// The sidebar's width: dragged by the user, remembered across launches, always kept between a
// floor that clears the macOS traffic lights and a ceiling that leaves room for the chat.

import { isMac } from "./platform";

export const RAIL_WIDTH_KEY = "cubes.sidebar.width";
export const RAIL_DEFAULT = 264;
/** Narrowest open rail: rows still show a face, a title line and the time. */
export const RAIL_MIN = 220;
export const RAIL_MAX = 440;
/**
 * The folded rail. On macOS the traffic lights sit at x = 18 and span about 52px (to x ≈ 70);
 * 96px leaves a clear 26px between the last light and the rail's edge, so they never look jammed
 * against it. Windows and the browser have no lights, so their folded rail hugs its faces
 * (36px) with 10px of margin on each side instead.
 */
export const RAIL_FOLDED = isMac ? 96 : 56;
/** Below this the chat column is too narrow to read; the rail gives way first. */
const CHAT_MIN = 420;

/** The width to apply for a requested one in a window `viewport` px wide. */
export function clampRailWidth(requested: number, viewport: number): number {
  const ceiling = Math.max(RAIL_MIN, Math.min(RAIL_MAX, viewport - CHAT_MIN));
  if (!Number.isFinite(requested)) return Math.min(RAIL_DEFAULT, ceiling);
  return Math.round(Math.min(ceiling, Math.max(RAIL_MIN, requested)));
}

export function readRailWidth(): number {
  const raw = Number(localStorage.getItem(RAIL_WIDTH_KEY));
  return raw > 0 ? raw : RAIL_DEFAULT;
}
