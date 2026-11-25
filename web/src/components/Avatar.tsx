// One shared requestAnimationFrame loop drives every mounted avatar. Frames are written
// straight to SVG attributes through refs, so animation never re-renders React and off-screen
// faces pause. An avatar that is not awake paints until it settles into the rest pose, then
// the loop skips it — and when nothing is left to paint the loop stops altogether.
//
// Pointer tracking is one passive `pointermove` listener on the window: it only records the
// position. The frame loop turns that into a look target for each visible avatar from a
// cached box, refreshed on scroll and resize. `track="region"` only aims an avatar while the
// pointer is inside its own row, so a whole sidebar of resting bots ignores the pointer.

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Agent, Appearance } from "../api/types";
import { resolveAppearance } from "../bot/catalog";
import { createBot, type Bot, type Frame, type Look } from "../bot/engine";
import { idleSeconds, noteActivity, SLEEP_AFTER } from "../bot/activity";
import type { Mood } from "../lib/mood";
import { useTeam } from "../state/store";

/** How an avatar pays attention to the pointer: the old distance rule, its own row, or never. */
export type Track = "near" | "region" | "off";

interface LiveAvatar {
  visible: boolean;
  origin: number;
  size: number;
  host: HTMLElement;
  rect: DOMRect | null;
  track: Track;
  /** The nearest `[data-track-region]` ancestor, found once on mount, for `track: "region"`. */
  region: Element | null;
  regionRect: DOMRect | null;
  bot: Bot;
  /** The props' opinion: whether this avatar should be painting at all. */
  awake: boolean;
  /** Nothing left to paint until something changes. */
  settled: boolean;
  paint: (t: number, look: Look | null, idle: number) => void;
}

const REGISTRY = new Set<LiveAvatar>();
const ENTRIES = new WeakMap<Element, LiveAvatar>();
let ticking = false;
let rectsDirty = true;

const POINTER = { x: 0, y: 0, inside: false };
/** Where an author line's face looks while it is speaking: toward its own text, down-right. */
const TEXT_LOOK: Look = { yaw: 20, pitch: -14, mix: 1, spin: 0, wander: 0.35 };
/** Extra distance, in CSS px, at which an avatar stops paying attention to the pointer. */
const REACH = 360;

function refreshRects() {
  rectsDirty = false;
  for (const entry of REGISTRY) {
    entry.rect = entry.visible ? entry.host.getBoundingClientRect() : null;
    entry.regionRect = entry.visible && entry.region ? entry.region.getBoundingClientRect() : null;
  }
}

/** Whether the pointer is inside the box, used to gate region tracking. */
function insideRect(rect: DOMRect | null, x: number, y: number): boolean {
  if (!rect || rect.width <= 0 || rect.height <= 0) return false;
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

/** Where this avatar should look, from the pointer and its size. Nothing at 24px or below. */
function lookFor(entry: LiveAvatar): Look | null {
  if (entry.track === "off" || !POINTER.inside || entry.size < 28 || !entry.rect) return null;
  // A row only aims its own bot: the pointer has to be inside that row (or pin card) for the
  // face to follow it at all.
  if (entry.track === "region" && !insideRect(entry.regionRect, POINTER.x, POINTER.y)) return null;
  const rect = entry.rect;
  if (rect.width <= 0 || rect.height <= 0) return null;
  const dx = POINTER.x - (rect.left + rect.width / 2);
  const dy = POINTER.y - (rect.top + rect.height / 2);
  const distance = Math.hypot(dx, dy);
  if (!Number.isFinite(distance) || distance > REACH) return null;
  const reach = Math.max(120, entry.size * 1.6);
  const nx = Math.max(-1, Math.min(1, dx / reach));
  const ny = Math.max(-1, Math.min(1, dy / reach));
  // Inside its own row the bot gives the pointer its full attention (and squares up to it);
  // the old distance rule fades attention out with distance.
  const mix =
    entry.track === "region" ? 1 : Math.max(0, Math.min(1, (REACH - distance) / (REACH * 0.45)));
  return {
    yaw: nx * 16,
    pitch: -ny * 13,
    mix,
    spin: 0,
    // While the pointer moves the head is aimed; when it stops the drift comes back.
    wander: 0.35,
  };
}

function step(now: number) {
  if (rectsDirty) refreshRects();
  const seconds = now / 1000;
  const idle = idleSeconds();
  let busy = false;
  for (const entry of REGISTRY) {
    if (!entry.visible || entry.settled) continue;
    const t = seconds - entry.origin;
    entry.paint(t, lookFor(entry), idle);
    // A resting avatar settles exactly onto the rest pose; from there the loop can skip it.
    if (!entry.awake && entry.bot.resting(t)) entry.settled = true;
    else busy = true;
  }
  if (REGISTRY.size && busy) requestAnimationFrame(step);
  else ticking = false;
}

/** Make sure the loop is running: waking, gestures, a new pointer position, a re-join. */
function kick() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(step);
}

const observer =
  typeof IntersectionObserver === "undefined"
    ? null
    : new IntersectionObserver((records) => {
        for (const record of records) {
          const entry = ENTRIES.get(record.target);
          if (!entry) continue;
          entry.visible = record.isIntersecting;
          if (entry.visible) {
            entry.rect = record.boundingClientRect;
            entry.settled = false;
            kick();
          }
        }
      });

let listening = false;

function listen() {
  if (listening || typeof window === "undefined") return;
  listening = true;
  const move = (event: PointerEvent) => {
    POINTER.x = event.clientX;
    POINTER.y = event.clientY;
    POINTER.inside = true;
    noteActivity();
    // A resting avatar is skipped by the loop; a fresh pointer position may wake it up.
    kick();
  };
  const leave = () => {
    POINTER.inside = false;
  };
  const invalidate = () => {
    rectsDirty = true;
    kick();
  };
  window.addEventListener("pointermove", move, { passive: true });
  window.addEventListener("pointerdown", move, { passive: true });
  document.addEventListener("pointerleave", leave);
  window.addEventListener("blur", leave);
  window.addEventListener("scroll", invalidate, { passive: true, capture: true });
  window.addEventListener("resize", invalidate);
}

function join(element: Element, entry: LiveAvatar) {
  listen();
  REGISTRY.add(entry);
  ENTRIES.set(element, entry);
  observer?.observe(element);
  rectsDirty = true;
  kick();
}

function leave(element: Element, entry: LiveAvatar) {
  REGISTRY.delete(entry);
  ENTRIES.delete(element);
  observer?.unobserve(element);
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

const DOT_SLOTS = 4;
/** The most thickness slices a frame draws (`LAYERS_FULL` in engine.ts). */
const LAYER_SLOTS = 8;
/** Space between the notify pastille and the body it is cut out of, in viewBox units. */
const NOTCH_GAP = 2.6;

/** A member's face: the silhouette and colour come from the stored appearance or the seed;
 *  the expression is its own — the mood tag and what it is doing drive it. */
export function Avatar({
  seed,
  appearance,
  size,
  name,
  mood = "idle",
  frozen = false,
  agentID,
  towardText = false,
  awake = true,
  track = "near",
}: {
  seed: string | null | undefined;
  appearance: Appearance | null | undefined;
  size: number;
  name?: string;
  mood?: Mood;
  /** An appearance preview: no gestures, no sleep, no pointer tracking. */
  frozen?: boolean;
  /** Enables gestures and, at 28px and up, pointer tracking. */
  agentID?: string;
  /** Author lines next to a streaming reply: the speaker looks toward its own text. */
  towardText?: boolean;
  /**
   * `false` settles the face onto its still rest pose and stops painting. Hovering, being
   * selected or being busy is the caller's business; the change itself is a ramp.
   */
  awake?: boolean;
  /** "near" (the old distance rule), "region" (only inside its own row), or "off". */
  track?: Track;
}) {
  const resolved = useMemo(() => resolveAppearance(appearance, seed), [appearance, seed]);
  const detail = size <= 24 ? "low" : "full";
  const reduced = useReducedMotion();
  const clipId = useId().replace(/:/g, "");
  // Reduced motion keeps every face at rest; a frozen preview still wakes on hover.
  const awakeNow = reduced ? false : awake;
  const interactive = !frozen && !reduced;

  // One bot for the life of the avatar. Appearance, mood and waking all ease from whatever is
  // already on screen, so a preview that comes alive under the pointer ramps instead of
  // jumping to a new face. The initial values are read once, on purpose.
  const bot = useMemo(
    () => createBot({ appearance: resolved, seed: seed ?? "", detail, mood, awake: awakeNow }),
    [seed, detail],
  );
  const botRef = useRef<Bot>(bot);
  botRef.current = bot;
  const awakeRef = useRef(awakeNow);
  awakeRef.current = awakeNow;
  const interactiveRef = useRef(interactive);
  interactiveRef.current = interactive;

  const pending = useTeam((state) => (agentID ? state.gestures[agentID] : undefined));
  const seenGesture = useRef<number | null>(pending?.at ?? null);

  const origin = useRef(performance.now() / 1000);
  const elapsed = () => performance.now() / 1000 - origin.current;

  const host = useRef<HTMLSpanElement>(null);
  const group = useRef<SVGGElement>(null);
  const body = useRef<SVGPathElement>(null);
  const clip = useRef<SVGPathElement>(null);
  const eyes = useRef<SVGGElement>(null);
  const leftEye = useRef<SVGPathElement>(null);
  const rightEye = useRef<SVGPathElement>(null);
  const dots = useRef<SVGGElement>(null);
  const dotSlots = useRef<(SVGCircleElement | null)[]>([]);
  const notif = useRef<SVGGElement>(null);
  const notifDot = useRef<SVGCircleElement>(null);
  const shade = useRef<SVGLinearGradientElement>(null);
  const shadeStops = useRef<(SVGStopElement | null)[]>([]);
  // The gap around the pastille is cut out of the body (bloub's notch), so the page shows
  // through and the pastille reads as a clean disc sitting just off the shoulder.
  const notchHole = useRef<SVGCircleElement>(null);
  const shadow = useRef<SVGEllipseElement>(null);
  const side = useRef<SVGGElement>(null);
  const cheeks = useRef<(SVGCircleElement | null)[]>([]);
  const layerSlots = useRef<(SVGPathElement | null)[]>([]);
  const mouth = useRef<SVGGElement>(null);
  const lips = useRef<SVGPathElement>(null);
  const lipsClip = useRef<SVGPathElement>(null);
  const tongue = useRef<SVGEllipseElement>(null);
  const sleeping = useRef(false);

  const setPath = (node: SVGPathElement | null, d: string) => {
    if (node && node.getAttribute("d") !== d) node.setAttribute("d", d);
  };

  const setAttr = (node: Element | null, name: string, value: string) => {
    if (node && node.getAttribute(name) !== value) node.setAttribute(name, value);
  };

  /** The rim light: one gradient in body space, painted as the body's fill. */
  const paintShade = (frame: Frame) => {
    const rim = frame.shade;
    setAttr(body.current, "fill", `url(#${clipId}-shade)`);
    setAttr(shade.current, "x1", String(rim.x1));
    setAttr(shade.current, "y1", String(rim.y1));
    setAttr(shade.current, "x2", String(rim.x2));
    setAttr(shade.current, "y2", String(rim.y2));
    rim.stops.forEach((stop, index) => {
      const node = shadeStops.current[index] ?? null;
      setAttr(node, "offset", String(stop.offset));
      setAttr(node, "stop-color", stop.color);
    });
  };

  const paintNow = (t: number, pointerLook: Look | null, idle: number) => {
    const current = botRef.current;
    const live = interactiveRef.current;
    // A gesture is an event worth being awake for, so it wakes a resting bot for its length
    // and the bot settles again once the gesture is over.
    current.setAwake(awakeRef.current || current.gestureActive(t), t);
    const aim = live ? (towardText ? TEXT_LOOK : pointerLook) : null;
    // Always dated, even at `null`: that is what releases the head back to its drift.
    current.look(aim, t);
    // A pointer that is merely nearby does not count as attention; one that is aimed does.
    const attended = live && (towardText || (pointerLook !== null && pointerLook.mix > 0.15));

    // Sleep is a visible-idle state: two minutes with nothing happening and nobody looking.
    if (live) {
      if (!sleeping.current && idle >= SLEEP_AFTER && !attended) {
        sleeping.current = true;
        current.gesture("sleep", t);
      } else if (sleeping.current && (idle < SLEEP_AFTER || attended)) {
        sleeping.current = false;
        current.gesture("wake", t);
      }
    }

    const frame = current.sample(t);
    // The turn in depth is already in the transforms: the face plane and the slices behind it
    // are each projected by the engine, so the side of the body shows as it turns.
    group.current?.setAttribute("transform", frame.transform);
    setPath(body.current, frame.body);
    paintShade(frame);
    setPath(clip.current, frame.body);
    eyes.current?.setAttribute("fill", frame.eyeFill);
    const nodes = [leftEye.current, rightEye.current];
    frame.eyes.forEach((eye, index) => {
      const node = nodes[index];
      if (!node) return;
      setPath(node, eye.d);
      node.setAttribute("transform", eye.matrix);
      node.setAttribute("opacity", String(eye.alpha));
    });

    // The body's thickness: the silhouette at planes behind the face, one solid colour.
    setAttr(side.current, "fill", frame.sideFill);
    setAttr(side.current, "opacity", String(frame.sideAlpha));
    layerSlots.current.forEach((node, index) => {
      const slice = frame.layers[index];
      setPath(node, slice ? frame.body : "");
      if (slice) setAttr(node, "transform", slice);
    });

    frame.cheeks.forEach((cheek, index) => {
      const node = cheeks.current[index] ?? null;
      setAttr(node, "transform", cheek.matrix);
      setAttr(node, "opacity", String(cheek.alpha));
      setAttr(node, "fill", frame.blushFill);
    });

    setAttr(mouth.current, "transform", frame.mouth.matrix);
    setAttr(mouth.current, "opacity", String(frame.mouth.alpha));
    setPath(lips.current, frame.mouth.d);
    setPath(lipsClip.current, frame.mouth.d);
    setAttr(lips.current, "fill", frame.eyeFill);
    setAttr(lips.current, "stroke", frame.eyeFill);
    setAttr(lips.current, "stroke-width", String(frame.mouth.stroke));
    const lick = frame.mouth.tongue;
    setAttr(tongue.current, "cx", String(lick.cx));
    setAttr(tongue.current, "cy", String(lick.cy));
    setAttr(tongue.current, "rx", String(lick.rx));
    setAttr(tongue.current, "ry", String(lick.ry));
    setAttr(tongue.current, "opacity", String(lick.opacity));
    setAttr(tongue.current, "fill", frame.tongueFill);

    // Under 40px the contact shadow is a grey smudge a few pixels wide, not a shadow.
    const ground = size >= 40 ? frame.shadow : null;
    setAttr(shadow.current, "opacity", ground ? String(ground.opacity) : "0");
    if (ground) {
      setAttr(shadow.current, "cx", String(ground.cx));
      setAttr(shadow.current, "cy", String(ground.cy));
      setAttr(shadow.current, "rx", String(ground.rx));
      setAttr(shadow.current, "ry", String(ground.ry));
    }

    const slots = dotSlots.current;
    frame.dots.forEach((dot, index) => {
      const circle = slots[index];
      if (!circle) return;
      circle.setAttribute("cx", String(dot.cx));
      circle.setAttribute("cy", String(dot.cy));
      circle.setAttribute("r", String(dot.r));
      circle.setAttribute("opacity", String(dot.opacity));
      circle.setAttribute("fill", dot.tone === "body" ? frame.fill : frame.eyeFill);
    });
    for (let index = frame.dots.length; index < slots.length; index += 1) {
      slots[index]?.setAttribute("r", "0");
      slots[index]?.setAttribute("opacity", "0");
    }

    const pastille = frame.notif;
    notif.current?.setAttribute("opacity", pastille ? "1" : "0");
    if (pastille) {
      notifDot.current?.setAttribute("cx", String(pastille.x));
      notifDot.current?.setAttribute("cy", String(pastille.y));
      notifDot.current?.setAttribute("r", String(pastille.r));
      // The UI accent, so "someone wants you" reads the same on every body colour and theme.
      notifDot.current?.setAttribute("fill", "var(--accent)");
      notchHole.current?.setAttribute("cx", String(pastille.x));
      notchHole.current?.setAttribute("cy", String(pastille.y));
      notchHole.current?.setAttribute("r", String(pastille.r + NOTCH_GAP));
    } else {
      notchHole.current?.setAttribute("r", "0");
    }
  };

  // The shared loop holds one entry per avatar for as long as it is mounted, so it must call
  // through a ref: a prop like `towardText` can change without re-joining the loop.
  const paint = useRef(paintNow);
  paint.current = paintNow;

  /** Clear the settled flag and make sure the loop will paint this avatar again. */
  const unsettle = () => {
    const element = host.current;
    const entry = element ? ENTRIES.get(element) : undefined;
    if (!entry) return;
    entry.settled = false;
    kick();
  };

  // Keep the bot on the current props; a changed face eases from what is already on screen.
  useEffect(() => {
    bot.set({ appearance: resolved, mood }, elapsed());
    unsettle();
  }, [bot, mood, resolved]);

  // The props decide whether this face is alive; the change itself ramps inside the engine.
  useEffect(() => {
    const element = host.current;
    const entry = element ? ENTRIES.get(element) : undefined;
    if (entry) entry.awake = awakeNow;
    unsettle();
  }, [awakeNow]);

  // A gesture fired while this avatar was unmounted is never replayed on mount.
  useEffect(() => {
    if (!interactive || !pending || pending.at === seenGesture.current) return;
    seenGesture.current = pending.at;
    sleeping.current = false;
    bot.gesture(pending.kind, elapsed());
    unsettle();
  }, [bot, interactive, pending]);

  // Draw before the browser paints, then join the shared loop (which skips a settled face).
  useLayoutEffect(() => {
    paintNow(elapsed(), null, idleSeconds());
  }, [bot, mood, resolved]);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const entry: LiveAvatar = {
      visible: false,
      origin: origin.current,
      size,
      host: element,
      rect: null,
      track,
      region: track === "region" ? element.closest("[data-track-region]") : null,
      regionRect: null,
      bot,
      awake: awakeNow,
      settled: !awakeNow,
      paint: (t, look, idle) => paint.current(t, look, idle),
    };
    join(element, entry);
    return () => leave(element, entry);
  }, [bot, size, track]);

  const label = name ? `${name}的头像` : "头像";

  return (
    <span
      ref={host}
      className="avatar"
      style={{ width: size, height: size }}
      {...(frozen ? { "aria-hidden": true } : { role: "img", "aria-label": label })}
    >
      <svg viewBox="0 0 100 100" width={size} height={size} aria-hidden="true">
        <defs>
          <clipPath id={clipId}>
            <path ref={clip} />
          </clipPath>
          <linearGradient
            ref={shade}
            id={`${clipId}-shade`}
            gradientUnits="userSpaceOnUse"
            x1={0}
            y1={0}
            x2={0}
            y2={100}
          >
            {[0, 1, 2, 3].map((index) => (
              <stop
                key={index}
                ref={(node) => {
                  shadeStops.current[index] = node;
                }}
              />
            ))}
          </linearGradient>
          <radialGradient id={`${clipId}-ground`}>
            <stop offset={0} stopColor="#000" stopOpacity={1} />
            <stop offset={1} stopColor="#000" stopOpacity={0} />
          </radialGradient>
          <clipPath id={`${clipId}-lips`}>
            <path ref={lipsClip} />
          </clipPath>
          <mask id={`${clipId}-notch`} maskUnits="userSpaceOnUse" x={-50} y={-50} width={200} height={200}>
            <rect x={-50} y={-50} width={200} height={200} fill="white" />
            <circle ref={notchHole} r={0} fill="black" />
          </mask>
        </defs>
        <ellipse ref={shadow} fill={`url(#${clipId}-ground)`} opacity={0} />
        <g mask={`url(#${clipId}-notch)`}>
          <g ref={side}>
            {Array.from({ length: LAYER_SLOTS }, (_, index) => (
              <path
                key={index}
                ref={(node) => {
                  layerSlots.current[index] = node;
                }}
              />
            ))}
          </g>
          <g ref={group}>
            <path ref={body} />
            <g clipPath={`url(#${clipId})`}>
              {[0, 1].map((index) => (
                <circle
                  key={index}
                  r={1}
                  opacity={0}
                  ref={(node) => {
                    cheeks.current[index] = node;
                  }}
                />
              ))}
              <g ref={eyes}>
                <path ref={leftEye} />
                <path ref={rightEye} />
              </g>
              <g ref={mouth}>
                <path ref={lips} strokeLinecap="round" strokeLinejoin="round" />
                <ellipse ref={tongue} clipPath={`url(#${clipId}-lips)`} opacity={0} />
              </g>
            </g>
          </g>
        </g>
        <g ref={dots}>
          {Array.from({ length: DOT_SLOTS }, (_, index) => (
            <circle
              key={index}
              r={0}
              opacity={0}
              ref={(node) => {
                dotSlots.current[index] = node;
              }}
            />
          ))}
        </g>
        <g ref={notif} opacity={0}>
          <circle ref={notifDot} r={0} />
        </g>
      </svg>
    </span>
  );
}

/** A group's face: up to four member faces; past four, three faces and a `+N`. */
export function GroupAvatar({
  members,
  size,
  awake = true,
  track = "near",
}: {
  members: Agent[];
  size: number;
  awake?: boolean;
  track?: Track;
}) {
  const compact = members.length > 4;
  const shown = members.slice(0, compact ? 3 : 4);
  const overflow = compact ? members.length - shown.length : 0;
  const count = Math.max(1, shown.length);
  const moods = useTeam((state) => state.moods);
  const face = Math.round(size * 0.62);
  const spread = size * 0.2;
  const slots: Record<number, [number, number][]> = {
    1: [[0, 0]],
    2: [[-1, 0.25], [1, -0.2]],
    3: [[0, -0.85], [-1, 0.7], [1, 0.7]],
    4: [[0.95, -0.75], [-1, 0.15], [0.45, 0.95], [-0.2, -0.95]],
  };
  const points = slots[Math.min(4, count)] ?? [[0, 0]];
  const place = (index: number) => {
    const [x, y] = points[Math.min(index, points.length - 1)] ?? [0, 0];
    return { transform: `translate(${x * spread}px, ${y * spread}px)` };
  };
  const badge = Math.max(13, Math.round(size * 0.42));
  return (
    <span
      className="group-avatar"
      style={{ width: size, height: size }}
      title={`${members.length} 位成员`}
    >
      {shown.map((member, index) => (
        <span key={member.id} className="group-avatar-slot" style={place(index)}>
          <Avatar
            seed={member.avatar_seed}
            appearance={member.appearance}
            size={face}
            name={member.name}
            agentID={member.id}
            mood={moods[member.id] ?? "idle"}
            awake={awake}
            track={track}
          />
        </span>
      ))}
      {overflow > 0 && (
        <span
          className="group-avatar-more"
          style={{
            width: badge,
            height: badge,
            fontSize: Math.max(8, Math.round(size * 0.27)),
          }}
        >
          +{overflow}
        </span>
      )}
    </span>
  );
}
