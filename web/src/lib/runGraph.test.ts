import { describe, expect, it } from "vitest";
import type { RunTraceEdge, RunTraceNode } from "../api/types";
import {
  EDGE_KIND_LABELS,
  edgeGeometry,
  edgeKindLabel,
  labelWidth,
  layoutRunGraph,
  placeLabels,
  textWidth,
} from "./runGraph";

function node(id: string, name: string, kind: RunTraceNode["kind"] = "member"): RunTraceNode {
  return { id, name, kind };
}

function edge(from: string, to: string, kind: RunTraceEdge["kind"] = "consult", label = ""): RunTraceEdge {
  return { from, to, kind, label, at: "2026-10-28T10:00:00.000Z", conversation_id: "c1", message_id: null };
}

describe("edgeKindLabel", () => {
  it("maps every hand-off kind to its Chinese word", () => {
    expect(edgeKindLabel("ask")).toBe("交给");
    expect(edgeKindLabel("mention")).toBe("@ 接力");
    expect(edgeKindLabel("consult")).toBe("咨询");
    expect(edgeKindLabel("dm")).toBe("私信");
    expect(edgeKindLabel("revise")).toBe("打回");
    expect(EDGE_KIND_LABELS).toEqual({
      ask: "交给",
      mention: "@ 接力",
      consult: "咨询",
      dm: "私信",
      revise: "打回",
    });
  });
});

describe("layoutRunGraph", () => {
  it("assigns depth as distance from the user", () => {
    const nodes = [node("user", "你", "user"), node("super", "主管", "supervisor"), node("a", "甲"), node("b", "乙")];
    const edges = [
      edge("user", "super", "ask"),
      edge("super", "a", "consult"),
      edge("super", "b", "consult"),
    ];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const byID = new Map(laid.map((n) => [n.id, n]));
    expect(byID.get("user")!.depth).toBe(0);
    expect(byID.get("super")!.depth).toBe(1);
    expect(byID.get("a")!.depth).toBe(2);
    expect(byID.get("b")!.depth).toBe(2);
  });

  it("keeps first-appearance order stable within a layer", () => {
    // 乙 appears before 甲 in the input, so it must keep the earlier vertical slot.
    const nodes = [node("user", "你", "user"), node("super", "主管", "supervisor"), node("b", "乙"), node("a", "甲")];
    const edges = [edge("user", "super", "ask"), edge("super", "a", "consult"), edge("super", "b", "consult")];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const layer = laid.filter((n) => n.depth === 2).sort((x, y) => x.row - y.row);
    expect(layer.map((n) => n.id)).toEqual(["b", "a"]);
  });

  it("marks a revise edge as a back edge", () => {
    const nodes = [node("user", "你", "user"), node("super", "主管", "supervisor"), node("a", "甲")];
    const edges = [edge("user", "super", "ask"), edge("super", "a", "consult"), edge("super", "a", "revise")];
    const { edges: laid } = layoutRunGraph(nodes, edges);
    const revise = laid.find((e) => e.kind === "revise")!;
    expect(revise.back).toBe(true);
    const consult = laid.find((e) => e.kind === "consult")!;
    expect(consult.back).toBe(false);
  });

  it("marks any right-to-left edge as back", () => {
    // a (depth 2) → super (depth 1) travels backwards even though it is a dm.
    const nodes = [node("user", "你", "user"), node("super", "主管", "supervisor"), node("a", "甲")];
    const edges = [edge("user", "super", "ask"), edge("super", "a", "consult"), edge("a", "super", "dm")];
    const { edges: laid } = layoutRunGraph(nodes, edges);
    expect(laid.find((e) => e.kind === "dm")!.back).toBe(true);
  });

  it("places isolated nodes one column past the deepest reachable layer", () => {
    const nodes = [
      node("user", "你", "user"),
      node("super", "主管", "supervisor"),
      node("a", "甲"),
      node("lonely", "独行"),
    ];
    const edges = [edge("user", "super", "ask"), edge("super", "a", "consult")];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const byID = new Map(laid.map((n) => [n.id, n]));
    expect(byID.get("lonely")!.depth).toBe(3); // user=0, super=1, a=2 → isolated at 3
  });

  it("puts a hand-off target one column right of its source, with or without a user node", () => {
    // Seen live: an old trace with no ask edge put 主管 and its three consulted members in one
    // column, so every consult ran vertically and the lines wound around each other.
    const nodes = [node("super", "主管", "supervisor"), node("a", "甲"), node("b", "乙")];
    const edges = [edge("super", "a", "consult"), edge("super", "b", "consult")];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const byID = new Map(laid.map((n) => [n.id, n]));
    expect(byID.get("a")!.x).toBeGreaterThan(byID.get("super")!.x);
    expect(byID.get("b")!.x).toBeGreaterThan(byID.get("super")!.x);
  });

  it("never crosses two hand-offs that leave the same bot", () => {
    // The live run: 你 → 产品经理 (an old, wrong ask edge), then 主管 consults all three.
    const nodes = [
      node("user", "你", "user"),
      node("pm", "产品经理"),
      node("super", "主管", "supervisor"),
      node("eng", "工程专家"),
      node("code", "代码助手"),
    ];
    const edges = [
      edge("user", "pm", "ask"),
      edge("super", "pm", "consult"),
      edge("super", "eng", "consult"),
      edge("super", "code", "consult"),
    ];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const at = new Map(laid.map((n) => [n.id, n]));
    for (const e of edges) expect(at.get(e.to)!.x).toBeGreaterThan(at.get(e.from)!.x);
    // Straight chords between columns cross exactly when their row orders disagree.
    const crosses = (a: RunTraceEdge, b: RunTraceEdge) => {
      const [a0, a1, b0, b1] = [at.get(a.from)!, at.get(a.to)!, at.get(b.from)!, at.get(b.to)!];
      if (a0.x !== b0.x || a1.x !== b1.x || a.from === b.from) return false;
      return (a0.y - b0.y) * (a1.y - b1.y) < 0;
    };
    for (const a of edges) for (const b of edges) expect(crosses(a, b)).toBe(false);
  });

  it("orders a column by where its sources sit, so parallel hand-offs stay parallel", () => {
    const nodes = [
      node("user", "你", "user"),
      node("p", "甲"),
      node("q", "乙"),
      node("qq", "乙的下游"),
      node("pp", "甲的下游"),
    ];
    const edges = [edge("user", "p", "ask"), edge("user", "q", "ask"), edge("q", "qq", "consult"), edge("p", "pp", "consult")];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const y = new Map(laid.map((n) => [n.id, n.y]));
    expect(Math.sign(y.get("p")! - y.get("q")!)).toBe(Math.sign(y.get("pp")! - y.get("qq")!));
  });

  it("produces monotonic x per depth and distinct y within a column", () => {
    const nodes = [node("user", "你", "user"), node("super", "主管", "supervisor"), node("a", "甲"), node("b", "乙")];
    const edges = [edge("user", "super", "ask"), edge("super", "a", "consult"), edge("super", "b", "consult")];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const byDepth = new Map<number, typeof laid>();
    for (const n of laid) {
      const list = byDepth.get(n.depth);
      if (list) list.push(n);
      else byDepth.set(n.depth, [n]);
    }
    for (const list of byDepth.values()) {
      const ys = list.map((n) => n.y);
      expect(new Set(ys).size).toBe(ys.length); // no two rows overlap in one column
    }
    const depths = [...byDepth.keys()].sort((a, b) => a - b);
    const xs = depths.map((d) => byDepth.get(d)?.[0]?.x ?? 0);
    for (let i = 1; i < xs.length; i += 1) {
      const later = xs[i] ?? 0;
      const earlier = xs[i - 1] ?? 0;
      expect(later).toBeGreaterThan(earlier);
    }
  });

  it("centres every column on the tallest column's axis", () => {
    const nodes = [
      node("user", "你", "user"),
      node("super", "主管", "supervisor"),
      node("a", "甲"),
      node("b", "乙"),
      node("c", "丙"),
    ];
    const edges = [
      edge("user", "super", "ask"),
      edge("super", "a", "consult"),
      edge("super", "b", "consult"),
      edge("super", "c", "consult"),
    ];
    const { nodes: laid } = layoutRunGraph(nodes, edges);
    const byID = new Map(laid.map((n) => [n.id, n]));
    // depth 2 has three rows, so it is the tallest column; its centre is the shared axis.
    const a = byID.get("a")!;
    const b = byID.get("b")!;
    const c = byID.get("c")!;
    const centre2 = (a.y + c.y) / 2;
    expect(b.y).toBeCloseTo(centre2, 5);
    // The single-row columns (user and supervisor) sit exactly on that centre.
    expect(byID.get("user")!.y).toBeCloseTo(centre2, 5);
    expect(byID.get("super")!.y).toBeCloseTo(centre2, 5);
  });
});

describe("label and bounds invariants", () => {
  it("spreads fan-out labels so no two pills overlap", () => {
    const nodes = [
      node("user", "你", "user"),
      node("super", "主管", "supervisor"),
      node("pm", "产品经理"),
      node("eng", "工程专家"),
      node("code", "代码助手"),
    ];
    const edges = [
      edge("user", "super", "ask"),
      edge("super", "pm", "consult"),
      edge("super", "eng", "consult"),
      edge("super", "code", "consult"),
    ];
    const { edges: laid } = layoutRunGraph(nodes, edges);
    const pills = laid.map((e) => ({
      x: e.labelAnchor.x,
      y: e.labelAnchor.y,
      halfW: labelWidth(edgeKindLabel(e.kind)) / 2,
      halfH: 10,
    }));
    for (let i = 0; i < pills.length; i += 1) {
      for (let j = i + 1; j < pills.length; j += 1) {
        const a = pills[i]!;
        const b = pills[j]!;
        const overlapX = Math.abs(a.x - b.x) < a.halfW + b.halfW;
        const overlapY = Math.abs(a.y - b.y) < a.halfH + b.halfH;
        expect(overlapX && overlapY).toBe(false);
      }
    }
  });

  it("keeps every node's name box inside the graph bounds", () => {
    const nodes = [
      node("user", "你", "user"),
      node("super", "主管", "supervisor"),
      node("pm", "产品经理"),
      node("eng", "工程专家"),
      node("code", "代码助手"),
    ];
    const edges = [
      edge("user", "super", "ask"),
      edge("super", "pm", "consult"),
      edge("super", "eng", "consult"),
      edge("super", "code", "consult"),
    ];
    const { nodes: laid, bounds } = layoutRunGraph(nodes, edges);
    const nodeRadius = 21;
    for (const n of laid) {
      const halfName = textWidth(n.name) / 2;
      const top = n.y + nodeRadius + 6;
      const bottom = top + 17;
      expect(n.x - halfName).toBeGreaterThanOrEqual(0);
      expect(n.x + halfName).toBeLessThanOrEqual(bounds.width);
      expect(top).toBeGreaterThanOrEqual(0);
      expect(bottom).toBeLessThanOrEqual(bounds.height);
    }
  });

  it("draws every label pill inside the graph bounds too", () => {
    const nodes = [
      node("user", "你", "user"),
      node("super", "主管", "supervisor"),
      node("pm", "产品经理"),
      node("eng", "工程专家"),
      node("code", "代码助手"),
    ];
    const edges = [
      edge("user", "super", "ask"),
      edge("super", "pm", "consult"),
      edge("super", "eng", "consult"),
      edge("super", "code", "consult"),
    ];
    const { edges: laid, bounds } = layoutRunGraph(nodes, edges);
    for (const e of laid) {
      const halfW = labelWidth(edgeKindLabel(e.kind)) / 2;
      expect(e.labelAnchor.x - halfW).toBeGreaterThanOrEqual(0);
      expect(e.labelAnchor.x + halfW).toBeLessThanOrEqual(bounds.width);
      expect(e.labelAnchor.y - 10).toBeGreaterThanOrEqual(0);
      expect(e.labelAnchor.y + 10).toBeLessThanOrEqual(bounds.height);
    }
  });
});

describe("edgeGeometry", () => {
  it("stops the curve at the ring edge and offsets the label along the normal", () => {
    const { d, anchor, normal } = edgeGeometry(0, 0, 100, 0, 30, 21);
    expect(d.startsWith("M 21.00 0.00")).toBe(true);
    expect(d.endsWith("79.00 0.00")).toBe(true);
    // A horizontal edge has a vertical normal; the label lands above or below the chord.
    expect(normal.x).toBeCloseTo(0, 5);
    expect(Math.abs(normal.y)).toBeCloseTo(1, 5);
    expect(Math.abs(anchor.y)).toBeGreaterThan(0);
  });
});

describe("placeLabels", () => {
  it("assigns each edge a finite anchor and keeps them separated", () => {
    const nodes = [node("user", "你", "user"), node("super", "主管", "supervisor"), node("a", "甲"), node("b", "乙")];
    // Give the map real coordinates so the edges have a geometry to place labels on.
    const laidNodes = [
      { ...nodes[0]!, depth: 0, row: 0, order: 0, x: 36, y: 100 },
      { ...nodes[1]!, depth: 1, row: 0, order: 1, x: 212, y: 100 },
      { ...nodes[2]!, depth: 2, row: 0, order: 2, x: 388, y: 48 },
      { ...nodes[3]!, depth: 2, row: 1, order: 3, x: 388, y: 152 },
    ];
    const coord = new Map(laidNodes.map((n) => [n.id, n]));
    const labels = placeLabels(
      [
        { from: "user", to: "super", kind: "ask", back: false, fanIndex: 0, fanCount: 1 },
        { from: "super", to: "a", kind: "consult", back: false, fanIndex: 0, fanCount: 2 },
        { from: "super", to: "b", kind: "consult", back: false, fanIndex: 1, fanCount: 2 },
      ],
      coord,
      21,
    );
    expect(labels).toHaveLength(3);
    for (const label of labels) {
      expect(Number.isFinite(label.anchor.x)).toBe(true);
      expect(Number.isFinite(label.anchor.y)).toBe(true);
    }
    // The two fan-out consult labels should not sit on top of each other.
    expect(labels[1]!.anchor.y).not.toBeCloseTo(labels[2]!.anchor.y, 1);
  });

  it("leaves an unconnected edge with no geometry", () => {
    const byID = new Map([["user", { id: "user", name: "你", kind: "user" as const, depth: 0, row: 0, order: 0, x: 36, y: 100 }]]);
    const labels = placeLabels([{ from: "user", to: "ghost", kind: "ask", back: false, fanIndex: 0, fanCount: 1 }], byID, 21);
    expect(labels[0]!.anchor).toEqual({ x: 0, y: 0 });
  });
});
