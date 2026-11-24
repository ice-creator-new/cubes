import { describe, expect, it } from "vitest";
import type { Message, SendMode } from "../api/types";
import {
  busyPlaceholder,
  queuedMessages,
  resolveSendMode,
  SEND_MODES,
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

describe("the mode a send carries", () => {
  it("follows the team setting when the bot has no pick of its own", () => {
    expect(resolveSendMode(null, "steer", false)).toBe("steer");
    expect(resolveSendMode(undefined, "queue", false)).toBe("queue");
    expect(resolveSendMode(null, "interrupt", false)).toBe("interrupt");
  });

  it("lets the bot's own pick win over the team setting", () => {
    expect(resolveSendMode("queue", "steer", false)).toBe("queue");
    expect(resolveSendMode("steer", "queue", false)).toBe("steer");
    expect(resolveSendMode("interrupt", "queue", false)).toBe("interrupt");
  });

  it("queues a steer to a CLI bot, which cannot take a message mid-turn", () => {
    expect(resolveSendMode("steer", "queue", true)).toBe("queue");
    expect(resolveSendMode(null, "steer", true)).toBe("queue");
  });

  it("queues a steer that carries images", () => {
    expect(resolveSendMode("steer", "steer", false, true)).toBe("queue");
    // The other modes are unaffected by images, which only matter to a steer.
    expect(resolveSendMode("queue", "steer", false, true)).toBe("queue");
    expect(resolveSendMode("interrupt", "steer", false, true)).toBe("interrupt");
  });
});

describe("the empty field's prompt", () => {
  it("names what a send would do", () => {
    expect(busyPlaceholder("steer")).toContain("这一轮");
    expect(busyPlaceholder("queue")).toContain("排队");
    expect(busyPlaceholder("interrupt")).toContain("打断");
  });
});

describe("the three settings", () => {
  it("offers every mode once", () => {
    expect(SEND_MODES.map((option) => option.id)).toEqual<SendMode[]>(["steer", "queue", "interrupt"]);
    for (const option of SEND_MODES) expect(option.hint.length).toBeGreaterThan(0);
  });
});

describe("queued sends in the transcript", () => {
  it("takes them out of the timeline and hands them to the strip", () => {
    const messages = [message("m1"), message("m2", { status: "queued" }), message("m3", { kind: "agent", status: "streaming" })];
    expect(withoutQueued(messages).map((item) => item.id)).toEqual(["m1", "m3"]);
    expect(queuedMessages(messages).map((item) => item.id)).toEqual(["m2"]);
  });
});
