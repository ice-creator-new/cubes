import { describe, expect, it } from "vitest";
import { insertSlash, skillSourceHost, skillSourceLine, skillUploadContentType, slashQuery, slashTokens } from "./skills";
import type { Skill } from "../api/types";

describe("skillUploadContentType", () => {
  it("uploads a .md as a single SKILL.md", () => {
    expect(skillUploadContentType("SKILL.md")).toBe("text/markdown");
    expect(skillUploadContentType("readme.MD")).toBe("text/markdown");
  });

  it("uploads anything else as a zip", () => {
    expect(skillUploadContentType("skill.zip")).toBe("application/zip");
    expect(skillUploadContentType("folder")).toBe("application/zip");
  });
});

describe("skillSourceHost", () => {
  it("reads the host from a GitHub URL", () => {
    expect(skillSourceHost("https://github.com/owner/repo/tree/main/skill")).toBe("github.com");
  });

  it("falls back to GitHub for a null or unreadable URL", () => {
    expect(skillSourceHost(null)).toBe("GitHub");
    expect(skillSourceHost("not a url")).toBe("GitHub");
  });
});

describe("skillSourceLine", () => {
  const base: Skill = {
    id: "sk_1",
    name: "weekly-report",
    description: "",
    source: "user",
    source_url: null,
    author_id: null,
    author_name: null,
    enabled: true,
    has_scripts: false,
    file_count: 1,
    use_count: 0,
    last_used_at: null,
    created_at: "",
    updated_at: "",
  };

  it("credits the user, an author, an upload, a local import and a GitHub URL", () => {
    expect(skillSourceLine({ ...base, source: "user" })).toBe("自己写的");
    expect(skillSourceLine({ ...base, source: "agent", author_name: "阿飞" })).toBe("由「阿飞」写的");
    expect(skillSourceLine({ ...base, source: "agent", author_name: null })).toBe("机器人写的");
    expect(skillSourceLine({ ...base, source: "upload" })).toBe("上传的");
    expect(skillSourceLine({ ...base, source: "local" })).toBe("从本机导入的");
    expect(skillSourceLine({ ...base, source: "github", source_url: "https://github.com/a/b" })).toBe("来自 github.com");
  });
});

describe("slash skills", () => {
  it("opens only at the start or after whitespace, while the name is still being typed", () => {
    expect(slashQuery("/")).toEqual({ text: "", start: 0 });
    expect(slashQuery("帮我 /web-de")).toEqual({ text: "web-de", start: 3 });
    expect(slashQuery("a/b")).toBeNull();
    expect(slashQuery("看 https://x.com/a")).toBeNull();
    expect(slashQuery("/web 后面")).toBeNull();
    expect(slashQuery("/we 后面", 3)).toEqual({ text: "we", start: 0 });
  });

  it("replaces the open query and keeps the rest of the draft", () => {
    expect(insertSlash("请 /we 看看", "web-design", 5)).toEqual({ text: "请 /web-design  看看", caret: 14 });
  });

  it("finds sent names, ignoring unknown ones and paths", () => {
    const names = new Set(["weekly-report"]);
    expect(slashTokens("/weekly-report 写周报", names)).toEqual([{ name: "weekly-report", start: 0, end: 14 }]);
    expect(slashTokens("看 a/weekly-report 和 /other", names)).toEqual([]);
    expect(slashTokens("用 /weekly-report-x", names)).toEqual([]);
  });
});
