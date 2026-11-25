import { describe, expect, it } from "vitest";
import type { Message, Run, SendMode } from "../api/types";
import {
  laneLabel,
  queuedMessages,
  resolveSendMode,
  SEND_MODES,
  stopChoices,
  withoutQueued,
} from "./sendMode";

function message(id: string, patch: Partial<Message> = {}): Message {
  return {
    id,
    conversation_id: "c_1",
    author_id: null,
    author_name: "你",
    avatar_seed: null,
    kind: "user",
    content: id,
    status: "complete",
    seq: 1,
    created_at: "2026-11-20T00:00:00Z",
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: null,
    ...patch,
  };
}

function run(id: string, lane: Run["lane"], agent: string | null = "a1"): Run {
  return {
    id,
    conversation_id: "c_1",
    status: "running",
    lane,
    current_step: "",
    error: null,
    model_calls: 0,
    tool_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    created_at: "2026-11-20T00:00:00Z",
    updated_at: "2026-11-20T00:00:00Z",
    finished_at: null,
    current_agent_id: agent,
    pending_agent_ids: [],
    hops: 0,
  };
}

describe("the mode a send carries", () => {
  it("follows the team setting when the bot has no pick of its own", () => {
    expect(resolveSendMode(null, "parallel")).toBe("parallel");
    expect(resolveSendMode(undefined, "queue")).toBe("queue");
    expect(resolveSendMode(null, "interrupt")).toBe("interrupt");
  });

  it("lets the bot's own pick win over the team setting", () => {
    expect(resolveSendMode("queue", "parallel")).toBe("queue");
    expect(resolveSendMode("interrupt", "queue")).toBe("interrupt");
    expect(resolveSendMode("parallel", "interrupt")).toBe("parallel");
  });
});

describe("the three settings", () => {
  it("offers every mode once, parallel first", () => {
    expect(SEND_MODES.map((option) => option.id)).toEqual<SendMode[]>(["parallel", "queue", "interrupt"]);
    expect(SEND_MODES[0]?.label).toContain("并行");
    for (const option of SEND_MODES) expect(option.hint.length).toBeGreaterThan(0);
  });
});

describe("stopping one lane", () => {
  const nameOf = (item: Run) => (item.id === "r2" ? "主簿" : "匠师");

  it("stops the only lane directly, without a menu", () => {
    expect(stopChoices([run("r1", "work")], nameOf)).toEqual([{ id: "r1", label: "停下 匠师（干活）" }]);
  });

  it("names each lane when a bot works and answers at once, then all of them", () => {
    const choices = stopChoices([run("r1", "work"), run("r2", "reply")], nameOf);
    expect(choices).toEqual([
      { id: "r1", label: "停下 匠师（干活）" },
      { id: "r2", label: "停下 主簿（回答）" },
      { id: "", label: "全部停下" },
    ]);
  });

  it("reads the two lanes in the user's words", () => {
    expect(laneLabel("work")).toBe("干活");
    expect(laneLabel("reply")).toBe("回答");
  });
});

describe("queued sends in the transcript", () => {
  it("takes them out of the timeline and hands them to the strip", () => {
    const messages = [message("m1"), message("m2", { status: "queued" }), message("m3", { kind: "agent", status: "streaming" })];
    expect(withoutQueued(messages).map((item) => item.id)).toEqual(["m1", "m3"]);
    expect(queuedMessages(messages).map((item) => item.id)).toEqual(["m2"]);
  });
});
