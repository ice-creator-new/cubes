import { describe, expect, it } from "vitest";
import { displayPath, followModeLabel } from "./permissions";

describe("displayPath", () => {
  it("shortens the home folder itself to ~", () => {
    expect(displayPath("/Users/anna", "/Users/anna")).toBe("~");
    expect(displayPath("/Users/anna/", "/Users/anna/")).toBe("~");
  });

  it("shortens anything under home", () => {
    expect(displayPath("/Users/anna/Downloads", "/Users/anna")).toBe("~/Downloads");
    expect(displayPath("/Users/anna/Downloads/", "/Users/anna")).toBe("~/Downloads");
    expect(displayPath("/Users/anna/a/b/c", "/Users/anna/")).toBe("~/a/b/c");
  });

  it("leaves paths outside home alone", () => {
    expect(displayPath("/tmp/cubes", "/Users/anna")).toBe("/tmp/cubes");
    expect(displayPath("/Volumes/Data", "/Users/anna")).toBe("/Volumes/Data");
  });

  it("does not shorten a sibling that only shares the name prefix", () => {
    expect(displayPath("/Users/anna2/Downloads", "/Users/anna")).toBe("/Users/anna2/Downloads");
    expect(displayPath("/Users/annabelle", "/Users/anna")).toBe("/Users/annabelle");
  });

  it("keeps the path as-is when home is unknown", () => {
    expect(displayPath("/Users/anna/Downloads", "")).toBe("/Users/anna/Downloads");
    expect(displayPath("/Users/anna/Downloads", "   ")).toBe("/Users/anna/Downloads");
  });
});

describe("followModeLabel", () => {
  it("names the workspace's current mode", () => {
    expect(followModeLabel("auto")).toBe("跟随团队（现在是 自动审批）");
    expect(followModeLabel(null)).toBe("跟随团队（现在是 手动审批）");
  });
});
