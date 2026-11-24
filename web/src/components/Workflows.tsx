// 「工作流」panel. One screen, three columns: the workflows on the left, the canvas in the
// middle, and an inspector on the right for whatever is selected — the workflow itself when
// nothing is, a node when one is. The header carries the name, the switch, and the two things
// you do with a plan: save it and run it.
//
// The canvas (./WorkflowCanvas) owns looking at the graph — pan, zoom, fit, full screen — and
// the gestures that edit it; this file owns what the graph is: drafts, edges, the rules a
// connection must follow, and talking to the server.

import { useEffect, useMemo, useState } from "react";
import type {
  Workflow,
  WorkflowDetail,
  WorkflowEdge,
  WorkflowRun,
  WorkflowStepFail,
  WorkflowStepGate,
  WorkflowStepKind,
} from "../api/types";
import { errorMessage } from "../api/client";
import {
  FAIL_LABELS,
  GATE_LABELS,
  KIND_LABELS,
  RUN_LABELS,
  draftOf,
  draftProblem,
  draftToText,
  emptyDraft,
  graphInput,
  isLoop,
  loopsTaken,
  newNodeId,
  reaches,
  type StepDraft,
} from "../lib/workflow";
import {
  ROW_STEP,
  autoLayout,
  edgeKey,
  freeSpot,
  materialize,
  nodeIssues,
  type Point,
} from "../lib/workflowCanvas";
import { listTimestamp } from "../lib/time";
import { supervisorName } from "../lib/team";
import { store, useTeam } from "../state/store";
import { Switch } from "./Connectors";
import { Icon } from "./Icon";
import { Dialog } from "./Panels";
import { WebhookSection } from "./WebhookSection";
import { WorkflowCanvas, type Selection } from "./WorkflowCanvas";

const KINDS: readonly WorkflowStepKind[] = ["do", "ask_user", "branch"];
const GATES: readonly WorkflowStepGate[] = ["auto", "confirm"];
const FAILS: readonly WorkflowStepFail[] = ["stop", "retry", "skip"];
/** A new loop's rounds; the server allows 1 to `MAX_LOOPS`. */
const DEFAULT_LOOPS = 3;
const MAX_LOOPS = 20;

const KIND_HELP: Record<WorkflowStepKind, string> = {
  do: "交给机器人按指令去做，工具照常可用。",
  ask_user: "把这段话当成问题发给你，停下来等你回答；你的回答就是这一步的结果。",
  branch: "让机器人在几个选项里选一条路，只有选中的那一支会继续跑。",
};

/** The owner a row is grouped under; a user-owned workflow has no owner name. */
function ownerLabel(workflow: Workflow): string {
  return workflow.owner_name ?? "你";
}

type Loaded = {
  name: string;
  description: string;
  enabled: boolean;
  drafts: StepDraft[];
  edges: WorkflowEdge[];
};

function loadedOf(workflow: Workflow): Loaded {
  const edges = workflow.edges ?? [];
  return {
    name: workflow.name,
    description: workflow.description,
    enabled: workflow.enabled,
    drafts: materialize(workflow.steps.map(draftOf), edges),
    edges,
  };
}

/** What would change on save: compared as the request body, so a drag of 0px is not a change. */
function signature(state: Loaded): string {
  return JSON.stringify({
    name: state.name.trim(),
    description: state.description,
    enabled: state.enabled,
    ...graphInput(state.drafts, state.edges),
  });
}

export function WorkflowsPanel({
  onClose,
  onOpenConversation,
}: {
  onClose: () => void;
  /** Jumps to the conversation a walk runs in; the panel steps aside for the chat. */
  onOpenConversation: (conversationId: string) => void;
}) {
  const workflows = useTeam((s) => s.workflows);
  const workflowsError = useTeam((s) => s.workflowsError);
  const runs = useTeam((s) => s.workflowRuns);
  const supervisor = supervisorName(useTeam((s) => s.workspace));

  const [selectedID, setSelectedID] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [state, setState] = useState<Loaded | null>(null);
  // The saved version the draft is compared against; `null` while nothing is open.
  const [baseline, setBaseline] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | "run" | "delete" | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [copied, setCopied] = useState(false);
  const [listOpen, setListOpen] = useState(true);
  // Bumped when a different workflow (or a new draft) is opened: the canvas remounts and plays
  // the graph in. A first save of a new draft keeps the same canvas.
  const [opening, setOpening] = useState(0);

  useEffect(() => {
    void store.loadWorkflows();
  }, []);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1800);
    return () => window.clearTimeout(timer);
  }, [copied]);

  // Open the first workflow when the panel opens on a list that has some.
  useEffect(() => {
    if (selectedID || creating || !workflows || workflows.length === 0) return;
    setSelectedID(workflows[0]!.id);
  }, [workflows, selectedID, creating]);

  // Refetch the detail whenever the selection changes; a save below re-reads it too.
  useEffect(() => {
    if (creating || !selectedID) return;
    let alive = true;
    setDetail(null);
    setError(null);
    store
      .workflowDetail(selectedID)
      .then((next) => {
        if (!alive) return;
        const loaded = loadedOf(next);
        setDetail(next);
        setState(loaded);
        setBaseline(signature(loaded));
        setSelection(null);
      })
      .catch((failure) => {
        if (alive) setError(errorMessage(failure));
      });
    return () => {
      alive = false;
    };
  }, [selectedID, creating]);

  const dirty = state !== null && (baseline === null || signature(state) !== baseline);

  /** Leave the open draft only when it is clean, or the user agrees to drop it. */
  const leave = (): boolean => {
    if (!dirty) return true;
    return window.confirm("这个工作流还有没保存的修改，确定放弃吗？");
  };

  // Below 720px the list covers the editor, so a pick there folds it away to show the result.
  const foldListWhenNarrow = () => {
    if (window.matchMedia("(max-width: 720px)").matches) setListOpen(false);
  };

  const startNew = () => {
    if (!leave()) return;
    const first = newNodeId([]);
    const draft = { ...emptyDraft(first), x: 48, y: 48 };
    const loaded: Loaded = { name: "", description: "", enabled: true, drafts: [draft], edges: [] };
    setSelectedID(null);
    setCreating(true);
    setDetail(null);
    setState(loaded);
    setBaseline(null);
    setSelection({ kind: "node", id: first });
    setConfirmingDelete(false);
    setError(null);
    foldListWhenNarrow();
    setOpening((value) => value + 1);
  };

  const open = (id: string) => {
    if (id === selectedID && !creating) {
      foldListWhenNarrow();
      return;
    }
    if (!leave()) return;
    setSelectedID(id);
    setCreating(false);
    setDetail(null);
    setState(null);
    setBaseline(null);
    setSelection(null);
    setConfirmingDelete(false);
    setError(null);
    foldListWhenNarrow();
    setOpening((value) => value + 1);
  };

  // ---- list ----
  const list = workflows ?? [];
  const needle = search.trim().toLowerCase();
  const filtered = needle ? list.filter((workflow) => workflow.name.toLowerCase().includes(needle)) : list;
  const groups = useMemo(() => {
    const owners = new Map<string, Workflow[]>();
    for (const workflow of filtered) {
      const owner = ownerLabel(workflow);
      owners.set(owner, [...(owners.get(owner) ?? []), workflow]);
    }
    return [...owners.entries()].sort((a, b) => a[0].localeCompare(b[0], "zh"));
  }, [filtered]);

  // ---- graph edits ----
  const patch = (change: Partial<Loaded>) => setState((current) => (current ? { ...current, ...change } : current));
  const patchDraft = (id: string, change: Partial<StepDraft>) =>
    setState((current) =>
      current
        ? { ...current, drafts: current.drafts.map((draft) => (draft.id === id ? { ...draft, ...change } : draft)) }
        : current,
    );

  const addNode = (at: Point | null) => {
    if (!state) return;
    const id = newNodeId(state.drafts.map((draft) => draft.id));
    const placed = state.drafts.map((draft) => ({ id: draft.id, x: draft.x ?? 0, y: draft.y ?? 0 }));
    // Without a point, the new card goes under the selected node (and is connected to it), or
    // under the lowest card.
    const parent =
      at === null && selection?.kind === "node" ? state.drafts.find((draft) => draft.id === selection.id) ?? null : null;
    const lowest = placed.reduce<{ x: number; y: number } | null>(
      (low, item) => (low === null || item.y > low.y ? item : low),
      null,
    );
    const prefer =
      at ?? (parent ? { x: parent.x ?? 48, y: (parent.y ?? 48) + ROW_STEP } : lowest ? { x: lowest.x, y: lowest.y + ROW_STEP } : { x: 48, y: 48 });
    const spot = freeSpot(placed, prefer);
    const draft = { ...emptyDraft(id), x: spot.x, y: spot.y };
    const link =
      parent && parent.kind !== "branch" ? [{ from: parent.id, to: id, when: null }] : [];
    setState({ ...state, drafts: [...state.drafts, draft], edges: [...state.edges, ...link] });
    setSelection({ kind: "node", id });
  };

  const deleteNode = (id: string) => {
    if (!state) return;
    setState({
      ...state,
      drafts: state.drafts.filter((draft) => draft.id !== id),
      edges: state.edges.filter((edge) => edge.from !== id && edge.to !== id),
    });
    setSelection(null);
  };

  const deleteEdge = (edge: WorkflowEdge) => {
    if (!state) return;
    setState({ ...state, edges: state.edges.filter((item) => edgeKey(item) !== edgeKey(edge)) });
    setSelection(null);
  };

  /**
   * One connection, or the reason it is refused. A branch's edge carries the port's choice.
   * Connecting back to a node that already leads here makes a loop: it starts at
   * `DEFAULT_LOOPS` rounds and is selected, so its bound can be set straight away.
   */
  const connect = (from: string, to: string, choice: string | null): string | null => {
    if (!state) return "还没有打开工作流。";
    const source = state.drafts.find((draft) => draft.id === from);
    if (!source) return "这个节点不见了。";
    if (from === to) return "不能把节点连到自己；要重复做，就连回它前面的节点。";
    if (source.kind === "branch") {
      if (!choice) return "先在右边给这个分支写好选项，再从选项的圆点连出去。";
      if (state.edges.some((edge) => edge.from === from && edge.when === choice)) {
        return `「${choice}」这一支已经连过了；先选中那条线删掉，再重连。`;
      }
    }
    if (state.edges.some((edge) => edge.from === from && edge.to === to)) {
      return "这两个节点已经连过了。";
    }
    const back = reaches(state.edges, to, from);
    const edge: WorkflowEdge = {
      from,
      to,
      when: source.kind === "branch" ? choice : null,
      ...(back ? { max_loops: DEFAULT_LOOPS } : {}),
    };
    setState({ ...state, edges: [...state.edges, edge] });
    if (back) setSelection({ kind: "edge", key: edgeKey(edge) });
    return null;
  };

  const setLoops = (target: WorkflowEdge, count: number) => {
    if (!state) return;
    const clamped = Math.min(MAX_LOOPS, Math.max(1, Math.round(count)));
    setState({
      ...state,
      edges: state.edges.map((edge) => (edgeKey(edge) === edgeKey(target) ? { ...edge, max_loops: clamped } : edge)),
    });
  };

  const tidy = () => {
    if (!state) return;
    const layout = autoLayout(state.drafts, state.edges);
    patch({ drafts: state.drafts.map((draft) => ({ ...draft, ...layout[draft.id] })) });
  };

  // A branch's choices are its ports: renaming one carries its edge along, removing one drops it.
  const setChoices = (id: string, next: string[]) => {
    if (!state) return;
    const before = state.drafts.find((draft) => draft.id === id)?.choices ?? [];
    const renamed = new Map<string, string>();
    before.forEach((choice, index) => {
      const after = next[index];
      if (after !== undefined && choice.trim() && after.trim() && choice !== after) renamed.set(choice, after);
    });
    const kept = new Set(next.map((choice) => choice.trim()).filter(Boolean));
    setState({
      ...state,
      drafts: state.drafts.map((draft) => (draft.id === id ? { ...draft, choices: next } : draft)),
      edges: state.edges
        .map((edge) => (edge.from === id && edge.when && renamed.has(edge.when) ? { ...edge, when: renamed.get(edge.when)! } : edge))
        .filter((edge) => edge.from !== id || !edge.when || kept.has(edge.when.trim())),
    });
  };

  const setKind = (id: string, kind: WorkflowStepKind) => {
    if (!state) return;
    const draft = state.drafts.find((item) => item.id === id);
    if (!draft || draft.kind === kind) return;
    const leavingBranch = draft.kind === "branch";
    setState({
      ...state,
      drafts: state.drafts.map((item) =>
        item.id === id
          ? {
              ...item,
              kind,
              gate: kind === "do" ? item.gate : "auto",
              choices: kind === "branch" ? (item.choices.length >= 2 ? item.choices : ["是", "否"]) : item.choices,
            }
          : item,
      ),
      // A branch's edges belong to its choices; a plain node's edges have none.
      edges: state.edges.map((edge) =>
        edge.from === id ? { ...edge, when: kind === "branch" ? edge.when : leavingBranch ? null : edge.when } : edge,
      ).filter((edge) => !(edge.from === id && kind === "branch" && !edge.when)),
    });
  };

  // ---- actions ----
  const issues = useMemo(() => (state ? nodeIssues(state.drafts, state.edges) : {}), [state]);
  const problem = state
    ? (state.name.trim() ? null : "先给工作流起个名字。") ?? draftProblem(state.drafts, state.edges)
    : null;

  const save = async (): Promise<string | null> => {
    if (!state) return null;
    if (problem) {
      setError(problem);
      return null;
    }
    setError(null);
    setBusy("save");
    try {
      const body = {
        name: state.name.trim(),
        description: state.description,
        enabled: state.enabled,
        ...graphInput(state.drafts, state.edges),
      };
      const saved = creating
        ? await store.client.createWorkflow(body)
        : await store.client.updateWorkflow(selectedID!, body);
      // The write answers with the workflow alone; its runs and markdown come from the detail.
      const fresh = await store.workflowDetail(saved.id);
      const loaded = loadedOf(fresh);
      setDetail(fresh);
      setState(loaded);
      setBaseline(signature(loaded));
      setCreating(false);
      setSelectedID(saved.id);
      await store.loadWorkflows();
      return saved.id;
    } catch (failure) {
      setError(errorMessage(failure));
      return null;
    } finally {
      setBusy(null);
    }
  };

  const run = async () => {
    let id = selectedID;
    if (creating || dirty || !id) {
      id = await save();
      if (!id) return;
    }
    setError(null);
    setBusy("run");
    try {
      const started = await store.runWorkflow(id);
      onOpenConversation(started.conversation_id);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (!selectedID) return;
    setError(null);
    setBusy("delete");
    try {
      await store.client.deleteWorkflow(selectedID);
      setConfirmingDelete(false);
      setSelectedID(null);
      setCreating(false);
      setDetail(null);
      setState(null);
      setBaseline(null);
      setSelection(null);
      setListOpen(true);
      await store.loadWorkflows();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(null);
    }
  };

  const discard = () => {
    if (creating) {
      setCreating(false);
      setState(null);
      setBaseline(null);
      setSelection(null);
      return;
    }
    if (detail) {
      const loaded = loadedOf(detail);
      setState(loaded);
      setBaseline(signature(loaded));
      setSelection(null);
    }
  };

  const copyMarkdown = async () => {
    if (!state) return;
    const text = creating || dirty || !detail ? draftToText(state.drafts, state.edges) : detail.markdown;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setError("复制失败，请重试。");
    }
  };

  // The newest walk of this workflow: the cards show their state from it. A run this window
  // started streams in through `workflowRuns`, so it is preferred over the detail's snapshot.
  const latestRun: WorkflowRun | null = useMemo(() => {
    if (!detail) return null;
    const live = Object.values(runs)
      .filter((item) => item.workflow_id === detail.id)
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
    return live ?? detail.runs[0] ?? null;
  }, [runs, detail]);

  // ---- inspector ----
  const selectedNode =
    state && selection?.kind === "node" ? state.drafts.find((draft) => draft.id === selection.id) ?? null : null;
  const selectedIndex = selectedNode && state ? state.drafts.indexOf(selectedNode) : -1;
  const selectedEdge =
    state && selection?.kind === "edge" ? state.edges.find((edge) => edgeKey(edge) === selection.key) ?? null : null;
  const titleOf = (id: string) => state?.drafts.find((draft) => draft.id === id)?.title.trim() || "未命名";

  const edgeInspector = selectedEdge && state && (
    <div className="wfx-inspector" key={edgeKey(selectedEdge)}>
      <header className="wfx-inspector-head">
        <h3>{isLoop(selectedEdge) ? "循环" : "连线"}</h3>
        <button
          type="button"
          className="icon-button"
          onClick={() => setSelection(null)}
          aria-label="收起连线"
          title="收起连线（Esc）"
        >
          <Icon name="close" size={15} />
        </button>
      </header>

      <p className="wfx-edge-route">
        <span>{titleOf(selectedEdge.from)}</span>
        {selectedEdge.when && <span className="wfx-edge-when">当「{selectedEdge.when}」</span>}
        <Icon name={isLoop(selectedEdge) ? "repeat" : "chevron"} size={13} />
        <span>{titleOf(selectedEdge.to)}</span>
      </p>

      {isLoop(selectedEdge) ? (
        <>
          <div className="wfx-field">
            <span className="wfx-label" id="wfx-loops-label">最多循环几轮</span>
            <div className="wfx-stepper" role="group" aria-labelledby="wfx-loops-label">
              <button
                type="button"
                aria-label="少一轮"
                disabled={(selectedEdge.max_loops ?? 1) <= 1}
                onClick={() => setLoops(selectedEdge, (selectedEdge.max_loops ?? 1) - 1)}
              >
                −
              </button>
              <input
                className="field"
                type="number"
                inputMode="numeric"
                min={1}
                max={MAX_LOOPS}
                value={selectedEdge.max_loops ?? 1}
                aria-labelledby="wfx-loops-label"
                onChange={(event) => {
                  const value = Number(event.target.value);
                  if (Number.isFinite(value) && value > 0) setLoops(selectedEdge, value);
                }}
              />
              <button
                type="button"
                aria-label="多一轮"
                disabled={(selectedEdge.max_loops ?? 1) >= MAX_LOOPS}
                onClick={() => setLoops(selectedEdge, (selectedEdge.max_loops ?? 1) + 1)}
              >
                +
              </button>
            </div>
          </div>
          <p className="wfx-help">
            走这条线时，「{titleOf(selectedEdge.to)}」和它后面的节点会重新跑一轮，最多 {selectedEdge.max_loops} 轮。
            用完之后这条线失效
            {selectedEdge.when
              ? `，分支不会再被允许选「${selectedEdge.when}」，只能走别的选项。`
              : `，流程从「${titleOf(selectedEdge.from)}」的其他连线继续。`}
          </p>
          {latestRun && (
            <p className="wfx-help">
              最近一次运行绕了 {loopsTaken(selectedEdge, latestRun)} / {selectedEdge.max_loops} 轮。
            </p>
          )}
        </>
      ) : (
        <p className="wfx-help">
          「{titleOf(selectedEdge.from)}」做完后接着做「{titleOf(selectedEdge.to)}」。要让流程重复，就从后面的节点连回前面的节点，连上后会变成循环。
        </p>
      )}

      <div className="wfx-inspector-foot">
        <button type="button" className="ghost-button danger-ghost" onClick={() => deleteEdge(selectedEdge)}>
          <Icon name="trash" size={14} />
          删除{isLoop(selectedEdge) ? "循环" : "连线"}
        </button>
      </div>
    </div>
  );

  const nodeInspector = selectedNode && (
    <div className="wfx-inspector" key={selectedNode.id}>
      <header className="wfx-inspector-head">
        <span className="wfx-inspector-index">{selectedIndex + 1}</span>
        <h3>节点</h3>
        <button
          type="button"
          className="icon-button"
          onClick={() => setSelection(null)}
          aria-label="收起节点"
          title="收起节点（Esc）"
        >
          <Icon name="close" size={15} />
        </button>
      </header>

      <div className="segmented wfx-segment" role="radiogroup" aria-label="节点类型">
        {KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            role="radio"
            aria-checked={selectedNode.kind === kind}
            className={selectedNode.kind === kind ? "on" : ""}
            onClick={() => setKind(selectedNode.id, kind)}
          >
            {KIND_LABELS[kind]}
          </button>
        ))}
      </div>
      <p className="wfx-help">{KIND_HELP[selectedNode.kind]}</p>

      <label className="wfx-field">
        <span className="wfx-label">标题</span>
        <input
          className="field"
          value={selectedNode.title}
          placeholder="一句话说明这一步"
          onChange={(event) => patchDraft(selectedNode.id, { title: event.target.value })}
        />
      </label>

      <label className="wfx-field">
        <span className="wfx-label">{selectedNode.kind === "ask_user" ? "要问你的问题" : "要做什么"}</span>
        <textarea
          className="field wfx-instruction"
          value={selectedNode.instruction}
          rows={6}
          placeholder={
            selectedNode.kind === "ask_user"
              ? "例如：这次调研关注哪几个竞品？"
              : selectedNode.kind === "branch"
                ? "例如：根据上一步的结论，判断要不要继续深挖。"
                : "写清楚这一步要做什么、产出什么。"
          }
          onChange={(event) => patchDraft(selectedNode.id, { instruction: event.target.value })}
        />
      </label>

      {selectedNode.kind === "branch" && (
        <div className="wfx-field">
          <span className="wfx-label">选项</span>
          <p className="wfx-help">每个选项是节点底部的一个圆点，从圆点拖到下一个节点就连出这一支。</p>
          <ul className="wfx-choices">
            {selectedNode.choices.map((choice, index) => (
              <li key={index}>
                <input
                  className="field"
                  value={choice}
                  placeholder={`选项 ${index + 1}`}
                  aria-label={`选项 ${index + 1}`}
                  onChange={(event) =>
                    setChoices(
                      selectedNode.id,
                      selectedNode.choices.map((item, i) => (i === index ? event.target.value : item)),
                    )
                  }
                />
                <button
                  type="button"
                  className="icon-button"
                  disabled={selectedNode.choices.length <= 2}
                  aria-label={`删掉选项 ${index + 1}`}
                  title={selectedNode.choices.length <= 2 ? "分支至少要两个选项" : "删掉这个选项（连出去的线也会删掉）"}
                  onClick={() =>
                    setChoices(
                      selectedNode.id,
                      selectedNode.choices.filter((_, i) => i !== index),
                    )
                  }
                >
                  <Icon name="close" size={14} />
                </button>
              </li>
            ))}
          </ul>
          {selectedNode.choices.length < 4 && (
            <button
              type="button"
              className="ghost-button wfx-add-choice"
              onClick={() => setChoices(selectedNode.id, [...selectedNode.choices, ""])}
            >
              <Icon name="plus" size={14} />
              加一个选项
            </button>
          )}
        </div>
      )}

      <div className="wfx-pair">
        <label className="wfx-field">
          <span className="wfx-label">开始前</span>
          <select
            className="field select"
            value={selectedNode.kind === "do" ? selectedNode.gate : "auto"}
            disabled={selectedNode.kind !== "do"}
            title={selectedNode.kind === "do" ? undefined : "只有「执行」节点能设置开始前确认"}
            onChange={(event) => patchDraft(selectedNode.id, { gate: event.target.value as WorkflowStepGate })}
          >
            {GATES.map((gate) => (
              <option key={gate} value={gate}>
                {GATE_LABELS[gate]}
              </option>
            ))}
          </select>
        </label>
        <label className="wfx-field">
          <span className="wfx-label">失败时</span>
          <select
            className="field select"
            value={selectedNode.on_fail}
            onChange={(event) => patchDraft(selectedNode.id, { on_fail: event.target.value as WorkflowStepFail })}
          >
            {FAILS.map((fail) => (
              <option key={fail} value={fail}>
                {FAIL_LABELS[fail]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {issues[selectedNode.id] && (
        <p className="wfx-issue" role="status">
          <Icon name="info" size={14} />
          {issues[selectedNode.id]}
        </p>
      )}

      <div className="wfx-inspector-foot">
        <button type="button" className="ghost-button danger-ghost" onClick={() => deleteNode(selectedNode.id)}>
          <Icon name="trash" size={14} />
          删除节点
        </button>
      </div>
    </div>
  );

  const workflowInspector = state && (
    <div className="wfx-inspector">
      <header className="wfx-inspector-head">
        <h3>工作流</h3>
      </header>
      <label className="wfx-field">
        <span className="wfx-label">说明</span>
        <textarea
          className="field"
          rows={3}
          value={state.description}
          placeholder="这个工作流做什么，给谁用（可选）"
          onChange={(event) => patch({ description: event.target.value })}
        />
      </label>

      <dl className="wfx-facts">
        <div>
          <dt>所有者</dt>
          <dd>{creating || !detail ? supervisor : ownerLabel(detail)}</dd>
        </div>
        <div>
          <dt>节点</dt>
          <dd>
            {state.drafts.length} 个 · {state.edges.length} 条连线
          </dd>
        </div>
        {detail && (
          <div>
            <dt>版本</dt>
            <dd>第 {detail.version} 版</dd>
          </div>
        )}
      </dl>

      <section className="wfx-section">
        <h4>运行记录</h4>
        {!detail || detail.runs.length === 0 ? (
          <p className="wfx-help">还没有跑过。点右上角「运行」开始第一次。</p>
        ) : (
          <ul className="wfx-runs">
            {detail.runs.slice(0, 8).map((item) => (
              <li key={item.id}>
                <button type="button" className="wfx-run" onClick={() => onOpenConversation(item.conversation_id)}>
                  <span className={`wfx-run-dot status-${item.status}`} aria-hidden="true" />
                  <span className="wfx-run-status">{RUN_LABELS[item.status]}</span>
                  <span className="wfx-run-when">{listTimestamp(item.created_at)}</span>
                  <Icon name="chevron" size={13} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {detail && !creating && (
        <WebhookSection targetKind="workflow" targetID={detail.id} />
      )}

      <section className="wfx-section">
        <h4>操作</h4>
        <div className="wfx-actions">
          <button type="button" className="ghost-button" onClick={() => void copyMarkdown()}>
            <Icon name={copied ? "check" : "copy"} size={14} />
            {copied ? "已复制" : "复制为 Markdown"}
          </button>
          {!creating &&
            (confirmingDelete ? (
              <span className="wfx-confirm">
                <span>删除后运行记录也会一起删掉。</span>
                <button type="button" className="danger-button" disabled={busy !== null} onClick={() => void remove()}>
                  {busy === "delete" ? "删除中…" : "删除"}
                </button>
                <button type="button" className="ghost-button" onClick={() => setConfirmingDelete(false)}>
                  取消
                </button>
              </span>
            ) : (
              <button type="button" className="ghost-button danger-ghost" onClick={() => setConfirmingDelete(true)}>
                <Icon name="trash" size={14} />
                删除工作流
              </button>
            ))}
        </div>
      </section>

      <section className="wfx-section wfx-tips">
        <h4>画布操作</h4>
        <ul>
          <li>从节点底部的圆点拖到另一个节点，就连出一条线</li>
          <li>连回前面已经走过的节点就是循环，可以设最多循环几轮</li>
          <li>拖动空白处移动画面，触控板捏合或 ⌘ + 滚轮缩放</li>
          <li>双击空白处加一个节点；选中后按 Delete 删除</li>
        </ul>
      </section>
    </div>
  );

  const inspector = selectedNode ? nodeInspector : selectedEdge ? edgeInspector : workflowInspector;

  // ---- render ----
  const editing = state !== null;
  const saveLabel = busy === "save" ? "保存中…" : creating ? "创建" : dirty ? "保存" : "已保存";

  return (
    <Dialog title="工作流" className="workflows-dialog" onClose={onClose}>
      <div className={listOpen || state === null ? "wfx" : "wfx list-closed"}>
        <aside className="wfx-list" aria-label="工作流列表">
          {/* The panel's top rows double as the window's title bar: empty space in them drags
              the window (desktop app). Buttons and fields inside are not drag regions. */}
          <div className="wfx-list-head" data-drag-region>
            <div className="wfx-search">
              <Icon name="search" size={14} />
              <input
                value={search}
                placeholder="搜索工作流"
                aria-label="搜索工作流"
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
            <button type="button" className="icon-button" onClick={startNew} aria-label="新建工作流" title="新建工作流">
              <Icon name="plus" size={16} />
            </button>
          </div>

          <div className="wfx-list-body">
            {workflows === null && !workflowsError && <p className="wfx-help">正在读取…</p>}
            {workflowsError && (
              <p className="form-error" role="alert">
                {workflowsError}
              </p>
            )}
            {workflows && filtered.length === 0 && (
              <p className="wfx-help">{needle ? "没有匹配的工作流。" : "还没有工作流。点上面的 + 新建一个。"}</p>
            )}
            {creating && (
              <section className="wfx-group">
                <ul>
                  <li>
                    <button type="button" className="wfx-row selected" aria-current="true">
                      <span className="wfx-row-name">{state?.name.trim() || "新的工作流"}</span>
                      <span className="wfx-row-facts">还没保存</span>
                    </button>
                  </li>
                </ul>
              </section>
            )}
            {groups.map(([owner, items]) => (
              <section key={owner} className="wfx-group">
                <h4>{owner}</h4>
                <ul>
                  {items.map((workflow) => {
                    const selected = !creating && selectedID === workflow.id;
                    return (
                      <li key={workflow.id}>
                        <button
                          type="button"
                          className={selected ? "wfx-row selected" : "wfx-row"}
                          aria-current={selected ? "true" : undefined}
                          onClick={() => open(workflow.id)}
                        >
                          <span className="wfx-row-name">
                            {workflow.name}
                            {!workflow.enabled && <span className="wfx-row-off">停用</span>}
                          </span>
                          <span className="wfx-row-facts">
                            {workflow.steps.length} 个节点 · {workflow.run_count} 次运行
                            {workflow.last_run_at ? ` · ${listTimestamp(workflow.last_run_at)}` : ""}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        </aside>

        <section className="wfx-main" aria-label="工作流编辑">
          {!editing ? (
            <div className="wfx-blank">
              {error ? (
                <p className="form-error" role="alert">
                  {error}
                </p>
              ) : selectedID ? (
                <p className="wfx-help">正在打开…</p>
              ) : (
                <>
                  <Icon name="workflows" size={28} />
                  <h3>把一件事拆成步骤</h3>
                  <p className="wfx-help">每个节点是一步，连线决定先后；分支让它在中途选一条路。</p>
                  <button type="button" className="primary-button" onClick={startNew}>
                    <Icon name="plus" size={14} />
                    新建工作流
                  </button>
                </>
              )}
            </div>
          ) : (
            <>
              <header className="wfx-head" data-drag-region>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => setListOpen((value) => !value)}
                  aria-label={listOpen ? "收起列表" : "展开列表"}
                  title={listOpen ? "收起列表" : "展开列表"}
                  aria-pressed={!listOpen}
                >
                  <Icon name="sidebar" size={16} />
                </button>
                <input
                  className="wfx-name"
                  value={state.name}
                  placeholder="给工作流起个名字"
                  aria-label="工作流名称"
                  // As wide as its text (CSS `field-sizing`), so the rest of the row stays free
                  // to drag the window.
                  onChange={(event) => patch({ name: event.target.value })}
                />
                {dirty && !creating && <span className="wfx-status">未保存</span>}
                <div className="wfx-head-actions" data-drag-region>
                  <label className="wfx-toggle" title={state.enabled ? "已启用：可以运行" : "已停用：运行前要先启用"}>
                    <Switch
                      checked={state.enabled}
                      label={state.enabled ? "停用这个工作流" : "启用这个工作流"}
                      onChange={() => patch({ enabled: !state.enabled })}
                    />
                    <span className="wfx-toggle-label">{state.enabled ? "已启用" : "已停用"}</span>
                  </label>
                  {(dirty || creating) && (
                    <button type="button" className="ghost-button" onClick={discard} disabled={busy !== null}>
                      {creating ? "取消" : "撤销修改"}
                    </button>
                  )}
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => void save()}
                    disabled={busy !== null || (!dirty && !creating) || problem !== null}
                    title={problem ?? undefined}
                  >
                    <Icon name="save" size={14} />
                    {saveLabel}
                  </button>
                  <button
                    type="button"
                    className="primary-button"
                    onClick={() => void run()}
                    disabled={busy !== null || problem !== null || !state.enabled}
                    title={problem ?? (state.enabled ? (dirty ? "先保存，再运行" : "运行这个工作流") : "先启用这个工作流")}
                  >
                    <Icon name="run" size={14} />
                    {busy === "run" ? "启动中…" : dirty ? "保存并运行" : "运行"}
                  </button>
                </div>
              </header>

              {error && (
                <p className="wfx-error" role="alert">
                  {error}
                </p>
              )}

              <div className="wfx-work">
                <WorkflowCanvas
                  key={opening}
                  drafts={state.drafts}
                  edges={state.edges}
                  run={latestRun}
                  selection={selection}
                  issues={issues}
                  notice={problem}
                  inspector={inspector}
                  onSelect={setSelection}
                  onMove={(id, at) => patchDraft(id, at)}
                  onConnect={connect}
                  onDeleteEdge={deleteEdge}
                  onDeleteNode={deleteNode}
                  onAddNode={addNode}
                  onTidy={tidy}
                />
                <aside className="wfx-side" aria-label={selectedNode ? "节点设置" : "工作流设置"}>
                  {inspector}
                </aside>
              </div>
            </>
          )}
        </section>
      </div>
    </Dialog>
  );
}
