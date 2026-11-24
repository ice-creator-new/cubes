// The pieces of motion CSS cannot do alone: a measured height that glides, presence that
// outlives the value being cleared, a thumb that follows the selected segment, and a position
// match for rows that move in the rail. Everything here snaps when the system asks for reduced
// motion, so the CSS transition it drives is skipped rather than merely shortened.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { DependencyList, RefObject } from "react";

/** Same curve as `--ease`; the layout effects below cannot read a custom property. */
export const EASE = "cubic-bezier(0.22, 1, 0.36, 1)";
export const T_FAST = 120;
export const T_BASE = 200;
export const T_SLOW = 320;

/** How long a key repeat may keep adding lines before the field stops gliding. */
const KEY_REPEAT_MS = 90;

export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export interface HeightMotion {
  animate: boolean;
  duration: number;
}

/**
 * Decide how one measured height change should land.
 *
 * A key repeat adds a single line every few milliseconds; giving each of those steps its own
 * transition makes the field trail the keys, so those snap. A deliberate edit — a paste, the
 * first letter after a pause, the cleared draft after a send — glides.
 */
export function heightMotion({
  from,
  to,
  elapsed,
  line,
  reduced = false,
}: {
  /** Height on screen before this measurement; null on the very first one. */
  from: number | null;
  to: number;
  elapsed: number;
  /** One line of the field, so "one line" is a real number rather than a guess. */
  line: number;
  reduced?: boolean;
}): HeightMotion {
  if (reduced) return { animate: false, duration: 0 };
  if (from === null || Math.abs(to - from) < 0.5) return { animate: false, duration: 0 };
  if (elapsed < KEY_REPEAT_MS && Math.abs(to - from) <= line * 1.5) {
    return { animate: false, duration: 0 };
  }
  return { animate: true, duration: T_BASE };
}

/**
 * Keep a text field's height (a textarea, or the composer's editable box) in step with its
 * content, gliding from the height on screen to the freshly measured one. `scrollHeight`
 * answers for growth on its own; a draft that got shorter is measured with the transition off
 * and `height: auto`, in one task, so no intermediate frame is ever painted. Reaching the height
 * it is already heading to leaves the glide untouched.
 */
export function useAnimatedHeight(
  ref: RefObject<HTMLElement | null>,
  text: string,
  {
    max = Number.POSITIVE_INFINITY,
    onChange,
  }: { max?: number; onChange?: (height: number) => void } = {},
): void {
  const previous = useRef<{ height: number; at: number } | null>(null);
  const lastText = useRef<string | null>(null);
  const notify = useRef(onChange);
  notify.current = onChange;

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const now = performance.now();
    const before = previous.current;
    // A draft that lost characters may have lost a line; scrollHeight alone cannot show that,
    // because it never reports less than the box.
    const shrank = lastText.current !== null && text.length < lastText.current.length;
    lastText.current = text;
    const box = element.clientHeight;
    let target = Math.min(element.scrollHeight, max);

    if (!(element.scrollHeight > box + 0.5)) {
      // The box is at least as tall as the text, so scrollHeight is clamped. If it still sits
      // where the last measurement put it and nothing was deleted, nothing moved.
      if (!shrank && before && Math.abs(before.height - box) < 0.5) return;
      element.style.transition = "none";
      element.style.height = "auto";
      target = Math.min(element.scrollHeight, max);
      element.style.height = `${box}px`;
      element.style.transition = "";
    }

    // Already heading there: a keystroke inside one line must not restart the glide.
    if (before && Math.abs(target - before.height) < 0.5) return;

    const line = parseFloat(getComputedStyle(element).lineHeight) || 22;
    const decision = heightMotion({
      from: before?.height ?? null,
      to: target,
      elapsed: now - (before?.at ?? now),
      line,
      reduced: prefersReducedMotion(),
    });

    if (!decision.animate) {
      element.style.transition = "none";
      element.style.height = `${target}px`;
      element.style.transition = "";
      previous.current = { height: target, at: now };
      notify.current?.(target);
      return;
    }

    element.style.transition = "none";
    element.style.height = `${box}px`;
    // Commit the starting height so the transition has somewhere to come from.
    void element.offsetHeight;
    element.style.transition = `height ${decision.duration}ms ${EASE}`;
    element.style.height = `${target}px`;
    previous.current = { height: target, at: now };
    notify.current?.(target);

    const settle = window.setTimeout(() => {
      element.style.transition = "";
    }, decision.duration + 40);
    return () => window.clearTimeout(settle);
  }, [text, max]);
}

export interface Presence<T> {
  /** The current value, or the last one while it is on its way out. */
  value: T | null;
  /** False only while the exit plays. */
  open: boolean;
}

/**
 * Keep a value rendered long enough to animate it away. The wrapper that owns the transition
 * must stay mounted; only the contents come and go.
 */
export function usePresence<T>(value: T | null, duration: number = T_BASE): Presence<T> {
  const entering = value !== null;
  const [open, setOpen] = useState(entering);
  const [held, setHeld] = useState<T | null>(value);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    if (entering) {
      setHeld(value);
      setOpen(true);
      return;
    }
    setOpen(false);
    timer.current = window.setTimeout(
      () => {
        timer.current = null;
        setHeld(null);
      },
      prefersReducedMotion() ? 0 : duration,
    );
  }, [entering, duration]); // the value itself is read live below

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return { value: entering ? value : held, open: entering || open };
}

/** A close that plays out before it happens. */
export function useExitTransition(
  onClose: () => void,
  duration: number = T_FAST,
): { closing: boolean; requestClose: () => void } {
  const [closing, setClosing] = useState(false);
  const latest = useRef(onClose);
  latest.current = onClose;
  const started = useRef(false);
  const timer = useRef<number | null>(null);

  const requestClose = useCallback(() => {
    if (started.current) return;
    if (prefersReducedMotion()) {
      latest.current();
      return;
    }
    started.current = true;
    setClosing(true);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      latest.current();
    }, duration);
  }, [duration]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return { closing, requestClose };
}

export interface MotionRect {
  left: number;
  top: number;
}

/**
 * Where a box used to be, relative to where it is now. Anything under half a pixel is left
 * alone: matching rows must not shimmer when a list is rebuilt.
 */
export function flipOffset(
  previous: MotionRect,
  next: MotionRect,
  threshold = 0.5,
): { dx: number; dy: number } | null {
  const dx = previous.left - next.left;
  const dy = previous.top - next.top;
  if (Math.abs(dx) < threshold && Math.abs(dy) < threshold) return null;
  return { dx, dy };
}

/**
 * Let elements marked `data-flip-key` glide from where they were to where they are now. The
 * caller decides when a move is worth animating by choosing `deps`: a layout change, not every
 * re-render, so nothing dances while messages arrive.
 */
export function useFlip(
  container: RefObject<HTMLElement | null>,
  deps: DependencyList,
  { duration = 240, enabled = true }: { duration?: number; enabled?: boolean } = {},
): void {
  const previous = useRef<Map<string, MotionRect> | null>(null);

  useLayoutEffect(() => {
    const root = container.current;
    // Nothing to compare against until the list has settled into its real shape: that first
    // pass only records, so a window opening never shuffles itself into place.
    if (!root || !enabled) {
      if (!enabled) previous.current = null;
      return;
    }
    const nodes = Array.from(root.querySelectorAll<HTMLElement>("[data-flip-key]"));
    const next = new Map<string, MotionRect>();
    for (const node of nodes) {
      const key = node.dataset.flipKey;
      if (!key) continue;
      // Read where layout put the box, not where a glide is currently showing it: a second
      // layout pass while the first is still moving must not compound the distance.
      const rect = node.getBoundingClientRect();
      const transform = getComputedStyle(node).transform;
      const glide = transform && transform !== "none" ? new DOMMatrixReadOnly(transform) : null;
      next.set(key, { left: rect.left - (glide?.m41 ?? 0), top: rect.top - (glide?.m42 ?? 0) });
    }
    const before = previous.current;
    previous.current = next;
    if (!before || prefersReducedMotion()) return;

    for (const node of nodes) {
      const key = node.dataset.flipKey;
      const start = key ? before.get(key) : undefined;
      const end = key ? next.get(key) : undefined;
      if (!start || !end) continue;
      // Already on its way there: let that glide finish.
      if (node.getAnimations().length > 0) continue;
      const offset = flipOffset(start, end);
      if (!offset) continue;
      node.animate(
        [
          { transform: `translate(${offset.dx}px, ${offset.dy}px)` },
          { transform: "translate(0px, 0px)" },
        ],
        { duration, easing: EASE },
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the caller owns the trigger list.
  }, deps);
}

/**
 * Track the selected segment so a thumb can slide under it. The first measurement is applied
 * without a transition, so a freshly opened dialog never slides its thumb in from the corner.
 */
export function useSegmentedThumb(
  container: RefObject<HTMLElement | null>,
  activeKey: string,
): { thumb: { x: number; width: number } | null; ready: boolean } {
  const [thumb, setThumb] = useState<{ x: number; width: number } | null>(null);
  const [ready, setReady] = useState(false);

  useLayoutEffect(() => {
    const root = container.current;
    if (!root) return;
    const measure = () => {
      const target = root.querySelector<HTMLElement>(`[data-seg="${CSS.escape(activeKey)}"]`);
      if (!target) {
        setThumb(null);
        return;
      }
      setThumb({ x: target.offsetLeft, width: target.offsetWidth });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
  }, [container, activeKey]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => setReady(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  return { thumb, ready };
}
