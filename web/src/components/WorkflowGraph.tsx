// A walk, drawn small: the plan's nodes in dependency order with the edges between them, every
// branch that is running lit, each node in its own status colour. It is the panel's canvas, cut
// down to what a status view needs — no dragging, no editing, no labels wider than the node.
// Pure: geometry in, SVG out.

import type { JSX } from "react";
import type { WorkflowEdge, WorkflowRun, WorkflowStepState } from "../api/types";
import { isQueued, layers, runGraph, runningSteps, stepWord } from "../lib/workflow";

/** A mini node: small enough for a rail, big enough to hit with a pointer. */
const NODE_W = 58;
const NODE_H = 20;
const GAP_X = 14;
const GAP_Y = 30;
const PAD = 8;

export interface WorkflowGraphProps {
  run: WorkflowRun;
  selected: string | null;
  onSelect: (nodeID: string) => void;
}

/** The status a node is drawn with. A ready node waiting for a worker is not "not started". */
function toneOf(step: WorkflowStepState, run: WorkflowRun): string {
  if (isQueued(run, step)) return "is-queued";
  return `is-${step.status}`;
}

/** The arrow head at a node's top edge, pointing down into it. */
function head(x: number, y: number): string {
  return `${x - 3.5},${y - 5} ${x + 3.5},${y - 5} ${x},${y}`;
}

export function WorkflowGraph({ run, selected, onSelect }: WorkflowGraphProps): JSX.Element {
  const rows = layers(runGraph(run));
  // Every branch that is running lights its own incoming edge: a fan-out shows two live edges.
  const busy = new Set(runningSteps(run).map((step) => step.node_id));
  const settled = new Set(
    run.steps
      .filter((step) => step.status === "done" || step.status === "skipped")
      .map((step) => step.node_id),
  );

  // Where each node sits: centred within its layer, one row per layer.
  const cols = Math.max(1, ...rows.map((row) => row.length));
  const width = PAD * 2 + cols * NODE_W + (cols - 1) * GAP_X;
  const height = PAD * 2 + rows.length * NODE_H + Math.max(0, rows.length - 1) * GAP_Y;
  const spot = new Map<string, { x: number; y: number }>();
  rows.forEach((row, rowIndex) => {
    const rowWidth = row.length * NODE_W + Math.max(0, row.length - 1) * GAP_X;
    const left = (width - rowWidth) / 2;
    row.forEach((nodeID, colIndex) => {
      spot.set(nodeID, {
        x: left + colIndex * (NODE_W + GAP_X),
        y: PAD + rowIndex * (NODE_H + GAP_Y),
      });
    });
  });

  const nodes: WorkflowStepState[] = run.steps;
  const edges: WorkflowEdge[] = run.edges ?? [];

  return (
    <svg
      className="wf-graph"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`工作流共 ${nodes.length} 个节点`}
    >
      {edges.map((edge) => {
        const from = spot.get(edge.from);
        const to = spot.get(edge.to);
        if (!from || !to) return null;
        const loop = (edge.max_loops ?? 0) > 0;
        const x1 = from.x + NODE_W / 2;
        const y1 = from.y + NODE_H;
        const x2 = to.x + NODE_W / 2;
        const y2 = to.y;
        if (loop) {
          // A loop leaves and comes back on the right, clear of the cards in between.
          const bulge = Math.max(x1, x2) + 16;
          return (
            <g key={`${edge.from}-${edge.to}-${edge.when ?? ""}`} className="wf-graph-loop">
              <path
                d={`M ${x1 + NODE_W / 2 - 6} ${y1 - NODE_H / 2} C ${bulge} ${y1 - NODE_H / 2}, ${bulge} ${y2 + NODE_H / 2}, ${x2 + NODE_W / 2 - 6} ${y2 + NODE_H / 2}`}
              />
            </g>
          );
        }
        const d = `M ${x1} ${y1} C ${x1} ${y1 + 14}, ${x2} ${y2 - 14}, ${x2} ${y2 - 1}`;
        const taken = settled.has(edge.from);
        const live = busy.has(edge.from);
        return (
          <g
            key={`${edge.from}-${edge.to}-${edge.when ?? ""}`}
            className={`wf-graph-edge${taken ? " is-taken" : ""}${live ? " is-live" : ""}`}
          >
            <path d={d} />
            {live && <path className="wf-graph-flow" d={d} />}
            <polygon points={head(x2, y2 - 1)} />
          </g>
        );
      })}

      {nodes.map((step) => {
        const at = spot.get(step.node_id);
        if (!at) return null;
        const tone = toneOf(step, run);
        const isSelected = selected === step.node_id;
        return (
          <g
            key={step.node_id}
            className={`wf-graph-node ${tone}${isSelected ? " is-selected" : ""}`}
            transform={`translate(${at.x} ${at.y})`}
            style={{ ["--i" as string]: String(step.index) }}
            onClick={() => onSelect(step.node_id)}
            role="button"
            tabIndex={0}
            aria-label={`第 ${step.index + 1} 步 ${step.title}（${stepWord(run, step)}）`}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(step.node_id);
              }
            }}
          >
            <title>{`第 ${step.index + 1} 步 · ${step.title}`}</title>
            {tone === "is-running" && (
              <rect className="wf-graph-halo" x={-3} y={-3} width={NODE_W + 6} height={NODE_H + 6} rx={9} />
            )}
            <rect className="wf-graph-card" width={NODE_W} height={NODE_H} rx={7} />
            <text className="wf-graph-num" x={NODE_W / 2} y={NODE_H / 2 + 3.5}>
              {step.index + 1}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
