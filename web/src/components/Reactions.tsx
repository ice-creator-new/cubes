// Emoji reactions on one message: the row of pills, plus the add-reaction button and its picker
// popover (a quick row, then a 「更多」 grouped grid). The popover follows the mention/memory
// conventions: absolute in a relative shell, grows upward near the window's foot, closes on
// Escape or an outside click, and never overflows the window.

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Reaction } from "../api/types";
import { EMOJI_GROUPS, QUICK_EMOJI, findEmoji, type EmojiEntry, type EmojiGroupId } from "../lib/emoji";
import { prefersReducedMotion } from "../lib/motion";

/**
 * The panel opens with no emoji drawn at all, then fills one row a frame.
 *
 * A glyph the system has not drawn before costs milliseconds to shape and rasterize, and a
 * picker is hundreds of them: painting even a screenful in the frame that opens the sheet makes
 * the click feel stuck, for longer on slower machines. Paint the chrome at once, then the emoji
 * over the next frames — the panel is on screen and usable the whole time.
 */
const FILL_STEP = 9;
/** The sheet's box, as `.reaction-picker.floating` declares it. */
const SHEET_W = 344;
const SHEET_H = 400;
import { FAMILY_STYLE, burst, burstLife, reactionFamily, type ReactionFamily, type Speck } from "../lib/reactionFx";
import { playBurst } from "../lib/reactionPlayback";
import { Icon } from "./Icon";

/** How long a pill's exit animation runs before the reaction is actually removed. */
const LEAVE_MS = 240;
/** How long a burst stays mounted: past the longest flight, as a safety net. */
const BURST_MS = 1050;

// One confetti piece takes one of the theme's colours, so a burst reads against any bubble.
const CONFETTI_TONES = ["var(--accent)", "var(--ok)", "var(--warn)", "var(--danger)"];

/**
 * The pills under a message; each toggles the user's own reaction for its emoji.
 *
 * A pill that appears (someone reacted, or the user just did) plays its family's entrance and
 * throws specks; a pill the user removes plays its exit first, so the goodbye is visible too.
 */
export function ReactionsRow({
  reactions,
  onToggle,
}: {
  reactions: Reaction[];
  onToggle: (emoji: string) => void;
}) {
  // What was already on screen: only pills that arrive later get the entrance, so opening a
  // conversation does not burst every reaction it holds.
  const known = useRef<Set<string> | null>(null);
  const fresh = new Set<string>();
  if (known.current === null) {
    known.current = new Set(reactions.map((reaction) => reaction.emoji));
  } else {
    for (const reaction of reactions) {
      if (!known.current.has(reaction.emoji)) fresh.add(reaction.emoji);
    }
  }
  useEffect(() => {
    for (const reaction of reactions) known.current?.add(reaction.emoji);
  }, [reactions]);
  // The row leaves the DOM when the last pill goes, so the seen set resets with it.
  if (reactions.length === 0) return null;
  return (
    <div className="reactions" role="group" aria-label="表情回应">
      {reactions.map((reaction) => (
        <ReactionPill
          key={reaction.emoji}
          reaction={reaction}
          fresh={fresh.has(reaction.emoji)}
          onToggle={onToggle}
        />
      ))}
    </div>
  );
}

/** One pill: the emoji, its count, its family's entrance/exit, and the specks it throws. */
function ReactionPill({
  reaction,
  fresh,
  onToggle,
}: {
  reaction: Reaction;
  fresh: boolean;
  onToggle: (emoji: string) => void;
}) {
  const reduced = prefersReducedMotion();
  const family = reactionFamily(reaction.emoji);
  const style = FAMILY_STYLE[family];
  const [burst, setBurst] = useState(0);
  const [leaving, setLeaving] = useState(false);
  // A burst fires when the pill is new, and again each time the count grows under the user's
  // own click; the first render of a pill that was already there stays still.
  const previous = useRef<{ count: number | null }>({ count: null });
  useEffect(() => {
    const last = previous.current.count;
    previous.current.count = reaction.count;
    if (reduced) return;
    if (fresh || (last !== null && reaction.count > last)) setBurst((value) => value + 1);
  }, [reaction.count, fresh, reduced]);
  useEffect(() => {
    if (burst === 0) return;
    const timer = window.setTimeout(() => setBurst((value) => (value === burst ? 0 : value)), BURST_MS);
    return () => window.clearTimeout(timer);
  }, [burst]);

  const names = reaction.actors.map((actor) => actor.name).join("、");
  const label = `${reaction.emoji}，${reaction.count} 个回应：${names}${reaction.mine ? "，点击移除" : "，点击添加"}`;
  return (
    <button
      type="button"
      className={[
        "reaction-pill",
        reaction.mine ? "mine" : "",
        burst > 0 ? `rx-in rx-in-${style.motion}` : "",
        leaving ? `rx-out rx-out-${style.motion}` : "",
      ]
        .filter(Boolean)
        .join(" ")}
      title={names}
      aria-label={label}
      aria-pressed={reaction.mine}
      onClick={() => {
        // Removing one's own last reaction: play the exit, then take it away.
        if (reaction.mine && !reduced && reaction.count === 1) {
          setLeaving(true);
          window.setTimeout(() => onToggle(reaction.emoji), LEAVE_MS);
          return;
        }
        onToggle(reaction.emoji);
      }}
    >
      <span className="reaction-emoji">{reaction.emoji}</span>
      <span className="reaction-count">{reaction.count}</span>
      {burst > 0 && <Burst family={family} seed={burst} />}
    </button>
  );
}

/** One burst of specks: built from the family and the trigger's count, then painted by the loop. */
function Burst({ family, seed }: { family: ReactionFamily; seed: number }) {
  const host = useRef<HTMLSpanElement>(null);
  const specks = useMemo(() => burst(family, seed), [family, seed]);
  const life = useMemo(() => burstLife(specks), [specks]);
  useEffect(() => {
    const nodes = host.current ? [...host.current.querySelectorAll<HTMLElement>(".rx-speck")] : [];
    return playBurst(nodes, specks);
  }, [specks]);
  return (
    <span ref={host} className="reaction-burst" aria-hidden="true" data-life={Math.ceil(life * 1000)}>
      {specks.map((speck, index) => (
        <span key={index} className={`rx-speck ${speck.look}`} style={speckStyle(speck)}>
          {speck.look === "emoji" || speck.look === "glyph" ? speck.glyph : ""}
        </span>
      ))}
    </span>
  );
}

/**
 * A speck's fixed look: its size, colour and — for the far ones — a softness. Depth is settled
 * here, once, so the loop only ever moves and fades it.
 */
function speckStyle(speck: Speck): React.CSSProperties {
  const size = speck.size * (0.7 + 0.55 * speck.z);
  const blur = speck.z < 0.72 ? (0.72 - speck.z) * 2.4 : 0;
  return {
    "--size": `${size.toFixed(1)}px`,
    "--tone": speck.tone || CONFETTI_TONES[speck.hue] || "var(--accent)",
    filter: blur > 0.05 ? `blur(${blur.toFixed(2)}px)` : undefined,
  } as React.CSSProperties;
}

/** The add-reaction button: on a message's top-right corner, or in a reply's tools row. */
/**
 * The emoji panel: a quick row, a search field, then the groups Unicode sorts emoji into. Every
 * emoji the platform knows is reachable — by opening its group or by typing part of its name.
 */
export function ReactionPanel({ onPick }: { onPick: (emoji: string) => void }) {
  const [tab, setTab] = useState(0);
  const [query, setQuery] = useState("");
  const search = useRef<HTMLInputElement>(null);
  const caption = useRef<HTMLDivElement>(null);
  const hoverName = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    search.current?.focus();
  }, []);
  const needle = query.trim();
  const group = EMOJI_GROUPS[tab] ?? EMOJI_GROUPS[0]!;
  const shown = useMemo(() => (needle ? findEmoji(needle) : group.entries), [needle, group]);
  // Nothing is drawn in the frame that opens the sheet; the quick row and the grid follow.
  const [started, setStarted] = useState(false);
  useEffect(() => {
    const frame = requestAnimationFrame(() => setStarted(true));
    return () => cancelAnimationFrame(frame);
  }, []);
  // A grid is filled a row at a time. The first time an emoji is drawn the system has to shape
  // and rasterize its glyph, and a whole group at once is a second of blocked paint. Each emoji
  // is also checked once against the system font on the way in, so one this Mac cannot draw
  // never shows up as an empty box.
  const [items, setItems] = useState<EmojiEntry[]>([]);
  const [filling, setFilling] = useState(true);
  useEffect(() => {
    setItems([]);
    setFilling(true);
    if (!started) return;
    let frame = 0;
    let at = 0;
    const kept: EmojiEntry[] = [];
    const grow = () => {
      const until = performance.now() + FRAME_BUDGET_MS;
      let added = 0;
      while (at < shown.length && added < FILL_STEP && performance.now() < until) {
        const entry = shown[at++]!;
        if (drawable(entry.emoji)) {
          kept.push(entry);
          added += 1;
        }
      }
      setItems(kept.slice());
      if (at < shown.length) frame = requestAnimationFrame(grow);
      else setFilling(false);
    };
    frame = requestAnimationFrame(grow);
    return () => cancelAnimationFrame(frame);
  }, [shown, started]);
  // The caption names whatever the pointer is over. Written straight to the node: a state change
  // per hover would re-render a grid of hundreds of buttons.
  const onHover = (event: React.PointerEvent) => {
    const option = (event.target as HTMLElement).closest<HTMLElement>(".reaction-option");
    if (!caption.current || !hoverName.current) return;
    const name = option?.dataset.name ?? "";
    hoverName.current.textContent = name;
    caption.current.dataset.hover = name ? "on" : "";
  };
  const option = (entry: EmojiEntry, key: string) => (
    <button
      key={key}
      type="button"
      className="reaction-option"
      aria-label={entry.name || entry.emoji}
      data-name={entry.name || entry.emoji}
      onClick={() => onPick(entry.emoji)}
    >
      {entry.emoji}
    </button>
  );
  return (
    <div className="reaction-panel" onPointerOver={onHover} onPointerLeave={() => caption.current && (caption.current.dataset.hover = "")}>
      <label className="reaction-search">
        <Icon name="search" size={14} />
        <input
          ref={search}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索表情，英文名，如 heart"
          aria-label="搜索表情"
          spellCheck={false}
        />
      </label>
      {!needle && (
        <div className="reaction-quick" role="group" aria-label="常用表情">
          {(started ? QUICK_ENTRIES : []).map((entry) => option(entry, entry.emoji))}
        </div>
      )}
      <div className="reaction-caption" ref={caption} aria-hidden="true">
        <span className="reaction-caption-group">
          {needle ? (filling ? "正在找…" : `${items.length} 个结果`) : group.label}
        </span>
        <span className="reaction-caption-name" ref={hoverName} />
      </div>
      <div
        key={needle ? "search" : group.id}
        className="reaction-grid"
        role="tabpanel"
        aria-label={needle ? "搜索结果" : group.label}
      >
        {items.map((entry, index) => option(entry, `${entry.emoji}-${index}`))}
        {needle && !filling && items.length === 0 && <p className="reaction-empty">没有找到这样的表情。</p>}
      </div>
      <div className="reaction-tabs" role="tablist" aria-label="表情分类">
        {EMOJI_GROUPS.map((item, index) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={!needle && index === tab}
            aria-label={item.label}
            title={item.label}
            className={!needle && index === tab ? "reaction-tab on" : "reaction-tab"}
            onClick={() => {
              setQuery("");
              setTab(index);
            }}
          >
            {GROUP_GLYPH[item.id]}
          </button>
        ))}
      </div>
    </div>
  );
}

/** Each group's tab, drawn as one of its own emoji. */
const GROUP_GLYPH: Record<EmojiGroupId, string> = {
  smileys: "😀",
  people: "👋",
  animals: "🐻",
  food: "🍔",
  travel: "🚗",
  activities: "⚽",
  objects: "🎁",
  symbols: "❤️",
  flags: "🚩",
};

/** The quick row, with names for its caption. */
const QUICK_ENTRIES: EmojiEntry[] = QUICK_EMOJI.map((emoji) => ({
  emoji,
  name: EMOJI_GROUPS.flatMap((group) => group.entries).find((entry) => entry.emoji === emoji)?.name ?? emoji,
}));

/** How long one frame may spend checking and adding emoji. */
const FRAME_BUDGET_MS = 6;

const DRAWABLE = new Map<string, boolean>();
let probe: CanvasRenderingContext2D | null | undefined;

/**
 * Whether the system can draw `emoji` as one colour glyph. The catalogue follows the newest
 * Unicode list, which is ahead of any shipping font: a missing emoji falls back to a plain-font
 * glyph (an empty box, a row of bars) or, for a sequence, to its parts side by side. Drawn in
 * magenta, a fallback glyph comes out magenta while a real emoji keeps its own colours, and a
 * sequence that fell apart is wider than one glyph.
 */
function drawable(emoji: string): boolean {
  const known = DRAWABLE.get(emoji);
  if (known !== undefined) return known;
  if (probe === undefined) {
    const canvas = document.createElement("canvas");
    canvas.width = PROBE_SIZE;
    canvas.height = PROBE_SIZE;
    probe = canvas.getContext("2d", { willReadFrequently: true });
    if (probe) {
      probe.font = `${PROBE_FONT}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`;
      probe.textAlign = "center";
      probe.textBaseline = "middle";
      probe.fillStyle = "#ff00ff";
    }
  }
  // No canvas (a test environment): show everything rather than nothing.
  if (!probe) return true;
  let ok = probe.measureText(emoji).width <= PROBE_FONT * 1.6;
  if (ok) {
    probe.clearRect(0, 0, PROBE_SIZE, PROBE_SIZE);
    probe.fillText(emoji, PROBE_SIZE / 2, PROBE_SIZE / 2);
    const data = probe.getImageData(0, 0, PROBE_SIZE, PROBE_SIZE).data;
    let painted = 0;
    let magenta = 0;
    for (let index = 0; index < data.length; index += 4) {
      if (data[index + 3]! < 160) continue;
      painted += 1;
      if (data[index]! > 200 && data[index + 1]! < 90 && data[index + 2]! > 200) magenta += 1;
    }
    ok = painted > 0 && magenta < painted * 0.8;
  }
  DRAWABLE.set(emoji, ok);
  return ok;
}

const PROBE_FONT = 24;
const PROBE_SIZE = 40;

/**
 * The sheet is a singleton: a request names the point and what a pick does, and one host at the
 * app's root renders it. Keeping its state out of the conversation is what makes opening it
 * cost nothing in a long chat — a state change there re-renders every message on screen.
 */
interface SheetRequest {
  x: number;
  y: number;
  onPick: (emoji: string) => void;
}
let present: ((request: SheetRequest | null) => void) | null = null;

export function openEmojiSheet(request: SheetRequest): void {
  present?.(request);
}

/** Mount once, at the root: it renders nothing until a sheet is opened. */
export function EmojiSheet() {
  const [request, setRequest] = useState<SheetRequest | null>(null);
  useEffect(() => {
    present = setRequest;
    return () => {
      present = null;
    };
  }, []);
  if (!request) return null;
  return (
    <ReactionMenu
      x={request.x}
      y={request.y}
      onPick={request.onPick}
      onClose={() => setRequest(null)}
    />
  );
}

/**
 * The panel as a floating sheet, opened from a message's right-click menu at the pointer. It
 * closes on a pick, on Escape, or on a click outside.
 *
 * The sheet is placed from the size it is allowed to take, never by measuring itself: measuring
 * would lay out the grid that was just mounted, and the first emoji of a group each need their
 * glyph shaped — a second of frozen UI behind a menu click.
 */
export function ReactionMenu({
  x,
  y,
  onPick,
  onClose,
}: {
  x: number;
  y: number;
  onPick: (emoji: string) => void;
  onClose: () => void;
}) {
  const sheet = useRef<HTMLDivElement>(null);
  // The sheet's own box from the stylesheet: never wider than 340, never taller than 420.
  const width = Math.min(SHEET_W, window.innerWidth - 32);
  const height = Math.min(SHEET_H, window.innerHeight - 24);
  const at = {
    left: Math.max(8, Math.min(x, window.innerWidth - width - 8)),
    top: y + height > window.innerHeight - 8 ? Math.max(8, y - height) : y,
  };
  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (!sheet.current?.contains(event.target as Node)) onClose();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("mousedown", onDown, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  // On <body>, like the context menu: the chat's own stacking and clip cannot cut into it, and
  // the sheet repaints on its own layer instead of redrawing the message list underneath.
  return createPortal(
    <div
      ref={sheet}
      className="reaction-picker floating"
      role="dialog"
      aria-label="选择表情回应"
      style={{ left: at.left, top: at.top }}
    >
      <ReactionPanel
        onPick={(emoji) => {
          onPick(emoji);
          onClose();
        }}
      />
    </div>,
    document.body,
  );
}
