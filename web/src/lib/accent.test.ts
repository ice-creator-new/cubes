import { describe, expect, it } from "vitest";
import { accentStyle, bubbleInk, onColor, parseAccent, readable } from "./accent";
import { contrast } from "../bot/math";

const CANVAS = { light: "#f3f2f0", dark: "#17181c" } as const;

/** A spread of hues at several lightnesses, plus the greys that sit where neither text clears. */
const SAMPLES = (() => {
  const out: string[] = ["#000000", "#ffffff", "#808080", "#767676", "#949494"];
  for (let hue = 0; hue < 360; hue += 30) {
    for (const lightness of [0.15, 0.35, 0.5, 0.65, 0.85]) {
      const h = hue / 360;
      const s = 0.7;
      const q = lightness < 0.5 ? lightness * (1 + s) : lightness + s - lightness * s;
      const p = 2 * lightness - q;
      const channel = (t: number) => {
        const value = t < 0 ? t + 1 : t > 1 ? t - 1 : t;
        const raw = value < 1 / 6 ? p + (q - p) * 6 * value : value < 1 / 2 ? q : value < 2 / 3 ? p + (q - p) * (2 / 3 - value) * 6 : p;
        return Math.round(raw * 255).toString(16).padStart(2, "0");
      };
      out.push(`#${channel(h + 1 / 3)}${channel(h)}${channel(h - 1 / 3)}`);
    }
  }
  return out;
})();

describe("text on a filled accent", () => {
  it("is white on dark fills and black on pale ones", () => {
    expect(onColor("#1f6feb")).toBe("#ffffff");
    expect(onColor("#f5e8c0")).toBe("#000000");
    // Mid tones: black wins by a hair, which is the only choice that reads.
    expect(onColor("#767676")).toBe("#000000");
  });

  it("always clears 4.5:1, whatever the fill", () => {
    for (const fill of SAMPLES) {
      expect(contrast(onColor(fill), fill), fill).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe("the bubble", () => {
  it("keeps a pale pick as it is, with black text", () => {
    expect(bubbleInk("#ffe066")).toEqual({ bubble: "#ffe066", ink: "#000000" });
    expect(bubbleInk("#ffffff")).toEqual({ bubble: "#ffffff", ink: "#000000" });
  });

  it("gives a saturated pick light text, deepening it only as far as that needs", () => {
    // The amber that reported the problem: black only won on paper (4.67 against 4.50).
    const amber = bubbleInk("#a96800");
    expect(amber.ink).toBe("#ffffff");
    expect(contrast(amber.ink, amber.bubble)).toBeGreaterThanOrEqual(4.6);
    // A small step, not a different colour: the hue survives.
    expect(contrast(amber.bubble, "#a96800")).toBeLessThan(1.6);
    // A dark pick already carries white text untouched.
    expect(bubbleInk("#123456")).toEqual({ bubble: "#123456", ink: "#ffffff" });
  });

  it("always reads, and keeps light text on anything saturated", () => {
    for (const fill of SAMPLES) {
      const { bubble, ink } = bubbleInk(fill);
      expect(contrast(ink, bubble), fill).toBeGreaterThanOrEqual(4.5);
      // Never black text on a fill too dark to carry it comfortably, never white on a pale one.
      if (ink === "#000000") expect(contrast(ink, bubble), fill).toBeGreaterThanOrEqual(7);
    }
  });
});

describe("the line colour", () => {
  it("leaves a colour that already reads untouched, and nudges one that does not", () => {
    expect(readable("#1c7a4b", "light")).toBe("#1c7a4b");
    expect(readable("#8db8f2", "dark")).toBe("#8db8f2");
    // The shipped blue is a hair under AA as a line on the page (4.36:1): it is darkened just
    // enough, rather than trusted because it is the default.
    expect(readable("#2f6fd0", "light")).not.toBe("#2f6fd0");
    expect(contrast(readable("#2f6fd0", "light"), CANVAS.light)).toBeGreaterThanOrEqual(4.6);
  });

  it("darkens a pale pick on a light page and lightens a dark one on a dark page", () => {
    for (const [color, mode] of [
      ["#ffe066", "light"],
      ["#0f6d72", "dark"],
      ["#ffffff", "light"],
      ["#000000", "dark"],
    ] as const) {
      const line = readable(color, mode);
      expect(line, `${color} ${mode}`).not.toBe(color);
      expect(contrast(line, CANVAS[mode]), `${color} ${mode}`).toBeGreaterThanOrEqual(4.6);
    }
  });

  it("reads on the page for every sample in both modes", () => {
    for (const mode of ["light", "dark"] as const) {
      for (const color of SAMPLES) {
        expect(contrast(readable(color, mode), CANVAS[mode]), `${color} ${mode}`).toBeGreaterThanOrEqual(4.6);
      }
    }
  });
});

describe("accent style", () => {
  it("keeps a pale pick for the bubble and adapts what sits on it", () => {
    const light = accentStyle("#ffe066", "light");
    expect(light["--bubble-user"]).toBe("#ffe066");
    expect(light["--on-bubble-user"]).toBe("#000000");
    expect(contrast(light["--accent"], CANVAS.light)).toBeGreaterThanOrEqual(4.6);
    expect(contrast(light["--on-accent"], light["--accent"])).toBeGreaterThanOrEqual(4.5);

    const dark = accentStyle("#123456", "dark");
    expect(dark["--bubble-user"]).toBe("#123456");
    expect(dark["--on-bubble-user"]).toBe("#ffffff");
    expect(accentStyle("#a96800", "dark")["--on-bubble-user"]).toBe("#ffffff");
    expect(contrast(dark["--accent"], CANVAS.dark)).toBeGreaterThanOrEqual(4.6);
  });

  it("sets exactly the tokens the presets set", () => {
    expect(Object.keys(accentStyle("#2f6fd0", "light")).sort()).toEqual(
      ["--accent", "--bubble-user", "--on-accent", "--on-bubble-user"].sort(),
    );
  });
});

describe("stored accents", () => {
  it("takes a preset id, a free colour, or falls back to blue", () => {
    expect(parseAccent("teal")).toBe("teal");
    expect(parseAccent("#FF8800")).toBe("#ff8800");
    expect(parseAccent("")).toBe("blue");
    expect(parseAccent(null)).toBe("blue");
    expect(parseAccent("#ff88")).toBe("blue");
    expect(parseAccent("chartreuse")).toBe("blue");
  });
});
