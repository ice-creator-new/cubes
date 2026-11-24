import { describe, expect, it } from "vitest";
import type { WorkflowEdge } from "../api/types";
import { emptyDraft, type StepDraft } from "./workflow";
import {
  MAX_COORD,
  MIN_ZOOM,
  NODE_H,
  NODE_W,
  autoLayout,
  edgeInto,
  routeEdge,
  bounds,
  fitView,
  freeSpot,
  hitNode,
  materialize,
  nodeIssues,
  outPoint,
  outPorts,
  toWorld,
  zoomAt,
  type Placed,
} from "./workflowCanvas";

const node = (id: string, patch: Partial<StepDraft> = {}): StepDraft => ({
  ...emptyDraft(id),
  instruction: `do ${id}`,
  ...patch,
});
const edge = (from: string, to: string, when: string | null = null): WorkflowEdge => ({ from, to, when });

function overlap(a: Placed, b: Placed): boolean {
  return a.x < b.x + NODE_W && a.x + NODE_W > b.x && a.y < b.y + NODE_H && a.y + NODE_H > b.y;
}

function placedOf(layout: Record<string, { x: number; y: number }>): Placed[] {
  return Object.entries(layout).map(([id, at]) => ({ id, ...at }));
}

describe("ports", () => {
  it("gives a branch one port per filled choice, left to right in choice order", () => {
    const branch = node("b", { kind: "branch", choices: ["是", "  ", "否", "再看看"] });
    const ports = outPorts(branch);
    expect(ports.map((port) => port.choice)).toEqual(["是", "否", "再看看"]);
    expect(ports[0]!.dx).toBeLessThan(ports[1]!.dx);
    expect(ports[1]!.dx).toBeLessThan(ports[2]!.dx);
    expect(ports.every((port) => port.dx > 0 && port.dx < NODE_W)).toBe(true);
  });

  it("starts an edge at its own choice's port on the bottom edge", () => {
    const branch = node("b", { kind: "branch", choices: ["是", "否"] });
    const yes = outPoint({ x: 100, y: 40 }, branch, "是");
    const no = outPoint({ x: 100, y: 40 }, branch, "否");
    expect(yes.y).toBe(40 + NODE_H);
    expect(yes.x).toBeLessThan(no.x);
  });

  it("ends an edge where its arrowhead starts, and points the tip down onto the target", () => {
    const into = edgeInto({ x: 100, y: 0 }, { x: 140, y: 200 });
    const curveEnd = into.d.trim().split(/[ ,]+/).slice(-2).map(Number);
    const head = into.head.match(/-?\d+(\.\d+)?/g)!.map(Number);
    const [lx, ly, rx, ry, tx, ty] = head;
    // The curve stops at the middle of the head's base, so line and head meet with no gap.
    expect(curveEnd).toEqual([(lx! + rx!) / 2, ly]);
    expect(ly).toBe(ry);
    // The tip is below the base, centred on the target, and short of the target's top edge.
    expect(tx).toBe(140);
    expect(ty).toBeGreaterThan(ly!);
    expect(ty).toBeLessThan(200);
  });

  it("routes a loop round the right of every card it spans, and into its target from above", () => {
    const placed: Placed[] = [
      { id: "draft", x: 48, y: 48 },
      { id: "wide", x: 400, y: 220 },
      { id: "review", x: 48, y: 392 },
    ];
    const review = node("review", { kind: "branch", choices: ["合格", "不合格"] });
    const loop = routeEdge({ when: "不合格", max_loops: 2 }, placed[2]!, review, placed[0]!, placed);
    const forward = routeEdge({ when: null }, placed[0]!, node("draft"), placed[2]!, placed);
    expect(loop.loop).toBe(true);
    expect(forward.loop).toBe(false);
    const xs = loop.d.match(/-?\d+(\.\d+)?/g)!.map(Number).filter((_, index) => index % 2 === 0);
    // Clears the wide card in the middle row, not just the two ends.
    expect(Math.max(...xs)).toBeGreaterThan(400 + NODE_W);
    expect(loop.mid.x).toBeGreaterThan(400 + NODE_W);
    // Same arrowhead rule as any edge: the tip rests above the target's top edge.
    const tipY = Number(loop.head.match(/-?\d+(\.\d+)?/g)!.at(-1));
    expect(tipY).toBeLessThan(48);
  });
});

describe("automatic layout", () => {
  it("never stacks two cards on each other", () => {
    const drafts = ["a", "b", "c", "d", "e", "f"].map((id) => node(id));
    const edges = [edge("a", "b"), edge("a", "c"), edge("a", "d"), edge("b", "e"), edge("c", "e"), edge("d", "f")];
    const placed = placedOf(autoLayout(drafts, edges));
    for (const a of placed) for (const b of placed) if (a.id !== b.id) expect(overlap(a, b)).toBe(false);
  });

  it("reads top-down: every edge points to a lower row", () => {
    const drafts = ["a", "b", "c", "d"].map((id) => node(id));
    const edges = [edge("a", "b"), edge("a", "c"), edge("b", "d"), edge("c", "d")];
    const layout = autoLayout(drafts, edges);
    for (const item of edges) expect(layout[item.to]!.y).toBeGreaterThan(layout[item.from]!.y);
  });

  it("keeps a chain in one straight column", () => {
    const drafts = ["a", "b", "c"].map((id) => node(id));
    const layout = autoLayout(drafts, [edge("a", "b"), edge("b", "c")]);
    expect(layout.a!.x).toBe(layout.b!.x);
    expect(layout.b!.x).toBe(layout.c!.x);
  });

  it("hangs each branch arm under its own port, so the arms do not cross", () => {
    // Plan order puts the "否" child first; the layout must still put it on the right.
    const drafts = [node("q", { kind: "branch", choices: ["是", "否"] }), node("no"), node("yes")];
    const layout = autoLayout(drafts, [edge("q", "no", "否"), edge("q", "yes", "是")]);
    expect(layout.yes!.x).toBeLessThan(layout.no!.x);
  });
});

describe("placing new cards", () => {
  it("takes the preferred spot when it is free, and moves off a taken one", () => {
    const taken: Placed[] = [{ id: "a", x: 48, y: 48 }];
    expect(freeSpot([], { x: 48, y: 48 })).toEqual({ x: 48, y: 48 });
    const spot = freeSpot(taken, { x: 48, y: 48 });
    expect(overlap({ id: "n", ...spot }, taken[0]!)).toBe(false);
  });

  it("stays inside the coordinates the server accepts", () => {
    const spot = freeSpot([], { x: -500, y: MAX_COORD + 900 });
    expect(spot.x).toBeGreaterThanOrEqual(0);
    expect(spot.y + NODE_W).toBeLessThanOrEqual(MAX_COORD);
  });

  it("lays out a graph nobody placed, and leaves placed cards where they are", () => {
    const fresh = materialize([node("a", { x: null, y: null }), node("b", { x: null, y: null })], [edge("a", "b")]);
    expect(fresh.every((draft) => draft.x !== null && draft.y !== null)).toBe(true);
    expect(fresh[1]!.y!).toBeGreaterThan(fresh[0]!.y!);

    const mixed = materialize([node("a", { x: 400, y: 200 }), node("b", { x: null, y: null })], [edge("a", "b")]);
    expect(mixed[0]).toMatchObject({ x: 400, y: 200 });
    expect(mixed[1]!.y!).toBeGreaterThan(200);
    expect(overlap({ id: "a", x: 400, y: 200 }, { id: "b", x: mixed[1]!.x!, y: mixed[1]!.y! })).toBe(false);
  });
});

describe("viewport", () => {
  it("never enlarges a small graph past 100%, and centres it", () => {
    const box = bounds([{ id: "a", x: 0, y: 0 }]);
    const view = fitView(box, 1200, 800);
    expect(view.k).toBe(1);
    const centre = toWorld(view, 600, 400);
    expect(centre.x).toBeCloseTo(NODE_W / 2, 0);
  });

  it("shrinks a large graph so all of it is on screen", () => {
    const placed: Placed[] = [
      { id: "a", x: 0, y: 0 },
      { id: "b", x: 3000, y: 1500 },
    ];
    const view = fitView(bounds(placed), 1000, 600);
    expect(view.k).toBeLessThan(1);
    const topLeft = toWorld(view, 0, 0);
    const bottomRight = toWorld(view, 1000, 600);
    expect(topLeft.x).toBeLessThanOrEqual(0);
    expect(topLeft.y).toBeLessThanOrEqual(0);
    expect(bottomRight.x).toBeGreaterThanOrEqual(3000 + NODE_W);
  });

  it("aligns a graph too big even at the minimum zoom to its top-left, the entry", () => {
    const view = fitView(bounds([{ id: "a", x: 0, y: 0 }, { id: "b", x: 0, y: 60_000 }]), 800, 600, 56);
    expect(view.k).toBe(MIN_ZOOM);
    // The entry card's top edge lands at the padding on screen instead of off the top.
    expect(view.y).toBe(56);
  });

  it("zooms about the pointer: the world point under it stays put", () => {
    const view = { x: 40, y: -20, k: 0.8 };
    const before = toWorld(view, 300, 200);
    const after = toWorld(zoomAt(view, 1.5, 300, 200), 300, 200);
    expect(after.x).toBeCloseTo(before.x, 6);
    expect(after.y).toBeCloseTo(before.y, 6);
    expect(zoomAt(view, 99, 0, 0).k).toBe(2);
  });

  it("hits the topmost card, with a little slack at the edge", () => {
    const placed: Placed[] = [
      { id: "under", x: 0, y: 0 },
      { id: "over", x: 100, y: 0 },
    ];
    expect(hitNode(placed, { x: 150, y: 20 })).toBe("over");
    expect(hitNode(placed, { x: -4, y: 20 })).toBe("under");
    expect(hitNode(placed, { x: -40, y: 20 })).toBeNull();
  });
});

describe("what a card flags", () => {
  it("names an empty instruction, a loose node and an unconnected choice", () => {
    const drafts = [
      node("a"),
      node("empty", { instruction: " " }),
      node("q", { kind: "branch", choices: ["是", "否"] }),
      node("loose"),
      node("yes"),
    ];
    const issues = nodeIssues(drafts, [edge("a", "q"), edge("q", "yes", "是"), edge("a", "empty")]);
    expect(issues.empty).toBe("还没写要做什么");
    expect(issues.loose).toBe("还没有连线");
    expect(issues.q).toBe("选项「否」还没连出去");
    expect(issues.a).toBeUndefined();
    expect(issues.yes).toBeUndefined();
  });

  it("leaves a lone first card alone: one node has nothing to connect to", () => {
    expect(nodeIssues([node("only")], [])).toEqual({});
  });
});
