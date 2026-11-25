import { describe, expect, it } from "vitest";
import { mentionCandidates } from "./mentions";

describe("mention candidates", () => {
  it("puts the group's members first and team outsiders under their own group", () => {
    const roster = [{ name: "匠师" }, { name: "画师" }];
    const outside = [{ name: "画师助手" }, { name: "军师" }];
    expect(mentionCandidates(roster, outside, "")).toEqual([
      { agent: { name: "匠师" }, outside: false },
      { agent: { name: "画师" }, outside: false },
      { agent: { name: "画师助手" }, outside: true },
      { agent: { name: "军师" }, outside: true },
    ]);
  });

  it("filters both lists by the typed needle", () => {
    const roster = [{ name: "匠师" }];
    const outside = [{ name: "画师" }];
    expect(mentionCandidates(roster, outside, "画")).toEqual([
      { agent: { name: "画师" }, outside: true },
    ]);
  });
});
