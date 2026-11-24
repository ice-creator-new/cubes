import { describe, expect, it } from "vitest";
import { EMOJI_GROUPS, QUICK_EMOJI, allEmoji, findEmoji, groupOf } from "./emoji";

const entries = EMOJI_GROUPS.flatMap((group) => group.entries.map((entry) => ({ ...entry, group: group.id })));

describe("the emoji catalogue", () => {
  it("covers every input keyboard there is, in Unicode's own groups", () => {
    expect(EMOJI_GROUPS.map((group) => group.id)).toEqual([
      "smileys",
      "people",
      "animals",
      "food",
      "travel",
      "activities",
      "objects",
      "symbols",
      "flags",
    ]);
    for (const group of EMOJI_GROUPS) {
      expect(group.entries.length, group.id).toBeGreaterThan(40);
      expect(group.label.length, group.id).toBeGreaterThan(0);
    }
    // The set a keyboard offers is about two thousand; a curtailed list would show up here.
    expect(entries.length).toBeGreaterThan(1500);
  });

  it("has no duplicate emoji, and every entry carries a searchable name", () => {
    const seen = new Map<string, string>();
    for (const entry of entries) {
      expect(seen.has(entry.emoji), `${entry.emoji} in ${entry.group} and ${seen.get(entry.emoji)}`).toBe(false);
      seen.set(entry.emoji, entry.group);
      expect(entry.name.trim().length, entry.emoji).toBeGreaterThan(0);
      expect(entry.name, entry.emoji).toBe(entry.name.toLowerCase());
    }
  });

  it("lists no skin-tone variant: the base emoji is what the picker shows", () => {
    for (const emoji of allEmoji()) {
      for (const glyph of emoji) {
        const code = glyph.codePointAt(0) ?? 0;
        expect(code < 0x1f3fb || code > 0x1f3ff, emoji).toBe(true);
      }
    }
    // …and a skinned form still finds its group through the base glyph.
    expect(groupOf("👍🏽")).toBe("people");
    expect(groupOf("👨‍👩‍👧")).toBe("people");
  });

  it("offers a quick row drawn from the catalogue", () => {
    const all = new Set(allEmoji());
    for (const emoji of QUICK_EMOJI) expect(all.has(emoji), emoji).toBe(true);
  });

  it("searches by a word of the name, or by pasting the emoji itself", () => {
    expect(findEmoji("owl").map((entry) => entry.emoji)).toContain("🦉");
    expect(findEmoji("red heart").length).toBeGreaterThan(0);
    expect(findEmoji("🦉").map((entry) => entry.emoji)).toContain("🦉");
    expect(findEmoji("  ")).toEqual([]);
    expect(findEmoji("zzzzzz")).toEqual([]);
    // A broad word stays within what a picker can show.
    expect(findEmoji("face").length).toBeLessThanOrEqual(240);
  });
});
