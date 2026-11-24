// The workflow canvas's geometry: where a node's ports are, how an edge is drawn, how a graph is
// laid out, and how the viewport fits, zooms and maps the screen to the world. Pure — no React,
// no DOM — so the maths the editor depends on is tested on its own.
//
// World coordinates are the ones a node stores (`x`/`y`, never negative: the server drops a
// negative coordinate). The viewport draws the world at `screen = world * k + (x, y)`.

import type { WorkflowEdge } from "../api/types";
import { edgeKey, forwardEdges, layers, type StepDraft } from "./workflow";

export { edgeKey };

/** A node card is a fixed size, so ports, edges and hit tests agree without measuring. */
export const NODE_W = 232;
export const NODE_H = 76;
/** Everything snaps to this grid when it is placed by hand. */
export const GRID = 8;
export const MIN_ZOOM = 0.25;
export const MAX_ZOOM = 2;
/** The server's own ceiling for a stored coordinate. */
export const MAX_COORD = 20_000;
/** The automatic layout's spacing: a column per node of a layer, a row per layer. */
export const COL_STEP = NODE_W + 48;
export const ROW_STEP = NODE_H + 96;
const ORIGIN = 48;
/** Room under a branch card for its choice labels, so they are inside the fitted bounds. */
const LABEL_ROOM = 30;

export type Point = { x: number; y: number };
export type View = { x: number; y: number; k: number };
export type Placed = { id: string; x: number; y: number };
type NodeShape = Pick<StepDraft, "id" | "kind" | "choices">;

export function snap(value: number): number {
  return Math.round(value / GRID) * GRID;
}

/** A coordinate the server will keep: on the grid, inside `[0, MAX_COORD - NODE_W]`. */
export function clampCoord(value: number): number {
  return Math.min(MAX_COORD - NODE_W, Math.max(0, snap(value)));
}

// ---------------------------------------------------------------- ports and edges

/** A node's outgoing ports along its bottom edge: one, or one per branch choice. */
export function outPorts(node: Pick<StepDraft, "kind" | "choices">): { choice: string | null; dx: number }[] {
  if (node.kind !== "branch") return [{ choice: null, dx: NODE_W / 2 }];
  const choices = node.choices.map((choice) => choice.trim()).filter(Boolean);
  // A branch that has no choice yet keeps one port, which refuses with a reason when used.
  if (choices.length === 0) return [{ choice: null, dx: NODE_W / 2 }];
  return choices.map((choice, index) => ({ choice, dx: (NODE_W * (index + 1)) / (choices.length + 1) }));
}

/** Where an edge leaves a node: the port of its choice, else the first port. */
export function outPoint(at: Point, node: Pick<StepDraft, "kind" | "choices">, choice: string | null): Point {
  const ports = outPorts(node);
  const port = ports.find((item) => item.choice === (choice ?? null)) ?? ports[0]!;
  return { x: at.x + port.dx, y: at.y + NODE_H };
}

/** Where every edge enters a node: the middle of its top edge. */
export function inPoint(at: Point): Point {
  return { x: at.x + NODE_W / 2, y: at.y };
}

/** How far the arrowhead reaches back from its tip. */
export const ARROW_LEN = 9;
/** The entry dot sits on the target's top edge with this radius; the tip stops on its rim. */
const ENTRY_RIM = 5;

/**
 * An edge into a target: the curve stops at the arrowhead's base, and the head is a triangle
 * (its corners rounded by the stroke) whose tip rests on the rim of the target's entry dot.
 * Every edge arrives from above (see `edgePath`), so the head always points straight down.
 */
export function edgeInto(from: Point, to: Point): { d: string; mid: Point; head: string } {
  const tip = { x: to.x, y: to.y - ENTRY_RIM };
  const base = tip.y - ARROW_LEN;
  const { d, mid } = edgePath(from, { x: to.x, y: base });
  const half = 4.5;
  const head = `M ${tip.x - half} ${base} L ${tip.x + half} ${base} L ${tip.x} ${tip.y} Z`;
  return { d, mid, head };
}

/**
 * The curve from a port to a target's top. Downwards it is a plain vertical S; when the target
 * sits level with or above the source the curve swings out below and comes back in from above,
 * so an edge never runs backwards through the cards it connects.
 */
export function edgePath(from: Point, to: Point): { d: string; mid: Point } {
  const dy = to.y - from.y;
  const pull = dy >= 32 ? Math.max(28, dy * 0.5) : Math.max(96, Math.abs(dy) * 0.6 + 72);
  const c1 = { x: from.x, y: from.y + pull };
  const c2 = { x: to.x, y: to.y - pull };
  const d = `M ${from.x} ${from.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${to.x} ${to.y}`;
  // The cubic's own midpoint (t = ½), where a selected edge offers its delete button.
  const mid = {
    x: (from.x + 3 * c1.x + 3 * c2.x + to.x) / 8,
    y: (from.y + 3 * c1.y + 3 * c2.y + to.y) / 8,
  };
  return { d, mid };
}

/** How far a loop arc swings out to the right of the cards it spans. */
const LOOP_SWING = 56;

/**
 * A loop edge: from its source's port, out to the right of every card between the two ends,
 * up, and back in to the target's top from above, where every edge enters. `right` is the
 * right-most card edge the arc must clear. `mid` sits on the vertical run, where the arc's
 * count chip and delete button go.
 */
export function loopInto(from: Point, to: Point, right: number): { d: string; mid: Point; head: string } {
  const x = Math.max(from.x, to.x, right) + LOOP_SWING;
  const tip = { x: to.x, y: to.y - ENTRY_RIM };
  const base = tip.y - ARROW_LEN;
  const below = from.y + 28;
  const above = base - 28;
  const r = 18;
  const d = [
    `M ${from.x} ${from.y}`,
    `C ${from.x} ${below}, ${from.x + 12} ${below}, ${from.x + r} ${below}`,
    `L ${x - r} ${below}`,
    `Q ${x} ${below}, ${x} ${below - r}`,
    `L ${x} ${above + r}`,
    `Q ${x} ${above}, ${x - r} ${above}`,
    `L ${to.x + r} ${above}`,
    `C ${to.x + 12} ${above}, ${to.x} ${above}, ${to.x} ${base}`,
  ].join(" ");
  const half = 4.5;
  const head = `M ${tip.x - half} ${base} L ${tip.x + half} ${base} L ${tip.x} ${tip.y} Z`;
  return { d, mid: { x, y: (below + above) / 2 }, head };
}

/**
 * The drawing of one edge between two placed cards: a loop goes round the right of every card
 * it spans (the ones whose rows lie between its ends), a forward edge is the usual S.
 */
export function routeEdge(
  edge: Pick<WorkflowEdge, "when" | "max_loops">,
  from: Point,
  source: Pick<StepDraft, "kind" | "choices">,
  to: Point,
  placed: readonly Placed[],
): { d: string; mid: Point; head: string; loop: boolean } {
  const start = outPoint(from, source, edge.when ?? null);
  const end = inPoint(to);
  if (!(typeof edge.max_loops === "number" && edge.max_loops > 0)) {
    return { ...edgeInto(start, end), loop: false };
  }
  const top = Math.min(from.y, to.y);
  const bottom = Math.max(from.y, to.y) + NODE_H;
  const right = placed
    .filter((item) => item.y + NODE_H >= top && item.y <= bottom)
    .reduce((edgeX, item) => Math.max(edgeX, item.x + NODE_W), Math.max(from.x, to.x) + NODE_W);
  return { ...loopInto(start, end, right), loop: true };
}

// ---------------------------------------------------------------- layout

/**
 * A layered layout, top to bottom: one row per dependency depth. Inside a row every node wants
 * to sit under the average of its parents (a branch's choices pull their own child towards the
 * port it hangs from); a left-to-right sweep then keeps them a column apart, and the row is
 * re-centred on what it wanted. Children land under their parents and chains stay straight.
 * Loop edges play no part: they are drawn round the side of the cards they span.
 */
export function autoLayout(nodes: readonly NodeShape[], all: readonly WorkflowEdge[]): Record<string, Point> {
  const edges = forwardEdges(all);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const rows = layers({ nodes, edges });
  const slot = new Map<string, number>();
  const portShift = (edge: WorkflowEdge): number => {
    const source = byId.get(edge.from);
    if (!source) return 0;
    const ports = outPorts(source);
    const index = Math.max(0, ports.findIndex((port) => port.choice === (edge.when ?? null)));
    return ((index + 1) / (ports.length + 1) - 0.5) * (NODE_W / COL_STEP);
  };
  rows.forEach((row, rowIndex) => {
    const desired = new Map<string, number>();
    row.forEach((id, order) => {
      const incoming = edges.filter((edge) => edge.to === id && slot.has(edge.from));
      desired.set(
        id,
        rowIndex === 0 || incoming.length === 0
          ? order - (row.length - 1) / 2
          : incoming.reduce((sum, edge) => sum + slot.get(edge.from)! + portShift(edge), 0) / incoming.length,
      );
    });
    const sorted = [...row].sort((a, b) => desired.get(a)! - desired.get(b)! || row.indexOf(a) - row.indexOf(b));
    const placed: number[] = [];
    sorted.forEach((id, index) => {
      const want = desired.get(id)!;
      placed.push(index === 0 ? want : Math.max(want, placed[index - 1]! + 1));
    });
    const wantMean = sorted.reduce((sum, id) => sum + desired.get(id)!, 0) / sorted.length;
    const gotMean = placed.reduce((sum, value) => sum + value, 0) / placed.length;
    sorted.forEach((id, index) => slot.set(id, placed[index]! + (wantMean - gotMean)));
  });
  const depth = new Map<string, number>();
  rows.forEach((row, rowIndex) => row.forEach((id) => depth.set(id, rowIndex)));
  const minSlot = Math.min(...[...slot.values()]);
  const result: Record<string, Point> = {};
  for (const node of nodes) {
    result[node.id] = {
      x: snap(ORIGIN + ((slot.get(node.id) ?? 0) - minSlot) * COL_STEP),
      y: snap(ORIGIN + (depth.get(node.id) ?? 0) * ROW_STEP),
    };
  }
  return result;
}

function overlaps(a: Point, b: Point, margin = 16): boolean {
  return (
    a.x < b.x + NODE_W + margin &&
    a.x + NODE_W + margin > b.x &&
    a.y < b.y + NODE_H + margin &&
    a.y + NODE_H + margin > b.y
  );
}

/** The nearest free spot to `prefer`: the spot itself, then sideways, then one row further down. */
export function freeSpot(placed: readonly Placed[], prefer: Point): Point {
  const start = { x: clampCoord(prefer.x), y: clampCoord(prefer.y) };
  for (let row = 0; row < 24; row += 1) {
    for (const step of [0, 1, -1, 2, -2, 3, -3]) {
      const spot = {
        x: clampCoord(start.x + step * COL_STEP),
        y: clampCoord(start.y + row * (NODE_H + 40)),
      };
      if (!placed.some((other) => overlaps(spot, other))) return spot;
    }
  }
  return start;
}

/**
 * Every draft with a position. A graph nobody has placed (a bot wrote it) is laid out whole;
 * a node added later without a place goes under its first parent, or under the graph.
 */
export function materialize(drafts: readonly StepDraft[], edges: readonly WorkflowEdge[]): StepDraft[] {
  const missing = drafts.filter((draft) => draft.x === null || draft.y === null);
  if (missing.length === 0) return [...drafts];
  if (missing.length === drafts.length) {
    const layout = autoLayout(drafts, edges);
    return drafts.map((draft) => ({ ...draft, x: layout[draft.id]!.x, y: layout[draft.id]!.y }));
  }
  const placed: Placed[] = drafts
    .filter((draft) => draft.x !== null && draft.y !== null)
    .map((draft) => ({ id: draft.id, x: draft.x!, y: draft.y! }));
  const result = drafts.map((draft) => ({ ...draft }));
  for (const draft of result) {
    if (draft.x !== null && draft.y !== null) continue;
    const parent = forwardEdges(edges)
      .filter((edge) => edge.to === draft.id)
      .map((edge) => placed.find((item) => item.id === edge.from))
      .find((item): item is Placed => item !== undefined);
    const lowest = placed.reduce((low, item) => Math.max(low, item.y), 0);
    const prefer = parent
      ? { x: parent.x, y: parent.y + ROW_STEP }
      : { x: placed[0]?.x ?? ORIGIN, y: lowest + ROW_STEP };
    const spot = freeSpot(placed, prefer);
    draft.x = spot.x;
    draft.y = spot.y;
    placed.push({ id: draft.id, x: spot.x, y: spot.y });
  }
  return result;
}

// ---------------------------------------------------------------- viewport

export type Bounds = { minX: number; minY: number; maxX: number; maxY: number };

/** The box every card (and a branch's choice labels) sits inside; `null` for an empty graph. */
export function bounds(placed: readonly Placed[]): Bounds | null {
  if (placed.length === 0) return null;
  return {
    minX: Math.min(...placed.map((item) => item.x)),
    minY: Math.min(...placed.map((item) => item.y)),
    maxX: Math.max(...placed.map((item) => item.x + NODE_W)),
    maxY: Math.max(...placed.map((item) => item.y + NODE_H)) + LABEL_ROOM,
  };
}

export function clampZoom(k: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k));
}

/**
 * The view that shows the whole graph in a `width × height` viewport: never enlarged past
 * 100%, never smaller than MIN_ZOOM. What still does not fit is aligned to the top-left
 * instead of centred, so the entry of a long plan is what shows first.
 */
export function fitView(box: Bounds | null, width: number, height: number, pad = 56): View {
  if (!box || width <= 0 || height <= 0) return { x: pad, y: pad, k: 1 };
  const w = Math.max(1, box.maxX - box.minX);
  const h = Math.max(1, box.maxY - box.minY);
  const k = clampZoom(Math.min((width - 2 * pad) / w, (height - 2 * pad) / h, 1));
  const x = w * k <= width - 2 * pad ? (width - w * k) / 2 - box.minX * k : pad - box.minX * k;
  const y = h * k <= height - 2 * pad ? (height - h * k) / 2 - box.minY * k : pad - box.minY * k;
  return { x, y, k };
}

/** Zoom to `next` keeping the world point under `(cx, cy)` where it is on screen. */
export function zoomAt(view: View, next: number, cx: number, cy: number): View {
  const k = clampZoom(next);
  const ratio = k / view.k;
  return { k, x: cx - (cx - view.x) * ratio, y: cy - (cy - view.y) * ratio };
}

export function toWorld(view: View, sx: number, sy: number): Point {
  return { x: (sx - view.x) / view.k, y: (sy - view.y) / view.k };
}

/** The topmost card under a world point, with a little slack around its edge. */
export function hitNode(placed: readonly Placed[], point: Point, slack = 6): string | null {
  for (let index = placed.length - 1; index >= 0; index -= 1) {
    const item = placed[index]!;
    if (
      point.x >= item.x - slack &&
      point.x <= item.x + NODE_W + slack &&
      point.y >= item.y - slack &&
      point.y <= item.y + NODE_H + slack
    ) {
      return item.id;
    }
  }
  return null;
}

// ---------------------------------------------------------------- what needs attention

/**
 * One thing to fix per node, shown on its card. Save is gated by `draftProblem`; these say
 * *where* the problem is, plus the softer ones (a branch choice nothing hangs off yet).
 */
export function nodeIssues(drafts: readonly StepDraft[], edges: readonly WorkflowEdge[]): Record<string, string> {
  const issues: Record<string, string> = {};
  const connected = new Set<string>();
  for (const edge of edges) {
    connected.add(edge.from);
    connected.add(edge.to);
  }
  for (const draft of drafts) {
    if (!draft.instruction.trim()) {
      issues[draft.id] = draft.kind === "ask_user" ? "还没写要问什么" : "还没写要做什么";
      continue;
    }
    if (drafts.length > 1 && !connected.has(draft.id)) {
      issues[draft.id] = "还没有连线";
      continue;
    }
    if (draft.kind !== "branch") continue;
    const choices = draft.choices.map((choice) => choice.trim()).filter(Boolean);
    if (choices.length < 2) {
      issues[draft.id] = "分支至少要两个选项";
      continue;
    }
    const loose = choices.find(
      (choice) => !edges.some((edge) => edge.from === draft.id && edge.when === choice),
    );
    if (loose) issues[draft.id] = `选项「${loose}」还没连出去`;
  }
  return issues;
}
