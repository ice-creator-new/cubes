// The theme colour: six presets, or any colour the user picks. A preset is plain CSS
// (`data-accent` in styles.css); a free colour is computed here and written to the same four
// custom properties the presets set, so nothing else in the stylesheet has to know the
// difference.
//
// A pick is used as it is where it is a surface — the chat bubble — and the text on top of it
// turns white or black to suit. Where the colour is a line or a glyph on the page (links, focus
// rings, the send button's fill) it is walked toward black on a light page or white on a dark
// one until it reads, which is what the hand-tuned presets do by eye.

import { asHexColor, type HexColor } from "../bot/catalog";
import { contrast, lerpHex } from "../bot/math";

/** The accent catalogue; 「blue」 is the app's own colour and the stylesheet's default. */
export const THEME_ACCENTS = [
  { id: "blue", label: "矿石蓝", swatch: "#2f6fd0" },
  { id: "violet", label: "紫罗兰", swatch: "#6a45d6" },
  { id: "green", label: "松石绿", swatch: "#1c7a4b" },
  { id: "amber", label: "琥珀", swatch: "#a2660a" },
  { id: "rose", label: "玫红", swatch: "#c02a55" },
  { id: "teal", label: "青碧", swatch: "#0f6d72" },
] as const;

export type ThemeAccent = (typeof THEME_ACCENTS)[number]["id"];
/** A preset's id, or any colour. */
export type Accent = ThemeAccent | HexColor;

export type ThemeMode = "light" | "dark";

/** The page behind text in each mode (`--canvas` in styles.css). Keep the two in step. */
const CANVAS: Record<ThemeMode, string> = { light: "#f3f2f0", dark: "#17181c" };
/** Body text on a filled surface, with headroom: WCAG AA asks 4.5. */
const LEGIBLE = 4.6;
const WHITE = "#ffffff";
const BLACK = "#000000";

/** The custom properties a free colour sets; a preset's stylesheet rule sets the same four. */
export const ACCENT_TOKENS = ["--accent", "--bubble-user", "--on-bubble-user", "--on-accent"] as const;
export type AccentToken = (typeof ACCENT_TOKENS)[number];

/** White or black, whichever reads better on `fill`. One of the two always clears 4.5:1: they
 *  meet at 4.58 on the luminance where neither is clearly better. */
export function onColor(fill: string): string {
  return contrast(WHITE, fill) >= contrast(BLACK, fill) ? WHITE : BLACK;
}

/** Black text this legible is a comfortable choice; below it a fill reads as a saturated
 *  surface, which wants light text like every preset bubble. */
const COMFORTABLE = 7;
/** What white text on a bubble must clear once the bubble has been deepened for it. */
const ON_SATURATED = 4.6;

/**
 * A bubble's fill and the text on it. A pale pick keeps its colour and takes black text. A
 * saturated one takes white text — the app's bubbles all do — and is deepened only as far as
 * white text needs, so a mid amber reads as amber with light text instead of amber with black
 * text that only just wins on paper (4.67 against 4.50) and looks muddy.
 */
export function bubbleInk(fill: string): { bubble: string; ink: string } {
  if (contrast(BLACK, fill) >= COMFORTABLE) return { bubble: fill, ink: BLACK };
  let bubble = fill;
  for (let step = 1; contrast(WHITE, bubble) < ON_SATURATED; step += 1) {
    // Toward black keeps the hue and only lowers the tone; 20 steps reach black, which clears.
    bubble = lerpHex(fill, BLACK, Math.min(1, step / 20));
    if (step >= 20) break;
  }
  return { bubble, ink: WHITE };
}

/**
 * `color` walked toward black on a light page (or white on a dark one) until it reads on that
 * page. A colour that already reads is returned untouched, so a pick is never changed needlessly.
 */
export function readable(color: string, mode: ThemeMode): string {
  const page = CANVAS[mode];
  const toward = mode === "light" ? BLACK : WHITE;
  let current = color;
  for (let step = 1; contrast(current, page) < LEGIBLE; step += 1) {
    // Each step is monotone toward the target, which clears the page on its own, so 20 is enough.
    current = lerpHex(color, toward, Math.min(1, step / 20));
    if (step >= 20) break;
  }
  return current;
}

/** The four tokens for a free colour in one theme: the bubble keeps the pick where it can, its
 *  text adapts, and the page's line colour is adjusted until it reads. */
export function accentStyle(color: string, mode: ThemeMode): Record<AccentToken, string> {
  const accent = readable(color, mode);
  const bubble = bubbleInk(color);
  return {
    "--accent": accent,
    "--bubble-user": bubble.bubble,
    "--on-bubble-user": bubble.ink,
    "--on-accent": onColor(accent),
  };
}

/** A stored accent: a preset id, a free colour, or 「blue」 when the value is missing or junk. */
export function parseAccent(raw: string | null | undefined): Accent {
  if (raw && THEME_ACCENTS.some((choice) => choice.id === raw)) return raw as ThemeAccent;
  return asHexColor(raw) ?? "blue";
}

/** The swatch to show for an accent: the free colour itself, else its preset for this theme. */
export function accentSwatch(accent: Accent, mode: ThemeMode): string {
  const custom = asHexColor(accent);
  if (custom) return custom;
  const preset = THEME_ACCENTS.find((choice) => choice.id === accent) ?? THEME_ACCENTS[0];
  return mode === "light" ? preset.swatch : readable(preset.swatch, mode);
}
