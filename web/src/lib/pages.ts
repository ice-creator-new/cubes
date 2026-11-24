// How one conversation's loaded window grows. The client reads history one newest page at a
// time and prepends older ones as the reader scrolls up; a refresh (after a send, after the
// stream reconnects) must never throw away what was already scrolled back to. These are the
// pure rules, keyed by each message's `seq` inside its conversation.

import type { Message } from "../api/types";

/** One page exactly as `GET /conversations/{id}?limit=` answers it. */
export interface Page {
  messages: Message[];
  /** Older messages exist beyond this page. */
  has_more: boolean;
  /** Messages the page quotes, although they sit outside it. */
  references: Message[];
}

/** What a view has loaded: the same shape a detail carries. */
export interface Loaded {
  messages: Message[];
  has_more: boolean;
  references: Message[];
}

/** Rows read per page. A long thread stays cheap to open, one page at a time. */
export const PAGE_SIZE = 80;

/** One row per id, in `seq` order; the same message can arrive by page, refresh and stream. */
function dedupe(messages: Message[]): Message[] {
  const seen = new Set<string>();
  const kept: Message[] = [];
  for (const message of messages) {
    if (seen.has(message.id)) continue;
    seen.add(message.id);
    kept.push(message);
  }
  return kept.sort((a, b) => a.seq - b.seq);
}

/** Quoted parents from outside the page, one copy each; one already on screen needs none. */
function parents(rows: Message[], ...sources: Message[][]): Message[] {
  const onScreen = new Set(rows.map((message) => message.id));
  const kept = new Map<string, Message>();
  for (const list of sources) {
    for (const message of list) {
      if (onScreen.has(message.id) || kept.has(message.id)) continue;
      kept.set(message.id, message);
    }
  }
  return [...kept.values()].sort((a, b) => a.seq - b.seq);
}

/**
 * Fold a freshly read newest page into what is loaded. Rows older than the page's first `seq`
 * stay where they were, and so does whether more sit beyond them: the reader who scrolled
 * back keeps their place after a send. With nothing older loaded, the page is the window.
 */
export function mergeNewestPage(current: Loaded | null, page: Page): Loaded {
  if (page.messages.length === 0) {
    return current ?? { messages: [], has_more: page.has_more, references: page.references };
  }
  const older = current?.messages.filter((message) => message.seq < page.messages[0]!.seq) ?? [];
  if (!current || older.length === 0) {
    return {
      messages: dedupe(page.messages),
      has_more: page.has_more,
      references: parents(page.messages, page.references, current?.references ?? []),
    };
  }
  const messages = dedupe([...older, ...page.messages]);
  return {
    messages,
    has_more: current.has_more,
    references: parents(messages, page.references, current.references),
  };
}

/** Add one older page in front: the window grows upward, nothing already loaded is dropped. */
export function prependOlderPage(current: Loaded, page: Page): Loaded {
  const messages = dedupe([...page.messages, ...current.messages]);
  return {
    messages,
    has_more: page.has_more,
    references: parents(messages, page.references, current.references),
  };
}

/**
 * Whether a live message belongs on screen. One older than the oldest loaded row, while older
 * rows remain unread, is outside the window: it is not inserted at the top, it comes with its
 * page when the reader scrolls up.
 */
export function acceptsLiveMessage(loaded: Loaded, message: Message): boolean {
  if (loaded.messages.some((item) => item.id === message.id)) return true;
  const oldest = loaded.messages[0]?.seq;
  if (oldest === undefined) return true;
  return message.seq >= oldest || !loaded.has_more;
}
