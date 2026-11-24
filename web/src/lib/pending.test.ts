import { describe, expect, it } from "vitest";
import type { Action, Conversation, Message, Run } from "../api/types";
import { firstResponder, pendingReply } from "./pending";

let seq = 0;

function message(patch: Partial<Message>): Message {
  seq += 1;
  return {
    id: `m${seq}`,
    conversation_id: "c1",
    author_id: "boss",
    author_name: "主管",
    avatar_seed: null,
    kind: "agent",
    content: "回复",
    status: "complete",
    seq,
    created_at: "2026-10-21T10:00:00Z",
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: null,
    ...patch,
  };
}

function tool(status: Action["status"], author = "boss"): Message {
  return message({
    kind: "action",
    author_id: author,
    content: "查看当前时间",
    action: { id: `a${seq}`, run_id: "r1", tool_call_id: `t${seq}`, tool_name: "current_time", status, summary: "查看当前时间" } as Action,
  });
}

function run(patch: Partial<Run> = {}): Run {
  return {
    id: "r1",
    conversation_id: "c1",
    status: "running",
    current_step: "",
    error: null,
    model_calls: 0,
    tool_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    created_at: "2026-10-21T10:00:00Z",
    updated_at: "2026-10-21T10:00:00Z",
    finished_at: null,
    current_agent_id: null,
    pending_agent_ids: [],
    hops: 0,
    ...patch,
  };
}

function conversation(patch: Partial<Conversation> = {}): Conversation {
  return {
    id: "c1",
    kind: "direct",
    title: "设计助手",
    purpose: "",
    member_ids: ["designer"],
    last_message: null,
    created_at: "2026-10-21T10:00:00Z",
    updated_at: "2026-10-21T10:00:00Z",
    ...patch,
  };
}

const user = (patch: Partial<Message> = {}) => message({ kind: "user", author_id: null, author_name: "你", ...patch });
const group = conversation({ kind: "group", member_ids: ["boss", "designer", "coder"] });

describe("who is thinking", () => {
  it("names the member of a direct chat before the run reports anyone", () => {
    // The bug: a fresh run has no current agent yet, and the supervisor was shown instead.
    expect(firstResponder(run(), conversation(), user(), "boss")).toBe("designer");
  });

  it("names the first member the user mentioned in a group, else the supervisor", () => {
    expect(firstResponder(run(), group, user({ mentions: ["coder", "designer"] }), "boss")).toBe("coder");
    expect(firstResponder(run(), group, user(), "boss")).toBe("boss");
  });

  it("follows the run once it reports who is answering or queued", () => {
    expect(firstResponder(run({ current_agent_id: "coder" }), conversation(), user(), "boss")).toBe("coder");
    expect(firstResponder(run({ pending_agent_ids: ["coder"] }), conversation(), user(), "boss")).toBe("coder");
  });
});

describe("what the timeline shows while a run works", () => {
  it("names the answering bot once, right after the user sends", () => {
    expect(pendingReply(run(), conversation(), [message({}), user()], "boss")).toEqual({ agentID: "designer" });
  });

  it("adds nothing in the gaps of a reply already on screen: that bot's avatar is thinking", () => {
    // The screenshot: a finished reply, then another 正在思考 block by the same bot.
    const replied = [user(), message({ author_id: "designer" })];
    expect(pendingReply(run({ current_agent_id: "designer" }), conversation(), replied, "boss")).toBeNull();
    const afterTool = [user(), tool("completed", "designer")];
    expect(pendingReply(run({ current_agent_id: "designer" }), conversation(), afterTool, "boss")).toBeNull();
  });

  it("names a different bot once when it takes the turn in a relay", () => {
    const relay = [user({ mentions: ["coder"] }), message({ author_id: "coder" })];
    expect(pendingReply(run({ current_agent_id: "designer" }), group, relay, "boss")).toEqual({ agentID: "designer" });
  });

  it("stays out of the way while something shows its own progress", () => {
    expect(pendingReply(run(), conversation(), [user(), message({ status: "streaming" })], "boss")).toBeNull();
    expect(pendingReply(run(), conversation(), [user(), tool("running")], "boss")).toBeNull();
    expect(pendingReply(run(), conversation(), [user(), tool("awaiting")], "boss")).toBeNull();
  });

  it("never shows without a running run", () => {
    expect(pendingReply(undefined, conversation(), [user()], "boss")).toBeNull();
    expect(pendingReply(run({ status: "completed" }), conversation(), [user()], "boss")).toBeNull();
  });
});
