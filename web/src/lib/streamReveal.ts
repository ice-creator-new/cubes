// A streamed reply arrives in bursts: a few characters, a pause, then a paragraph at once. This
// module turns that into a steady, ChatGPT-style reveal. Everything here is pure.
//
// - `nextVisible` paces the text: each frame shows a few more characters, faster the further
//   behind it is, so a burst is spread out and a long backlog still catches up quickly.
// - Each frame's step is one batch: where it starts in the source, and when it was revealed.
//   `rehypeFadeIn` wraps every batch's text in a span whose CSS fade is offset by the batch's age
//   (a negative `animation-delay`). The fade is therefore a function of time, not of mounting: when
//   Markdown restructures a line (a closing `**` turns text into <strong>, a name becomes a chip)
//   and React re-mounts a span, it resumes exactly where it was instead of blinking.
// - `splitBlocks` cuts the source into top-level blocks at boundaries appending text cannot move,
//   so only the block being written is parsed again each frame.
// - `rehypeMentions` turns `@名字` into chips inside the tree, before the fade splits the text.

import { mentionTokens } from "./mentions";

// Minimal hast shapes: only what these plugins read and write.
interface Point {
  offset?: number;
}
interface Position {
  start: Point;
  end: Point;
}
export interface HText {
  type: "text";
  value: string;
  position?: Position;
}
export interface HElement {
  type: "element";
  tagName: string;
  properties: Record<string, unknown>;
  children: HNode[];
  position?: Position;
}
type HNode = HText | HElement | { type: "comment" | "doctype" | "raw"; value?: string };
export interface HRoot {
  type: "root";
  children: HNode[];
}

// ------------------------------------------------------------------------ pacing

/** Characters per second at a steady trickle. */
const BASE_RATE = 55;
/** Share of the waiting backlog revealed per second, on top of the base rate. */
const CATCH_UP = 2.4;
/** The longest frame gap the pacing honours: a window coming back must not dump everything. */
export const MAX_FRAME_SECONDS = 0.1;

/**
 * How many characters of `target` should be visible `elapsed` seconds after `shown` were. A step
 * never splits a surrogate pair, so an emoji or a rare CJK character appears whole.
 */
export function nextVisible(target: string, shown: number, elapsed: number): number {
  if (shown >= target.length) return target.length;
  const dt = Math.min(MAX_FRAME_SECONDS, Math.max(0, elapsed));
  const backlog = target.length - shown;
  const step = Math.max(1, Math.round((BASE_RATE + backlog * CATCH_UP) * dt));
  let next = Math.min(target.length, shown + step);
  const code = target.charCodeAt(next - 1);
  if (next < target.length && code >= 0xd800 && code <= 0xdbff) next += 1;
  return next;
}

// ------------------------------------------------------------------------ batches

export interface Batch {
  /** Source offset where the batch begins. */
  at: number;
  /** `performance.now()` when it was revealed. */
  when: number;
}

/** How long a batch fades; the CSS animation uses the same duration. */
export const FADE_MS = 480;

/** The batches whose fade is still running at `now`; the oldest are settled and dropped. */
export function liveBatches(batches: readonly Batch[], now: number): Batch[] {
  let first = 0;
  while (first < batches.length && now - batches[first]!.when >= FADE_MS) first += 1;
  return first === 0 ? [...batches] : batches.slice(first);
}

// ------------------------------------------------------------------------ blocks

export interface Block {
  /** Offset of the block in the whole source. */
  start: number;
  source: string;
}

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
/** A display-math fence (`$$` alone, or opening a block that closes later): never split inside. */
const MATH_FENCE = /^ {0,3}\$\$/;
/** Reference links and footnotes resolve across blocks: such a source is never split. */
const DEFINITION = /^ {0,3}\[[^\]\n]+\]:/m;

/**
 * Whether a line, after a blank line, starts a block nothing before it can absorb: not indented
 * (a list continuation or indented code), not a list item (a loose list keeps going), not a quote.
 * Undecided starts, like a lone digit or `-`, count as unsafe; not splitting is always correct.
 */
function opensBlock(line: string): boolean {
  const first = line[0];
  if (first === undefined || first === " " || first === "\t" || first === ">") return false;
  if (first === "-" || first === "*" || first === "+" || first === "_") return /^(\*{3,}|-{3,}|_{3,})\s*$/.test(line);
  return !/\d/.test(first);
}

/**
 * Top-level blocks of a Markdown source. The blocks concatenate back to the source, and appending
 * text never moves an earlier boundary, so a block keyed by its start stays the same block.
 */
export function splitBlocks(source: string): Block[] {
  if (DEFINITION.test(source)) return [{ start: 0, source }];
  const blocks: Block[] = [];
  let fence: string | null = null;
  let math = false;
  let start = 0;
  let offset = 0;
  let afterBlank = false;
  for (const line of source.split(/(?<=\n)/)) {
    const complete = line.endsWith("\n");
    const content = complete ? line.slice(0, -1) : line;
    // A partial last line may still turn into a list item or a rule: only whole lines split.
    if (fence === null && !math && afterBlank && complete && offset > start && opensBlock(content)) {
      blocks.push({ start, source: source.slice(start, offset) });
      start = offset;
    }
    const marker = FENCE.exec(content)?.[1];
    if (marker !== undefined) {
      if (fence === null) fence = marker;
      else if (marker[0] === fence[0] && marker.length >= fence.length && content.trim() === marker) fence = null;
    } else if (fence === null && MATH_FENCE.test(content)) {
      // `$$` opens display math unless the same line closes it again (`$$ x $$`).
      const rest = content.trim().slice(2);
      if (math) math = false;
      else if (!rest.includes("$$")) math = true;
    } else if (math && content.trimEnd().endsWith("$$")) {
      math = false;
    }
    afterBlank = fence === null && !math && complete && content.trim() === "";
    offset += line.length;
  }
  blocks.push({ start, source: source.slice(start) });
  return blocks;
}

/** First index in `batches` (ascending by `at`) whose `at` is `>= target`. */
function lowerBound(batches: readonly Batch[], target: number): number {
  let low = 0;
  let high = batches.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (batches[middle]!.at < target) low = middle + 1;
    else high = middle;
  }
  return low;
}

/**
 * The batches as seen from inside `block`, offsets relative to its start. A block that begins in
 * the middle of a batch gets that batch at offset 0, so its first text fades with it.
 */
export function batchesWithin(batches: readonly Batch[], block: Block): Batch[] {
  const end = block.start + block.source.length;
  if (batches.length === 0 || batches[0]!.at >= end) return [];
  const from = lowerBound(batches, block.start);
  const to = lowerBound(batches, end);
  const inside = batches.slice(from, to).map((batch) => ({ at: batch.at - block.start, when: batch.when }));
  if (from > 0 && (inside.length === 0 || inside[0]!.at > 0)) inside.unshift({ at: 0, when: batches[from - 1]!.when });
  return inside;
}

export function sameBatches(a: readonly Batch[], b: readonly Batch[]): boolean {
  if (a.length !== b.length) return false;
  for (let index = 0; index < a.length; index += 1) {
    if (a[index]!.at !== b[index]!.at || a[index]!.when !== b[index]!.when) return false;
  }
  return true;
}

// ------------------------------------------------------------------------ fade

export const FADE_CLASS = "fade-in";

function fadeSpan(value: string, delay: number): HElement {
  return {
    type: "element",
    tagName: "span",
    properties: { className: [FADE_CLASS], style: `animation-delay:${Math.round(delay)}ms` },
    children: [{ type: "text", value }],
  };
}

function offsets(node: { position?: Position }): [number, number] | null {
  const start = node.position?.start.offset;
  const end = node.position?.end.offset;
  return start === undefined || end === undefined ? null : [start, end];
}

/** One text node split at every batch boundary inside it; settled text stays plain. */
function fadeText(node: HText, batches: readonly Batch[], now: number): HNode[] {
  const range = offsets(node);
  if (range === null) return [node];
  const [start, end] = range;
  const first = batches[0]!.at;
  if (end <= first) return [node];
  // The batch an offset belongs to: the last one that starts at or before it.
  const owner = (offset: number) => batches[Math.max(0, lowerBound(batches, offset + 1) - 1)]!;
  // A source span longer than its value (`&amp;` → `&`, `\*` → `*`) cannot be cut by offset: it
  // fades as one piece with the batch it starts in.
  if (end - start !== node.value.length) {
    return start >= first ? [fadeSpan(node.value, owner(start).when - now)] : [node];
  }

  const pieces: HNode[] = [];
  let at = start;
  if (start < first) {
    pieces.push({ type: "text", value: node.value.slice(0, first - start) });
    at = first;
  }
  while (at < end) {
    const batch = owner(at);
    const index = lowerBound(batches, at + 1);
    const stop = Math.min(end, index < batches.length ? batches[index]!.at : end);
    pieces.push(fadeSpan(node.value.slice(at - start, stop - start), batch.when - now));
    at = stop;
  }
  return pieces;
}

function fadeTree(node: HRoot | HElement, batches: readonly Batch[], now: number): void {
  const children: HNode[] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      children.push(...fadeText(child as HText, batches, now));
      continue;
    }
    // Code is shown as written: its characters appear with the reveal, without a fade, so a
    // closing backtick never makes visible text vanish and come back. Typeset math is whole:
    // KaTeX's spans are layout, and wrapping their text would break the formula.
    if (child.type === "element") {
      const element = child as HElement;
      if (element.tagName !== "code" && element.tagName !== "pre" && !isMath(element)) fadeTree(element, batches, now);
    }
    children.push(child);
  }
  node.children = children;
}

export interface FadeOptions {
  /** Ascending batches; text before the first is settled. */
  batches: readonly Batch[];
  /** The clock the batches were stamped with; injectable for tests. */
  now?: () => number;
}

/** A rehype plugin: fade every revealed batch of text in, on the batch's own clock. */
export function rehypeFadeIn(options: FadeOptions) {
  return (tree: HRoot) => {
    if (options.batches.length === 0) return;
    const now = (options.now ?? (() => performance.now()))();
    fadeTree(tree, options.batches, now);
  };
}

// ------------------------------------------------------------------------ mentions

/** Splits text at real members' `@名字`, keeping exact source positions for the fade. */
function mentionText(node: HText, names: ReadonlySet<string>): HNode[] {
  const tokens = mentionTokens(node.value).filter((token) => names.has(token.name));
  if (tokens.length === 0) return [node];
  const range = offsets(node);
  const exact = range !== null && range[1] - range[0] === node.value.length;
  const at = (from: number, to: number): Position | undefined =>
    exact ? { start: { offset: range![0] + from }, end: { offset: range![0] + to } } : undefined;
  const text = (from: number, to: number): HText => ({ type: "text", value: node.value.slice(from, to), position: at(from, to) });

  const pieces: HNode[] = [];
  let cursor = 0;
  for (const token of tokens) {
    if (token.start > cursor) pieces.push(text(cursor, token.start));
    // The `@` sits in its own span so the capsule can show the face in its place; both pieces
    // keep their source offsets, so the fade still covers the whole token.
    pieces.push({
      type: "element",
      tagName: "button",
      properties: { className: ["mention"], type: "button", dataMention: token.name },
      children: [
        { type: "element", tagName: "span", properties: { className: ["mention-at"] }, children: [text(token.start, token.start + 1)] },
        text(token.start + 1, token.end),
      ],
      position: at(token.start, token.end),
    });
    cursor = token.end;
  }
  if (cursor < node.value.length) pieces.push(text(cursor, node.value.length));
  return pieces;
}

const NO_MENTIONS: Record<string, true> = { a: true, code: true, pre: true, button: true };

/** KaTeX output (inline `katex`, display `katex-display`): never rewritten after typesetting. */
function isMath(element: HElement): boolean {
  const names = element.properties?.className;
  return Array.isArray(names) && names.some((name) => name === "katex" || name === "katex-display");
}

function mentionTree(node: HRoot | HElement, names: ReadonlySet<string>): void {
  const children: HNode[] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      children.push(...mentionText(child as HText, names));
      continue;
    }
    if (child.type === "element" && !NO_MENTIONS[(child as HElement).tagName] && !isMath(child as HElement)) {
      mentionTree(child as HElement, names);
    }
    children.push(child);
  }
  node.children = children;
}

/** A rehype plugin: `@名字` for a real member becomes a chip. */
export function rehypeMentions(options: { names: ReadonlySet<string> }) {
  return (tree: HRoot) => {
    if (options.names.size > 0) mentionTree(tree, options.names);
  };
}
