// One requestAnimationFrame loop paints every burst on screen. `sample` in reactionFx says where
// a speck is at a time; this only writes it to the node, so a burst never re-renders React and
// the loop stops once the last speck has died. Several bursts at once cost one loop.

import { burstLife, sample, type Speck } from "./reactionFx";

interface Run {
  nodes: HTMLElement[];
  specks: Speck[];
  start: number;
  end: number;
  done: () => void;
}

const RUNS = new Set<Run>();
let ticking = false;

function paint(run: Run, seconds: number) {
  run.specks.forEach((speck, index) => {
    const node = run.nodes[index];
    if (!node) return;
    const frame = sample(speck, seconds);
    node.style.transform = `translate3d(${frame.x.toFixed(2)}px, ${frame.y.toFixed(2)}px, 0) rotate(${frame.rot.toFixed(1)}deg) scale(${frame.scale.toFixed(3)})`;
    node.style.opacity = frame.opacity.toFixed(3);
  });
}

function step(now: number) {
  let busy = false;
  for (const run of [...RUNS]) {
    const seconds = (now - run.start) / 1000;
    if (seconds >= run.end) {
      RUNS.delete(run);
      run.done();
      continue;
    }
    busy = true;
    paint(run, seconds);
  }
  if (busy) requestAnimationFrame(step);
  else ticking = false;
}

function kick() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(step);
}

/**
 * Paint `specks` into `nodes`, starting now. The first frame is written immediately so nothing
 * flashes at the origin. Returns a stop function for when the burst unmounts early.
 */
export function playBurst(nodes: HTMLElement[], specks: Speck[], done: () => void = () => undefined): () => void {
  if (typeof window === "undefined" || specks.length === 0) return () => undefined;
  const start = performance.now();
  const run: Run = { nodes, specks, start, end: burstLife(specks) + 0.02, done };
  paint(run, 0);
  RUNS.add(run);
  kick();
  return () => {
    RUNS.delete(run);
  };
}
