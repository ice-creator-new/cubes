// Pure geometry for the run-trace hand-off graph: which column (depth from the user) each node
// sits in, its vertically centred row, the curve each edge takes, and where its label pill lands
// so that fan-out edges from one bot spread instead of stacking. No rendering and no DOM: the
// component turns these coordinates into an SVG, and the tests check the invariants (no label
// overlap, names inside the bounds) without mounting anything.

import type { RunTraceEdge, RunTraceNode, TraceEdgeKind } from "../api/types";

export type { TraceEdgeKind, TraceNodeKind } from "../api/types";

/** The short word on an edge's arrow, per hand-off kind. */
export const EDGE_KIND_LABELS: Record<TraceEdgeKind, string> = {
  ask: "交给",
  mention: "@ 接力",
  consult: "咨询",
  dm: "私信",
  revise: "打回",
};

export function edgeKindLabel(kind: TraceEdgeKind): string {
  return EDGE_KIND_LABELS[kind];
}

/** One node with its position decided. `order` is its first-appearance index (a stable tie-break). */
export interface GraphNode {
  id: string;
  name: string;
  kind: RunTraceNode["kind"];
  /** Shortest directed distance from the user; isolated nodes sit one column past the rest. */
  depth: number;
  /** Vertical slot within the node's column, in first-appearance order. */
  row: number;
  /** First-appearance index in the input. */
  order: number;
  x: number;
  y: number;
}

/** One edge, annotated for drawing. `back` edges are return arcs (review send-backs). */
export interface GraphEdge {
  from: string;
  to: string;
  kind: TraceEdgeKind;
  label: string;
  /** True for a revise send-back, or any edge that travels right-to-left. */
  back: boolean;
  /** Which of a node's outgoing edges this is (0-based), used to spread fan-out labels. */
  fanIndex: number;
  /** How many edges leave the same source; used to centre the fan. */
  fanCount: number;
  /** The resolved anchor the label pill centres on. */
  labelAnchor: { x: number; y: number };
  /** The normal of the curve at the anchor, for nudging labels off the line. */
  labelNormal: { x: number; y: number };
  /** The curve itself, drawn between the two ring edges. */
  path: string;
}

/** The SVG path of one edge plus where its label sits, in layout units. */
export interface EdgeGeometry {
  d: string;
  anchor: { x: number; y: number };
  normal: { x: number; y: number };
}

/** The whole diagram's drawing bounds: everything fits inside `0..width × 0..height`. */
export interface GraphBounds {
  width: number;
  height: number;
}

const COLUMN_GAP = 176;
const ROW_GAP = 104;
const PAD = 36;
/** Half the label pill's fixed height, so anchors clear the curve by this much. */
const LABEL_LIFT = 11;

/** A label pill's width: the word, its step number, padding and a hair of slack. */
export function labelWidth(text: string): number {
  const word = charWidths(text);
  // 「① 咨询」: number glyph (16) + gap (4) + word.
  return word + 16 + 4 + 16;
}

/** The pixel width of a piece of text at the graph's 12px label font. */
export function textWidth(text: string): number {
  return charWidths(text);
}

function charWidths(text: string): number {
  let width = 0;
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    width += code > 0x2e7f ? 13 : 7;
  }
  return width;
}

/**
 * The quadratic curve between two node boundaries. `bend` pushes the control point off the chord
 * (positive bends one way, negative the other); the label sits just clear of the curve's peak.
 */
export function edgeGeometry(
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  bend: number,
  nodeRadius: number,
): EdgeGeometry {
  const dx = x1 - x0;
  const dy = y1 - y0;
  const dist = Math.hypot(dx, dy) || 1;
  const sx = x0 + (dx / dist) * nodeRadius;
  const sy = y0 + (dy / dist) * nodeRadius;
  const ex = x1 - (dx / dist) * nodeRadius;
  const ey = y1 - (dy / dist) * nodeRadius;
  const nx = -dy / dist;
  const ny = dx / dist;
  const cx = (sx + ex) / 2 + nx * bend;
  const cy = (sy + ey) / 2 + ny * bend;
  // The curve's midpoint (t = 0.5) sits a quarter of the way along the control offset.
  const apexX = (sx + ex) / 2 + nx * bend * 0.5;
  const apexY = (sy + ey) / 2 + ny * bend * 0.5;
  // Lift the anchor a touch along the normal so the pill clears the stroke.
  const lift = LABEL_LIFT;
  return {
    d: `M ${sx.toFixed(2)} ${sy.toFixed(2)} Q ${cx.toFixed(2)} ${cy.toFixed(2)} ${ex.toFixed(2)} ${ey.toFixed(2)}`,
    anchor: { x: apexX + nx * lift, y: apexY + ny * lift },
    normal: { x: nx, y: ny },
  };
}

/**
 * How far an edge bows off its chord. Forward hand-offs run between distinct rows already, so
 * they stay nearly straight (a slight bow reads as a hand-off, not a wire); bending them by fan
 * position is what made sibling lines cross. Back arcs (send-backs, right-to-left) bow well
 * clear of the forward line they retrace, the same way every time.
 */
function fanBend(back: boolean): number {
  return back ? -46 : 10;
}

/**
 * Resolve label anchors for every edge, spreading fan-out labels and then separating any pills
 * that would still overlap. Pure and deterministic: the same graph gives the same labels.
 */
export function placeLabels(
  laidEdges: readonly Pick<GraphEdge, "from" | "to" | "kind" | "back" | "fanIndex" | "fanCount">[],
  byID: ReadonlyMap<string, GraphNode>,
  nodeRadius: number,
): Array<{ anchor: { x: number; y: number }; normal: { x: number; y: number } }> {
  const widths = laidEdges.map((edge) => labelWidth(edgeKindLabel(edge.kind)));
  const placed = laidEdges.map((edge) => {
    const from = byID.get(edge.from);
    const to = byID.get(edge.to);
    if (!from || !to) {
      return { anchor: { x: 0, y: 0 }, normal: { x: 0, y: 1 } };
    }
    return edgeGeometry(from.x, from.y, to.x, to.y, fanBend(edge.back), nodeRadius);
  });

  // Separate overlapping pills with a few relaxation passes along the perpendicular axis. Most
  // graphs never need it; when two hand-offs cross at the same height it nudges them apart.
  const H = 20; // label pill height
  for (let pass = 0; pass < 6; pass += 1) {
    let moved = false;
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        const a = placed[i];
        const b = placed[j];
        if (!a || !b) continue;
        const ax = widths[i] ?? 40;
        const bx = widths[j] ?? 40;
        const overlapX = (ax + bx) / 2 - Math.abs(a.anchor.x - b.anchor.x);
        const overlapY = H - Math.abs(a.anchor.y - b.anchor.y);
        if (overlapX <= 0 || overlapY <= 0) continue;
        const push = overlapY / 2 + 1;
        const dir = a.anchor.y <= b.anchor.y ? 1 : -1;
        a.anchor.y -= push * dir;
        b.anchor.y += push * dir;
        moved = true;
      }
    }
    if (!moved) break;
  }

  return placed;
}

export interface LaidRunGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  bounds: GraphBounds;
}

export function layoutRunGraph(
  nodes: readonly RunTraceNode[],
  edges: readonly RunTraceEdge[],
  options: { nodeRadius?: number } = {},
): LaidRunGraph {
  const nodeRadius = options.nodeRadius ?? 21;
  const order = new Map<string, number>(nodes.map((node, i) => [node.id, i]));
  // Layering: every forward hand-off sits one column right of its source (longest path), so a
  // consult never runs vertically inside a column. A revise is a send-back and never pushes a
  // column. Sources nothing points at (the user, or a responder an old trace did not link to)
  // start at column 0; relaxation is capped at the node count, so a cycle cannot loop forever.
  const forward = edges.filter((edge) => edge.kind !== "revise" && edge.from !== edge.to);
  const depth = new Map<string, number>(nodes.map((node) => [node.id, 0]));
  for (let pass = 0; pass < nodes.length; pass += 1) {
    let changed = false;
    for (const edge of forward) {
      const from = depth.get(edge.from);
      const to = depth.get(edge.to);
      if (from === undefined || to === undefined) continue;
      if (to < from + 1 && from + 1 < nodes.length) {
        depth.set(edge.to, from + 1);
        changed = true;
      }
    }
    if (!changed) break;
  }
  // The user always reads as the start: alone in the first column when it has a hand-off.
  if (nodes.some((node) => node.id === "user")) {
    const others = nodes.filter((node) => node.id !== "user");
    const userLinked = forward.some((edge) => edge.from === "user");
    if (userLinked && others.some((node) => (depth.get(node.id) ?? 0) === 0)) {
      for (const node of others) depth.set(node.id, (depth.get(node.id) ?? 0) + 1);
    }
    depth.set("user", 0);
  }
  // Nodes that take part in no hand-off at all go one column past the rest.
  const linked = new Set(edges.flatMap((edge) => [edge.from, edge.to]));
  if (linked.size > 0) {
    const maxDepth = Math.max(0, ...[...linked].map((id) => depth.get(id) ?? 0));
    for (const node of nodes) if (!linked.has(node.id)) depth.set(node.id, maxDepth + 1);
  }

  // Columns, then rows: the first column keeps first-appearance order; every later column is
  // sorted by where its sources sit (barycentre), so lines from one bot fan out instead of
  // crossing. First appearance breaks ties, keeping the layout stable between polls.
  const columns = new Map<number, RunTraceNode[]>();
  for (const node of nodes) {
    const d = depth.get(node.id)!;
    const list = columns.get(d);
    if (list) list.push(node);
    else columns.set(d, [node]);
  }
  const depthKeys = [...columns.keys()].sort((a, b) => a - b);
  const rowOf = new Map<string, number>();
  const orderedColumns = depthKeys.map((d) => {
    const column = columns.get(d)!;
    const centre = (id: string) => {
      const rows = forward
        .filter((edge) => edge.to === id && rowOf.has(edge.from))
        .map((edge) => rowOf.get(edge.from)!);
      return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Number.POSITIVE_INFINITY;
    };
    const sorted = [...column].sort((a, b) => {
      const byCentre = centre(a.id) - centre(b.id);
      if (Number.isFinite(byCentre) && byCentre !== 0) return byCentre;
      return (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0);
    });
    // Rows are compared across columns of different heights, so they are kept centred too.
    sorted.forEach((node, row) => rowOf.set(node.id, row - (sorted.length - 1) / 2));
    return sorted;
  });

  // Vertically centre every column on the shared axis of the tallest column, so the diagram is
  // a balanced tree rather than rows stacked from the top of each column.
  const maxRows = Math.max(1, ...orderedColumns.map((column) => column.length));
  const maxColumnHeight = (maxRows - 1) * ROW_GAP;
  const laid: GraphNode[] = [];
  orderedColumns.forEach((column, columnIndex) => {
    const columnHeight = (column.length - 1) * ROW_GAP;
    const shift = (maxColumnHeight - columnHeight) / 2;
    column.forEach((node, row) => {
      laid.push({
        id: node.id,
        name: node.name,
        kind: node.kind,
        depth: columnIndex,
        row,
        order: order.get(node.id) ?? 0,
        x: PAD + columnIndex * COLUMN_GAP,
        y: PAD + shift + row * ROW_GAP,
      });
    });
  });
  // Depth by laid column, for the back-edge test below (columns are renumbered densely).
  for (const node of laid) depth.set(node.id, node.depth);

  const byID = new Map(laid.map((node) => [node.id, node]));

  // Fan index per outgoing source, in edge order, so concurrent hand-offs spread their labels.
  const fanBySource = new Map<string, number>();
  const fanCountBySource = new Map<string, number>();
  for (const edge of edges) fanCountBySource.set(edge.from, (fanCountBySource.get(edge.from) ?? 0) + 1);
  const fanIndexByEdge = edges.map((edge) => {
    const index = fanBySource.get(edge.from) ?? 0;
    fanBySource.set(edge.from, index + 1);
    return index;
  });

  const labels = placeLabels(
    edges.map((edge, index) => ({
      from: edge.from,
      to: edge.to,
      kind: edge.kind,
      back:
        edge.kind === "revise" ||
        (depth.get(edge.from) !== undefined &&
          depth.get(edge.to) !== undefined &&
          (depth.get(edge.from) ?? 0) > (depth.get(edge.to) ?? 0)),
      fanIndex: fanIndexByEdge[index] ?? 0,
      fanCount: fanCountBySource.get(edge.from) ?? 1,
    })),
    byID,
    nodeRadius,
  );
  const laidEdges: GraphEdge[] = edges.map((edge, index) => {
    const fromDepth = depth.get(edge.from);
    const toDepth = depth.get(edge.to);
    const back =
      edge.kind === "revise" || (fromDepth !== undefined && toDepth !== undefined && fromDepth > toDepth);
    const label = labels[index] ?? { anchor: { x: 0, y: 0 }, normal: { x: 0, y: 1 } };
    const from = byID.get(edge.from);
    const to = byID.get(edge.to);
    const geometry =
      from && to
        ? edgeGeometry(from.x, from.y, to.x, to.y, fanBend(back), nodeRadius)
        : null;
    return {
      from: edge.from,
      to: edge.to,
      kind: edge.kind,
      label: edge.label,
      back,
      fanIndex: fanIndexByEdge[index] ?? 0,
      fanCount: fanCountBySource.get(edge.from) ?? 1,
      labelAnchor: label.anchor,
      labelNormal: label.normal,
      path: geometry?.d ?? "",
    };
  });

  return { nodes: laid, edges: laidEdges, bounds: graphBounds(laid, laidEdges, nodeRadius) };
}

/** Everything the diagram draws must fit inside these bounds, including names and label pills. */
export function graphBounds(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
  nodeRadius: number,
): GraphBounds {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  const include = (x: number, y: number) => {
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  };
  for (const node of nodes) {
    include(node.x - nodeRadius, node.y - nodeRadius);
    include(node.x + nodeRadius, node.y + nodeRadius);
    const nameWidth = textWidth(node.name);
    const nameTop = node.y + nodeRadius + 6;
    const nameBottom = nameTop + 17;
    include(node.x - nameWidth / 2, nameTop);
    include(node.x + nameWidth / 2, nameBottom);
  }
  for (const edge of edges) {
    const anchor = edge.labelAnchor;
    const width = labelWidth(edgeKindLabel(edge.kind));
    include(anchor.x - width / 2, anchor.y - 10);
    include(anchor.x + width / 2, anchor.y + 10);
  }
  if (!Number.isFinite(minX)) return { width: 0, height: 0 };
  const pad = 12;
  return {
    width: Math.ceil(maxX - minX + pad * 2),
    height: Math.ceil(maxY - minY + pad * 2),
  };
}
