// The run-trace sheet: a run's hand-off graph above a replayed step list. Opened from a chat's
// 查看过程 link, the right-click menu, or a member's profile. It polls while the run works and
// offers a play/step replay that respects reduced-motion. The graph and the step list stay in
// lockstep: each step owns an edge (or a node), so hovering either side lights up the other.

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Agent, Message, Run, RunTrace as RunTraceData } from "../api/types";
import { errorMessage } from "../api/client";
import { resolveAppearance } from "../bot/catalog";
import { swatchOf } from "../bot/palette";
import { prefersReducedMotion } from "../lib/motion";
import { formatTokens } from "../lib/team";
import { momentStamp } from "../lib/time";
import { edgeKindLabel, labelWidth, layoutRunGraph, type GraphNode } from "../lib/runGraph";
import {
  activeNodeOf,
  buildSteps,
  edgeOrder,
  stepDuration,
  type RunStep,
  type StepKind,
} from "../lib/runReplay";
import type { RunRef } from "../lib/tabs";
import { store, useTeam } from "../state/store";
import { Avatar } from "./Avatar";
import { TOOL_LABEL } from "../lib/toolLabels";
import { ActionDetailPanel } from "./ChatView";
import { TOOL_ICON } from "../lib/toolIcons";
import { Icon, type IconName } from "./Icon";
import { MessageBody } from "./MessageBody";
import { Dialog } from "./Panels";

const REFRESH_MS = 2000;
/** Stable empty list: a fresh `[]` from a selector is a new snapshot on every read, which React
 *  treats as an endless store change (the page dies when a restored tab mounts before the
 *  workspace arrives). */
const NO_AGENTS: Agent[] = [];
const NODE_R = 21;

type Focus =
  | { type: "node"; id: string }
  | { type: "edge"; index: number }
  | { type: "step"; index: number }
  | null;

/** The run's outcome in one word, for the status pill. */
function statusWord(run: Run): string {
  switch (run.status) {
    case "running":
      return "进行中";
    case "completed":
      return "完成";
    case "stopped":
      return "停止";
    case "error":
      return "出错";
    case "limited":
      return "受限";
    case "queued":
      return "排队中";
  }
}

const STATUS_TONE: Record<Run["status"], string> = {
  running: "accent",
  completed: "ok",
  stopped: "ink",
  error: "danger",
  limited: "warn",
  queued: "ink",
};

/** How long the run took, in human terms. */
function runDuration(run: Run): string {
  const end = run.finished_at ? Date.parse(run.finished_at) : Date.now();
  const ms = end - Date.parse(run.created_at);
  if (!Number.isFinite(ms) || ms < 0) return "";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
}

/** A step's time offset from the start of the run: 开始 / +12 秒 / +1 分 5 秒. */
function offsetLabel(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 1) return "开始";
  if (seconds < 60) return `+${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `+${minutes} 分 ${rest} 秒` : `+${minutes} 分`;
}

/** The short chip word on a step card. */
const CHIP: Record<StepKind, string> = {
  ask: "发起",
  consult: "咨询",
  dm: "私信",
  revise: "打回",
  reply: "回复",
  tool: "工具",
  mention: "@ 接力",
};

/** The tick colour on the scrubber, by step kind. */
const TICK_TONE: Record<StepKind, string> = {
  ask: "var(--ink-3)",
  consult: "var(--ink-3)",
  dm: "var(--ink-3)",
  revise: "var(--warn)",
  reply: "var(--ok)",
  tool: "var(--accent)",
  mention: "var(--accent)",
};

/** ①②③… for the edge order pills. */
function orderGlyph(n: number): string {
  if (n >= 1 && n <= 20) return String.fromCodePoint(0x2460 + n - 1);
  return `${n}`;
}

/** What an action row says, with the tool's word as the fallback. */
function actionText(message: Message): string {
  const action = message.action;
  const tool = action?.tool_name ?? "";
  const label = TOOL_LABEL[tool] ?? "工具调用";
  const base = action?.summary || message.content || label;
  const error = action?.error && !(action.summary ?? "").includes(action.error) ? action.error : null;
  return error ? `${base}：${error}` : base;
}

interface Visual {
  seed: string | null;
  appearance: Agent["appearance"];
  fill: string;
}

function agentVisual(agents: readonly Agent[], id: string): Visual {
  if (id === "user") return { seed: null, appearance: null, fill: "var(--tint-strong)" };
  const agent = agents.find((a) => a.id === id);
  const seed = agent?.avatar_seed ?? null;
  const appearance = agent?.appearance ?? null;
  return { seed, appearance, fill: swatchOf(resolveAppearance(appearance, seed)).fill };
}

// ---- graph rendering -------------------------------------------------------

interface GraphNodeViewProps {
  node: GraphNode;
  visual: Visual;
  isActive: boolean;
  onHover: (id: string) => void;
  onLeave: () => void;
  onClick: (id: string) => void;
}

const GraphNodeView = memo(function GraphNodeView({
  node,
  visual,
  isActive,
  onHover,
  onLeave,
  onClick,
}: GraphNodeViewProps) {
  const isUser = node.kind === "user";
  return (
    <g
      className={`run-node${isUser ? " user" : ""}${isActive ? " active" : ""}`}
      transform={`translate(${node.x} ${node.y})`}
      onMouseEnter={() => onHover(node.id)}
      onMouseLeave={onLeave}
      onFocus={() => onHover(node.id)}
      onBlur={onLeave}
      onClick={() => onClick(node.id)}
      tabIndex={0}
      role="button"
      aria-label={node.name}
    >
      <circle className="run-node-ring" r={NODE_R} stroke={visual.fill} />
      {isUser ? (
        <>
          <circle className="run-node-user" r={19} />
          <text className="run-node-user-label" textAnchor="middle" dominantBaseline="central">
            你
          </text>
        </>
      ) : null}
      <text className="run-node-name" textAnchor="middle" y={NODE_R + 15}>
        {node.name}
      </text>
    </g>
  );
});

/** What the speaking node says during the current step, shown as a bubble on the graph. */
export interface Speech {
  /** The node the bubble hangs from: the user for the trigger, else whoever acts. */
  nodeID: string;
  kind: StepKind;
  /** A short lead before the text: the tool's name, or who a hand-off went to. */
  lead: string | null;
  text: string;
  /** How long the text takes to type out, so it finishes inside the step's own time. */
  revealMs: number;
  /** Restarts the typing when the replay moves, even onto the same node. */
  stepIndex: number;
}

interface RunGraphProps {
  layout: ReturnType<typeof layoutRunGraph>;
  visuals: ReadonlyMap<string, Visual>;
  edgeNumbers: readonly number[];
  reachedEdges: ReadonlySet<number>;
  activeEdge: number | null;
  /** The replay step on screen; restarts the current edge's draw-in and packet when it changes. */
  activeStep: number;
  activeNode: string | null;
  speech: Speech | null;
  /** The current step is a reply riding back along the hand-off that reached its author. */
  replying: boolean;
  focus: Focus;
  reduced: boolean;
  onHoverNode: (id: string) => void;
  onLeave: () => void;
  onClickNode: (id: string) => void;
  onHoverEdge: (index: number) => void;
  onClickEdge: (index: number) => void;
}

/** Markdown read aloud: the marks a bubble has no room to render, gone. */
function plainText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}|[-*+]|\d+\.|>)\s+/gm, "")
    .replace(/[*_`~|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

const SPEECH_CHARS = 90;

/** The speech bubble: the step's words typed out over its own time, then held. */
function SpeechBubble({ speech, x, y, width, height, reduced }: { speech: Speech; x: number; y: number; width: number; height: number; reduced: boolean }) {
  const text = speech.text.length > SPEECH_CHARS ? `${speech.text.slice(0, SPEECH_CHARS)}…` : speech.text;
  const [shown, setShown] = useState(reduced ? text.length : 0);
  useEffect(() => {
    if (reduced) {
      setShown(text.length);
      return;
    }
    setShown(0);
    const started = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const next = Math.min(text.length, Math.ceil(((now - started) / speech.revealMs) * text.length));
      setShown(next);
      if (next < text.length) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [text, speech.revealMs, speech.stepIndex, reduced]);
  // Above a node in the lower half, below one in the upper half (clear of its name); pinned to
  // the node's side near either edge so the bubble never leaves the card.
  const below = y < height / 2;
  const side = x < width * 0.3 ? "start" : x > width * 0.7 ? "end" : "middle";
  return (
    <div
      key={speech.stepIndex}
      className={`run-speech kind-${speech.kind} ${below ? "below" : "above"} ${side}`}
      style={{ left: `${(x / width) * 100}%`, top: `${(y / height) * 100}%` }}
      aria-live="polite"
    >
      {speech.lead && <span className="run-speech-lead">{speech.lead}</span>}
      <span className="run-speech-text">
        {text.slice(0, shown)}
        {shown < text.length && <span className="run-speech-caret" aria-hidden="true" />}
      </span>
    </div>
  );
}

const RunGraph = memo(function RunGraph({
  layout,
  visuals,
  edgeNumbers,
  reachedEdges,
  activeEdge,
  activeStep,
  activeNode,
  speech,
  replying,
  focus,
  reduced,
  onHoverNode,
  onLeave,
  onClickNode,
  onHoverEdge,
  onClickEdge,
}: RunGraphProps) {
  // The faces are HTML laid over the SVG, never inside it: WebKit (the macOS window) draws a
  // transformed element inside <foreignObject> relative to the page, not to its node, so an
  // animated face jumped to the window's top-left corner while its ring stayed empty.
  const { width, height } = layout.bounds;
  const speaker = speech ? layout.nodes.find((node) => node.id === speech.nodeID) : undefined;
  return (
    <div
      className="run-graph-stage"
      style={{ width, aspectRatio: `${width} / ${height}`, ["--graph-ratio" as string]: width / height }}
    >
    <svg
      className="run-graph"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="运行交接图"
    >
      <defs>
        {/* One arrowhead per tone, filled through CSS: WebKit has no `context-stroke`, which
            left every head black and all but invisible on the dark card. */}
        {(["ink", "accent", "warn"] as const).map((tone) => (
          <marker
            key={tone}
            id={`run-edge-arrow-${tone}`}
            className={`run-edge-head ${tone}`}
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
        ))}
      </defs>
      {layout.edges.map((edge, index) => {
        const state =
          activeEdge === index ? "current" : reachedEdges.has(index) ? "past" : "future";
        // A reply travels the hand-off that reached its author back the other way: the arrow
        // turns round at the far end and runs home to whoever asked.
        const turning = state === "current" && replying;
        const isFocus = focus?.type === "edge" && focus.index === index;
        const word = turning ? "回复" : edgeKindLabel(edge.kind);
        const pillWidth = labelWidth(word);
        const tone = edge.kind === "revise" ? "warn" : edge.kind === "mention" ? "accent" : "ink";
        return (
          <g
            key={`${edge.from}-${edge.to}-${index}`}
            className={`run-edge ${edge.kind} ${edge.back ? "back" : ""} ${state}${turning ? " replying" : ""}${isFocus ? " focus" : ""}`}
            onMouseEnter={() => onHoverEdge(index)}
            onMouseLeave={onLeave}
            onFocus={() => onHoverEdge(index)}
            onBlur={onLeave}
            onClick={() => onClickEdge(index)}
            tabIndex={0}
            role="button"
            aria-label={`${word}${edge.label ? `：${edge.label}` : ""}`}
          >
            {/* Every hand-off and every reply moves the same way: the line draws in behind an
                arrowhead that rides the curve, forward from the asker for a hand-off, and for a
                reply turned round at the far end and back home. While it travels, the edge's own
                head gives way to it (pathLength 1 makes the dash maths exact for any curve). */}
            <path
              d={edge.path}
              className="run-edge-path"
              markerEnd={state === "current" ? undefined : `url(#run-edge-arrow-${tone})`}
            />
            {state === "current" && (
              <path
                key={`draw-${activeStep}`}
                d={edge.path}
                className={turning ? "run-edge-draw back-draw" : `run-edge-draw ${tone}`}
                pathLength={1}
              />
            )}
            {state === "current" && (
              // Keyed by the step, so replaying the same edge restarts it; CSS offset-path
              // animates on insertion, unlike SMIL inside an already-running SVG. Reduced motion
              // shows the head where it ends.
              <path
                key={`head-${activeStep}`}
                d="M -6 -5 L 5 0 L -6 5 z"
                className={[
                  "run-edge-travel",
                  turning ? "back" : `forward ${tone}`,
                  reduced ? "settled" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                style={{ offsetPath: `path("${edge.path}")` }}
              />
            )}
            <g className="run-edge-label" transform={`translate(${edge.labelAnchor.x} ${edge.labelAnchor.y})`}>
              <rect x={-pillWidth / 2} y={-9} width={pillWidth} height={18} rx={9} />
              <text textAnchor="middle" dominantBaseline="central">
                {orderGlyph(edgeNumbers[index] ?? index + 1)} {word}
              </text>
            </g>
          </g>
        );
      })}
      {layout.nodes.map((node) => (
        <GraphNodeView
          key={node.id}
          node={node}
          visual={visuals.get(node.id) ?? { seed: null, appearance: null, fill: "var(--tint-strong)" }}
          isActive={activeNode === node.id}
          onHover={onHoverNode}
          onLeave={onLeave}
          onClick={onClickNode}
        />
      ))}
    </svg>
      {layout.nodes
        .filter((node) => node.kind !== "user")
        .map((node) => {
          const visual = visuals.get(node.id);
          const active = activeNode === node.id;
          return (
            <span
              key={node.id}
              className={active ? "run-node-face active" : "run-node-face"}
              style={{ left: `${(node.x / width) * 100}%`, top: `${(node.y / height) * 100}%` }}
              aria-hidden="true"
            >
              <Avatar
                seed={visual?.seed ?? null}
                appearance={visual?.appearance ?? null}
                size={38}
                mood={active ? "speaking" : "idle"}
                frozen={!active}
                awake={active}
                track="off"
              />
            </span>
          );
        })}
      {speech && speaker && (
        <SpeechBubble
          speech={speech}
          x={speaker.x}
          y={speaker.y}
          width={width}
          height={height}
          reduced={reduced}
        />
      )}
    </div>
  );
});

// ---- step list -------------------------------------------------------------

/** The mark on the timeline rail, by what the step is. A tool step uses its tool's own glyph. */
const STEP_ICON: Record<StepKind, IconName> = {
  ask: "send",
  consult: "reply",
  dm: "reply",
  revise: "review",
  reply: "chat",
  tool: "info",
  mention: "at",
};

interface StepCardProps {
  step: RunStep;
  message: Message;
  agent: Agent | undefined;
  /** The member a hand-off went to, and its name as the trace knows it. */
  target: Agent | undefined;
  targetName: string | null;
  /** What a hand-off handed over: the task, the private message, the review's feedback. */
  brief: string | null;
  memberNames: string[];
  state: "past" | "current" | "future";
  highlighted: boolean;
  dimmed: boolean;
  expanded: boolean;
  onToggle: (id: string) => void;
  onHover: (index: number) => void;
  onLeave: () => void;
  onClick: (index: number) => void;
  onScrollRef: (id: string, el: HTMLLIElement | null) => void;
  onOpenConversation: (id: string) => void;
}

/**
 * One step on the timeline. What it is shows before anything is read: a coloured mark on the
 * rail (green speech for a reply, the tool's own glyph for a tool call, an arrow for a hand-off)
 * and a matching chip. A reply is a card with its text; a hand-off names who it went to and
 * quotes what was handed over; a tool call is one compact line whose result opens in place.
 */
const StepCard = memo(function StepCard({
  step,
  message,
  agent,
  target,
  targetName,
  brief,
  memberNames,
  state,
  highlighted,
  dimmed,
  expanded,
  onToggle,
  onHover,
  onLeave,
  onClick,
  onScrollRef,
  onOpenConversation,
}: StepCardProps) {
  const name = agent?.name ?? message.author_name ?? (step.actorID ? "成员" : "你");
  const seed = agent?.avatar_seed ?? message.avatar_seed;
  const action = message.action;
  const tool = action?.tool_name ?? "";
  const handoff = step.kind === "consult" || step.kind === "dm" || step.kind === "revise";
  const failed = action?.status === "error";
  const chip = step.kind === "tool" ? (TOOL_LABEL[tool] ?? "工具") : CHIP[step.kind];
  const icon = step.kind === "tool" ? (TOOL_ICON[tool] ?? "info") : STEP_ICON[step.kind];
  const detail = step.kind === "tool" && action?.status !== "running" ? (action?.detail ?? null) : null;
  const found = detail?.kind === "search" ? (detail.items?.length ?? 0) : 0;
  const toolLine = found > 0 ? `搜索「${detail?.query ?? ""}」，找到 ${found} 条` : actionText(message);
  const text = handoff ? (brief ?? "") : message.content;
  const long = text.length > 160 || text.includes("\n");
  const toggle = (event: React.MouseEvent) => {
    event.stopPropagation();
    onToggle(message.id);
  };
  return (
    <li
      ref={(el) => onScrollRef(step.messageID, el)}
      className={[
        "run-step",
        `kind-${step.kind}`,
        state,
        failed ? "failed" : "",
        highlighted ? "highlight" : "",
        dimmed ? "dim" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      onClick={() => onClick(step.index)}
      onMouseEnter={() => onHover(step.index)}
      onMouseLeave={onLeave}
      onFocus={() => onHover(step.index)}
      onBlur={onLeave}
      tabIndex={0}
      role="button"
      aria-label={`${name} ${chip}`}
    >
      <span className="run-step-mark" aria-hidden="true">
        <Icon name={icon} size={13} />
      </span>
      <div className="run-step-main">
        <div className="run-step-head">
          <Avatar seed={seed} appearance={agent?.appearance} size={20} name={name} frozen />
          <span className="run-step-name">{name}</span>
          {handoff && targetName && (
            <>
              <span className="run-step-arrow" aria-label="交给">
                →
              </span>
              {target && <Avatar seed={target.avatar_seed} appearance={target.appearance} size={20} name={targetName} frozen />}
              <span className="run-step-name">{targetName}</span>
            </>
          )}
          <span className="run-step-chip">{chip}</span>
          <span className="run-step-time">{offsetLabel(step.offsetMs)}</span>
        </div>

        {step.kind === "tool" ? (
          <div className="run-step-tool">
            <span className="run-step-tool-text">{toolLine}</span>
            {detail && (
              <button type="button" className="run-step-expand" onClick={toggle} aria-expanded={expanded}>
                {expanded ? "收起结果" : "查看结果"}
              </button>
            )}
          </div>
        ) : text ? (
          <div className={handoff ? "run-step-brief" : "run-step-text"}>
            <div className={expanded ? "run-step-body" : "run-step-body clamped"}>
              <MessageBody
                text={text}
                markdown={message.kind === "agent" || handoff}
                streaming={false}
                memberNames={memberNames}
                onMention={() => {}}
              />
            </div>
          </div>
        ) : (
          handoff && <p className="run-step-tool-text">{actionText(message)}</p>
        )}

        {detail && expanded && (
          <div className="run-step-result" onClick={(event) => event.stopPropagation()}>
            <ActionDetailPanel detail={detail} />
          </div>
        )}

        {(long && step.kind !== "tool") || (step.kind === "dm" && action?.target_conversation_id) ? (
          <div className="run-step-foot">
            {long && (
              <button type="button" className="run-step-expand" onClick={toggle} aria-expanded={expanded}>
                {expanded ? "收起" : "展开全文"}
              </button>
            )}
            {step.kind === "dm" && action?.target_conversation_id && (
              <button
                type="button"
                className="run-step-open"
                onClick={(event) => {
                  event.stopPropagation();
                  onOpenConversation(action.target_conversation_id!);
                }}
              >
                查看私聊
              </button>
            )}
          </div>
        ) : null}
      </div>
    </li>
  );
});

// ---- the sheet -------------------------------------------------------------

export function RunTraceSheet({
  runID,
  onClose,
  onOpenConversation,
  onLoaded,
}: {
  runID: string;
  onClose: () => void;
  onOpenConversation: (id: string) => void;
  /** The trace has read the run: the tab can name the conversation it ran in and its start. */
  onLoaded?: (run: RunRef) => void;
}) {
  const workspace = useTeam((s) => s.workspace);
  const schedules = useTeam((s) => s.schedules);
  const agents = workspace?.agents ?? NO_AGENTS;
  const [trace, setTrace] = useState<RunTraceData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [focus, setFocus] = useState<Focus>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const follow = useRef(true);
  const revealedRef = useRef(0);
  const cardRefs = useRef(new Map<string, HTMLLIElement>());
  const bodyRef = useRef<HTMLDivElement>(null);
  const onLoadedRef = useRef(onLoaded);
  onLoadedRef.current = onLoaded;

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const controller = new AbortController();
    const read = async () => {
      try {
        const next = await (await store.readyClient()).runTrace(runID, controller.signal);
        if (!alive) return;
        setTrace(next);
        onLoadedRef.current?.({
          id: next.run.id,
          conversation: next.conversation_id,
          at: next.run.created_at,
          schedule: next.run.schedule_id,
        });
        setError(null);
        if (next.run.status === "running") timer = window.setTimeout(read, REFRESH_MS);
      } catch (failure) {
        if (alive && !controller.signal.aborted) setError(errorMessage(failure));
      }
    };
    void read();
    return () => {
      alive = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runID]);

  const reduced = prefersReducedMotion();
  const run = trace?.run ?? null;
  const messages = trace?.messages ?? [];
  const total = messages.length;

  const steps = useMemo(() => (trace ? buildSteps(trace) : []), [trace]);
  const edgeNumbers = useMemo(() => (trace ? edgeOrder(trace) : []), [trace]);
  const layout = useMemo(() => (trace ? layoutRunGraph(trace.nodes, trace.edges) : null), [trace]);
  const visuals = useMemo(() => {
    const map = new Map<string, Visual>();
    for (const node of layout?.nodes ?? []) map.set(node.id, agentVisual(agents, node.id));
    return map;
  }, [layout, agents]);

  // Follow the newest step until the user takes over by hand.
  useEffect(() => {
    if (follow.current) setRevealed(total);
  }, [total]);
  useEffect(() => {
    revealedRef.current = revealed;
  }, [revealed]);

  const currentIndex = revealed > 0 ? revealed - 1 : -1;
  const currentStep = currentIndex >= 0 ? steps[currentIndex] : null;
  const activeEdge = currentStep?.edgeIndex ?? null;
  const activeNode = currentStep ? activeNodeOf(currentStep) : null;

  const reachedEdges = useMemo(() => {
    const set = new Set<number>();
    for (let i = 0; i < revealed; i += 1) {
      const edge = steps[i]?.edgeIndex;
      if (edge != null) set.add(edge);
    }
    return set;
  }, [steps, revealed]);

  // Which graph elements each step owns, for hover linking in both directions.
  const stepTargets = useMemo(() => {
    return steps.map((step) => {
      const nodes = new Set<string>();
      if (step.actorID) nodes.add(step.actorID);
      if (step.targetID) nodes.add(step.targetID);
      const active = activeNodeOf(step);
      if (active) nodes.add(active);
      return { edge: step.edgeIndex, nodes };
    });
  }, [steps]);

  const togglePlay = useCallback(() => {
    if (playing) {
      setPlaying(false);
      return;
    }
    follow.current = false;
    if (revealedRef.current >= total) {
      revealedRef.current = 0;
      setRevealed(0);
    }
    if (reduced) setRevealed((r) => Math.min(total, r + 1));
    else setPlaying(true);
  }, [playing, reduced, total]);

  const step = useCallback(
    (delta: number) => {
      follow.current = false;
      setPlaying(false);
      setRevealed((r) => Math.max(0, Math.min(total, r + delta)));
    },
    [total],
  );

  const messageByID = useMemo(() => new Map(messages.map((m) => [m.id, m])), [messages]);
  const nodeNames = useMemo(() => new Map((trace?.nodes ?? []).map((n) => [n.id, n.name])), [trace]);
  // What each step says, typed out on the graph while it plays: the user's request at the
  // start, a reply's words at its author, a tool's line, or what a hand-off passed to whom. One
  // list for the whole run, so the playback clock and the typing agree on each step's length.
  const speeches = useMemo(
    () =>
      steps.map((step): Speech | null => {
        const message = messageByID.get(step.messageID);
        if (!message) return null;
        const edge = step.edgeIndex != null ? trace?.edges[step.edgeIndex] : undefined;
        const action = message.action;
        let lead: string | null = null;
        let text: string;
        if (step.kind === "tool") {
          lead = TOOL_LABEL[action?.tool_name ?? ""] ?? "工具";
          const found = action?.detail?.kind === "search" ? (action.detail.items?.length ?? 0) : 0;
          text = found > 0 ? `「${action?.detail?.query ?? ""}」，找到 ${found} 条` : actionText(message);
        } else if (step.kind === "consult" || step.kind === "dm" || step.kind === "revise") {
          lead = `${CHIP[step.kind]} ${step.targetID ? (nodeNames.get(step.targetID) ?? "成员") : "成员"}`;
          text = (edge?.message_id === message.id ? edge.brief : null) ?? actionText(message);
        } else {
          text = message.content;
        }
        const plain = plainText(text);
        if (!plain) return null;
        const shown = Math.min(plain.length, SPEECH_CHARS);
        return {
          nodeID: step.actorID ?? "user",
          kind: step.kind,
          lead,
          text: plain,
          // Typing ends a beat before the step does, so the line is read before the next one.
          revealMs: Math.max(300, stepDuration(step, shown) - 600),
          stepIndex: step.index,
        };
      }),
    [steps, messageByID, nodeNames, trace],
  );

  // Time-based playback: each step shows for its own duration; hidden pages pause.
  useEffect(() => {
    if (!playing) return;
    let alive = true;
    let timer: number;
    const advance = () => {
      if (!alive) return;
      if (typeof document !== "undefined" && document.hidden) {
        timer = window.setTimeout(advance, 300);
        return;
      }
      const next = revealedRef.current + 1;
      if (next > total) {
        setPlaying(false);
        return;
      }
      revealedRef.current = next;
      setRevealed(next);
      if (next >= total) {
        setPlaying(false);
        return;
      }
      const shown = steps[next - 1];
      timer = window.setTimeout(advance, shown ? stepDuration(shown, speeches[next - 1]?.text.length ?? 0) : 900);
    };
    timer = window.setTimeout(advance, 120);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [playing, total, steps, speeches]);

  // Space toggles play; ←/→ step, when focus is inside the dialog and not in a text field.
  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (event.key === " ") {
        event.preventDefault();
        togglePlay();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        step(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        step(1);
      }
    },
    [togglePlay, step],
  );

  useEffect(() => {
    bodyRef.current?.focus();
  }, []);

  // During a replay or a step by hand, the current step glides into view below the pinned bar.
  // Not while following the newest step: opening the page must start at the top, on the graph.
  useLayoutEffect(() => {
    const el = currentStep ? cardRefs.current.get(currentStep.messageID) : undefined;
    if (!el || follow.current) return;
    el.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
  }, [currentStep, reduced]);

  const scrollRef = useCallback((messageID: string, el: HTMLLIElement | null) => {
    if (el) cardRefs.current.set(messageID, el);
    else cardRefs.current.delete(messageID);
  }, []);

  const openConversation = useCallback(
    (id: string) => {
      onClose();
      onOpenConversation(id);
    },
    [onClose, onOpenConversation],
  );

  const jumpToStep = useCallback((index: number) => {
    follow.current = false;
    setPlaying(false);
    setRevealed(Math.max(0, Math.min(total, index + 1)));
  }, [total]);

  const hoverNode = useCallback((id: string) => setFocus({ type: "node", id }), []);
  const hoverEdge = useCallback((index: number) => setFocus({ type: "edge", index }), []);
  const hoverStep = useCallback((index: number) => setFocus({ type: "step", index }), []);
  const clearFocus = useCallback(() => setFocus(null), []);

  const onClickNode = useCallback(
    (id: string) => {
      const active = steps.findIndex((s) => activeNodeOf(s) === id);
      const any = active >= 0 ? active : steps.findIndex((s) => s.actorID === id || s.targetID === id);
      if (any >= 0) jumpToStep(any);
    },
    [steps, jumpToStep],
  );
  const onClickEdge = useCallback(
    (index: number) => {
      const idx = steps.findIndex((s) => s.edgeIndex === index);
      if (idx >= 0) jumpToStep(idx);
    },
    [steps, jumpToStep],
  );

  const stepMatchesFocus = useCallback(
    (index: number): boolean => {
      if (!focus) return true;
      if (focus.type === "step") return focus.index === index;
      const targets = stepTargets[index];
      if (!targets) return false;
      if (focus.type === "edge") return targets.edge === focus.index;
      return targets.nodes.has(focus.id);
    },
    [focus, stepTargets],
  );

  const toggleExpand = useCallback((id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const running = run?.status === "running";
  const status = !run ? "" : running ? "进行中" : statusWord(run);
  const tone = run ? STATUS_TONE[run.status] : "ink";
  const tokens = run ? run.input_tokens + run.output_tokens + run.cached_tokens : 0;
  const duration = run ? runDuration(run) : "";
  const caption = currentStep?.caption ?? "";
  const hasSteps = total > 0;
  const countLabel = currentIndex >= 0 ? `第 ${currentIndex + 1} 步 / 共 ${total} 步` : `共 ${total} 步`;

  const memberNames = useMemo(() => agents.map((a) => a.name), [agents]);
  const agentByID = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const speech = currentIndex >= 0 ? (speeches[currentIndex] ?? null) : null;
  // What the run consisted of, shown beside the caption so its shape reads before the list.
  const tally = useMemo(() => {
    let replies = 0;
    let tools = 0;
    let handoffs = 0;
    for (const s of steps) {
      if (s.kind === "reply" || s.kind === "mention") replies += 1;
      else if (s.kind === "tool") tools += 1;
      else if (s.kind !== "ask") handoffs += 1;
    }
    return { replies, tools, handoffs };
  }, [steps]);

  // The page names the chat it ran in and when, matching its tab: several runs can be open.
  const place =
    workspace?.conversations.find((c) => c.id === trace?.conversation_id)?.title ??
    (run?.schedule_id ? schedules?.schedules.find((s) => s.id === run.schedule_id)?.title : undefined);
  const title = run ? `${place ?? "运行过程"} · ${momentStamp(run.created_at)}` : "运行过程";

  return (
    <Dialog title={title} onClose={onClose} wide className="run-trace-dialog">
      <div className="run-trace-body" ref={bodyRef} tabIndex={-1} onKeyDown={onKeyDown}>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {!trace && !error && <p className="hint">正在读取这次运行…</p>}
        {trace && !run && <p className="hint">没有找到这次运行。</p>}

        {run && (
          <header className="run-trace-header">
            <span className={`run-status-pill ${tone}`}>{status}</span>
            <span className="run-trace-stats">
              {duration && <span>时长 {duration}</span>}
              <span>模型 {run.model_calls} 次</span>
              <span>工具 {run.tool_calls} 次</span>
              {tokens > 0 && <span>{formatTokens(tokens)}</span>}
            </span>
          </header>
        )}

        {layout && layout.nodes.length > 0 && (
          <div className="run-graph-card">
            <RunGraph
              layout={layout}
              visuals={visuals}
              edgeNumbers={edgeNumbers}
              reachedEdges={reachedEdges}
              activeEdge={activeEdge}
              activeStep={currentIndex}
              activeNode={activeNode}
              speech={speech}
              replying={currentStep?.kind === "reply" || currentStep?.kind === "mention"}
              focus={focus}
              reduced={reduced}
              onHoverNode={hoverNode}
              onLeave={clearFocus}
              onClickNode={onClickNode}
              onHoverEdge={hoverEdge}
              onClickEdge={onClickEdge}
            />
          </div>
        )}

        {hasSteps && (
          <>
            <div className="run-replay-bar">
              <div className="run-replay" role="group" aria-label="回放">
                <button
                  type="button"
                  className="run-play-btn"
                  onClick={togglePlay}
                  aria-label={playing ? "暂停" : "播放"}
                  title={playing ? "暂停" : "播放"}
                >
                  <Icon name={playing ? "pause" : "run"} size={16} />
                </button>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => step(-1)}
                  disabled={revealed <= 0}
                  aria-label="上一步"
                  title="上一步"
                >
                  <Icon name="step-back" size={15} />
                </button>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => step(1)}
                  disabled={revealed >= total}
                  aria-label="下一步"
                  title="下一步"
                >
                  <Icon name="step-forward" size={15} />
                </button>
                <div className="run-scrubber-wrap">
                  <input
                    type="range"
                    className="run-scrubber"
                    min={0}
                    max={total}
                    step={1}
                    value={Math.min(revealed, total)}
                    aria-label="回放进度"
                    onChange={(event) => {
                      follow.current = false;
                      setPlaying(false);
                      setRevealed(Number(event.target.value));
                    }}
                  />
                  <div className="run-scrubber-ticks" aria-hidden="true">
                    {steps.map((s) => (
                      <span
                        key={s.messageID}
                        className={`run-scrubber-tick ${s.kind}`}
                        style={{ left: `${((s.index + 1) / total) * 100}%`, background: TICK_TONE[s.kind] }}
                      />
                    ))}
                  </div>
                </div>
                <span className="run-replay-count">{countLabel}</span>
              </div>
              <div className="run-replay-meta">
                <span className="run-replay-caption">{caption || "按 ▶ 从头回放"}</span>
                <span className="run-step-legend" aria-label="这次运行包含">
                  {tally.replies > 0 && <span className="legend reply">回复 {tally.replies}</span>}
                  {tally.tools > 0 && <span className="legend tool">工具 {tally.tools}</span>}
                  {tally.handoffs > 0 && <span className="legend handoff">交接 {tally.handoffs}</span>}
                </span>
              </div>
            </div>

            <div className="run-step-scroll">
            <ol className="run-step-list">
              {steps.map((s, index) => {
                const message = messageByID.get(s.messageID);
                if (!message) return null;
                const state = index < currentIndex ? "past" : index === currentIndex ? "current" : "future";
                const matches = stepMatchesFocus(index);
                const edge = s.edgeIndex != null ? trace?.edges[s.edgeIndex] : undefined;
                return (
                  <StepCard
                    key={s.messageID}
                    step={s}
                    message={message}
                    agent={s.actorID ? agentByID.get(s.actorID) : undefined}
                    target={s.targetID ? agentByID.get(s.targetID) : undefined}
                    targetName={s.targetID ? (nodeNames.get(s.targetID) ?? null) : null}
                    brief={edge?.message_id === s.messageID ? (edge.brief ?? null) : null}
                    memberNames={memberNames}
                    state={state}
                    highlighted={focus != null && matches}
                    dimmed={focus != null && !matches}
                    expanded={expanded.has(s.messageID)}
                    onToggle={toggleExpand}
                    onHover={hoverStep}
                    onLeave={clearFocus}
                    onClick={jumpToStep}
                    onScrollRef={scrollRef}
                    onOpenConversation={openConversation}
                  />
                );
              })}
            </ol>
            </div>
          </>
        )}
      </div>
    </Dialog>
  );
}
