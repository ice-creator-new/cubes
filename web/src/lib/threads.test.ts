import { describe, expect, it } from "vitest";
import type { Conversation, GroupTask } from "../api/types";
import { findThreadOrigin, inSidebar, isScheduleTarget, isThread, taskByID, threadHeading, threadTabTitle } from "./threads";

function conversation(id: string, kind: Conversation["kind"]): Conversation {
  return {
    id,
    kind,
    title: id,
    purpose: "",
    member_ids: [],
    last_message: null,
    created_at: "2026-11-23T10:00:00Z",
    updated_at: "2026-11-23T10:00:00Z",
  };
}

function task(id: string, thread_id: string | null, conversation_id = "g1"): GroupTask {
  return {
    id,
    conversation_id,
    request_message_id: "req",
    parent_id: null,
    owner_id: "a1",
    assigned_by: null,
    title: "修复登录",
    instruction: "",
    status: "running",
    run_id: null,
    result_message_id: null,
    rounds: 1,
    created_at: "2026-11-23T10:00:00Z",
    updated_at: "2026-11-23T10:00:00Z",
    card_message_id: null,
    started_at: null,
    thread_id,
    steps: 2,
    current_step: "跑测试",
    latest_text: "",
    recent_steps: [],
    awaiting_action_id: null,
    waiting_for: null,
    end_reason: null,
    stopped_by: null,
    dismissed_at: null,
    seen_at: null,
  };
}

describe("thread conversations", () => {
  it("keeps only chats you talk in on the sidebar", () => {
    expect(inSidebar(conversation("s", "direct"))).toBe(true);
    expect(inSidebar(conversation("g", "group"))).toBe(true);
    expect(inSidebar(conversation("t", "thread"))).toBe(false);
    expect(inSidebar(conversation("p", "private"))).toBe(false);
    expect(inSidebar(conversation("w", "workflow"))).toBe(false);
  });

  it("never offers a thread or a private chat as a scheduled target", () => {
    expect(isScheduleTarget(conversation("s", "direct"))).toBe(true);
    expect(isScheduleTarget(conversation("g", "group"))).toBe(true);
    expect(isScheduleTarget(conversation("t", "thread"))).toBe(false);
    expect(isScheduleTarget(conversation("p", "private"))).toBe(false);
    expect(isScheduleTarget(conversation("w", "workflow"))).toBe(false);
    expect(isThread(conversation("t", "thread"))).toBe(true);
    expect(isThread(conversation("g", "group"))).toBe(false);
  });

  it("finds the task a card draws and the group a thread belongs to", () => {
    const tasks = { g1: [task("t1", "th1"), task("t2", null)], g2: [task("t3", "th3", "g2")] };
    expect(taskByID(tasks.g1, "t2")?.id).toBe("t2");
    expect(taskByID(tasks.g1, null)).toBeUndefined();
    expect(findThreadOrigin(tasks, "th3")).toMatchObject({ conversationID: "g2", task: { id: "t3" } });
    expect(findThreadOrigin(tasks, "missing")).toBeNull();
  });

  it("names a thread by its member and its task", () => {
    expect(threadTabTitle("小北", "修复登录")).toBe("小北 · 修复登录");
    expect(threadTabTitle(undefined, "修复登录")).toBe("成员 · 修复登录");
    expect(threadHeading("修复登录")).toBe("任务 · 修复登录");
  });
});
