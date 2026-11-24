import { describe, expect, it } from "vitest";
import type { Reaction } from "../api/types";
import { hasOwnReaction, toggleOwnReaction } from "./reactions";

function pill(emoji: string, actors: { id: string; name: string }[]): Reaction {
  return { emoji, count: actors.length, mine: actors.some((a) => a.id === "user"), actors };
}

const user = { id: "user", name: "你" };
const agent = { id: "a_1", name: "主管" };

describe("toggleOwnReaction", () => {
  it("adds a new pill when the emoji is absent", () => {
    const next = toggleOwnReaction([], "👍");
    expect(next).toEqual([{ emoji: "👍", count: 1, mine: true, actors: [user] }]);
  });

  it("increments and marks mine an existing pill the user has not touched", () => {
    const before = [pill("👍", [agent])];
    const next = toggleOwnReaction(before, "👍");
    expect(next).toEqual([pill("👍", [agent, user])]);
    expect(next[0]!.mine).toBe(true);
  });

  it("removes only the user's own reaction, keeping other actors", () => {
    const before = [pill("👍", [user, agent])];
    const next = toggleOwnReaction(before, "👍");
    expect(next).toEqual([pill("👍", [agent])]);
    expect(next[0]!.mine).toBe(false);
  });

  it("drops the pill at zero when the user was the only actor", () => {
    const before = [pill("👍", [user])];
    expect(toggleOwnReaction(before, "👍")).toEqual([]);
  });

  it("leaves the input list untouched", () => {
    const before = [pill("👍", [user])];
    toggleOwnReaction(before, "👍");
    expect(before).toEqual([pill("👍", [user])]);
  });
});

describe("hasOwnReaction", () => {
  it("reads the mine flag for an emoji", () => {
    expect(hasOwnReaction([pill("👍", [user])], "👍")).toBe(true);
    expect(hasOwnReaction([pill("👍", [agent])], "👍")).toBe(false);
  });

  it("is false for a missing emoji or an empty list", () => {
    expect(hasOwnReaction([], "👍")).toBe(false);
    expect(hasOwnReaction(undefined, "👍")).toBe(false);
    expect(hasOwnReaction([pill("❤️", [user])], "👍")).toBe(false);
  });
});
