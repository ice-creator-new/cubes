import { describe, expect, it } from "vitest";
import type { Message } from "../api/types";
import { acceptsLiveMessage, mergeNewestPage, prependOlderPage, type Loaded } from "./pages";

function message(seq: number, patch: Partial<Message> = {}): Message {
  return {
    id: `m${seq}`,
    conversation_id: "c1",
    author_id: "boss",
    author_name: "主管",
    avatar_seed: null,
    kind: "agent",
    content: `第${seq}条`,
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

function loaded(seqs: number[], has_more: boolean, references: Message[] = []): Loaded {
  return { messages: seqs.map((seq) => message(seq)), has_more, references };
}

function page(seqs: number[], has_more: boolean, references: Message[] = []) {
  return { messages: seqs.map((seq) => message(seq)), has_more, references };
}

const ids = (messages: Message[]) => messages.map((item) => item.seq);

describe("mergeNewestPage", () => {
  it("takes the newest page as the window when nothing was scrolled back to", () => {
    const merged = mergeNewestPage(null, page([81, 82, 83], true));
    expect(ids(merged.messages)).toEqual([81, 82, 83]);
    expect(merged.has_more).toBe(true);
  });

  it("keeps rows older than the page and their has_more after a refresh", () => {
    const merged = mergeNewestPage(loaded([1, 2, 3, 81, 82], true), page([81, 82, 83], true));
    expect(ids(merged.messages)).toEqual([1, 2, 3, 81, 82, 83]);
    expect(merged.has_more).toBe(true);
  });

  it("replaces the window when every loaded row is inside the new page", () => {
    const merged = mergeNewestPage(loaded([79, 80], true), page([79, 80, 81], false));
    expect(ids(merged.messages)).toEqual([79, 80, 81]);
    expect(merged.has_more).toBe(false);
  });

  it("keeps the row that sits just above the page", () => {
    const merged = mergeNewestPage(loaded([78, 79, 80], true), page([79, 80, 81], true));
    expect(ids(merged.messages)).toEqual([78, 79, 80, 81]);
    expect(merged.has_more).toBe(true);
  });

  it("keeps the loaded window when the server answers an empty page", () => {
    const current = loaded([4, 5], true);
    expect(mergeNewestPage(current, page([], false))).toBe(current);
  });

  it("keeps a quoted parent that neither page carries as a row", () => {
    const parent = message(40);
    const merged = mergeNewestPage(
      { messages: [message(81)], has_more: true, references: [parent] },
      page([82], true, [message(70)]),
    );
    expect(ids(merged.references)).toEqual([40, 70]);
  });

  it("drops a reference once its row is on screen", () => {
    const parent = message(40);
    const merged = mergeNewestPage(
      { messages: [parent], has_more: false, references: [] },
      page([40, 41], false, [message(40)]),
    );
    expect(merged.references).toEqual([]);
  });
});

describe("prependOlderPage", () => {
  it("puts the older page in front and takes its has_more", () => {
    const merged = prependOlderPage(loaded([81, 82], true), page([79, 80], true));
    expect(ids(merged.messages)).toEqual([79, 80, 81, 82]);
    expect(merged.has_more).toBe(true);
    const start = prependOlderPage(loaded([79, 80], true), page([77, 78], false));
    expect(ids(start.messages)).toEqual([77, 78, 79, 80]);
    expect(start.has_more).toBe(false);
  });

  it("never repeats a row that pages overlap on", () => {
    const merged = prependOlderPage(loaded([80, 81], true), page([79, 80], true));
    expect(ids(merged.messages)).toEqual([79, 80, 81]);
  });

  it("carries both pages' quoted parents", () => {
    const merged = prependOlderPage(
      { messages: [message(80)], has_more: true, references: [message(60)] },
      page([78], true, [message(20)]),
    );
    expect(ids(merged.references)).toEqual([20, 60]);
  });
});

describe("acceptsLiveMessage", () => {
  const window = loaded([40, 41, 42], true);

  it("accepts a message at or past the oldest loaded row", () => {
    expect(acceptsLiveMessage(window, message(43))).toBe(true);
    expect(acceptsLiveMessage(window, message(41))).toBe(true);
  });

  it("ignores one older than the window while older rows remain", () => {
    expect(acceptsLiveMessage(window, message(39))).toBe(false);
  });

  it("accepts an older one once the whole history is loaded", () => {
    expect(acceptsLiveMessage(loaded([1, 2, 3], false), message(0))).toBe(true);
  });

  it("accepts a row already in the window", () => {
    expect(acceptsLiveMessage(window, message(41, { content: "改过" }))).toBe(true);
  });

  it("accepts anything into an empty window", () => {
    expect(acceptsLiveMessage(loaded([], true), message(9))).toBe(true);
  });
});
