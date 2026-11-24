import { describe, expect, it } from "vitest";
import type { Message } from "../api/types";
import { messagePreview } from "./preview";

function message(patch: Partial<Message>): Message {
  return {
    id: "m1",
    conversation_id: "c1",
    author_id: "a1",
    author_name: "小北",
    avatar_seed: null,
    kind: "agent",
    content: "",
    status: "complete",
    seq: 1,
    created_at: "2026-10-20T10:00:00Z",
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: null,
    ...patch,
  };
}

describe("messagePreview", () => {
  it("collapses whitespace in ordinary replies", () => {
    expect(messagePreview(message({ content: "  研究\n完成 了 " }))).toBe("研究 完成 了");
  });

  it("ignores relay markers even when an old row still carries text", () => {
    const relay = { conversation_id: "p1", direction: "in" as const, peer_id: "a2", peer_name: "小南" };
    expect(messagePreview(message({ relay }))).toBeNull();
    expect(messagePreview(message({ relay, content: "旧版本存下的转述" }))).toBeNull();
  });

  it("keeps the bot's own summary visible", () => {
    const relay = { conversation_id: "p1", direction: "summary" as const, peer_id: "a2", peer_name: "小南" };
    expect(messagePreview(message({ relay, content: "和小南定了方案。" }))).toBe("和小南定了方案。");
  });

  it("shows an action's summary", () => {
    const action = {
      id: "ac1",
      run_id: "r1",
      tool_call_id: "t1",
      tool_name: "delete_agent",
      status: "awaiting" as const,
      summary: "删除成员「小南」",
      target_agent_id: "a2",
      target_conversation_id: null,
      error: null,
      confirmation: { prompt: "确认吗？", confirm_label: "删除", deny_label: "保留" },
    };
    expect(messagePreview(message({ kind: "action", content: "确认吗？", action }))).toBe("删除成员「小南」");
  });
});
