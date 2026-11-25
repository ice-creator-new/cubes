// A conversation's live lanes, kept as a pure list so the rule can be tested without a store.
// One bot may hold a working lane and a reply lane at once; both stay live until they end.

import type { Run } from "../api/types";

/** The lane a run holds; an event from an older server that omits it is ordinary work. */
export function laneOf(run: Run): Run["lane"] {
  return run.lane === "reply" ? "reply" : "work";
}

/**
 * The live runs after one run's newest payload: a running lane joins (or replaces its own
 * older row, keeping the rest), a lane that ended leaves the list.
 */
export function applyLiveRun(active: Run[], incoming: Run): Run[] {
  const run = incoming.lane === "reply" ? incoming : { ...incoming, lane: "work" as const };
  const others = active.filter((item) => item.id !== run.id);
  return run.status === "running" ? [...others, run] : others;
}

/** Every lane of one conversation, in the order the store keeps them. */
export function runsIn(active: Run[], conversationID: string): Run[] {
  return active.filter((run) => run.conversation_id === conversationID);
}

/** The lane the chat's own step line follows: the working one when there is one. */
export function leadRun(active: Run[], conversationID: string): Run | undefined {
  const runs = runsIn(active, conversationID);
  return runs.find((run) => laneOf(run) !== "reply") ?? runs[0];
}
