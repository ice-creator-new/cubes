import { describe, expect, it } from "vitest";
import type { Memory, MemoryOrigin, MemoryScope } from "../api/types";
import {
  groupByAgent,
  memoriesByMessage,
  memoriesInScope,
  memorySpan,
  originLabel,
  retirementLabel,
  searchMemories,
  sortMemories,
  splitMemories,
} from "./memories";

let seq = 0;

function row(over: Partial<Memory> & { content: string }): Memory {
  seq += 1;
  const at = `2026-10-0${seq}T00:00:00+00:00`;
  const origin: MemoryOrigin = over.origin ?? "user";
  const scope: MemoryScope = over.scope ?? "user";
  return {
    id: over.id ?? `m${seq}`,
    scope,
    agent_id: over.agent_id ?? null,
    topic: over.topic ?? "",
    content: over.content,
    pinned: over.pinned ?? false,
    status: over.status ?? "active",
    superseded_by: over.superseded_by ?? null,
    origin,
    author_id: over.author_id ?? null,
    source_conversation_id: over.source_conversation_id ?? null,
    source_message_ids: over.source_message_ids ?? [],
    valid_from: over.valid_from ?? at,
    valid_to: over.valid_to ?? null,
    created_at: over.created_at ?? at,
    updated_at: over.updated_at ?? at,
  };
}

describe("active and retired", () => {
  it("splits the table into what still counts and what left", () => {
    const active = row({ content: "喜欢黑咖啡", id: "a" });
    const replaced = row({ content: "喝拿铁", id: "b", status: "retired", superseded_by: "a" });
    const forgotten = row({ content: "住在杭州", id: "c", status: "retired" });
    const { active: live, retired } = splitMemories([active, replaced, forgotten]);
    expect(live.map((m) => m.id)).toEqual(["a"]);
    expect(retired.map((m) => m.id)).toEqual(["b", "c"]);
  });

  it("says why each retired row left", () => {
    expect(retirementLabel(row({ content: "x", status: "retired", superseded_by: "y" }))).toBe("被新的替换");
    expect(retirementLabel(row({ content: "x", status: "retired" }))).toBe("已忘记");
  });
});

describe("the scope tabs", () => {
  it("keeps each row on its own tab", () => {
    const rows = [
      row({ content: "我不吃辣", id: "u", scope: "user" }),
      row({ content: "主管理查安全", id: "a", scope: "agent", agent_id: "bot-1" }),
      row({ content: "发货前先跑测试", id: "t", scope: "team" }),
    ];
    expect(memoriesInScope(rows, "user").map((m) => m.id)).toEqual(["u"]);
    expect(memoriesInScope(rows, "agent").map((m) => m.id)).toEqual(["a"]);
    expect(memoriesInScope(rows, "team").map((m) => m.id)).toEqual(["t"]);
  });
});

describe("search", () => {
  const rows = [
    row({ content: "喜欢黑咖啡", topic: "口味", id: "coffee" }),
    row({ content: "住在杭州", topic: "住址", id: "city" }),
  ];

  it("finds the words inside a memory", () => {
    expect(searchMemories(rows, "咖啡").map((m) => m.id)).toEqual(["coffee"]);
  });

  it("finds the topic too", () => {
    expect(searchMemories(rows, "住址").map((m) => m.id)).toEqual(["city"]);
  });

  it("ignores case and padding", () => {
    const latin = [row({ content: "Uses Bun", topic: "工具", id: "bun" })];
    expect(searchMemories(latin, "  bun ").map((m) => m.id)).toEqual(["bun"]);
  });

  it("keeps everything for an empty query", () => {
    expect(searchMemories(rows, "   ")).toHaveLength(2);
  });

  it("answers nothing for a word no row holds", () => {
    expect(searchMemories(rows, "咖啡豆")).toEqual([]);
  });
});

describe("order", () => {
  it("puts pinned rows first, then the newest", () => {
    const rows = [
      row({ content: "旧", id: "old", created_at: "2026-01-01T00:00:00+00:00" }),
      row({ content: "新", id: "new", created_at: "2026-05-01T00:00:00+00:00" }),
      row({ content: "钉住", id: "pin", pinned: true, created_at: "2025-01-01T00:00:00+00:00" }),
    ];
    expect(sortMemories(rows).map((m) => m.id)).toEqual(["pin", "new", "old"]);
  });

  it("leaves the input alone", () => {
    const rows = [
      row({ content: "旧", id: "old", created_at: "2026-01-01T00:00:00+00:00" }),
      row({ content: "新", id: "new", created_at: "2026-05-01T00:00:00+00:00" }),
    ];
    sortMemories(rows);
    expect(rows.map((m) => m.id)).toEqual(["old", "new"]);
  });

  it("keeps the given order when two rows share a moment", () => {
    const rows = [
      row({ content: "一", id: "one", created_at: "2026-03-01T00:00:00+00:00" }),
      row({ content: "二", id: "two", created_at: "2026-03-01T00:00:00+00:00" }),
    ];
    expect(sortMemories(rows).map((m) => m.id)).toEqual(["one", "two"]);
  });
});

describe("grouping by bot", () => {
  it("names each section from the directory and orders sections by name", () => {
    const rows = [
      row({ content: "a", id: "a1", scope: "agent", agent_id: "z" }),
      row({ content: "b", id: "b1", scope: "agent", agent_id: "a" }),
      row({ content: "c", id: "a2", scope: "agent", agent_id: "z" }),
    ];
    const groups = groupByAgent(rows, { z: "主管", a: "小艾" });
    expect(groups.map((group) => group.name)).toEqual(["小艾", "主管"]);
    expect(groups[1]!.memories.map((m) => m.id)).toEqual(["a1", "a2"]);
  });

  it("keeps a section for a bot that no longer exists", () => {
    const groups = groupByAgent([row({ content: "x", scope: "agent", agent_id: "gone" })], {});
    expect(groups).toHaveLength(1);
    expect(groups[0]!.name).toBe("已移除的成员");
    expect(groups[0]!.agentID).toBe("gone");
  });
});

describe("memories under a reply", () => {
  it("maps each active row onto every message it came from", () => {
    const rows = [
      row({ content: "一", id: "one", source_message_ids: ["msg-1", "msg-2"] }),
      row({ content: "二", id: "two", source_message_ids: ["msg-2"] }),
    ];
    const map = memoriesByMessage(rows);
    expect(map.get("msg-1")!.map((m) => m.id)).toEqual(["one"]);
    expect(map.get("msg-2")!.map((m) => m.id)).toEqual(["one", "two"]);
    expect(map.get("msg-3")).toBeUndefined();
  });

  it("leaves retired rows out", () => {
    const map = memoriesByMessage([
      row({ content: "旧", id: "old", status: "retired", superseded_by: "new", source_message_ids: ["msg-1"] }),
    ]);
    expect(map.size).toBe(0);
  });
});

describe("how a row reads", () => {
  it("names who wrote it", () => {
    expect(originLabel(row({ content: "x", origin: "user" }), null)).toBe("你添加的");
    expect(originLabel(row({ content: "x", origin: "agent", author_id: "bot" }), "主管")).toBe("「主管」记下的");
    expect(originLabel(row({ content: "x", origin: "extract", author_id: "bot" }), "主管")).toBe("「主管」整理的");
    expect(originLabel(row({ content: "x", origin: "agent" }), undefined)).toBe("「机器人」记下的");
  });

  it("gives the window a row counted in", () => {
    const memory = row({
      content: "x",
      valid_from: "2026-03-02T02:00:00+00:00",
      valid_to: "2026-03-09T02:00:00+00:00",
    });
    expect(memorySpan(memory, new Date("2026-10-23T02:00:00Z"))).toBe("3/2 – 3/9");
  });
});
