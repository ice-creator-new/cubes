import { describe, expect, it } from "vitest";
import type { Action, Message, Run, RunTrace, RunTraceEdge, RunTraceNode } from "../api/types";
import { activeNodeOf, buildSteps, edgeOrder, stepDuration, type RunStep } from "./runReplay";

let seq = 0;
function msg(over: Partial<Message> & { id: string; created_at: string }): Message {
  seq += 1;
  return {
    conversation_id: "c1",
    author_id: null,
    author_name: "",
    avatar_seed: null,
    kind: "agent",
    content: "",
    status: "complete",
    seq,
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: "r1",
    ...over,
  };
}

function action(over: Partial<Action>): Action {
  return {
    id: "a1",
    run_id: "r1",
    tool_call_id: "tc1",
    tool_name: "ask_agent",
    status: "completed",
    summary: "咨询成员",
    target_agent_id: null,
    target_conversation_id: null,
    error: null,
    ...over,
  };
}

function runTrace(over: Partial<RunTrace>): RunTrace {
  const run: Run = {
    id: "r1",
    conversation_id: "c1",
    status: "completed",
    current_step: "",
    error: null,
    model_calls: 5,
    tool_calls: 3,
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    created_at: "2026-10-28T10:00:00.000Z",
    updated_at: "2026-10-28T10:00:30.000Z",
    finished_at: "2026-10-28T10:00:30.000Z",
    current_agent_id: null,
    pending_agent_ids: [],
    hops: 3,
  };
  return {
    run,
    conversation_id: "c1",
    nodes: [],
    edges: [],
    messages: [],
    ...over,
  };
}

function node(id: string, name: string, kind: RunTraceNode["kind"] = "member"): RunTraceNode {
  return { id, name, kind };
}

function edge(from: string, to: string, kind: RunTraceEdge["kind"], at: string, message_id: string | null): RunTraceEdge {
  return { from, to, kind, at, message_id, conversation_id: "c1", label: "" };
}

describe("buildSteps", () => {
  it("replays a planned group talk: each speaker answers the user along its own ask edge", () => {
    const pm = node("pm", "产品经理");
    const eng = node("eng", "工程专家");
    const trace = runTrace({
      nodes: [node("user", "你", "user"), node("super", "主管", "supervisor"), pm, eng],
      edges: [
        edge("user", "pm", "ask", "2026-10-28T10:00:00.000Z", "m0"),
        edge("user", "eng", "ask", "2026-10-28T10:00:00.000Z", "m0"),
        edge("user", "super", "ask", "2026-10-28T10:00:00.000Z", "m0"),
      ],
      messages: [
        msg({ id: "m0", created_at: "2026-10-28T10:00:00.000Z", kind: "user", content: "大家怎么看", author_id: null }),
        msg({ id: "m1", created_at: "2026-10-28T10:00:03.000Z", author_id: "pm", content: "产品看法", reply_to_id: "m0" }),
        msg({ id: "m2", created_at: "2026-10-28T10:00:06.000Z", author_id: "eng", content: "同意，补充一点", reply_to_id: "m0" }),
        msg({ id: "m3", created_at: "2026-10-28T10:00:09.000Z", author_id: "super", content: "那就先试", reply_to_id: "m0" }),
      ],
    });
    const steps = buildSteps(trace);
    expect(steps.map((s) => [s.kind, s.edgeIndex])).toEqual([
      ["ask", 0],
      ["reply", 0],
      ["reply", 1],
      ["reply", 2],
    ]);
  });

  it("turns the group-run example into ordered ask/consult/reply steps", () => {
    const pm = node("pm", "产品经理");
    const eng = node("eng", "工程专家");
    const code = node("code", "代码助手");
    const trace = runTrace({
      nodes: [node("user", "你", "user"), node("super", "主管", "supervisor"), pm, eng, code],
      edges: [
        edge("user", "super", "ask", "2026-10-28T10:00:00.000Z", "m0"),
        edge("super", "pm", "consult", "2026-10-28T10:00:02.000Z", "m1"),
        edge("super", "eng", "consult", "2026-10-28T10:00:08.000Z", "m3"),
        edge("super", "code", "consult", "2026-10-28T10:00:14.000Z", "m5"),
      ],
      messages: [
        msg({ id: "m0", created_at: "2026-10-28T10:00:00.000Z", kind: "user", content: "出个方案", author_id: null }),
        msg({
          id: "m1",
          created_at: "2026-10-28T10:00:02.000Z",
          kind: "action",
          author_id: "super",
          action: action({ id: "a1", tool_call_id: "tc1", tool_name: "ask_agent", summary: "咨询产品经理", target_agent_id: "pm" }),
        }),
        msg({ id: "m2", created_at: "2026-10-28T10:00:05.000Z", kind: "agent", author_id: "pm", content: "产品的意见" }),
        msg({
          id: "m3",
          created_at: "2026-10-28T10:00:08.000Z",
          kind: "action",
          author_id: "super",
          action: action({ id: "a3", tool_call_id: "tc3", tool_name: "ask_agent", summary: "咨询工程专家", target_agent_id: "eng" }),
        }),
        msg({ id: "m4", created_at: "2026-10-28T10:00:11.000Z", kind: "agent", author_id: "eng", content: "工程的意见" }),
        msg({
          id: "m5",
          created_at: "2026-10-28T10:00:14.000Z",
          kind: "action",
          author_id: "super",
          action: action({ id: "a5", tool_call_id: "tc5", tool_name: "ask_agent", summary: "咨询代码助手", target_agent_id: "code" }),
        }),
        msg({ id: "m6", created_at: "2026-10-28T10:00:17.000Z", kind: "agent", author_id: "code", content: "代码的意见" }),
        msg({ id: "m7", created_at: "2026-10-28T10:00:25.000Z", kind: "agent", author_id: "super", content: "汇总完成" }),
      ],
    });

    const steps = buildSteps(trace);
    expect(steps.map((s) => s.kind)).toEqual([
      "ask",
      "consult",
      "reply",
      "consult",
      "reply",
      "consult",
      "reply",
      "reply",
    ]);
    expect(steps[0]!.caption).toBe("你 发起任务");
    expect(steps[1]!.caption).toBe("主管 咨询 产品经理");
    expect(steps[2]!.caption).toBe("产品经理 回复");
    expect(steps[7]!.caption).toBe("主管 回复");

    // Action rows travel their own edge by message_id.
    expect(steps[1]!.edgeIndex).toBe(1);
    expect(steps[3]!.edgeIndex).toBe(2);
    expect(steps[5]!.edgeIndex).toBe(3);
    // Replies map to the edge that led to their author.
    expect(steps[2]!.edgeIndex).toBe(1);
    expect(steps[4]!.edgeIndex).toBe(2);
    expect(steps[6]!.edgeIndex).toBe(3);
    // The final supervisor reply maps back to the ask edge (user → supervisor).
    expect(steps[7]!.edgeIndex).toBe(0);

    expect(steps[2]!.offsetMs).toBe(5000);
    expect(steps[7]!.offsetMs).toBe(25000);
  });

  it("labels a reply as a mention when an @ hand-off led to it", () => {
    const trace = runTrace({
      nodes: [node("user", "你", "user"), node("a", "甲"), node("b", "乙")],
      edges: [
        edge("user", "a", "ask", "2026-10-28T10:00:00.000Z", "m0"),
        edge("a", "b", "mention", "2026-10-28T10:00:05.000Z", "m1"),
      ],
      messages: [
        msg({ id: "m0", created_at: "2026-10-28T10:00:00.000Z", kind: "user", content: "问甲" }),
        msg({ id: "m1", created_at: "2026-10-28T10:00:05.000Z", kind: "agent", author_id: "a", content: "方案，@乙 评估" }),
        msg({ id: "m2", created_at: "2026-10-28T10:00:10.000Z", kind: "agent", author_id: "b", content: "风险可控" }),
      ],
    });
    const steps = buildSteps(trace);
    expect(steps[1]!.kind).toBe("reply"); // 甲's reply still maps to the ask edge.
    expect(steps[1]!.edgeIndex).toBe(0);
    expect(steps[2]!.kind).toBe("mention"); // 乙's reply was pulled in by the @ edge.
    expect(steps[2]!.edgeIndex).toBe(1);
  });

  it("keeps a plain tool row as a tool step with no edge", () => {
    const trace = runTrace({
      nodes: [node("user", "你", "user"), node("super", "主管", "supervisor")],
      edges: [edge("user", "super", "ask", "2026-10-28T10:00:00.000Z", "m0")],
      messages: [
        msg({ id: "m0", created_at: "2026-10-28T10:00:00.000Z", kind: "user", content: "查一下" }),
        msg({
          id: "m1",
          created_at: "2026-10-28T10:00:02.000Z",
          kind: "action",
          author_id: "super",
          action: action({ id: "a1", tool_call_id: "tc1", tool_name: "web_search", summary: "找到了", target_agent_id: null }),
        }),
      ],
    });
    const steps = buildSteps(trace);
    expect(steps[1]!.kind).toBe("tool");
    expect(steps[1]!.edgeIndex).toBe(null);
    expect(steps[1]!.caption).toBe("主管 · 搜索网页");
  });
});

describe("edgeOrder", () => {
  it("numbers edges by the first step that travels them", () => {
    const trace = runTrace({
      nodes: [node("user", "你", "user"), node("super", "主管", "supervisor"), node("pm", "产品经理"), node("eng", "工程专家")],
      edges: [
        edge("user", "super", "ask", "2026-10-28T10:00:00.000Z", "m0"),
        edge("super", "pm", "consult", "2026-10-28T10:00:02.000Z", "m1"),
        edge("super", "eng", "consult", "2026-10-28T10:00:06.000Z", "m3"),
      ],
      messages: [
        msg({ id: "m0", created_at: "2026-10-28T10:00:00.000Z", kind: "user", content: "出方案" }),
        msg({
          id: "m1",
          created_at: "2026-10-28T10:00:02.000Z",
          kind: "action",
          author_id: "super",
          action: action({ id: "a1", tool_call_id: "tc1", tool_name: "ask_agent", summary: "咨询", target_agent_id: "pm" }),
        }),
        msg({ id: "m2", created_at: "2026-10-28T10:00:04.000Z", kind: "agent", author_id: "pm", content: "好" }),
        msg({
          id: "m3",
          created_at: "2026-10-28T10:00:06.000Z",
          kind: "action",
          author_id: "super",
          action: action({ id: "a2", tool_call_id: "tc2", tool_name: "ask_agent", summary: "咨询", target_agent_id: "eng" }),
        }),
        msg({ id: "m4", created_at: "2026-10-28T10:00:08.000Z", kind: "agent", author_id: "eng", content: "行" }),
      ],
    });
    expect(edgeOrder(trace)).toEqual([1, 2, 3]);
  });
});

describe("stepDuration", () => {
  it("gives replies time by their length, capped; hand-offs and tools a fixed beat", () => {
    expect(stepDuration({ kind: "reply" })).toBe(1400);
    expect(stepDuration({ kind: "reply" }, 40)).toBeGreaterThan(stepDuration({ kind: "reply" }, 10));
    expect(stepDuration({ kind: "mention" }, 5000)).toBe(stepDuration({ kind: "reply" }, 90));
    expect(stepDuration({ kind: "reply" }, 5000)).toBeLessThanOrEqual(4200);
    for (const kind of ["ask", "consult", "tool", "dm", "revise"] as const) {
      expect(stepDuration({ kind }, 500)).toBe(stepDuration({ kind }));
    }
  });
});

describe("activeNodeOf", () => {
  const step = (over: Partial<RunStep> & { kind: RunStep["kind"] }): RunStep => ({
    index: 0,
    messageID: "m",
    actorID: null,
    targetID: null,
    edgeIndex: null,
    caption: "",
    offsetMs: 0,
    ...over,
  });

  it("pulses the target of a hand-off and the speaker of a reply", () => {
    expect(activeNodeOf(step({ kind: "consult", actorID: "super", targetID: "pm" }))).toBe("pm");
    expect(activeNodeOf(step({ kind: "ask", actorID: null, targetID: "super" }))).toBe("super");
    expect(activeNodeOf(step({ kind: "reply", actorID: "pm", targetID: null }))).toBe("pm");
    expect(activeNodeOf(step({ kind: "tool", actorID: "super", targetID: null }))).toBe("super");
  });
});
