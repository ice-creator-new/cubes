import { describe, expect, it } from "vitest";
import { shortenPathSummary } from "./paths";

const ROOT = "/Users/sunflyice/Documents/AI-Native/cubes/cubes";

describe("shortenPathSummary", () => {
  it("keeps the file name of a long write path and drops the leading folders", () => {
    expect(shortenPathSummary(`写入 ${ROOT}/web/src/components/Sidebar.tsx`)).toBe(
      "写入 …/components/Sidebar.tsx",
    );
  });

  it("leaves a path short enough to fit, and ordinary text, untouched", () => {
    expect(shortenPathSummary("写入 /tmp/a.ts")).toBe("写入 /tmp/a.ts");
    expect(shortenPathSummary("搜索「侧边栏」")).toBe("搜索「侧边栏」");
  });

  it("keeps the punctuation after a path and the home-relative form", () => {
    expect(shortenPathSummary(`新建 ~/Documents/AI-Native/cubes/cubes/web/src/styles.css，共 26 行`)).toBe(
      "新建 …/src/styles.css，共 26 行",
    );
  });
});
