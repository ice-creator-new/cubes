// Group threads: the hidden conversation one member works a task in. The group shows a card; the
// turns live here. These helpers keep threads out of every list the user picks a chat from, and
// find the group a thread belongs to so its page can link back.

import type { ConversationKind, GroupTask } from "../api/types";

/** The smallest shape the filters need: the server's list carries whole conversations. */
type Kinded = { kind: ConversationKind };

/** A thread is a conversation, but never a destination the user browses to on their own. */
export function isThread(conversation: Kinded): boolean {
  return conversation.kind === "thread";
}

/** The chats the sidebar lists: the ones the user talks in, never private bot-to-bot chats,
 *  a walk's own page, or a task thread (which is opened from its card). */
export function inSidebar(conversation: Kinded): boolean {
  return conversation.kind === "direct" || conversation.kind === "group";
}

/** The chats a scheduled task may target. A thread is a side line inside one group's task, not
 *  a place to schedule work of its own. */
export function isScheduleTarget(conversation: Kinded): boolean {
  return conversation.kind === "direct" || conversation.kind === "group";
}

/** One task by its row id, from whatever the store has loaded for its group. */
export function taskByID(tasks: readonly GroupTask[] | undefined, taskID: string | null | undefined): GroupTask | undefined {
  if (!taskID) return undefined;
  return tasks?.find((task) => task.id === taskID);
}

/** The group a thread belongs to, found through the tasks loaded from each group. */
export function findThreadOrigin(
  tasksByConversation: Record<string, GroupTask[]>,
  threadID: string,
): { conversationID: string; task: GroupTask } | null {
  for (const [conversationID, tasks] of Object.entries(tasksByConversation)) {
    const task = tasks.find((row) => row.thread_id === threadID);
    if (task) return { conversationID, task };
  }
  return null;
}

/** A thread tab reads 「<member> · <title>」: who is working, and on what. */
export function threadTabTitle(memberName: string | undefined, title: string): string {
  return `${memberName ?? "成员"} · ${title}`;
}

/** The thread page's own heading: the card it came from, in words. */
export function threadHeading(title: string): string {
  return `任务 · ${title}`;
}
