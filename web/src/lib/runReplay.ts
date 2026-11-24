// Pure ordering of a run's messages into replay steps, and how each step links back to the
// hand-off graph. No DOM and no React: the sheet turns these steps into cards and the graph
// into an SVG, and this module decides which edge or node belongs to which step.

import type { RunTrace } from "../api/types";
import { TOOL_LABEL } from "./toolLabels";

/** What a step is, in the user's terms. `mention` is a reply pulled in by an @ hand-off. */
export type StepKind = "ask" | "consult" | "dm" | "revise" | "reply" | "tool" | "mention";

/** One replay step: a message, how it moves the graph, and how long it should take to play. */
export interface RunStep {
  /** 0-based position in the step order. */
  index: number;
  messageID: string;
  kind: StepKind;
  /** Who acts in this step; null for the user's own trigger. */
  actorID: string | null;
  /** Who the hand-off points at; null for replies and plain tools. */
  targetID: string | null;
  /** Index into `trace.edges`, or null for a step that does not travel an edge. */
  edgeIndex: number | null;
  /** The one-line caption for the replay, e.g. 「主管 咨询 工程专家」. */
  caption: string;
  /** Milliseconds after the run started. */
  offsetMs: number;
}

/**
 * Which edge a step's message belongs to. Action rows carry the edge's own `message_id`;
 * the user trigger and agent replies fall back to the geometry of the graph.
 */
export function buildSteps(trace: RunTrace): RunStep[] {
  const { run, nodes, edges, messages } = trace;
  const names = new Map(nodes.map((node) => [node.id, node.name]));
  const start = Date.parse(run.created_at);
  const steps: RunStep[] = [];

  // The ask edge (user → first responder) for the trigger. A group whose planning let several
  // members speak has one ask edge per speaker: each of their replies travels its own.
  const askEdge = edges.find((edge) => edge.kind === "ask");
  const askIndex = askEdge ? edges.indexOf(askEdge) : -1;
  const trigger = messages.find((message) => message.kind === "user");
  const askTo = new Map<string, number>();
  edges.forEach((edge, index) => {
    if (edge.kind === "ask" && !askTo.has(edge.to)) askTo.set(edge.to, index);
  });

  /** The latest edge that leads to `author` at or before this message's time. */
  const leadingEdge = (author: string, at: string): number => {
    let found = -1;
    for (let i = 0; i < edges.length; i += 1) {
      const edge = edges[i];
      if (!edge) continue;
      if (edge.to === author && edge.at <= at) found = i;
    }
    return found;
  };

  for (const message of messages) {
    const offsetMs = Number.isFinite(start) ? Math.max(0, Date.parse(message.created_at) - start) : 0;
    const actor = message.author_id;
    const actorName = actor ? (names.get(actor) ?? "成员") : "你";

    if (message.kind === "user") {
      const target = askEdge?.to ?? null;
      steps.push({
        index: steps.length,
        messageID: message.id,
        kind: "ask",
        actorID: null,
        targetID: target,
        edgeIndex: askIndex >= 0 ? askIndex : null,
        caption: "你 发起任务",
        offsetMs,
      });
      continue;
    }

    const action = message.action;
    if (message.kind === "action" && action && action.target_agent_id) {
      // A hand-off action row: consult / dm / revise. It is the edge by construction.
      let kind: StepKind | null = null;
      let label = "工具";
      if (action.tool_name === "ask_agent") {
        kind = "consult";
        label = "咨询";
      } else if (action.tool_name === "direct_message") {
        kind = "dm";
        label = "私信";
      } else if (action.tool_name === "review") {
        kind = "revise";
        label = "打回";
      }
      const byID = edges.findIndex((edge) => edge.message_id === message.id);
      const byShape = edges.findIndex((edge) => edge.from === actor && edge.to === action.target_agent_id);
      const edgeIndex = byID >= 0 ? byID : byShape >= 0 ? byShape : -1;
      const targetName = names.get(action.target_agent_id) ?? "成员";
      if (kind) {
        steps.push({
          index: steps.length,
          messageID: message.id,
          kind,
          actorID: actor,
          targetID: action.target_agent_id,
          edgeIndex: edgeIndex >= 0 ? edgeIndex : null,
          caption: `${actorName} ${label} ${targetName}`,
          offsetMs,
        });
      } else {
        steps.push({
          index: steps.length,
          messageID: message.id,
          kind: "tool",
          actorID: actor,
          targetID: null,
          edgeIndex: null,
          caption: `${actorName} · ${TOOL_LABEL[action.tool_name] ?? "工具"}`,
          offsetMs,
        });
      }
      continue;
    }

    if (message.kind === "agent" && actor) {
      // A reply to the user's own message rides that speaker's ask edge back; anything else
      // follows whichever hand-off reached its author last.
      const direct = trigger && message.reply_to_id === trigger.id ? askTo.get(actor) : undefined;
      const edgeIndex = direct ?? leadingEdge(actor, message.created_at);
      const leading = edgeIndex >= 0 ? edges[edgeIndex] : undefined;
      const isMention = leading?.kind === "mention";
      steps.push({
        index: steps.length,
        messageID: message.id,
        kind: isMention ? "mention" : "reply",
        actorID: actor,
        targetID: null,
        edgeIndex: edgeIndex >= 0 ? edgeIndex : null,
        caption: `${actorName} 回复`,
        offsetMs,
      });
      continue;
    }

    // An action row with no target (a plain tool call) still replays as a step.
    if (message.kind === "action") {
      const tool = action?.tool_name ?? "";
      steps.push({
        index: steps.length,
        messageID: message.id,
        kind: "tool",
        actorID: actor,
        targetID: null,
        edgeIndex: null,
        caption: `${actorName} · ${TOOL_LABEL[tool] ?? "工具"}`,
        offsetMs,
      });
    }
  }

  return steps;
}

/** The 1-based order number of every edge, by the first step that travels it. */
export function edgeOrder(trace: RunTrace): number[] {
  const order = new Array<number>(trace.edges.length).fill(0);
  let next = 1;
  const steps = buildSteps(trace);
  for (const step of steps) {
    if (step.edgeIndex == null || step.edgeIndex < 0 || order[step.edgeIndex] !== 0) continue;
    order[step.edgeIndex] = next;
    next += 1;
  }
  // An edge that no step travels (e.g. an orphaned @ that never got a reply) still needs a
  // number so the sequence stays complete; it follows the edges that did move.
  for (let i = 0; i < order.length; i += 1) {
    if (order[i] === 0) {
      order[i] = next;
      next += 1;
    }
  }
  return order;
}

/**
 * How long a step plays. Hand-offs and tools read at a glance; a reply's words type out in a
 * bubble on the graph, so it gets time by its length (about 40 characters a second on top of a
 * base), capped so one long answer never stalls the replay.
 */
export function stepDuration(step: Pick<RunStep, "kind">, chars = 0): number {
  if (step.kind !== "reply" && step.kind !== "mention") return 1100;
  return Math.min(4200, 1400 + Math.round(Math.min(chars, 90) * 25));
}

/** The node that "speaks" during a step, or null when nothing should pulse. */
export function activeNodeOf(step: RunStep): string | null {
  switch (step.kind) {
    case "ask":
    case "consult":
    case "dm":
    case "revise":
      return step.targetID;
    case "reply":
    case "mention":
    case "tool":
      return step.actorID;
    default:
      return null;
  }
}
