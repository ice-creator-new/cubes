// The composer draft is plain text; `@名字` and `/技能名` inside it are drawn as capsules. These
// pure helpers cut a draft into the runs the field shows: plain text, a member capsule, or a
// skill capsule. The field compares this shape with what it has on screen and redraws only
// when they differ, so ordinary typing never rebuilds the DOM under the caret.

import { mentionTokens } from "./mentions";
import { slashTokens } from "./skills";

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "mention" | "skill"; name: string; text: string };

/**
 * The draft as runs. A reference becomes a capsule once it is finished: something follows it,
 * or the caret has moved away. One the caret still sits at the end of stays text, so `@甲`
 * does not lock in while the user is still typing `@甲乙`.
 */
export function draftSegments(
  text: string,
  caret: number | null,
  members: ReadonlySet<string>,
  skills: ReadonlySet<string>,
): Segment[] {
  const tokens = [
    ...mentionTokens(text)
      .filter((token) => members.has(token.name))
      .map((token) => ({ ...token, kind: "mention" as const })),
    ...slashTokens(text, skills).map((token) => ({ ...token, kind: "skill" as const })),
  ].sort((a, b) => a.start - b.start);
  const out: Segment[] = [];
  const pushText = (value: string) => {
    if (!value) return;
    const last = out[out.length - 1];
    if (last?.kind === "text") out[out.length - 1] = { kind: "text", text: last.text + value };
    else out.push({ kind: "text", text: value });
  };
  let cursor = 0;
  for (const token of tokens) {
    if (token.start < cursor || token.end === caret) continue;
    pushText(text.slice(cursor, token.start));
    out.push({ kind: token.kind, name: token.name, text: text.slice(token.start, token.end) });
    cursor = token.end;
  }
  pushText(text.slice(cursor));
  return out;
}

/** A comparable fingerprint of the runs: same key, same thing on screen. */
export function segmentsKey(segments: readonly Segment[]): string {
  return segments.map((segment) => `${segment.kind}\u0001${segment.text}`).join("\u0002");
}

/**
 * What one Backspace or Delete at a collapsed `caret` removes when it is next to a capsule: the
 * whole reference, never one character of it. A picked reference is followed by a space the
 * pick inserted; that space is invisible next to the pill, so a Backspace after it takes the
 * space and the capsule together instead of spending a press on the gap. `null`: an ordinary
 * character deletion.
 */
export function capsuleDeletion(
  segments: readonly Segment[],
  caret: number,
  direction: "backward" | "forward",
): [number, number] | null {
  let start = 0;
  for (const [index, segment] of segments.entries()) {
    const end = start + segment.text.length;
    if (segment.kind !== "text") {
      const next = segments[index + 1];
      const gap = next?.kind === "text" && /^[ \u00a0]/.test(next.text) ? 1 : 0;
      if (direction === "backward" && (caret === end || (gap === 1 && caret === end + 1))) return [start, caret];
      if (direction === "forward" && caret === start) return [start, end + gap];
    }
    start = end;
  }
  return null;
}
