// `@名字` on the wire is plain text, so the client recognises it itself: highlighting, the
// picker query and the ids sent with a run all come from these pure functions.

export interface MentionToken {
  name: string;
  start: number;
  end: number;
}

export interface MentionQuery {
  text: string;
  start: number;
}

const TERMINATORS: Record<string, true> = Object.fromEntries(
  Array.from("@，。！？、；：,.!?;:()（）[]【】<>《》\"'“”‘’·`*|~\\/").map((char) => [char, true]),
);

function isTerminator(character: string): boolean {
  return TERMINATORS[character] === true || /\s/u.test(character);
}

/** Every `@名字` run in the text, in order. `a@b.com` is not a mention; `@A@B` is two. */
export function mentionTokens(text: string): MentionToken[] {
  const chars = Array.from(text);
  const tokens: MentionToken[] = [];
  let offset = 0;
  const offsets = chars.map((char) => {
    const start = offset;
    offset += char.length;
    return start;
  });
  let i = 0;
  let openedByPrevious = false;
  while (i < chars.length) {
    if (chars[i] !== "@") {
      openedByPrevious = false;
      i += 1;
      continue;
    }
    const before = chars[i - 1];
    if (!openedByPrevious && before !== undefined && !isTerminator(before)) {
      i += 1;
      continue;
    }
    let j = i + 1;
    while (j < chars.length && !isTerminator(chars[j]!)) j += 1;
    const name = chars.slice(i + 1, j).join("");
    openedByPrevious = name.length > 0;
    if (openedByPrevious) {
      tokens.push({ name, start: offsets[i]!, end: j < chars.length ? offsets[j]! : text.length });
    }
    i = j;
  }
  return tokens;
}

/** The unfinished `@…` right before the caret, when the picker should be showing. */
export function mentionQuery(text: string, caret = text.length): MentionQuery | null {
  const head = text.slice(0, caret);
  const marker = head.lastIndexOf("@");
  if (marker < 0) return null;
  const before = head[marker - 1];
  if (before !== undefined && !isTerminator(before)) return null;
  const suffix = head.slice(marker + 1);
  if (Array.from(suffix).some(isTerminator)) return null;
  return { text: suffix, start: marker };
}

/** Replaces the open query with `@名字 `, or inserts one at the caret. Returns text and new caret. */
export function insertMention(
  draft: string,
  name: string,
  caret = draft.length,
): { text: string; caret: number } {
  const replacement = `@${name} `;
  const query = mentionQuery(draft, caret);
  if (query) {
    const text = draft.slice(0, query.start) + replacement + draft.slice(caret);
    return { text, caret: query.start + replacement.length };
  }
  const head = draft.slice(0, caret);
  const spacer = head === "" || /\s$/u.test(head) ? "" : " ";
  const text = head + spacer + replacement + draft.slice(caret);
  return { text, caret: head.length + spacer.length + replacement.length };
}

/** Members the draft actually names, in roster order. */
export function mentionedIDs(draft: string, roster: { id: string; name: string }[]): string[] {
  const names = new Set(mentionTokens(draft).map((token) => token.name));
  return roster.filter((agent) => names.has(agent.name)).map((agent) => agent.id);
}
