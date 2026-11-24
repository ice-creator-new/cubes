import { describe, expect, it } from "vitest";
import { capsuleDeletion, draftSegments } from "./composerTokens";

const members = new Set(["甲", "教育科技调研员"]);
const skills = new Set(["weekly-report"]);

describe("draftSegments", () => {
  it("draws finished references as capsules and keeps the text between them", () => {
    const text = "@教育科技调研员 用 /weekly-report 写，@路人 不算";
    expect(draftSegments(text, text.length, members, skills)).toEqual([
      { kind: "mention", name: "教育科技调研员", text: "@教育科技调研员" },
      { kind: "text", text: " 用 " },
      { kind: "skill", name: "weekly-report", text: "/weekly-report" },
      { kind: "text", text: " 写，@路人 不算" },
    ]);
  });

  it("leaves the reference under the caret as text until the user moves on", () => {
    expect(draftSegments("看 @甲", 4, members, skills)).toEqual([{ kind: "text", text: "看 @甲" }]);
    expect(draftSegments("看 @甲", 0, members, skills)).toEqual([
      { kind: "text", text: "看 " },
      { kind: "mention", name: "甲", text: "@甲" },
    ]);
    expect(draftSegments("/weekly-report", 14, members, skills)).toEqual([{ kind: "text", text: "/weekly-report" }]);
  });

  it("is one text run for a draft with no references", () => {
    expect(draftSegments("", 0, members, skills)).toEqual([]);
    expect(draftSegments("a/weekly-report 和 @甲乙", 0, members, skills)).toEqual([
      { kind: "text", text: "a/weekly-report 和 @甲乙" },
    ]);
  });
});

describe("capsuleDeletion", () => {
  // "看 @甲 x": the capsule spans 2–4, the picked space is at 4.
  const segments = draftSegments("看 @甲 x", null, members, skills);

  it("takes the whole capsule from right after it, or after the space a pick added", () => {
    expect(capsuleDeletion(segments, 4, "backward")).toEqual([2, 4]);
    expect(capsuleDeletion(segments, 5, "backward")).toEqual([2, 5]);
  });

  it("takes the capsule and its space on a forward delete from right before it", () => {
    expect(capsuleDeletion(segments, 2, "forward")).toEqual([2, 5]);
  });

  it("leaves ordinary characters to the browser", () => {
    expect(capsuleDeletion(segments, 6, "backward")).toBeNull();
    expect(capsuleDeletion(segments, 1, "backward")).toBeNull();
    expect(capsuleDeletion(segments, 4, "forward")).toBeNull();
    expect(capsuleDeletion(draftSegments("看 @甲x", null, members, skills), 5, "backward")).toBeNull();
  });
});
