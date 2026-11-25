import { describe, expect, it } from "vitest";
import type { Message } from "../api/types";
import { outOfOrderQuote } from "./quotes";

let seq = 0;

function message(patch: Partial<Message>): Message {
  seq += 1;
  return {
    id: `m${seq}`,
    conversation_id: "c1",
    author_id: "a1",
    author_name: "小北",
    avatar_seed: null,
    kind: "agent",
    content: "",
    status: "complete",
    seq,
    created_at: "2026-11-20T10:00:00Z",
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: null,
    ...patch,
  };
}

describe("outOfOrderQuote", () => {
  it("stays quiet when the answer follows its question directly", () => {
    const question = message({ kind: "user", content: "帮我看下登录 bug" });
    const answer = message({ content: "看过了，是缓存没清。" });
    expect(outOfOrderQuote([question, answer], 1, question.id)).toBeNull();
  });

  it("names the question when the user spoke again before the answer landed", () => {
    const first = message({ kind: "user", content: "帮我看下登录 bug，**比较急**" });
    const second = message({ kind: "user", content: "顺便看下注册" });
    const answer = message({ content: "登录那边是缓存没清。" });
    expect(outOfOrderQuote([first, second, answer], 2, first.id)).toEqual({
      id: first.id,
      label: "你",
      excerpt: "帮我看下登录 bug，比较急",
    });
  });

  it("counts only user messages, not bot chatter, as a newer question", () => {
    const question = message({ kind: "user", content: "现在进度如何？" });
    const note = message({ content: "我还在跑测试。" });
    const answer = message({ content: "数据收好了，正在写结论。" });
    expect(outOfOrderQuote([question, note, answer], 2, question.id)).toBeNull();
  });

  it("does not count a reminder row as the user speaking again", () => {
    const question = message({ kind: "user", content: "帮我看下登录 bug" });
    const reminder = message({ kind: "user", content: "提醒：看登录 bug", reminder: true });
    const answer = message({ content: "看过了，是缓存没清。" });
    expect(outOfOrderQuote([question, reminder, answer], 2, question.id)).toBeNull();
  });

  it("never quotes a bot message, and needs the question on this page", () => {
    const question = message({ kind: "user", content: "在吗" });
    const other = message({ kind: "user", content: "还在吗" });
    const answer = message({ content: "在。" });
    expect(outOfOrderQuote([question, other, answer], 2, "gone")).toBeNull();
    expect(outOfOrderQuote([question, message({ kind: "agent", content: "嗯" }), answer], 2, question.id)).toBeNull();
  });

  it("quotes an image-only question by name instead of an empty line", () => {
    const question = message({
      kind: "user",
      content: "",
      attachments: [{ id: "a1", mime: "image/png", size: 12, name: "" }],
    });
    const other = message({ kind: "user", content: "顺便问一句" });
    const answer = message({ content: "图里是报错栈。" });
    expect(outOfOrderQuote([question, other, answer], 2, question.id)?.excerpt).toBe("（图片）");
  });
});
