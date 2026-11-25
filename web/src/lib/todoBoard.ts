// The progress board a working turn writes: which of its ``todo_write`` rows the chat keeps,
// and how the board reads in one line. Pure so the rule and the wording can be tested without
// a chat.

import type { ActivityTodo, Message } from "../api/types";

/** Whether one chat row is a progress board write. */
export function isTodoBoard(message: Message): boolean {
  return message.kind === "action" && message.action?.tool_name === "todo_write";
}

/** The board a ``todo_write`` row carries, in the order it was written; [] for older rows. */
export function boardTodos(message: Message): ActivityTodo[] {
  return message.action?.todos ?? [];
}

/** Where a turn's board rows fold to one: the row that draws it, and the board it shows. */
export interface TodoBoardPlan {
  /** The row that draws the board, mapped to the turn's newest board. */
  shown: Map<string, Message>;
  /** Older board rows of the same turn: the chat skips them. */
  hidden: Set<string>;
}

/** A row that ends a bot's turn rather than continuing it. */
function endsTurn(message: Message): boolean {
  return (
    message.kind === "user" ||
    message.kind === "notice" ||
    message.kind === "task" ||
    message.kind === "status" ||
    Boolean(message.relay && message.relay.direction !== "summary")
  );
}

/**
 * Fold one turn's board writes into one row.

 * The newest write's items are what the row shows; the row itself stays where the turn's
 * first board was written, so a turn that opens with a plan reads right under its author
 * line, and the row never jumps as later writes arrive. A turn is one speaker's burst: the
 * next speaker, the user, or a system row starts a new one.
 */
export function planTodoBoards(messages: Message[]): TodoBoardPlan {
  const shown = new Map<string, Message>();
  const hidden = new Set<string>();
  let boards: Message[] = [];
  let author: string | null = null;
  const flush = () => {
    if (boards.length === 0) return;
    // The first write keeps the row; the newest supplies what it reads.
    const head = boards[0]!;
    const newest = boards[boards.length - 1]!;
    shown.set(head.id, newest);
    for (const message of boards.slice(1)) hidden.add(message.id);
    boards = [];
  };
  for (const message of messages) {
    if (endsTurn(message)) {
      flush();
      author = null;
      continue;
    }
    if (author !== null && message.author_id !== author) flush();
    author = message.author_id;
    // A write that carries no items (a rejected board, or a row from before the field) is
    // not folded: its own row still says what happened, and it never hides a later board.
    if (isTodoBoard(message) && boardTodos(message).length > 0) boards.push(message);
  }
  flush();
  return { shown, hidden };
}

/** How many items are finished, and how many there are; a dropped one is not finished. */
export function boardProgress(todos: ActivityTodo[]): { done: number; total: number } {
  return {
    done: todos.filter((todo) => todo.status === "done").length,
    total: todos.length,
  };
}

/**
 * The board as one line of words: what is being done now, else the next step, else that the
 * turn is finished. A count alone never says what the work is.
 */
export function boardLine(todos: ActivityTodo[]): string {
  // An item often says 正在 itself (「正在填写 #4」); the line's own verb replaces it.
  const phrase = (text: string) => text.replace(/^正在/, "").trim();
  const doing = todos.find((todo) => todo.status === "doing");
  if (doing) return `正在：${phrase(doing.text)}`;
  const next = todos.find((todo) => todo.status === "pending");
  if (next) return `下一步：${next.text}`;
  if (todos.some((todo) => todo.status === "done")) return "全部完成";
  return todos.length > 0 ? "没有要做的了" : "";
}
