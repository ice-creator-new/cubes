// The live side of the reveal: one requestAnimationFrame loop per streaming reply. It walks the
// visible length toward the text the server has sent and records each frame's step as a batch
// (where it starts, when it was revealed) so that batch can fade in on its own clock.
//
// Only text that arrives while the reply is on screen animates. A reply that mounts with a lot of
// text already there (history, a reload, switching back mid-stream) shows that text as it is;
// reduced motion shows everything at once.

import { useEffect, useRef, useState } from "react";
import { prefersReducedMotion } from "./motion";
import { liveBatches, nextVisible, type Batch } from "./streamReveal";

export interface Reveal {
  /** The part of the text on screen now. */
  text: string;
  /** Batches whose fade is still running, ascending; empty once the reply has settled. */
  batches: readonly Batch[];
}

/** A reply mounting with at most this much text is fresh: its first words fade in too. */
export const FRESH_CHARS = 160;

export function useStreamReveal(text: string, streaming: boolean): Reveal {
  const [animated] = useState(() => streaming && !prefersReducedMotion());
  const shown = useRef(animated && text.length <= FRESH_CHARS ? 0 : text.length);
  const batches = useRef<Batch[]>([]);
  const target = useRef(text);
  target.current = text;
  const [, repaint] = useState(0);
  const running = useRef(false);
  const raf = useRef(0);

  // Runs after every render: a new delta (a longer `text`) wakes the loop if it had gone idle.
  useEffect(() => {
    if (!animated || running.current) return;
    if (shown.current >= text.length && batches.current.length === 0) return;
    running.current = true;
    let last = performance.now();
    const tick = (now: number) => {
      const before = shown.current;
      const next = nextVisible(target.current, before, (now - last) / 1000);
      last = now;
      const live = liveBatches(batches.current, now);
      if (next > before) live.push({ at: before, when: now });
      const changed = next !== before || live.length !== batches.current.length;
      shown.current = next;
      batches.current = live;
      if (changed) repaint((frame) => frame + 1);
      if (next < target.current.length || live.length > 0) {
        raf.current = requestAnimationFrame(tick);
      } else {
        // Caught up and every fade is over: sleep until the next delta.
        running.current = false;
      }
    };
    raf.current = requestAnimationFrame(tick);
  });

  useEffect(
    () => () => {
      cancelAnimationFrame(raf.current);
      running.current = false;
    },
    [],
  );

  if (!animated) return { text, batches: [] };
  return { text: text.slice(0, Math.min(shown.current, text.length)), batches: batches.current };
}
