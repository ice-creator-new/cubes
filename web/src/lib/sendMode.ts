// Sending while a reply runs: which mode reaches the server, and how the composer says what a
// send will do. The choice is a setting — the bot's own pick, else the team's — not something to
// pick at send time. A send never interrupts by default: it opens a lane of its own.

import type { Message, Run, RunLane, SendMode } from "../api/types";

export const SEND_MODES: { id: SendMode; label: string; hint: string }[] = [
  {
    id: "parallel",
    label: "并行（不打断）",
    hint: "新消息另开一路马上回答，正在做的事不停；回答会先读那一路的进度，不会重复也不会抢着做。",
  },
  {
    id: "queue",
    label: "排队",
    hint: "先排队，等这一轮结束后再作为新消息发送；发送前可以撤回。",
  },
  {
    id: "interrupt",
    label: "停下再发",
    hint: "停下正在写的回复，立刻回答这条新消息。",
  },
];

/** What the server is actually asked to do for a send that meets a running reply. */
export function resolveSendMode(agentMode: SendMode | null | undefined, teamMode: SendMode): SendMode {
  return agentMode ?? teamMode;
}

/** How a lane reads in a menu: the work it does, in the user's words. */
export function laneLabel(lane: RunLane): string {
  return lane === "reply" ? "回答" : "干活";
}

/** One entry of the stop menu: which lane stops, or every lane at once. */
export interface StopChoice {
  /** The run to stop; empty for the whole conversation. */
  id: string;
  label: string;
}

/**
 * The stop button's menu. One lane stops directly, so the menu is only built when there are
 * several; the last entry always stops them all.
 */
export function stopChoices(runs: Run[], nameOf: (run: Run) => string): StopChoice[] {
  const choices = runs.map((run) => ({ id: run.id, label: `停下 ${nameOf(run)}（${laneLabel(run.lane)}）` }));
  return runs.length > 1 ? [...choices, { id: "", label: "全部停下" }] : choices;
}

/** Sends still waiting for their turn: shown above the composer, never in the transcript. */
export function queuedMessages(messages: Message[]): Message[] {
  return messages.filter((message) => message.status === "queued");
}

/** The transcript without the queued sends, which the queued strip above the composer owns. */
export function withoutQueued(messages: Message[]): Message[] {
  return messages.filter((message) => message.status !== "queued");
}
