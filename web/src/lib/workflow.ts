// Reading a workflow graph and the walk through it: labels, layers, progress, and the node
// anything waits on. Pure: no fetching, no DOM. The panel's canvas and the chat's strip both
// read a run through here, so "where is it" is answered one way on every surface.

import type {
  WorkflowEdge,
  WorkflowEdgeInput,
  WorkflowRun,
  WorkflowRunStatus,
  WorkflowStep,
  WorkflowStepFail,
  WorkflowStepGate,
  WorkflowStepInput,
  WorkflowStepKind,
  WorkflowStepState,
  WorkflowStepStatus,
} from "../api/types";

/** A node's state as one word for the strip. */
export const STEP_LABELS: Record<WorkflowStepStatus, string> = {
  pending: "未开始",
  running: "进行中",
  waiting: "等你回答",
  done: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

export const RUN_LABELS: Record<WorkflowRunStatus, string> = {
  queued: "排队中",
  running: "进行中",
  waiting_user: "等你回答",
  paused: "已暂停",
  done: "已完成",
  failed: "失败",
  stopped: "已停止",
};

export const KIND_LABELS: Record<WorkflowStepKind, string> = {
  do: "执行",
  ask_user: "问你",
  branch: "分支",
};

export const GATE_LABELS: Record<WorkflowStepGate, string> = {
  auto: "直接开始",
  confirm: "先问我",
};

export const FAIL_LABELS: Record<WorkflowStepFail, string> = {
  stop: "停下",
  retry: "重试一次",
  skip: "跳过",
};

/** A walk that can still be moved: everything except its three terminal states. */
export function isLive(status: WorkflowRunStatus): boolean {
  return status === "queued" || status === "running" || status === "waiting_user" || status === "paused";
}

export function canPause(run: WorkflowRun): boolean {
  return run.status === "queued" || run.status === "running";
}

export function canResume(run: WorkflowRun): boolean {
  return run.status === "paused";
}

/** The node the cursor points at in plan order, or `null` when the walk is past its graph. */
export function currentStep(run: WorkflowRun): WorkflowStepState | null {
  return run.steps[run.cursor] ?? null;
}

/**
 * How many nodes one walk may run at the same time. The backend's copy of this number is
 * `MAX_PARALLEL_STEPS` in backend/app/workflows.py; both say when a node is 「排队」.
 */
export const PARALLEL_STEPS = 4;

/** The nodes running right now. A walk with several branches has more than one. */
export function runningSteps(run: WorkflowRun): WorkflowStepState[] {
  return run.steps.filter((step) => step.status === "running");
}

/** The node whose question is waiting for an answer, if one is. */
export function pendingQuestion(run: WorkflowRun): WorkflowStepState | null {
  if (run.status !== "waiting_user") return null;
  return run.steps.find((step) => step.status === "waiting") ?? currentStep(run);
}

/** How far along a walk is, counting the terminal node states. */
export function progress(run: WorkflowRun): { done: number; total: number } {
  const settled = run.steps.filter(
    (step) => step.status === "done" || step.status === "skipped",
  ).length;
  return { done: settled, total: run.steps.length };
}

/** The one line under the strip: where the walk stands and why. */
export function runHeadline(run: WorkflowRun): string {
  const { done, total } = progress(run);
  if (run.status === "waiting_user") {
    const question = pendingQuestion(run);
    return question ? `「${question.title}」在等你回答` : "等你回答";
  }
  if (run.status === "paused") {
    const node = currentStep(run);
    return node ? `已暂停在「${node.title}」` : "已暂停";
  }
  if (run.status === "failed") return run.summary || "这一步失败了";
  if (run.status === "stopped") return "已停止";
  if (run.status === "done") return `${total} 个节点都完成了`;
  const running = runningSteps(run);
  if (running.length === 0) return "正在收尾";
  const names = running.map((step) => `「${step.title}」`).join("");
  // Several branches at once is the normal case now, and the line says so instead of naming
  // one of them as if the walk were doing it alone.
  return running.length > 1
    ? `${names}同时在跑（${done}/${total} 已完成）`
    : `正在做${names}（${done}/${total} 已完成）`;
}

/**
 * Which node (and which round of it) a chat message belongs to, so a reply can be labelled.
 * A node a loop brought back keeps its earlier messages in `history`.
 */
export function stepOfMessage(
  run: WorkflowRun,
  messageId: string,
): { step: WorkflowStepState; round: number } | null {
  for (const step of run.steps) {
    if (step.message_id === messageId) return { step, round: step.round ?? 0 };
    const earlier = step.history?.find((item) => item.message_id === messageId);
    if (earlier) return { step, round: earlier.round };
  }
  return null;
}

/** "第 3 步 · 审稿" — with "第 2 轮" when a loop brought the node back. */
export function stepLabel(step: WorkflowStepState, round: number): string {
  return `第 ${step.index + 1} 步 · ${step.title}${round > 0 ? ` · 第 ${round + 1} 轮` : ""}`;
}

/** The node a state row belongs to, across a reorder: the id is what matters. */
export function stateOfNode(run: WorkflowRun, nodeId: string): WorkflowStepState | null {
  return run.steps.find((step) => step.node_id === nodeId) ?? null;
}

// ---------------------------------------------------------------- graph shape

/** The least a shape must say to be laid out: node ids and the edges between them. */
export type Graph = { nodes: ReadonlyArray<{ id: string }>; edges: WorkflowEdge[] };

/** A run's graph, in the shape the layout helpers take. */
export function runGraph(run: WorkflowRun): Graph {
  return { nodes: run.steps.map((step) => ({ id: step.node_id })), edges: run.edges };
}

/** An edge that sends the walk back to an earlier node, a bounded number of times. */
export function isLoop(edge: Pick<WorkflowEdge, "max_loops">): boolean {
  return typeof edge.max_loops === "number" && edge.max_loops > 0;
}

/** The edges that say what follows what; loop edges are left out. They form a DAG. */
export function forwardEdges<T extends Pick<WorkflowEdge, "max_loops">>(edges: readonly T[]): T[] {
  return edges.filter((edge) => !isLoop(edge));
}

/** One edge's identity, the same string the server keys a run's loop counters by. */
export function edgeKey(edge: Pick<WorkflowEdge, "from" | "to" | "when">): string {
  return `${edge.from}->${edge.to}#${edge.when ?? ""}`;
}

/** How many rounds a loop edge has taken in a run. */
export function loopsTaken(edge: WorkflowEdge, run: WorkflowRun | null): number {
  return run?.loops?.[edgeKey(edge)] ?? 0;
}

/** Whether `from` can already reach `to` along forward edges: a new edge `from → to` would loop. */
export function reaches(edges: readonly WorkflowEdge[], from: string, to: string): boolean {
  const next = new Map<string, string[]>();
  for (const edge of forwardEdges(edges)) {
    next.set(edge.from, [...(next.get(edge.from) ?? []), edge.to]);
  }
  const seen = new Set([from]);
  const pending = [from];
  while (pending.length > 0) {
    const id = pending.pop()!;
    if (id === to) return true;
    for (const target of next.get(id) ?? []) {
      if (!seen.has(target)) {
        seen.add(target);
        pending.push(target);
      }
    }
  }
  return false;
}

/**
 * One row per dependency depth: everything in row 0 feeds row 1, and so on. Nodes of one row
 * can run in any order (the engine takes plan order); a node sits one row below its deepest
 * parent so a join lands after every branch that reaches it. Loop edges are not dependencies,
 * so a loop's target keeps its place above its source.
 */
export function layers(graph: Graph): string[][] {
  const depth = new Map<string, number>();
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const parents = new Map<string, string[]>();
  for (const node of graph.nodes) parents.set(node.id, []);
  for (const edge of forwardEdges(graph.edges)) {
    if (byId.has(edge.from) && byId.has(edge.to)) parents.get(edge.to)!.push(edge.from);
  }
  // Longest path from an entry; a forward cycle (which the server refuses) stops deepening.
  const visiting = new Set<string>();
  const depthOf = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const incoming = parents.get(id) ?? [];
    const value = incoming.length === 0 ? 0 : 1 + Math.max(...incoming.map(depthOf));
    visiting.delete(id);
    depth.set(id, value);
    return value;
  };
  for (const node of graph.nodes) depthOf(node.id);
  const rows: string[][] = [];
  for (const node of graph.nodes) {
    const row = depth.get(node.id) ?? 0;
    while (rows.length <= row) rows.push([]);
    rows[row]!.push(node.id);
  }
  return rows;
}

/**
 * Whether an edge was taken in a run's latest pass: its source finished and, for a branch,
 * picked it. A loop edge counts as taken once it has sent the walk round at least once.
 */
export function edgeLive(edge: WorkflowEdge, run: WorkflowRun | null): boolean {
  if (!run) return false;
  if (isLoop(edge)) return loopsTaken(edge, run) > 0;
  const source = stateOfNode(run, edge.from);
  if (!source || source.status !== "done") return false;
  if (!edge.when) return true;
  return (source.choice ?? "") === edge.when;
}

/** Whether an edge's source is still going: drawn faintly on the canvas while it waits. */
export function edgeSettled(edge: WorkflowEdge, run: WorkflowRun | null): boolean {
  if (!run) return false;
  const source = stateOfNode(run, edge.from);
  return !source || source.status === "done" || source.status === "failed" || source.status === "skipped";
}

/**
 * A dead edge — its source is settled and did not take it — is drawn struck through. A loop
 * edge that never ran is dead only once the walk is over: until then it may still be taken.
 */
export function edgeDead(edge: WorkflowEdge, run: WorkflowRun | null): boolean {
  if (isLoop(edge)) return !!run && !isLive(run.status) && loopsTaken(edge, run) === 0;
  return edgeSettled(edge, run) && !edgeLive(edge, run);
}

/** An incoming edge the engine would treat as live: its source finished and, for a branch, picked it. */
function edgeTaken(run: WorkflowRun, edge: WorkflowEdge): boolean {
  const source = stateOfNode(run, edge.from);
  if (!source || source.status !== "done") return false;
  if (source.kind === "branch") return (source.choice ?? "") === (edge.when ?? "");
  return true;
}

/** Pending, every parent settled, and at least one incoming edge actually taken (or none exist). */
function isReady(run: WorkflowRun, step: WorkflowStepState): boolean {
  if (step.status !== "pending") return false;
  const incoming = forwardEdges(run.edges).filter((edge) => edge.to === step.node_id);
  for (const edge of incoming) {
    const parent = stateOfNode(run, edge.from);
    const settled = parent?.status === "done" || parent?.status === "failed" || parent?.status === "skipped";
    if (!settled) return false;
  }
  return incoming.length === 0 || incoming.some((edge) => edgeTaken(run, edge));
}

/**
 * A ready node waiting for a free worker. The engine starts every node whose parents settled
 * at once, up to `PARALLEL_STEPS` of them, so a ready node is 「排队」 only when every slot is
 * taken — or while the walk itself is still queued for its page. A second `ask_user` also
 * waits, because one page answers one question at a time.
 */
export function isQueued(run: WorkflowRun, step: WorkflowStepState): boolean {
  if (!isLive(run.status) || !isReady(run, step)) return false;
  if (run.status === "queued") return true;
  if (runningSteps(run).length >= PARALLEL_STEPS) return true;
  return step.kind === "ask_user" && run.steps.some((item) => item.status === "waiting");
}

/** The one word a node shows: 「排队」 only for a ready node waiting for a worker. */
export function stepWord(run: WorkflowRun, step: WorkflowStepState): string {
  return isQueued(run, step) ? "排队" : STEP_LABELS[step.status];
}


// ---------------------------------------------------------------- the editor's draft

export interface StepDraft {
  id: string;
  title: string;
  instruction: string;
  kind: WorkflowStepKind;
  gate: WorkflowStepGate;
  on_fail: WorkflowStepFail;
  choices: string[];
  x: number | null;
  y: number | null;
}

const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789";

/** A fresh node id: short, opaque and stable — renames are the user's business, not ours. */
export function newNodeId(taken: readonly string[]): string {
  for (let attempt = 0; attempt < 64; attempt += 1) {
    let id = "n";
    for (let index = 0; index < 6; index += 1) {
      id += ID_ALPHABET[Math.floor(Math.random() * ID_ALPHABET.length)];
    }
    if (!taken.includes(id)) return id;
  }
  return `n${taken.length + 1}`;
}

export function draftOf(step: WorkflowStep): StepDraft {
  return {
    id: step.id,
    title: step.title,
    instruction: step.instruction,
    kind: step.kind,
    gate: step.gate,
    on_fail: step.on_fail,
    choices: step.choices ?? [],
    x: typeof step.x === "number" ? step.x : null,
    y: typeof step.y === "number" ? step.y : null,
  };
}

export function emptyDraft(id: string, at?: { x: number; y: number }): StepDraft {
  return {
    id,
    title: "",
    instruction: "",
    kind: "do",
    gate: "auto",
    on_fail: "stop",
    choices: [],
    x: at?.x ?? null,
    y: at?.y ?? null,
  };
}

/** The request body for a draft graph; a blank instruction is dropped rather than sent. */
export function graphInput(
  drafts: StepDraft[],
  edges: WorkflowEdge[],
): { steps: WorkflowStepInput[]; edges: WorkflowEdgeInput[] } {
  const kept = drafts.filter((draft) => draft.instruction.trim().length > 0);
  const ids = new Set(kept.map((draft) => draft.id));
  return {
    steps: kept.map((draft) => ({
      id: draft.id,
      title: draft.title.trim() || undefined,
      instruction: draft.instruction.trim(),
      kind: draft.kind,
      gate: draft.kind === "do" ? draft.gate : "auto",
      on_fail: draft.on_fail,
      choices: draft.kind === "branch" ? draft.choices : undefined,
      x: draft.x,
      y: draft.y,
    })),
    // An edge whose node was dropped or emptied goes with it.
    edges: edges
      .filter((edge) => ids.has(edge.from) && ids.has(edge.to))
      .map((edge) => ({
        from: edge.from,
        to: edge.to,
        when: edge.when,
        ...(isLoop(edge) ? { max_loops: edge.max_loops } : {}),
      })),
  };
}

/**
 * A cycle of forward edges in the draft, as the node ids on it, or `null`. A loop must be
 * marked as one (it carries `max_loops`); an unmarked cycle has no bound, so the server
 * refuses it too.
 */
export function cycleIn(drafts: StepDraft[], edges: WorkflowEdge[]): string[] | null {
  const ids = new Set(drafts.map((draft) => draft.id));
  const state = new Map<string, number>(); // 1 = on the stack, 2 = done
  const next = new Map<string, string[]>();
  for (const draft of drafts) next.set(draft.id, []);
  for (const edge of forwardEdges(edges)) {
    if (!ids.has(edge.from) || !ids.has(edge.to)) continue;
    next.get(edge.from)!.push(edge.to);
  }
  const stack: string[] = [];
  const walk = (id: string): string[] | null => {
    const mark = state.get(id);
    if (mark === 2) return null;
    if (mark === 1) return [...stack.slice(stack.indexOf(id)), id];
    state.set(id, 1);
    stack.push(id);
    for (const target of next.get(id) ?? []) {
      const found = walk(target);
      if (found) return found;
    }
    stack.pop();
    state.set(id, 2);
    return null;
  };
  for (const draft of drafts) {
    const found = walk(draft.id);
    if (found) return found;
  }
  return null;
}

/** What is wrong with the draft graph, or `null` when it can be saved. */
export function draftProblem(drafts: StepDraft[], edges: WorkflowEdge[]): string | null {
  if (drafts.length === 0) return "至少要有一步。";
  if (drafts.some((draft) => !draft.instruction.trim())) return "每一步都要写清楚做什么。";
  if (drafts.length > 1) {
    const connected = new Set<string>();
    for (const edge of edges) {
      connected.add(edge.from);
      connected.add(edge.to);
    }
    const loose = drafts.filter((draft) => !connected.has(draft.id));
    if (loose.length > 0) return `这些节点还没有连线：${loose.map((d) => d.title || "未命名").join("、")}。`;
  }
  for (const draft of drafts) {
    if (draft.kind !== "branch") continue;
    if (draft.choices.length < 2) return `分支「${draft.title || "未命名"}」至少要两个选项。`;
    const outgoing = edges.filter((edge) => edge.from === draft.id);
    if (outgoing.length === 0) return `分支「${draft.title || "未命名"}」还没有连出去。`;
    const unlabelled = outgoing.find((edge) => !edge.when);
    if (unlabelled) return `分支「${draft.title || "未命名"}」的连线要选一个选项。`;
  }
  const cycle = cycleIn(drafts, edges);
  if (cycle) {
    const names = cycle.map(
      (id) => drafts.find((draft) => draft.id === id)?.title || "未命名",
    );
    return `连线成环了：${names.join(" → ")}。把绕回去的那条线设成循环，并写最多几轮。`;
  }
  for (const edge of edges) {
    if (isLoop(edge) && !reaches(edges, edge.to, edge.from)) {
      const name = (id: string) => drafts.find((draft) => draft.id === id)?.title || "未命名";
      return `「${name(edge.from)}」→「${name(edge.to)}」设了循环，但它没有绕回前面的节点。`;
    }
  }
  return null;
}

/** The plan as plain text, so a draft can be copied out without asking the server. */
export function draftToText(drafts: StepDraft[], edges: WorkflowEdge[]): string {
  const title = (id: string) => drafts.find((draft) => draft.id === id)?.title || id;
  const lines: string[] = [];
  drafts.forEach((draft, index) => {
    lines.push(`${index + 1}. [${KIND_LABELS[draft.kind]}] ${draft.title || draft.instruction}`);
    lines.push(`   ${draft.instruction}`);
    if (draft.kind === "branch" && draft.choices.length > 0) {
      lines.push(`   （选项：${draft.choices.join(" / ")}）`);
    }
    for (const edge of edges.filter((item) => item.from === draft.id)) {
      const when = edge.when ? `当「${edge.when}」` : "";
      lines.push(
        isLoop(edge)
          ? `   ↺ ${when}回到 ${title(edge.to)}（最多循环 ${edge.max_loops} 轮）`
          : `   → ${when}接着 ${title(edge.to)}`,
      );
    }
    lines.push("");
  });
  return lines.join("\n").trimEnd();
}
