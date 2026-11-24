// The workflow editor's canvas: an infinite surface the graph is drawn on. It owns everything
// about *looking at* the graph — pan, zoom, fit, full screen — and the pointer gestures that
// edit it (drag a card, drag from a port to another card to connect, select, delete). What the
// graph *is* stays with the panel: the canvas gets drafts and edges, and reports every change.
//
// One transformed layer holds the edges (SVG) and the cards (HTML), so both move and scale
// together and nothing is ever clipped by a scroll box. Full screen is the canvas alone, drawn
// over the whole window; the inspector rides along as a floating card so a node can still be
// edited there.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { WorkflowEdge, WorkflowRun, WorkflowStepState } from "../api/types";
import { KIND_LABELS, edgeDead, edgeLive, isQueued, loopsTaken, reaches, stateOfNode, stepWord, type StepDraft } from "../lib/workflow";
import {
  NODE_H,
  NODE_W,
  bounds,
  clampCoord,
  clampZoom,
  routeEdge,
  edgeKey,
  edgePath,
  fitView,
  hitNode,
  outPoint,
  outPorts,
  toWorld,
  zoomAt,
  type Placed,
  type Point,
  type View,
} from "../lib/workflowCanvas";
import { prefersReducedMotion } from "../lib/motion";
import { Icon, type IconName } from "./Icon";

export type Selection = { kind: "node"; id: string } | { kind: "edge"; key: string } | null;

const KIND_ICON: Record<StepDraft["kind"], IconName> = {
  do: "run",
  ask_user: "chat",
  branch: "workflows",
};

/** How far the pointer must travel before a press on a card becomes a drag. */
const DRAG_SLOP = 4;

/** The strip at the stage's bottom the floating toolbar sits in (16px inset + 38px bar + gap). */
const TOOLBAR_ROOM = 60;
/** How many pixels a wheel/trackpad pinch step zooms by, per pixel of delta. */
const WHEEL_ZOOM = 0.0018;

// ---- motion ----
// Every movement here says what just happened: the view glides to where a button sent it,
// 整理 carries each card to its new place, a card arrives and leaves, an edge is drawn from its
// port, and a connection either clicks into the target or is shaken off. The first open of a
// workflow plays it in row by row. With reduced motion everything lands at once.
const GLIDE_MS = 340;
const TIDY_MS = 460;
/** The delay between one row of cards and the next when a workflow first opens. */
const STAGGER_MS = 70;
const ENTER_MS = 520;
const LEAVE_MS = 200;
const DRAW_MS = 560;
const PULSE_MS = 520;

const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

/** Each card's delay on first open: one step per row, top to bottom, capped at eight rows. */
function rowDelays(drafts: readonly StepDraft[]): Map<string, number> {
  const rows = [...new Set(drafts.map((draft) => Math.round(draft.y ?? 0)))].sort((a, b) => a - b);
  return new Map(
    drafts.map((draft) => [draft.id, Math.min(rows.indexOf(Math.round(draft.y ?? 0)), 8) * STAGGER_MS]),
  );
}

/** A card that was just removed, drawn where it was for as long as its exit takes. */
type Leaving = { draft: StepDraft; index: number; at: Point };

type Gesture =
  | { type: "pan"; startX: number; startY: number; view: View; moved: boolean }
  | { type: "node"; id: string; startX: number; startY: number; origin: Point; moved: boolean }
  | { type: "link"; from: string; choice: string | null; pointer: Point }
  | null;

export interface WorkflowCanvasProps {
  drafts: StepDraft[];
  edges: WorkflowEdge[];
  /** The walk the cards show their state from (the newest run), if there is one. */
  run: WorkflowRun | null;
  selection: Selection;
  /** One message per node that needs attention, shown on its card. */
  issues: Record<string, string>;
  onSelect: (selection: Selection) => void;
  onMove: (id: string, at: Point) => void;
  /** Refuses with a reason (shown on the canvas), or accepts by returning null. */
  onConnect: (from: string, to: string, choice: string | null) => string | null;
  onDeleteEdge: (edge: WorkflowEdge) => void;
  onDeleteNode: (id: string) => void;
  onAddNode: (at: Point | null) => void;
  onTidy: () => void;
  /** The node inspector, rendered beside the canvas — or floating over it in full screen. */
  inspector: ReactNode;
  /** A short line under the canvas toolbar (a refusal, a reason save is off). */
  notice?: string | null;
}

export function WorkflowCanvas(props: WorkflowCanvasProps) {
  const { drafts, edges, run, selection, issues, onSelect, onMove, onConnect, onDeleteEdge, onDeleteNode } = props;
  const [full, setFull] = useState(false);
  const stage = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [view, setView] = useState<View>({ x: 48, y: 48, k: 1 });
  // Until the user pans or zooms, the view follows the graph: it re-fits when the stage
  // resizes or full screen toggles. Their first gesture hands the view over to them.
  const [following, setFollowing] = useState(true);
  const gesture = useRef<Gesture>(null);
  const [linking, setLinking] = useState<{ from: string; choice: string | null; pointer: Point } | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [panning, setPanning] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);

  // ---- motion state ----
  // Cards and edges that are arriving (id/key -> delay in ms) or leaving (drawn where they
  // were until their exit ends). The first render plays the whole graph in, row by row; the
  // panel remounts the canvas per workflow, so opening another one plays it in again.
  const [entering, setEntering] = useState<Map<string, number>>(() =>
    prefersReducedMotion() ? new Map() : rowDelays(drafts),
  );
  const [drawing, setDrawing] = useState<Map<string, number>>(() => {
    if (prefersReducedMotion()) return new Map();
    const delays = rowDelays(drafts);
    return new Map(edges.map((edge) => [edgeKey(edge), (delays.get(edge.from) ?? 0) + 160]));
  });
  const [leaving, setLeaving] = useState<Leaving[]>([]);
  const [fading, setFading] = useState<{ key: string; d: string; head: string }[]>([]);
  // A connection that just landed on a card (a pulse) or was refused there (a shake).
  const [flash, setFlash] = useState<{ id: string; ok: boolean } | null>(null);
  // While 整理 runs, the positions cards are drawn at; hit-testing keeps the real ones.
  const [shown, setShown] = useState<Map<string, Point> | null>(null);
  const tweenFrom = useRef<Map<string, Point> | null>(null);
  const tween = useRef<number | null>(null);

  const placed: Placed[] = useMemo(
    () => drafts.map((draft) => ({ id: draft.id, x: draft.x ?? 0, y: draft.y ?? 0 })),
    [drafts],
  );
  const at = useMemo(() => new Map(placed.map((item) => [item.id, item] as const)), [placed]);
  const byId = useMemo(() => new Map(drafts.map((draft) => [draft.id, draft] as const)), [drafts]);
  const box = useMemo(() => bounds(placed), [placed]);
  /** Where a card is drawn this frame: mid-flight during 整理, else where it is. */
  const pos = (id: string): Point | undefined => shown?.get(id) ?? at.get(id);

  // ---- the view glides to where a button sends it; a gesture takes over at once ----
  const viewRef = useRef(view);
  viewRef.current = view;
  const glide = useRef<{ frame: number; target: View } | null>(null);
  const stopGlide = useCallback(() => {
    if (glide.current) cancelAnimationFrame(glide.current.frame);
    glide.current = null;
  }, []);
  const glideTo = useCallback(
    (target: View, ms: number = GLIDE_MS) => {
      stopGlide();
      const from = viewRef.current;
      const still =
        Math.abs(from.x - target.x) < 0.5 && Math.abs(from.y - target.y) < 0.5 && Math.abs(from.k - target.k) < 0.001;
      if (still || prefersReducedMotion()) {
        setView(target);
        return;
      }
      const start = performance.now();
      const step = (now: number) => {
        const t = Math.min(1, (now - start) / ms);
        const e = easeInOut(t);
        // Zoom is interpolated geometrically, so 25% -> 100% feels as even as 100% -> 400%.
        setView({
          x: from.x + (target.x - from.x) * e,
          y: from.y + (target.y - from.y) * e,
          k: from.k * Math.pow(target.k / from.k, e),
        });
        if (t < 1) glide.current = { frame: requestAnimationFrame(step), target };
        else glide.current = null;
      };
      glide.current = { frame: requestAnimationFrame(step), target };
    },
    [stopGlide],
  );
  useEffect(
    () => () => {
      stopGlide();
      if (tween.current !== null) cancelAnimationFrame(tween.current);
    },
    [stopGlide],
  );

  // ---- size and fit ----
  useLayoutEffect(() => {
    const element = stage.current;
    if (!element) return;
    const measure = () => setSize({ width: element.clientWidth, height: element.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [full]);

  // A short stage (a narrow window stacks the inspector under it) cannot spare 56px a side, and
  // the floating toolbar covers the bottom, so the graph is fitted into the area above it.
  const pad = Math.round(Math.min(56, Math.max(20, Math.min(size.width, size.height) / 10)));
  const fitted = useCallback(
    (of: typeof box = box) => fitView(of, size.width, Math.max(1, size.height - TOOLBAR_ROOM), pad),
    [box, size.width, size.height, pad],
  );

  const fit = useCallback(() => {
    glideTo(fitted());
    setFollowing(true);
  }, [fitted, glideTo]);

  // A resize or full-screen switch re-fits at once: gliding would lag behind the window. Turning
  // `following` on is not a reason here: whoever turns it on (⌘0, 整理) already glides.
  const followingRef = useRef(following);
  followingRef.current = following;
  useLayoutEffect(() => {
    if (!followingRef.current || size.width <= 0) return;
    stopGlide();
    setView(fitted());
    // `box` is left out on purpose: dragging a card must not re-fit the view under the pointer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size.width, size.height, full]);

  // Adding or removing a node re-fits too, gliding, but only while the view follows the graph.
  const identity = drafts.map((draft) => draft.id).join("|");
  const firstIdentity = useRef(true);
  useLayoutEffect(() => {
    if (firstIdentity.current) {
      firstIdentity.current = false;
      return;
    }
    if (following && size.width > 0) glideTo(fitted());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity]);

  // ---- arrivals and departures ----
  const previous = useRef<{ drafts: StepDraft[]; edges: WorkflowEdge[] }>({ drafts, edges });
  useLayoutEffect(() => {
    const before = previous.current;
    previous.current = { drafts, edges };
    if (prefersReducedMotion() || (before.drafts === drafts && before.edges === edges)) return;
    const nowIds = new Set(drafts.map((draft) => draft.id));
    const wasIds = new Set(before.drafts.map((draft) => draft.id));
    const added = drafts.filter((draft) => !wasIds.has(draft.id)).map((draft) => draft.id);
    if (added.length) setEntering((current) => new Map([...current, ...added.map((id) => [id, 0] as const)]));
    const gone = before.drafts.flatMap((draft, index) =>
      nowIds.has(draft.id) ? [] : [{ draft, index, at: { x: draft.x ?? 0, y: draft.y ?? 0 } }],
    );
    if (gone.length) setLeaving((current) => [...current, ...gone]);

    const nowKeys = new Set(edges.map(edgeKey));
    const wasKeys = new Set(before.edges.map(edgeKey));
    const drawn = edges.map(edgeKey).filter((key) => !wasKeys.has(key));
    if (drawn.length) setDrawing((current) => new Map([...current, ...drawn.map((key) => [key, 0] as const)]));
    const oldAt = new Map(before.drafts.map((draft) => [draft.id, draft] as const));
    const oldPlaced = before.drafts.map((draft) => ({ id: draft.id, x: draft.x ?? 0, y: draft.y ?? 0 }));
    const faded = before.edges.flatMap((edge) => {
      if (nowKeys.has(edgeKey(edge))) return [];
      const source = oldAt.get(edge.from);
      const target = oldAt.get(edge.to);
      if (!source || !target) return [];
      const { d, head } = routeEdge(
        edge,
        { x: source.x ?? 0, y: source.y ?? 0 },
        source,
        { x: target.x ?? 0, y: target.y ?? 0 },
        oldPlaced,
      );
      return [{ key: `${edgeKey(edge)}@${performance.now()}`, d, head }];
    });
    if (faded.length) setFading((current) => [...current, ...faded]);
  }, [drafts, edges]);

  // ---- 整理: every card travels from where it was to its new place, and the view follows ----
  const tidy = () => {
    tweenFrom.current = new Map(placed.map((item) => [item.id, pos(item.id) ?? item] as const));
    props.onTidy();
    setFollowing(true);
  };
  useLayoutEffect(() => {
    const from = tweenFrom.current;
    if (!from) return;
    tweenFrom.current = null;
    if (size.width > 0) glideTo(fitted(), TIDY_MS);
    if (prefersReducedMotion()) return;
    if (tween.current !== null) cancelAnimationFrame(tween.current);
    const to = new Map(placed.map((item) => [item.id, { x: item.x, y: item.y }] as const));
    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / TIDY_MS);
      const e = easeInOut(t);
      const frame = new Map<string, Point>();
      for (const [id, end] of to) {
        const begin = from.get(id) ?? end;
        frame.set(id, { x: begin.x + (end.x - begin.x) * e, y: begin.y + (end.y - begin.y) * e });
      }
      setShown(t < 1 ? frame : null);
      tween.current = t < 1 ? requestAnimationFrame(step) : null;
    };
    tween.current = requestAnimationFrame(step);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placed]);

  // ---- full screen ----
  useEffect(() => {
    if (!full) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Escape first leaves a half-drawn edge, then the selection, then full screen.
      if (gesture.current?.type === "link") return;
      event.preventDefault();
      event.stopPropagation();
      setFull(false);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [full]);

  // ---- toast ----
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // ---- coordinates ----
  const stagePoint = (clientX: number, clientY: number): Point => {
    const rect = stage.current?.getBoundingClientRect();
    return { x: clientX - (rect?.left ?? 0), y: clientY - (rect?.top ?? 0) };
  };
  const worldPoint = (clientX: number, clientY: number): Point => {
    const local = stagePoint(clientX, clientY);
    return toWorld(view, local.x, local.y);
  };

  // ---- wheel: pinch/ctrl zooms at the pointer, a plain wheel pans ----
  useEffect(() => {
    const element = stage.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      stopGlide();
      setFollowing(false);
      const rect = element.getBoundingClientRect();
      const cx = event.clientX - rect.left;
      const cy = event.clientY - rect.top;
      setView((current) => {
        if (event.ctrlKey || event.metaKey) {
          return zoomAt(current, current.k * Math.exp(-event.deltaY * WHEEL_ZOOM * 4), cx, cy);
        }
        return { ...current, x: current.x - event.deltaX, y: current.y - event.deltaY };
      });
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, [full, stopGlide]);

  const zoomBy = (factor: number) => {
    setFollowing(false);
    const from = glide.current?.target ?? view;
    glideTo(zoomAt(from, from.k * factor, size.width / 2, size.height / 2), 200);
  };

  // ---- pointer gestures ----
  const onStageDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 && event.button !== 1) return;
    const target = event.target as HTMLElement;
    if (target.closest("[data-canvas-ui]")) return;
    const port = target.closest<HTMLElement>("[data-port]");
    const card = target.closest<HTMLElement>("[data-node]");
    stage.current?.setPointerCapture(event.pointerId);
    stopGlide();
    if (port && event.button === 0) {
      const from = port.dataset.port!;
      const choice = port.dataset.choice ?? null;
      const pointer = worldPoint(event.clientX, event.clientY);
      gesture.current = { type: "link", from, choice, pointer };
      setLinking({ from, choice, pointer });
      onSelect({ kind: "node", id: from });
      return;
    }
    if (card && event.button === 0) {
      const id = card.dataset.node!;
      const item = at.get(id);
      if (!item) return;
      gesture.current = {
        type: "node",
        id,
        startX: event.clientX,
        startY: event.clientY,
        origin: { x: item.x, y: item.y },
        moved: false,
      };
      return;
    }
    // Empty canvas (or the middle button anywhere): pan.
    gesture.current = { type: "pan", startX: event.clientX, startY: event.clientY, view, moved: false };
    setPanning(true);
  };

  const onStageMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const current = gesture.current;
    if (!current) {
      const card = (event.target as HTMLElement).closest<HTMLElement>("[data-node]");
      setHover(card?.dataset.node ?? null);
      return;
    }
    if (current.type === "pan") {
      const dx = event.clientX - current.startX;
      const dy = event.clientY - current.startY;
      if (!current.moved && Math.hypot(dx, dy) < DRAG_SLOP) return;
      current.moved = true;
      setFollowing(false);
      setView({ ...current.view, x: current.view.x + dx, y: current.view.y + dy });
      return;
    }
    if (current.type === "node") {
      const dx = (event.clientX - current.startX) / view.k;
      const dy = (event.clientY - current.startY) / view.k;
      if (!current.moved && Math.hypot(dx * view.k, dy * view.k) < DRAG_SLOP) return;
      current.moved = true;
      setDragging(current.id);
      onMove(current.id, { x: clampCoord(current.origin.x + dx), y: clampCoord(current.origin.y + dy) });
      return;
    }
    const pointer = worldPoint(event.clientX, event.clientY);
    current.pointer = pointer;
    setLinking({ from: current.from, choice: current.choice, pointer });
    const target = hitNode(placed, pointer);
    setHover(target && target !== current.from ? target : null);
  };

  const onStageUp = (event: React.PointerEvent<HTMLDivElement>) => {
    const current = gesture.current;
    gesture.current = null;
    setPanning(false);
    setDragging(null);
    stage.current?.releasePointerCapture?.(event.pointerId);
    if (!current) return;
    if (current.type === "pan") {
      // A click on the empty canvas (no pan) clears the selection.
      if (!current.moved) onSelect(null);
      return;
    }
    if (current.type === "node") {
      if (!current.moved) onSelect({ kind: "node", id: current.id });
      return;
    }
    setLinking(null);
    setHover(null);
    const target = hitNode(placed, worldPoint(event.clientX, event.clientY));
    if (!target || target === current.from) return;
    const refusal = onConnect(current.from, target, current.choice);
    if (refusal) setToast(refusal);
    setFlash({ id: target, ok: !refusal });
  };

  // ---- keyboard on the stage ----
  const onStageKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, select, [contenteditable='true']")) return;
    if (event.key === "Escape" && linking) {
      gesture.current = null;
      setLinking(null);
      return;
    }
    if (event.key === "Escape" && selection) {
      onSelect(null);
      return;
    }
    if ((event.key === "Delete" || event.key === "Backspace") && selection) {
      event.preventDefault();
      if (selection.kind === "node") onDeleteNode(selection.id);
      else {
        const edge = edges.find((item) => edgeKey(item) === selection.key);
        if (edge) onDeleteEdge(edge);
      }
      return;
    }
    if (event.key === "0" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      fit();
      return;
    }
    if ((event.key === "=" || event.key === "+") && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      zoomBy(1.2);
      return;
    }
    if (event.key === "-" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      zoomBy(1 / 1.2);
    }
  };

  // ---- motion clean-up: each state clears once the longest animation it started has ended ----
  useEffect(() => {
    if (entering.size === 0) return;
    const longest = Math.max(...entering.values()) + ENTER_MS;
    const timer = window.setTimeout(() => setEntering(new Map()), longest);
    return () => window.clearTimeout(timer);
  }, [entering]);
  useEffect(() => {
    if (drawing.size === 0) return;
    const longest = Math.max(...drawing.values()) + DRAW_MS;
    const timer = window.setTimeout(() => setDrawing(new Map()), longest);
    return () => window.clearTimeout(timer);
  }, [drawing]);
  useEffect(() => {
    if (leaving.length === 0 && fading.length === 0) return;
    const timer = window.setTimeout(() => {
      setLeaving([]);
      setFading([]);
    }, LEAVE_MS);
    return () => window.clearTimeout(timer);
  }, [leaving, fading]);
  useEffect(() => {
    if (!flash) return;
    const timer = window.setTimeout(() => setFlash(null), PULSE_MS);
    return () => window.clearTimeout(timer);
  }, [flash]);

  // ---- drawing ----
  const stateOf = (id: string): WorkflowStepState | null => (run ? stateOfNode(run, id) : null);
  const transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
  const linkSource = linking ? pos(linking.from) : null;
  const linkDraft = linking ? byId.get(linking.from) : null;
  const linkStart = linkSource && linkDraft ? outPoint(linkSource, linkDraft, linking!.choice) : null;
  const selectedEdge = selection?.kind === "edge" ? edges.find((edge) => edgeKey(edge) === selection.key) : null;

  const edgeLayer = edges.map((edge) => {
    const from = pos(edge.from);
    const to = pos(edge.to);
    const source = byId.get(edge.from);
    if (!from || !to || !source) return null;
    const drawnAt = placed.map((item) => ({ ...item, ...(pos(item.id) ?? item) }));
    const { d, mid, head, loop } = routeEdge(edge, from, source, to, drawnAt);
    const taken = loop ? loopsTaken(edge, run) : 0;
    const key = edgeKey(edge);
    const isSelected = selection?.kind === "edge" && selection.key === key;
    const touches =
      (selection?.kind === "node" && (selection.id === edge.from || selection.id === edge.to)) ||
      hover === edge.from ||
      hover === edge.to;
    const fromState = stateOf(edge.from);
    const dead = edgeDead(edge, run);
    const live = edgeLive(edge, run);
    const drawDelay = drawing.get(key);
    const cls = [
      "wfg-edge",
      loop ? "is-loop" : "",
      isSelected ? "is-selected" : "",
      touches ? "is-near" : "",
      dead ? "is-dead" : "",
      live ? "is-live" : "",
      drawDelay !== undefined ? "is-drawing" : "",
    ]
      .filter(Boolean)
      .join(" ");
    return (
      <g key={key} className={cls} style={drawDelay ? ({ "--delay": `${drawDelay}ms` } as React.CSSProperties) : undefined}>
        <path className="wfg-edge-line" d={d} pathLength={1} />
        <path className="wfg-edge-head" d={head} />
        {/* A running walk sends a dot down every edge out of the step it is on. */}
        {fromState?.status === "running" && !loop && <path className="wfg-edge-flow" d={d} pathLength={1} />}
        <path
          className="wfg-edge-hit"
          d={d}
          onPointerDown={(event) => {
            event.stopPropagation();
            onSelect({ kind: "edge", key });
          }}
        />
        {/* A loop says how many rounds it allows (and, during a walk, how many it took) on the
            arc itself: the bound is the point of a loop, so it is never hidden in a form. */}
        {loop && !isSelected && (
          <foreignObject x={mid.x - 30} y={mid.y - 12} width={60} height={24} className="wfg-edge-tools">
            <span className="wfg-loop-chip" title={`最多循环 ${edge.max_loops} 轮`}>
              <Icon name="repeat" size={11} />
              {run ? `${taken}/${edge.max_loops}` : `×${edge.max_loops}`}
            </span>
          </foreignObject>
        )}
        {isSelected && (
          <foreignObject x={mid.x - 14} y={mid.y - 14} width={28} height={28} className="wfg-edge-tools">
            <button
              type="button"
              className="wfg-edge-delete"
              aria-label="删除这条连线"
              title="删除这条连线（Delete）"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={() => onDeleteEdge(edge)}
            >
              <Icon name="close" size={13} />
            </button>
          </foreignObject>
        )}
      </g>
    );
  });

  const cards = drafts.map((draft, index) => {
    const item = pos(draft.id)!;
    const state = stateOf(draft.id);
    const issue = issues[draft.id];
    const isSelected = selection?.kind === "node" && selection.id === draft.id;
    const isTarget = linking !== null && hover === draft.id;
    const enterDelay = entering.get(draft.id);
    const flashed = flash?.id === draft.id ? (flash.ok ? "is-linked" : "is-refused") : "";
    const cls = [
      "wfg-node",
      `kind-${draft.kind}`,
      isSelected ? "is-selected" : "",
      isTarget ? "is-target" : "",
      dragging === draft.id ? "is-dragging" : "",
      state ? `state-${state.status}` : "",
      state && run && isQueued(run, state) ? "state-queued" : "",
      issue ? "has-issue" : "",
      enterDelay !== undefined ? "is-entering" : "",
      flashed,
    ]
      .filter(Boolean)
      .join(" ");
    const title = draft.title.trim() || draft.instruction.trim().split("\n")[0]?.slice(0, 40) || "未命名";
    const ports = outPorts(draft);
    return (
      <div
        key={draft.id}
        className={cls}
        style={
          {
            // Position lives in `translate`, which the browser applies after `scale`/`rotate`/
            // `transform`: hover, drag and entry effects then act about the card's own centre.
            // In `transform` they would also scale the card's distance from the origin.
            translate: `${item.x}px ${item.y}px`,
            ...(enterDelay ? { "--delay": `${enterDelay}ms` } : {}),
          } as React.CSSProperties
        }
        data-node={draft.id}
        role="button"
        tabIndex={0}
        aria-pressed={isSelected}
        aria-label={`第 ${index + 1} 个节点：${title}，${KIND_LABELS[draft.kind]}${issue ? `，${issue}` : ""}`}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onSelect({ kind: "node", id: draft.id });
          }
        }}
        onFocus={() => setHover(draft.id)}
        onBlur={() => setHover(null)}
      >
        <span className="wfg-node-in" aria-hidden="true" />
        <span className="wfg-node-icon" aria-hidden="true">
          <Icon name={KIND_ICON[draft.kind]} size={15} />
        </span>
        <span className="wfg-node-body">
          <span className="wfg-node-title">{title}</span>
          <span className="wfg-node-meta">
            <span className="wfg-node-kind">{KIND_LABELS[draft.kind]}</span>
            {draft.kind === "do" && draft.gate === "confirm" && <span className="wfg-node-flag">先问我</span>}
            {issue ? (
              <span className="wfg-node-issue">{issue}</span>
            ) : state ? (
              <span className={`wfg-node-state${run && isQueued(run, state) ? " state-queued" : ` state-${state.status}`}`}>
                {run ? stepWord(run, state) : ""}
                {state.choice ? ` · ${state.choice}` : ""}
              </span>
            ) : null}
          </span>
        </span>
        <span className="wfg-node-index" aria-hidden="true">
          {index + 1}
        </span>
        <span className="wfg-node-ports">
          {ports.map((port) => (
            <span
              key={port.choice ?? "out"}
              className={port.choice ? "wfg-port has-label" : "wfg-port"}
              style={{ left: port.dx }}
              data-port={draft.id}
              {...(port.choice ? { "data-choice": port.choice } : {})}
              title={port.choice ? `拖出「${port.choice}」这一支` : "拖到另一个节点上连线"}
            >
              <span className="wfg-port-dot" />
              {port.choice && <span className="wfg-port-label">{port.choice}</span>}
            </span>
          ))}
        </span>
      </div>
    );
  });

  // Removed cards and edges, drawn where they were while they fade out. Not interactive.
  const leavingCards = leaving.map(({ draft, index, at: where }) => (
    <div
      key={`leaving-${draft.id}`}
      className={`wfg-node kind-${draft.kind} is-leaving`}
      style={{ translate: `${where.x}px ${where.y}px` }}
      aria-hidden="true"
    >
      <span className="wfg-node-icon">
        <Icon name={KIND_ICON[draft.kind]} size={15} />
      </span>
      <span className="wfg-node-body">
        <span className="wfg-node-title">{draft.title.trim() || "未命名"}</span>
        <span className="wfg-node-meta">
          <span className="wfg-node-kind">{KIND_LABELS[draft.kind]}</span>
        </span>
      </span>
      <span className="wfg-node-index">{index + 1}</span>
    </div>
  ));
  const fadingEdges = fading.map((item) => (
    <g key={item.key} className="wfg-edge is-leaving">
      <path className="wfg-edge-line" d={item.d} />
      <path className="wfg-edge-head" d={item.head} />
    </g>
  ));

  const empty = drafts.length === 0;
  const zoomPercent = Math.round(view.k * 100);

  const body = (
    <div className={full ? "wfg is-full" : "wfg"}>
      <div
        ref={stage}
        className={["wfg-stage", panning ? "is-panning" : "", linking ? "is-linking" : ""].filter(Boolean).join(" ")}
        tabIndex={0}
        role="application"
        aria-label="工作流画布。拖动空白处移动画面，拖动节点调整位置，从节点底部的圆点拖到另一个节点连线；Delete 删除选中的节点或连线，⌘0 适应画面。"
        onPointerDown={onStageDown}
        onPointerMove={onStageMove}
        onPointerUp={onStageUp}
        onPointerCancel={onStageUp}
        onPointerLeave={() => !gesture.current && setHover(null)}
        onKeyDown={onStageKey}
        onDoubleClick={(event) => {
          const target = event.target as HTMLElement;
          if (target.closest("[data-node], [data-canvas-ui]")) return;
          const point = worldPoint(event.clientX, event.clientY);
          props.onAddNode({ x: point.x - NODE_W / 2, y: point.y - NODE_H / 2 });
        }}
        style={
          {
            "--grid-size": `${24 * view.k}px`,
            "--grid-x": `${view.x}px`,
            "--grid-y": `${view.y}px`,
          } as React.CSSProperties
        }
      >
        <div className="wfg-world" style={{ transform }}>
          <svg className="wfg-edges" aria-hidden="true">
            {/* The arrowhead belongs to each edge (`.wfg-edge-head`) rather than an SVG
                marker, so it takes the edge's colour and state and keeps one size at any stroke. */}
            {fadingEdges}
            {edgeLayer}
            {linkStart && linking && (() => {
              // Connecting back to a card the source already leads to makes a loop: the ghost
              // takes the loop's route and colour, so it is clear before letting go.
              const target = hover ? pos(hover) : undefined;
              const back = !!hover && reaches(edges, hover, linking.from);
              const route = target
                ? routeEdge(
                    { when: linking.choice, max_loops: back ? 1 : null },
                    linkSource!,
                    linkDraft!,
                    target,
                    placed,
                  )
                : null;
              return (
                <>
                  <path
                    className={["wfg-edge-ghost", route ? "is-snapped" : "", back ? "is-loop" : ""].filter(Boolean).join(" ")}
                    d={route ? route.d : edgePath(linkStart, linking.pointer).d}
                  />
                  {route && <path className={back ? "wfg-edge-head is-ghost is-loop" : "wfg-edge-head is-ghost"} d={route.head} />}
                  {!route && <circle className="wfg-edge-ghost-tip" cx={linking.pointer.x} cy={linking.pointer.y} r={5} />}
                </>
              );
            })()}
          </svg>
          {leavingCards}
          {cards}
        </div>

        {empty && (
          <div className="wfg-empty" data-canvas-ui>
            <p>画布是空的。</p>
            <button type="button" className="primary-button" onClick={() => props.onAddNode(null)}>
              <Icon name="plus" size={14} />
              加第一个节点
            </button>
          </div>
        )}

        <div className="wfg-toolbar" data-canvas-ui role="toolbar" aria-label="画布">
          <button type="button" className="wfg-tool" onClick={() => props.onAddNode(null)} title="加一个节点（双击画布空白处也可以）">
            <Icon name="plus" size={15} />
            <span>节点</span>
          </button>
          <span className="wfg-tool-sep" aria-hidden="true" />
          <button type="button" className="wfg-tool icon" onClick={() => zoomBy(1 / 1.2)} aria-label="缩小" title="缩小（⌘−）" disabled={view.k <= clampZoom(0) + 0.001}>
            −
          </button>
          <button type="button" className="wfg-tool zoom" onClick={fit} title="适应画面（⌘0）">
            {zoomPercent}%
          </button>
          <button type="button" className="wfg-tool icon" onClick={() => zoomBy(1.2)} aria-label="放大" title="放大（⌘+）" disabled={view.k >= clampZoom(99) - 0.001}>
            +
          </button>
          <span className="wfg-tool-sep" aria-hidden="true" />
          <button type="button" className="wfg-tool" onClick={tidy} title="按依赖关系重新排列">
            <Icon name="workflows" size={15} />
            <span>整理</span>
          </button>
          <span className="wfg-tool-sep" aria-hidden="true" />
          <button
            type="button"
            className="wfg-tool icon"
            onClick={() => {
              setFull((value) => !value);
              followingRef.current = true;
              setFollowing(true);
            }}
            aria-label={full ? "退出全屏" : "全屏画布"}
            title={full ? "退出全屏（Esc）" : "全屏画布"}
            aria-pressed={full}
          >
            <Icon name={full ? "collapse" : "expand"} size={15} />
          </button>
        </div>

        {/* One line at the top: a refused gesture first, then what is selected, then what to fix. */}
        {toast ? (
          <p className="wfg-notice is-toast" data-canvas-ui role="status">
            {toast}
          </p>
        ) : selectedEdge ? (
          <p className="wfg-hint" data-canvas-ui>
            已选中连线{selectedEdge.when ? `「${selectedEdge.when}」` : ""}，按 Delete 或点它中间的 × 删除
          </p>
        ) : props.notice ? (
          <p className="wfg-notice" data-canvas-ui role="status">
            {props.notice}
          </p>
        ) : null}
      </div>

      {full && props.inspector && <aside className="wfg-float">{props.inspector}</aside>}
    </div>
  );

  // Full screen is the canvas alone over the whole window — the rail, the tabs and the panel's
  // list are under it. A portal keeps any ancestor's transform or overflow from clipping it.
  if (full) return createPortal(<div className="wfg-portal">{body}</div>, document.body);
  return body;
}
