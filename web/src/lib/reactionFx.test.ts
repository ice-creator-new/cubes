import { describe, expect, it } from "vitest";
import { FAMILY_STYLE, burst, burstLife, reactionFamily, sample, type ReactionFamily, type Speck } from "./reactionFx";

const FAMILIES = Object.keys(FAMILY_STYLE) as ReactionFamily[];

/** Every frame of one speck, from before its delay to just past its end. */
function trail(speck: Speck, step = 0.01) {
  const out = [];
  for (let t = 0; t <= speck.delay + speck.life + step; t += step) out.push({ t, ...sample(speck, t) });
  return out;
}

describe("reactionFamily", () => {
  it("sorts emoji into their families, ignoring tone and joiners", () => {
    expect(reactionFamily("❤️")).toBe("hearts");
    expect(reactionFamily("🩷")).toBe("hearts");
    expect(reactionFamily("🎉")).toBe("confetti");
    expect(reactionFamily("👍🏽")).toBe("sparks");
    expect(reactionFamily("😂")).toBe("faces");
    expect(reactionFamily("🍰")).toBe("food");
    expect(reactionFamily("🐨")).toBe("animals");
    // The catalogue's groups decide the rest: eyes are a person's part, a melting face is a face.
    expect(reactionFamily("👀")).toBe("sparks");
    expect(reactionFamily("🫠")).toBe("faces");
    // Anything that is not an emoji at all still gets a burst.
    expect(reactionFamily("x")).toBe("marks");
    expect(reactionFamily("")).toBe("marks");
  });
});

describe("the burst", () => {
  it("throws the family's count, drawn from its own kinds", () => {
    for (const family of FAMILIES) {
      const style = FAMILY_STYLE[family];
      const specks = burst(family);
      expect(specks).toHaveLength(style.count);
      const looks = new Set(style.kinds.map((kind) => kind.look));
      for (const speck of specks) {
        expect(looks.has(speck.look), family).toBe(true);
        if (speck.look === "emoji" || speck.look === "glyph") expect(speck.glyph).not.toBe("");
      }
    }
  });

  it("is deterministic, so a re-render never reshuffles the burst", () => {
    expect(burst("confetti", 3)).toEqual(burst("confetti", 3));
    expect(burst("confetti", 3)).not.toEqual(burst("confetti", 4));
  });

  it("stays inside a burst's budget", () => {
    for (const family of FAMILIES) {
      const specks = burst(family, 2);
      expect(specks.length).toBeLessThanOrEqual(20);
      expect(burstLife(specks), family).toBeLessThanOrEqual(1);
      // Staggered, but not so late that the burst reads as two.
      for (const speck of specks) expect(speck.delay).toBeLessThanOrEqual(0.07);
    }
  });

  it("gives the specks depth: sizes and sharpness differ inside one burst", () => {
    for (const family of FAMILIES) {
      const specks = burst(family, 5);
      const depths = specks.map((speck) => speck.z);
      expect(Math.min(...depths), family).toBeLessThan(0.6);
      expect(Math.max(...depths), family).toBeGreaterThan(0.9);
      const sizes = new Set(specks.map((speck) => Math.round(speck.size * (0.7 + 0.55 * speck.z) * 10)));
      expect(sizes.size, family).toBeGreaterThan(2);
    }
  });
});

describe("a speck's flight", () => {
  it("leaves the pill: every family reaches past its own width", () => {
    for (const family of FAMILIES) {
      let reach = 0;
      for (const speck of burst(family, 1)) {
        for (const frame of trail(speck, 0.02)) reach = Math.max(reach, Math.hypot(frame.x, frame.y));
      }
      // A pill is ~28px wide; a burst shorter than that never leaves it.
      expect(reach, family).toBeGreaterThan(32);
      expect(reach, family).toBeLessThan(140);
    }
  });

  it("rises for hearts and falls for confetti", () => {
    const end = (family: ReactionFamily) => {
      const specks = burst(family, 1);
      const lows = specks.map((speck) => sample(speck, speck.delay + speck.life).y);
      return { lowest: Math.max(...lows), highest: Math.min(...lows) };
    };
    expect(end("hearts").lowest).toBeLessThan(-20);
    expect(end("confetti").highest).toBeGreaterThan(20);
  });

  it("starts hidden at the pill's edge and is gone by its own end", () => {
    for (const family of FAMILIES) {
      for (const speck of burst(family, 3)) {
        const before = sample(speck, 0);
        expect(before.opacity).toBe(0);
        expect(Math.hypot(before.x, before.y), family).toBeLessThan(14);
        const last = sample(speck, speck.delay + speck.life);
        expect(last.opacity, family).toBe(0);
        // Fades in quickly rather than popping at full strength on frame one.
        expect(sample(speck, speck.delay + 0.02).opacity).toBeLessThan(0.6);
      }
    }
  });

  it("stays finite and inside its envelope at every frame", () => {
    for (const family of FAMILIES) {
      for (const speck of burst(family, 4)) {
        for (const frame of trail(speck, 0.02)) {
          for (const value of [frame.x, frame.y, frame.rot, frame.scale, frame.opacity]) {
            expect(Number.isFinite(value), family).toBe(true);
          }
          expect(frame.opacity).toBeGreaterThanOrEqual(0);
          expect(frame.opacity).toBeLessThanOrEqual(1);
          expect(frame.scale).toBeGreaterThanOrEqual(0);
          expect(Math.abs(frame.x)).toBeLessThan(160);
          expect(Math.abs(frame.y)).toBeLessThan(160);
        }
      }
    }
  });
});
