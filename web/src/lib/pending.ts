// What the timeline shows while a run is busy but nothing is streaming.
//
// The bot's avatar is the loading indicator: in its thinking pose it becomes three breathing
// dots. So the timeline adds at most one author line, "<name> 正在思考", and only when nobody is
// on screen working yet:
//
// - Before anything answers the user, it names the bot that will actually answer.
// - When a different bot takes the turn (a relay), it names that bot, once.
// - In the gaps of one bot's work (between a tool and the text after it) it adds nothing: that
//   bot's author line is already on screen and its avatar is already thinking.
// - A streaming message, a running tool or a pending confirmation shows its own state.

import type { Conversation, Message, Run } from "../api/types";

export interface PendingReply {
  /** The bot about to answer, for the "正在思考" author line. */
  agentID: string;
}

/**
 * The bot that answers first when `run` starts in `conversation`. Mirrors the backend's queue:
 * the run's own `current_agent_id` once known; a direct chat's only member; in a group, the first
 * member the user named; otherwise the supervisor.
 */
export function firstResponder(
  run: Run,
  conversation: Conversation,
  trigger: Message | undefined,
  supervisorID: string | undefined,
): string | null {
  if (run.current_agent_id) return run.current_agent_id;
  if (run.pending_agent_ids.length > 0) return run.pending_agent_ids[0] ?? null;
  if (conversation.kind === "direct" || conversation.kind === "thread") {
    return conversation.member_ids[0] ?? supervisorID ?? null;
  }
  const named = trigger?.mentions.find((id) => conversation.member_ids.includes(id));
  return named ?? supervisorID ?? null;
}

/** Something on screen already shows that the run is working. */
function showsOwnProgress(message: Message): boolean {
  if (message.status === "streaming") return true;
  const state = message.action?.status;
  return message.kind === "action" && (state === "running" || state === "awaiting");
}

/** The "<name> 正在思考" line for a running conversation, or null when it should not show. */
export function pendingReply(
  run: Run | undefined,
  conversation: Conversation,
  messages: readonly Message[],
  supervisorID: string | undefined,
): PendingReply | null {
  if (!run || run.status !== "running") return null;
  if (messages.some(showsOwnProgress)) return null;

  let lastUser = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]!.kind === "user") {
      lastUser = index;
      break;
    }
  }
  const answered = messages.slice(lastUser + 1);
  // Nothing has answered the user yet: one line, named for whoever will answer.
  if (answered.length === 0) {
    const agentID = firstResponder(run, conversation, messages[lastUser], supervisorID);
    return agentID ? { agentID } : null;
  }
  // A different bot is taking the turn: name it once. Otherwise its avatar above already shows it.
  const lastSpeaker = [...answered].reverse().find((message) => message.author_id)?.author_id ?? null;
  const next = run.current_agent_id;
  return next && next !== lastSpeaker ? { agentID: next } : null;
}
