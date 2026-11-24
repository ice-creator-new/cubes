// The macOS window material behind the sidebar and the tab band: the system's Liquid Glass, or a
// frosted blur whose see-through amount and blur the user sets. The page owns the choice (it is
// remembered here and sent to the shell); the shell draws the native layer, and the page tints
// it with `--glass-tint`, so both halves always describe the same material.

import { desktop, isDesktop, isMac } from "./platform";

export type GlassKind = "liquid" | "frosted";

export interface Glass {
  kind: GlassKind;
  /** Frosted only: how much shows through, 0 (a solid tint) to 100 (no tint at all). */
  transparency: number;
  /** Frosted only: the blur radius of what is behind the window, in points. */
  blur: number;
}

export const GLASS_KEY = "cubes.glass";
export const MAX_BLUR = 80;
/** Today's frosted look: the sidebar tint at 40% over a 30pt blur. */
export const DEFAULT_GLASS: Glass = { kind: "frosted", transparency: 60, blur: 30 };
/** Liquid Glass carries its own tint; the page adds only a whisper so text keeps its contrast. */
const LIQUID_TINT = 0.15;

/** The window material can only be chosen inside the macOS desktop window. */
export const glassAvailable = isDesktop && isMac;

function clamp(value: unknown, low: number, high: number, fallback: number): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? Math.round(Math.min(high, Math.max(low, number))) : fallback;
}

/** A stored value, or the default for anything missing or broken. */
export function parseGlass(raw: string | null): Glass {
  if (!raw) return DEFAULT_GLASS;
  try {
    const value = JSON.parse(raw) as Partial<Glass>;
    return {
      kind: value.kind === "liquid" ? "liquid" : "frosted",
      transparency: clamp(value.transparency, 0, 100, DEFAULT_GLASS.transparency),
      blur: clamp(value.blur, 0, MAX_BLUR, DEFAULT_GLASS.blur),
    };
  } catch {
    return DEFAULT_GLASS;
  }
}

/** The alpha of the page's own tint over the native material. */
export function glassTint(glass: Glass): number {
  return glass.kind === "liquid" ? LIQUID_TINT : (100 - glass.transparency) / 100;
}

/** Which materials this Mac can draw; null outside the desktop window. */
export interface GlassSupport {
  liquid: boolean;
  frosted: boolean;
}

export function glassSupport(): Promise<GlassSupport | null> {
  if (!glassAvailable || !desktop) return Promise.resolve(null);
  return desktop.glassSupport();
}

/** A slider drag sends one update per step; the native view is rebuilt once the hand rests. */
const NATIVE_DELAY_MS = 120;
let pending: number | undefined;

/** Tint the page at once and hand the material to the shell. A no-op in a browser. */
export function applyGlass(glass: Glass): void {
  if (!glassAvailable) return;
  document.documentElement.style.setProperty("--glass-tint", String(glassTint(glass)));
  document.documentElement.dataset.glass = glass.kind;
  window.clearTimeout(pending);
  pending = window.setTimeout(() => desktop?.setGlass(glass), NATIVE_DELAY_MS);
}
