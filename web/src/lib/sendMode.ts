// Sending while a reply runs: which mode reaches the server, and how the composer explains it.
// The choice is a setting — the bot's own pick, else the team's — not something to pick at send
// time. A CLI bot drives its own session, and images cannot join a request already on the wire,
// so both fall back to a queue.

import type { Message, SendMode } from "../api/types";

export const SEND_MODES: { id: SendMode; label: string; hint: string }[] = [
  {
    id: "steer",
    label: "插进这一轮",
    hint: "消息会加入正在进行的这一轮，机器人在下一次思考时看到并调整，不打断它。",
  },
  {
    id: "queue",
    label: "做完再发",
    hint: "先排队，等这一轮结束后再作为新消息发送；发送前可以撤回。",
  },
  {
    id: "interrupt",
    label: "停止并发送",
    hint: "停下正在写的回复，立刻回答这条新消息。",
  },
];

/** What the server is actually asked to do for a send that meets a running reply. */
export function resolveSendMode(
  agentMode: SendMode | null | undefined,
  teamMode: SendMode,
  cliBot: boolean,
  withAttachments = false,
): SendMode {
  const mode = agentMode ?? teamMode;
  return mode === "steer" && (cliBot || withAttachments) ? "queue" : mode;
}

/** The empty field's prompt while a reply runs. */
export function busyPlaceholder(mode: SendMode): string {
  if (mode === "steer") return "发送会加入当前这一轮";
  if (mode === "queue") return "发送会排队，等这一轮结束";
  return "发送会打断当前回复";
}

/** Sends still waiting for their turn: shown above the composer, never in the transcript. */
export function queuedMessages(messages: Message[]): Message[] {
  return messages.filter((message) => message.status === "queued");
}

/** The transcript without the queued sends, which the queued strip above the composer owns. */
export function withoutQueued(messages: Message[]): Message[] {
  return messages.filter((message) => message.status !== "queued");
}
