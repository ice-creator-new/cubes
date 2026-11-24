// The composer's text field. The draft stays a plain string (what is sent, searched and saved);
// the field shows `@名字` and `/技能名` in it as capsules, the same ones a sent message shows.
//
// A `<textarea>` can only show plain text, so this is a plain-text `contentEditable` whose DOM
// we own: React never renders its children. Typing, deleting and input methods edit it natively;
// after each change the field reads its text back and, only when the capsules it should show
// differ from the ones on screen, redraws itself and puts the caret back. Line breaks and pasted
// text are inserted through the draft, so the browser never adds markup of its own.

import { useEffect, useLayoutEffect, useRef, type ClipboardEvent, type KeyboardEvent, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Agent } from "../api/types";
import { capsuleDeletion, draftSegments, segmentsKey, type Segment } from "../lib/composerTokens";
import { Avatar } from "./Avatar";
import { Icon } from "./Icon";

/** The capsule's source text (`@甲`, `/weekly-report`); its presence marks a capsule. */
const SOURCE = "data-source";
/** The `<br>` that makes a trailing line break visible; it is not part of the text. */
const TAIL = "data-tail";
/** A caret stop after a capsule that ends the draft; never part of the text. */
const GUARD = "\u200B";

const isText = (node: Node): node is Text => node.nodeType === Node.TEXT_NODE;
const isElement = (node: Node): node is HTMLElement => node.nodeType === Node.ELEMENT_NODE;
const plain = (data: string) => data.replaceAll(GUARD, "");
/** The `<br>` at the end of the field: ours (marked), or the placeholder WebKit leaves after a
 *  deletion. Never part of the text; line breaks the user typed are inside text nodes. */
const isPlaceholder = (node: Node) =>
  isElement(node) && node.tagName === "BR" && (node.hasAttribute(TAIL) || node.nextSibling === null);

/** How many characters of the draft one top-level node stands for. */
function sourceLength(node: Node): number {
  if (isText(node)) return plain(node.data).length;
  if (!isElement(node)) return 0;
  const source = node.getAttribute(SOURCE);
  if (source !== null) return source.length;
  if (node.tagName === "BR") return isPlaceholder(node) ? 0 : 1;
  return plain(node.textContent ?? "").length;
}

/** The draft as the field holds it, its runs, and whether the browser added markup we did not. */
function readField(root: HTMLElement): { text: string; segments: Segment[]; foreign: boolean } {
  const segments: Segment[] = [];
  let foreign = false;
  const pushText = (value: string) => {
    if (!value) return;
    const last = segments[segments.length - 1];
    if (last?.kind === "text") segments[segments.length - 1] = { kind: "text", text: last.text + value };
    else segments.push({ kind: "text", text: value });
  };
  for (const node of root.childNodes) {
    if (isText(node)) pushText(plain(node.data));
    else if (isElement(node) && node.hasAttribute(SOURCE)) {
      const source = node.getAttribute(SOURCE)!;
      const kind = node.dataset.kind === "skill" ? "skill" : "mention";
      segments.push({ kind, name: source.slice(1), text: source });
    } else if (isElement(node) && node.tagName === "BR") {
      if (!isPlaceholder(node)) pushText("\n");
    } else {
      foreign = true;
      pushText(plain(node.textContent ?? ""));
    }
  }
  return { text: segments.map((segment) => segment.text).join(""), segments, foreign };
}

/** The draft offset of a DOM position inside the field (a position in a capsule means after it). */
function offsetAt(root: HTMLElement, node: Node, offset: number): number {
  let child: Node | null = node;
  while (child && child.parentNode !== root) child = child.parentNode;
  if (node === root || !child) {
    let total = 0;
    const end = node === root ? offset : root.childNodes.length;
    for (let i = 0; i < end; i += 1) total += sourceLength(root.childNodes[i]!);
    return total;
  }
  let before = 0;
  for (const sibling of root.childNodes) {
    if (sibling === child) break;
    before += sourceLength(sibling);
  }
  if (isText(child) && node === child) return before + plain(child.data.slice(0, offset)).length;
  if (isElement(child) && child.hasAttribute(SOURCE)) return before + sourceLength(child);
  return before + (offset > 0 ? sourceLength(child) : 0);
}

/** The selection as draft offsets `[start, end]`, or null when it is not in the field. */
export function fieldSelection(root: HTMLElement | null): [number, number] | null {
  const selection = root ? window.getSelection() : null;
  if (!root || !selection || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  return [offsetAt(root, range.startContainer, range.startOffset), offsetAt(root, range.endContainer, range.endOffset)];
}

/** Put a collapsed caret at a draft offset. Never inside a capsule: before or after it. */
function placeCaret(root: HTMLElement, offset: number): void {
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  let seen = 0;
  for (const node of root.childNodes) {
    const length = sourceLength(node);
    if (isText(node) && offset <= seen + length) {
      // The last index whose plain prefix is `offset - seen` long: after a guard, not before it.
      const want = offset - seen;
      let index = 0;
      let count = 0;
      for (let i = 0; i <= node.data.length; i += 1) {
        if (count === want) index = i;
        if (i < node.data.length && node.data[i] !== GUARD) count += 1;
        if (count > want) break;
      }
      range.setStart(node, index);
      range.collapse(true);
      selection.removeAllRanges();
      selection.addRange(range);
      return;
    }
    if (isElement(node) && offset <= seen && length > 0) {
      range.setStartBefore(node);
      range.collapse(true);
      selection.removeAllRanges();
      selection.addRange(range);
      return;
    }
    seen += length;
  }
  const tail = root.lastChild;
  if (tail && isElement(tail) && tail.hasAttribute(TAIL)) {
    range.setStartBefore(tail);
  } else {
    range.selectNodeContents(root);
    range.collapse(false);
  }
  selection.removeAllRanges();
  selection.addRange(range);
}

export function ComposerField({
  fieldRef,
  value,
  caretTarget,
  agents,
  skills,
  placeholder,
  composing,
  describedBy,
  onChange,
  onCaret,
  onKeyDown,
  onPaste,
  onCompositionStart,
  onCompositionEnd,
}: {
  fieldRef: RefObject<HTMLDivElement | null>;
  value: string;
  /** Where a pick put the caret; applied once, as soon as the new draft is on screen. */
  caretTarget: RefObject<number | null>;
  /** Members whose `@名字` becomes a capsule, by name. */
  agents: ReadonlyMap<string, Agent>;
  /** Skill names whose `/名称` becomes a capsule. */
  skills: ReadonlySet<string>;
  placeholder: string;
  /** An input method is composing: the field must not redraw under it. */
  composing: RefObject<{ open: boolean }>;
  describedBy?: string;
  onChange: (text: string, caret: number) => void;
  onCaret: (caret: number) => void;
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  onPaste: (event: ClipboardEvent<HTMLDivElement>) => void;
  onCompositionStart: () => void;
  onCompositionEnd: () => void;
}) {
  // React roots of the faces and marks inside the capsules on screen.
  const roots = useRef<Root[]>([]);
  const members = useRef(new Set<string>());
  members.current = new Set(agents.keys());
  const latest = useRef({ agents, onChange, onCaret, value });
  latest.current = { agents, onChange, onCaret, value };

  const draw = (root: HTMLElement, segments: Segment[], text: string) => {
    // Unmounting a root while React commits warns; the old capsules are already off screen.
    const gone = roots.current.splice(0);
    if (gone.length > 0) queueMicrotask(() => gone.forEach((island) => island.unmount()));
    const nodes: Node[] = [];
    for (const segment of segments) {
      if (segment.kind === "text") {
        nodes.push(document.createTextNode(segment.text));
        continue;
      }
      const capsule = document.createElement("span");
      capsule.className = segment.kind === "mention" ? "mention" : "skill-call";
      capsule.contentEditable = "false";
      capsule.dataset.kind = segment.kind;
      capsule.setAttribute(SOURCE, segment.text);
      const mark = document.createElement("span");
      mark.className = segment.kind === "mention" ? "mention-face" : "skill-call-mark";
      mark.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.className = "mention-label";
      label.textContent = segment.kind === "mention" ? segment.name : segment.text;
      capsule.append(mark, label);
      const island = createRoot(mark);
      const agent = latest.current.agents.get(segment.name);
      island.render(
        segment.kind === "skill" ? (
          <Icon name="skills" size={12} />
        ) : agent ? (
          <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={16} name={agent.name} frozen />
        ) : null,
      );
      roots.current.push(island);
      nodes.push(capsule);
    }
    // A capsule that ends the draft needs a text position after it for the caret to land in.
    if (segments[segments.length - 1]?.kind !== "text" && segments.length > 0) nodes.push(document.createTextNode(GUARD));
    // A trailing line break shows only with something after it.
    if (text.endsWith("\n")) {
      const tail = document.createElement("br");
      tail.setAttribute(TAIL, "");
      nodes.push(tail);
    }
    root.replaceChildren(...nodes);
  };

  // After every render: redraw only when the capsules on screen are not the ones the draft needs.
  useLayoutEffect(() => {
    const root = fieldRef.current;
    if (!root || composing.current.open) return;
    const focused = document.activeElement === root;
    const target = caretTarget.current;
    caretTarget.current = null;
    const caret = target ?? (focused ? fieldSelection(root)?.[1] : undefined) ?? null;
    const shown = readField(root);
    // Only the reference being typed stays text: a capsule already on screen keeps its shape
    // when the caret lands right after it.
    let end = 0;
    const capsuleEnds = new Set<number>();
    for (const segment of shown.segments) {
      end += segment.text.length;
      if (segment.kind !== "text") capsuleEnds.add(end);
    }
    const typing = caret !== null && !capsuleEnds.has(caret) ? caret : null;
    const wanted = draftSegments(value, typing, members.current, skills);
    const redraw = shown.foreign || shown.text !== value || segmentsKey(shown.segments) !== segmentsKey(wanted);
    if (redraw) draw(root, wanted, value);
    if (target !== null) root.focus();
    if (target !== null || (redraw && focused)) placeCaret(root, caret ?? value.length);
  });

  // The caret moving (arrows, clicks) can finish a reference, so it is state, read from the DOM.
  useEffect(() => {
    const onSelection = () => {
      const root = fieldRef.current;
      if (!root || document.activeElement !== root) return;
      const range = fieldSelection(root);
      if (range) latest.current.onCaret(range[1]);
    };
    document.addEventListener("selectionchange", onSelection);
    return () => document.removeEventListener("selectionchange", onSelection);
  }, [fieldRef]);

  useEffect(
    () => () => {
      const gone = roots.current.splice(0);
      queueMicrotask(() => gone.forEach((island) => island.unmount()));
    },
    [],
  );

  /** Replace `[start, end]` (default: the selection) with `text` through the draft, caret after it. */
  const insert = (text: string, span?: [number, number]) => {
    const root = fieldRef.current;
    const current = latest.current.value;
    const [start, end] = span ?? fieldSelection(root) ?? [current.length, current.length];
    caretTarget.current = start + text.length;
    latest.current.onChange(current.slice(0, start) + text + current.slice(end), start + text.length);
  };

  return (
    <div
      ref={fieldRef}
      className={value === "" ? "composer-field empty" : "composer-field"}
      contentEditable="plaintext-only"
      role="textbox"
      aria-multiline="true"
      aria-label="消息"
      aria-placeholder={placeholder}
      aria-describedby={describedBy}
      data-placeholder={placeholder}
      spellCheck
      onInput={(event) => {
        const root = event.currentTarget;
        const { text } = readField(root);
        const range = fieldSelection(root);
        latest.current.onChange(text, range ? range[1] : text.length);
      }}
      onKeyDown={(event) => {
        onKeyDown(event);
        if (event.defaultPrevented) return;
        const { isComposing, keyCode } = event.nativeEvent;
        if (isComposing || keyCode === 229) return;
        if (event.key === "Enter" && event.shiftKey) {
          event.preventDefault();
          insert("\n");
          return;
        }
        // One press removes a whole capsule (and the space a pick put after it); the browser
        // would spend a press on that space, and cannot step into a capsule to delete forward.
        const deleting = event.key === "Backspace" ? "backward" : event.key === "Delete" ? "forward" : null;
        if (deleting && !event.altKey && !event.metaKey && !event.ctrlKey) {
          const range = fieldSelection(event.currentTarget);
          if (!range || range[0] !== range[1]) return;
          const span = capsuleDeletion(readField(event.currentTarget).segments, range[1], deleting);
          if (!span) return;
          event.preventDefault();
          insert("", span);
        }
      }}
      onPaste={(event) => {
        onPaste(event);
        if (event.defaultPrevented) return;
        event.preventDefault();
        const text = event.clipboardData.getData("text/plain").replace(/\r\n?/g, "\n");
        if (text) insert(text);
      }}
      onDrop={(event) => {
        // Dropped text would bring the source's markup with it; images are the composer's.
        if (event.dataTransfer.types.includes("text/plain") && !event.defaultPrevented) {
          event.preventDefault();
          insert(event.dataTransfer.getData("text/plain"));
        }
      }}
      onCompositionStart={onCompositionStart}
      onCompositionEnd={(event) => {
        onCompositionEnd();
        // The committed characters may have finished a reference; read them back once the
        // composition is over, since the field held off while it was open.
        const root = event.currentTarget;
        const { text } = readField(root);
        const range = fieldSelection(root);
        latest.current.onChange(text, range ? range[1] : text.length);
      }}
    />
  );
}
