// One conversation's rows, plus where its copy/reply tools belong.
//
// A "reply" is the stretch of agent text and tool rows that follows a user message and ends
// at the next user message (or at the end of the thread). In a private chat between two bots
// there are no user messages, so each consecutive run of one author counts as a reply.
// Tools render only under the last completed text row of a reply, and copying takes the whole
// reply — never a single mid-run line.

import type { Message } from "../api/types";

export interface TimelineRow {
  message: Message;
  /** Spacing only: the previous row was by the same speaker, action rows included. */
  continues: boolean;
  /** Whether this author has not introduced itself in this burst yet. */
  showAuthor: boolean;
  /** True on the one row of a reply that carries the copy and reply tools. */
  tools: boolean;
  /**
   * On the row that introduces the author: what the burst under it is doing right now. The
   * author line appears once per burst, so a later streaming or stopped row reports here
   * instead of opening a second author line. `null` on every other row and on a settled burst.
   */
  live: BurstState | null;
  /**
   * Where a user message sits in a run of the user's own messages sent back to back:
   * `single` alone, else `first` / `middle` / `last`. The bubbles of one run tuck together.
   * `null` on every row that is not a user message.
   */
  run: RunPosition | null;
}

export type RunPosition = "single" | "first" | "middle" | "last";

export type BurstState = "thinking" | "speaking" | "interrupted";

/** A burst's state from its newest text row: an empty stream is still thinking. */
function burstState(message: Message): BurstState | null {
  if (message.kind !== "agent") return null;
  if (message.status === "streaming") return message.content === "" ? "thinking" : "speaking";
  return message.status === "interrupted" ? "interrupted" : null;
}

/** Relay markers (in/out) only say who spoke elsewhere; they never carry text. */
function isRelayMarker(message: Message): boolean {
  return Boolean(message.relay && message.relay.direction !== "summary");
}

/** A row that ends a burst rather than continuing it: the user speaking, a system note, the
 *  group's task card (written under the member's name, but not by the member), or the
 *  dispatcher's progress card. */
function breaksBurst(message: Message): boolean {
  return (
    message.kind === "user" ||
    message.kind === "notice" ||
    message.kind === "task" ||
    message.kind === "status"
  );
}

/**
 * Rows a chat does not show. In a group, a finished `ask_agent` consult only says "X 已回复",
 * and X's reply is the very next message in the same chat: the row is noise. A consult still
 * running, or one that failed, stays — those are the only times it says something new.
 */
export function visibleMessages(messages: Message[], conversationKind: string | undefined): Message[] {
  if (conversationKind !== "group") return messages;
  return messages.filter(
    (message) =>
      !(message.kind === "action" && message.action?.tool_name === "ask_agent" && message.action.status === "completed"),
  );
}

/** Index rows into replies: the number is stable per reply, `null` for ungrouped rows. */
function groupReplies(messages: Message[], privateChat: boolean): (number | null)[] {
  const groups: (number | null)[] = [];
  let current: number | null = null;
  let next = 0;
  let author: string | null = null;
  for (const message of messages) {
    if (breaksBurst(message) || isRelayMarker(message)) {
      current = null;
      author = null;
      groups.push(null);
      continue;
    }
    // A tool row belongs to the prose around it; it may open the reply the agent then writes.
    const switched = privateChat && message.kind === "agent" && author !== null && author !== message.author_id;
    if (current === null || switched) {
      current = next;
      next += 1;
      author = null;
    }
    if (message.kind === "agent" && author === null) author = message.author_id;
    groups.push(current);
  }
  return groups;
}

/** One line of quoted text: markdown markers dropped, trimmed, and cut to `limit` characters. */
export function quoteExcerpt(text: string, limit = 120): string {
  const flat = text
    .split("\n")
    .map(plainLine)
    .filter(Boolean)
    .join(" ")
    .replace(/\s+/gu, " ")
    .trim();
  if (flat.length <= limit) return flat;
  return `${flat.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

/** A quote is a glance, not a render: headings, lists, tables and emphasis become plain words. */
function plainLine(line: string): string {
  return line
    .replace(/^\s*\|?(?:\s*[-:]+\s*\|)+\s*$/u, "")
    .replace(/^\s*#{1,6}\s+/u, "")
    .replace(/^\s*>+\s?/u, "")
    .replace(/^\s*[-*+]\s+/u, "")
    .replace(/^\s*\d+[.)]\s+/u, "")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/gu, "$1")
    .replace(/(\*\*|__)(.+?)\1/gu, "$2")
    .replace(/(\*|_)(.+?)\1/gu, "$2")
    .replace(/~~(.+?)~~/gu, "$1")
    .replace(/`([^`]*)`/gu, "$1")
    .replace(/\|/gu, " ")
    .trim();
}

/** What the user is answering: their next message quotes this reply. */
export interface ReplyTarget {
  /** The quoted message: the last agent text row of the reply. */
  messageID: string;
  /** Who wrote the quoted reply. */
  name: string;
  /** The reply's whole text, one line, already trimmed for display. */
  excerpt: string;
}

/** Every row of one conversation, with spacing and the tools flag resolved. */
export function buildTimeline(
  messages: Message[],
  options: { privateChat?: boolean; running?: boolean } = {},
): TimelineRow[] {
  const groups = groupReplies(messages, options.privateChat ?? false);

  // The one row per reply that may carry tools: its last text turn, only once nothing there
  // is still streaming.
  const lastText = new Map<number, number>();
  const busy = new Set<number>();
  messages.forEach((message, index) => {
    const group = groups[index] ?? null;
    if (group === null) return;
    const streaming =
      message.status === "streaming" ||
      (message.kind === "action" &&
        (message.action?.status === "running" || message.action?.status === "awaiting"));
    if (streaming) busy.add(group);
    if (message.kind === "agent") lastText.set(group, index);
  });
  // Between two steps of a run (a tool just finished, the next model turn has not opened its
  // row yet) every row of the newest reply reads as complete; the run says it is not. Only a
  // reply after the user's last message can be the one running: before its first row lands,
  // the earlier reply stays settled.
  const lastUser = messages.findLastIndex((message) => message.kind === "user");
  const tail = groups.slice(lastUser + 1).findLast((group) => group !== null);
  const newest = options.running && tail !== undefined ? tail : null;
  if (newest !== null) busy.add(newest);
  const toolRow = new Map<number, number>();
  lastText.forEach((index, group) => {
    if (busy.has(group) || messages[index]?.status !== "complete") return;
    toolRow.set(group, index);
  });

  // The author introduces itself once per burst, on its first row, whether that row is prose
  // or a tool call: a reply that opens with a search must show the search inside it.
  let lastAuthor: string | null = null;
  let previous: Message | undefined;
  let opener: TimelineRow | null = null;
  const rows = messages.map((message, index) => {
    const adjacent =
      previous !== undefined &&
      !breaksBurst(previous) &&
      !isRelayMarker(previous) &&
      previous.author_id === message.author_id;
    previous = message;
    const group = groups[index] ?? null;
    if (breaksBurst(message) || isRelayMarker(message)) {
      lastAuthor = null;
      opener = null;
      return { message, continues: false, showAuthor: false, tools: false, live: null, run: null };
    }
    const showAuthor = lastAuthor !== message.author_id;
    lastAuthor = message.author_id;
    const row: TimelineRow = {
      message,
      continues: adjacent,
      showAuthor,
      tools: message.kind !== "action" && group !== null && toolRow.get(group) === index,
      live: null,
      run: null,
    };
    if (showAuthor) opener = row;
    // The newest text row decides; a tool row in between leaves the state as it was.
    if (opener && message.kind === "agent") opener.live = burstState(message);
    return row;
  });
  // The user's own messages sent one after another read as one run of bubbles. A reminder is
  // a system line, not the user speaking, so it breaks a run like any other row.
  const isUserBubble = (row: TimelineRow | undefined) => row?.message.kind === "user" && !row.message.reminder;
  rows.forEach((row, index) => {
    if (!isUserBubble(row)) return;
    const before = isUserBubble(rows[index - 1]);
    const after = isUserBubble(rows[index + 1]);
    row.run = before ? (after ? "middle" : "last") : after ? "first" : "single";
  });
  // The same gap on the author line: a run still going is still thinking, not settled.
  if (newest !== null && opener !== null) {
    const last: TimelineRow = opener;
    if (last.live === null && groups[messages.indexOf(last.message)] === newest) last.live = "thinking";
  }
  return rows;
}

/** One merged commentary bubble: the short notes of one burst, and the row that draws it. */
export interface CommentaryBlock {
  /** The row that draws the merged bubble: the burst's first short note. */
  head: number;
  /** Every short note inside it, oldest first (row indices). */
  members: number[];
  /** The newest note: the menu, pills and copy act on this real message. */
  latest: number;
}

export interface CommentaryOptions {
  /** Longest note still read as running commentary, in characters. */
  limit?: number;
  /** Rows folded elsewhere (a sandbox session): they neither merge nor let notes merge across. */
  exclude?: ReadonlySet<number>;
}

/** A note told as running commentary: short, settled, and nothing that needs a render of its own. */
function isRunCommentary(row: TimelineRow, limit: number): boolean {
  const message = row.message;
  if (message.kind !== "agent" || message.status !== "complete") return false;
  // The reply's copy/tools row is where the answer ends; it keeps its own bubble.
  if (row.tools) return false;
  if ((message.attachments?.length ?? 0) > 0) return false;
  const text = message.content.trim();
  if (text === "" || Array.from(text).length >= limit) return false;
  // A code snippet is content, not a progress note, however short.
  return !text.includes("```") && !text.includes("~~~");
}

/**
 * Running commentary in one burst, folded for display only: consecutive short notes from the
 * same bot become one bubble that lists them, while the burst's last text row — the conclusion —
 * stays a full bubble of its own. Tool rows between notes do not break the run (they render as
 * their own group); a long note, a code block, an attachment, a sandbox session or another
 * speaker does end it. Stored messages are untouched: every line still points at its own row.
 */
export function commentaryBlocks(rows: TimelineRow[], options: CommentaryOptions = {}): CommentaryBlock[] {
  const limit = options.limit ?? 80;
  const exclude = options.exclude;
  const blocks: CommentaryBlock[] = [];
  let burst: number[] = [];
  let lastText = -1;
  let pending: number[] = [];

  const flushNotes = () => {
    // One note on its own reads as the bubble it already is: merging changes nothing.
    if (pending.length > 1) {
      blocks.push({ head: pending[0]!, members: [...pending], latest: pending[pending.length - 1]! });
    }
    pending = [];
  };
  // The burst's text rows, in order: adjacent short notes merge, anything else cuts the run,
  // and the burst's last text row — the conclusion — always stands on its own.
  const closeBurst = () => {
    for (const index of burst) {
      if (index === lastText) {
        flushNotes();
        continue;
      }
      const row = rows[index]!;
      if (row.message.kind !== "agent") continue;
      if (isRunCommentary(row, limit)) pending.push(index);
      else flushNotes();
    }
    flushNotes();
    burst = [];
    lastText = -1;
  };

  for (const [index, row] of rows.entries()) {
    if (exclude?.has(index)) {
      closeBurst();
      continue;
    }
    // A tool row belongs to the burst it sits in and never breaks the notes around it.
    if (row.message.kind === "action") {
      burst.push(index);
      continue;
    }
    if (breaksBurst(row.message) || isRelayMarker(row.message)) {
      closeBurst();
      continue;
    }
    // A new speaker is a new burst: notes never merge across two voices.
    if (row.showAuthor) closeBurst();
    burst.push(index);
    if (row.message.kind === "agent") lastText = index;
  }
  closeBurst();
  return blocks;
}

/**
 * The tools that drive the sandbox VM (commands, the browser, the screen) plus the images it
 * looks at, which almost always follow a screenshot or a saved file. A run of them folds; a
 * viewed image between two computer steps stays inside the same stretch instead of splitting it.
 */
export const COMPUTER_TOOLS: ReadonlySet<string> = new Set([
  "computer",
  "computer_shell",
  "computer_browser",
  "view_image",
]);
/** One folded stretch of a reply: rows `start` through `end`, both inclusive. */
export interface ComputerSpan {
  start: number;
  end: number;
  /**
   * The rows after the last sandbox step that already belong to the fold: text whose response
   * started a tool call (`leads_to_tools === true`) or text still streaming with no verdict
   * yet. They render in the fold's note slot under its header, so the answer never flashes
   * outside the box and then jumps in.
   */
  noteStart?: number;
  noteEnd?: number;
}

/**
 * Where one bot was working the sandbox: from its first computer step to its last one in a
 * reply, with the narration it wrote in between. What it says after its last step stays outside,
 * so the answer is never folded away — unless its own model response went on to call tools,
 * which is the exact sign that the text is narration and not the answer. Any other tool (a web
 * search, a private message), another speaker, a user message or a relay marker ends the
 * stretch; the next sandbox step starts a new one on its own line.
 */
export function computerSpans(messages: Message[], options: { privateChat?: boolean } = {}): ComputerSpan[] {
  const groups = groupReplies(messages, options.privateChat ?? false);
  const spans: ComputerSpan[] = [];
  let open: (ComputerSpan & { author: string | null; group: number }) | null = null;
  const flush = (span: ComputerSpan) => {
    const done: ComputerSpan = { start: span.start, end: span.end };
    if (span.noteStart !== undefined) {
      done.noteStart = span.noteStart;
      done.noteEnd = span.noteEnd;
    }
    spans.push(done);
  };
  const note = (span: ComputerSpan, index: number) => {
    span.noteStart ??= index;
    span.noteEnd = index;
    span.end = index;
  };
  for (const [index, message] of messages.entries()) {
    const group = groups[index] ?? null;
    const same = open !== null && group === open.group && message.author_id === open.author;
    if (message.kind === "action" && COMPUTER_TOOLS.has(message.action?.tool_name ?? "")) {
      if (open !== null && same) {
        open.end = index;
        // The note is now a step's narration, inside the step list; the slot empties.
        delete open.noteStart;
        delete open.noteEnd;
        continue;
      }
      if (open !== null) flush(open);
      open = group === null ? null : { start: index, end: index, author: message.author_id, group };
      continue;
    }
    if (open !== null && same && message.kind === "agent") {
      // The model response itself said whether this text led into tools. `true` joins the
      // fold at once (even while it streams); `false` is the answer and closes the fold.
      if (message.leads_to_tools === true) {
        note(open, index);
        continue;
      }
      if (message.leads_to_tools === false) {
        flush(open);
        open = null;
        continue;
      }
      // No verdict yet: a streaming row waits in the note slot; a settled row from before the
      // field existed keeps the old rule and is absorbed only when another step follows.
      if (message.status === "streaming") {
        note(open, index);
        continue;
      }
      continue;
    }
    const otherTool = message.kind === "action";
    if (open !== null && (!same || otherTool || message.kind !== "agent")) {
      flush(open);
      open = null;
    }
  }
  if (open !== null) flush(open);
  return spans;
}

/** One reply as a unit: what copy and reply act on. */
export interface ReplyAnchor {
  /** Its last text row: the row a quote points at. */
  messageID: string;
  name: string;
  /** Every text row of the reply, joined. */
  text: string;
  /** No row of it is still streaming. */
  settled: boolean;
}

/** The reply `messageID` belongs to, or `null` for a row outside any reply. */
export function replyAnchor(
  messages: Message[],
  messageID: string,
  options: { privateChat?: boolean } = {},
): ReplyAnchor | null {
  const groups = groupReplies(messages, options.privateChat ?? false);
  const index = messages.findIndex((message) => message.id === messageID);
  if (index < 0) return null;
  const group = groups[index] ?? null;
  if (group === null) return null;
  const members = messages.filter((_, position) => groups[position] === group);
  const texts = members.filter((message) => message.kind === "agent" && message.content.trim());
  const last = texts.at(-1);
  if (!last) return null;
  return {
    messageID: last.id,
    name: last.author_name,
    text: texts.map((message) => message.content.trim()).join("\n\n"),
    settled: members.every((message) => message.status !== "streaming"),
  };
}

/** The whole reply that `messageID` belongs to: every agent text row, in order. */
export function replyText(
  messages: Message[],
  messageID: string,
  options: { privateChat?: boolean } = {},
): string {
  return replyAnchor(messages, messageID, options)?.text ?? "";
}

/** The waiting steps the backend reports while a model call shows nothing yet. */
export const WAITING_STEP = /^正在(思考|等模型回复)|没有回应/;
/** A run parked on the user (a card, a question): the bot is not replying, it is waiting. */
export const USER_STEP = /^等(待)?你/;

/**
 * What the author line says while this burst is live. Only live state belongs here: an answer's
 * question is named by the bubble's own quote when it is out of order, never as 「回复 你」.
 */
export function liveNote(live: BurstState | null, step: string, author: string): string | undefined {
  if (live === "interrupted") return "已停止";
  if (!live) return undefined;
  if (USER_STEP.test(step)) return step;
  // The run names the bot it waits on; the author line already does, so only the rest shows.
  const prefix = `「${author}」`;
  const waiting = step.startsWith(prefix) ? step.slice(prefix.length).replace(/^的/, "") : "";
  return WAITING_STEP.test(waiting) || USER_STEP.test(waiting) ? waiting : "正在回复";
}
