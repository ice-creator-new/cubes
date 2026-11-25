import { describe, expect, it } from "vitest";
import type { Run } from "../api/types";
import { applyLiveRun, laneOf, leadRun, runsIn } from "./runs";

function run(patch: Partial<Run> = {}): Run {
  return {
    id: "r1",
    conversation_id: "c1",
    status: "running",
    lane: "work",
    current_step: "",
    error: null,
    model_calls: 0,
    tool_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    created_at: "2026-11-25T07:14:00Z",
    updated_at: "2026-11-25T07:14:00Z",
    finished_at: null,
    current_agent_id: "a1",
    pending_agent_ids: [],
    hops: 0,
    ...patch,
  };
}

describe("a conversation's live lanes", () => {
  it("keeps a working lane and a reply lane side by side", () => {
    const work = run({ id: "r1", lane: "work" });
    const reply = run({ id: "r2", lane: "reply", current_agent_id: "a2" });
    const lanes = applyLiveRun(applyLiveRun([], work), reply);
    expect(lanes.map((item) => item.id)).toEqual(["r1", "r2"]);
  });

  it("replaces a lane's own older row instead of adding it twice", () => {
    const first = applyLiveRun([], run({ id: "r1", current_step: "开始" }));
    const second = applyLiveRun(first, run({ id: "r1", current_step: "写文件" }));
    expect(second).toHaveLength(1);
    expect(second[0]?.current_step).toBe("写文件");
  });

  it("drops a lane that ended and leaves the other running", () => {
    const lanes = [run({ id: "r1", lane: "work" }), run({ id: "r2", lane: "reply" })];
    const after = applyLiveRun(lanes, run({ id: "r2", lane: "reply", status: "completed" }));
    expect(after.map((item) => item.id)).toEqual(["r1"]);
  });

  it("reads an event with no lane as ordinary work", () => {
    const legacy = { ...run({ id: "r3" }), lane: undefined } as unknown as Run;
    expect(laneOf(legacy)).toBe("work");
    expect(applyLiveRun([], legacy)[0]?.lane).toBe("work");
  });

  it("filters by conversation and follows the working lane for the step line", () => {
    const lanes = [
      run({ id: "r1", conversation_id: "c1", lane: "reply" }),
      run({ id: "r2", conversation_id: "c1", lane: "work" }),
      run({ id: "r3", conversation_id: "c2" }),
    ];
    expect(runsIn(lanes, "c1").map((item) => item.id)).toEqual(["r1", "r2"]);
    expect(leadRun(lanes, "c1")?.id).toBe("r2");
  });

  it("falls back to the reply lane when it is the only one live", () => {
    expect(leadRun([run({ id: "r1", lane: "reply" })], "c1")?.id).toBe("r1");
    expect(leadRun([], "c1")).toBeUndefined();
  });
});
