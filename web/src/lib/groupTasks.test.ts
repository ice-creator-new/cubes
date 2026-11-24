import { describe, expect, it } from "vitest";
import type { GroupTask } from "../api/types";
import { groupByRequest, workSummary } from "./groupTasks";

function task(
  id: string,
  request: string,
  created_at: string,
  status: GroupTask["status"] = "done",
  parent_id: string | null = null,
): GroupTask {
  return {
    id,
    conversation_id: "c",
    request_message_id: request,
    parent_id,
    owner_id: id,
    assigned_by: null,
    title: request,
    instruction: "",
    status,
    run_id: null,
    result_message_id: null,
    rounds: 1,
    created_at,
    updated_at: created_at,
    card_message_id: null,
    started_at: null,
    thread_id: null,
    steps: 0,
    current_step: "",
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

describe("group work by request", () => {
  // The server answers newest first, one row per member, parts mixed in.
  const rows = [
    task("part", "old", "2026-11-23T10:05:00Z", "running", "a"),
    task("c", "new", "2026-11-23T10:04:00Z", "stopped"),
    task("b", "old", "2026-11-23T10:02:00Z"),
    task("a", "old", "2026-11-23T10:01:00Z"),
  ];

  it("folds the members answering one request into one entry, newest request first", () => {
    const work = groupByRequest(rows);
    expect(work.map((item) => item.key)).toEqual(["new", "old"]);
    expect(work[1]?.owners.map((t) => t.id)).toEqual(["a", "b"]);
    expect(work[1]?.parts.map((t) => t.id)).toEqual(["part"]);
  });

  it("says a request is running while any part of it runs, and counts only open work", () => {
    const work = groupByRequest(rows);
    expect(work.map((item) => item.status)).toEqual(["stopped", "running"]);
    expect(workSummary(work)).toEqual({ running: 1, awaiting: 0, queued: 0, stopped: 1 });
  });

  it("counts a waiting part even while its request still runs", () => {
    const waiting = {
      ...task("w", "old", "2026-11-23T10:00:00Z", "running"),
      awaiting_action_id: "act_1",
    };
    const work = groupByRequest([waiting]);
    expect(workSummary(work)).toEqual({ running: 0, awaiting: 1, queued: 0, stopped: 0 });
  });
});
