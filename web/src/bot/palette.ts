// Twelve body colours with an eye colour chosen for contrast, plus the matching easing curves
// for light and dark. Bodies use mid tones so the same swatch reads on both canvases (#f3f2f0, #17181c).
// A user may also pick any body colour and any eye colour; `swatchOf` resolves both.

import { isColorId, type Appearance, type ColorId, type ColorValue } from "./catalog";
import { contrast, lerpHex } from "./math";

const FILLS: Record<ColorId, string> = {
  ink: "#3b4352",
  cream: "#e7d3aa",
  clay: "#c4744f",
  coral: "#e8735f",
  tangerine: "#ef9445",
  honey: "#e5b13c",
  moss: "#82994f",
  jade: "#3f9c7e",
  sky: "#5c9fd0",
  iris: "#8175c9",
  plum: "#b06aa8",
  slate: "#77828f",
};

const INK_SHADE = "#0a0b0f";
const CREAM_SHADE = "#fffdf6";

export interface Swatch {
  fill: string;
  eyeFill: string;
}

/** The eye takes a tinted dark or a warm light, whichever clears 4.5:1 against the body. */
function eyeFillFor(fill: string): string {
  // Dark eyes read hardest on mid and pale bodies, so deepen the tint before falling back to light.
  for (const amount of [0.88, 0.92, 0.96, 1]) {
    const dark = lerpHex(fill, INK_SHADE, amount);
    if (contrast(fill, dark) >= 4.5) return dark;
  }
  for (const amount of [0.92, 0.96, 1]) {
    const light = lerpHex(fill, CREAM_SHADE, amount);
    if (contrast(fill, light) >= 4.5) return light;
  }
  return contrast(fill, INK_SHADE) >= contrast(fill, CREAM_SHADE) ? INK_SHADE : CREAM_SHADE;
}

export const SWATCHES: Record<ColorId, Swatch> = Object.fromEntries(
  (Object.keys(FILLS) as ColorId[]).map((id) => [id, { fill: FILLS[id], eyeFill: eyeFillFor(FILLS[id]) }]),
) as Record<ColorId, Swatch>;

/** The hex a body colour paints with. */
export function fillOf(color: ColorValue): string {
  return isColorId(color) ? FILLS[color] : color;
}

/** Body and eye colours for an appearance: a chosen eye wins, otherwise the contrast eye. */
export function swatchOf(appearance: Pick<Appearance, "color" | "eye">): Swatch {
  const fill = fillOf(appearance.color);
  const auto = isColorId(appearance.color) ? SWATCHES[appearance.color].eyeFill : eyeFillFor(fill);
  return { fill, eyeFill: appearance.eye ?? auto };
}
