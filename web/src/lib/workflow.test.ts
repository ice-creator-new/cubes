import { describe, expect, it } from "vitest";
import type { WorkflowEdge, WorkflowRun, WorkflowStepState } from "../api/types";
import {
  canPause,
  canResume,
  currentStep,
  cycleIn,
  draftProblem,
  draftToText,
  edgeDead,
  edgeLive,
  emptyDraft,
  graphInput,
  isLive,
  layers,
  newNodeId,
  pendingQuestion,
  progress,
  reaches,
  runHeadline,
  runningSteps,
  stateOfNode,
  stepLabel,
  stepOfMessage,
  stepWord,
  type StepDraft,
} from "./workflow";

function state(
  nodeID: string,
  index: number,
  status: WorkflowStepState["status"],
  patch: Partial<WorkflowStepState> = {},
): WorkflowStepState {
  return {
    node_id: nodeID,
    index,
    title: nodeID,
    kind: "do",
    status,
    attempts: 0,
    output: "",
    choice: null,
    message_id: null,
    started_at: null,
    finished_at: null,
    ...patch,
  };
}

const edges: WorkflowEdge[] = [
  { from: "a", to: "b", when: null },
  { from: "a", to: "c", when: null },
  { from: "b", to: "d", when: null },
  { from: "c", to: "d", when: null },
];

function run(patch: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    id: "wr_1",
    workflow_id: "w_1",
    workflow_name: "早报",
    workflow_version: 1,
    conversation_id: "c_1",
    run_id: "r_1",
    status: "running",
    cursor: 0,
    input: "",
    summary: "",
    started_by: "user",
    steps: [state("a", 0, "running"), state("b", 1, "pending"), state("c", 2, "pending"), state("d", 3, "pending")],
    edges,
    created_at: "2026-11-04T00:00:00Z",
    updated_at: "2026-11-04T00:00:00Z",
    finished_at: null,
    ...patch,
  };
}

describe("the frontier, as the strip reads it", () => {
  it("reads the cursor and stops at the end of the graph", () => {
    expect(currentStep(run())?.node_id).toBe("a");
    expect(currentStep(run({ cursor: 4 }))).toBeNull();
  });

  it("counts done and skipped nodes as settled", () => {
    const walked = run({
      cursor: 3,
      steps: [
        state("a", 0, "done"),
        state("b", 1, "done"),
        state("c", 2, "skipped"),
        state("d", 3, "running"),
      ],
    });
    expect(progress(walked)).toEqual({ done: 3, total: 4 });
  });

  it("finds the node whose question is open", () => {
    const waiting = run({
      status: "waiting_user",
      cursor: 2,
      steps: [state("a", 0, "done"), state("b", 1, "skipped"), state("c", 2, "waiting")],
    });
    expect(pendingQuestion(waiting)?.node_id).toBe("c");
    expect(pendingQuestion(run())).toBeNull();
  });

  it("names the node a message belongs to, across the run ordering", () => {
    const withMessage = run();
    withMessage.steps[3] = state("d", 3, "done", { message_id: "m_9" });
    expect(stepOfMessage(withMessage, "m_9")?.step.node_id).toBe("d");
    expect(stateOfNode(withMessage, "d")?.message_id).toBe("m_9");
    expect(stepOfMessage(withMessage, "m_missing")).toBeNull();
  });

  it("finds a message from an earlier round of a looped node, and labels its round", () => {
    const looped = run();
    looped.steps[1] = state("b", 1, "done", {
      message_id: "m_now",
      round: 2,
      history: [
        { round: 0, status: "done", output: "", choice: null, message_id: "m_r0", started_at: null, finished_at: null },
        { round: 1, status: "done", output: "", choice: null, message_id: "m_r1", started_at: null, finished_at: null },
      ],
    });
    const first = stepOfMessage(looped, "m_r0")!;
    expect([first.step.node_id, first.round]).toEqual(["b", 0]);
    expect(stepOfMessage(looped, "m_now")!.round).toBe(2);
    expect(stepLabel(first.step, 0)).toBe(`第 2 步 · ${first.step.title}`);
    expect(stepLabel(first.step, 2)).toBe(`第 2 步 · ${first.step.title} · 第 3 轮`);
  });

  it("says what the user must do, per status", () => {
    expect(runHeadline(run({ status: "paused", cursor: 1 }))).toBe("已暂停在「b」");
    expect(runHeadline(run({ status: "done", cursor: 4 }))).toBe("4 个节点都完成了");
    expect(runHeadline(run({ status: "failed", summary: "模型连不上" }))).toBe("模型连不上");
    expect(runHeadline(run())).toContain("正在做「a」");
  });

  it("names every branch that is running at once", () => {
    const both = run({
      steps: [
        state("a", 0, "done"),
        state("b", 1, "running"),
        state("c", 2, "running"),
        state("d", 3, "pending"),
      ],
    });
    expect(runHeadline(both)).toBe("「b」「c」同时在跑（1/4 已完成）");
  });

  it("offers pause only while it runs, resume only when parked", () => {
    expect(canPause(run())).toBe(true);
    expect(canResume(run({ status: "paused" }))).toBe(true);
    expect(isLive("paused")).toBe(true);
    expect(isLive("done")).toBe(false);
  });
});

describe("layers", () => {
  it("puts a fan-out on one row and the join below both arms", () => {
    const rows = layers({ nodes: [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }], edges });
    expect(rows).toEqual([["a"], ["b", "c"], ["d"]]);
  });

  it("keeps a loop's target above its source: a loop is not a dependency", () => {
    const rows = layers({
      nodes: [{ id: "draft" }, { id: "review" }, { id: "send" }],
      edges: [
        { from: "draft", to: "review", when: null },
        { from: "review", to: "send", when: "合格" },
        { from: "review", to: "draft", when: "不合格", max_loops: 3 },
      ],
    });
    expect(rows).toEqual([["draft"], ["review"], ["send"]]);
  });
});

describe("loops", () => {
  const loopEdges: WorkflowEdge[] = [
    { from: "draft", to: "review", when: null },
    { from: "review", to: "send", when: "合格" },
    { from: "review", to: "draft", when: "不合格", max_loops: 2 },
  ];
  const loopRun = (patch: Partial<WorkflowRun> = {}): WorkflowRun =>
    ({
      ...run(),
      edges: loopEdges,
      steps: [state("draft", 0, "done"), state("review", 1, "done", { choice: "合格" }), state("send", 2, "done")],
      ...patch,
    }) as WorkflowRun;

  it("knows when a new edge would go back: the target already leads to the source", () => {
    expect(reaches(loopEdges, "draft", "send")).toBe(true);
    expect(reaches(loopEdges, "send", "draft")).toBe(false);
    // The loop edge itself does not count as a way forward.
    expect(reaches(loopEdges, "review", "draft")).toBe(false);
  });

  it("draws a loop live once it was taken, and dead only when a finished walk never took it", () => {
    const loop = loopEdges[2]!;
    expect(edgeLive(loop, loopRun({ loops: { "review->draft#不合格": 1 } }))).toBe(true);
    expect(edgeDead(loop, loopRun({ status: "running" }))).toBe(false);
    expect(edgeDead(loop, loopRun({ status: "done" }))).toBe(true);
    expect(edgeDead(loop, loopRun({ status: "done", loops: { "review->draft#不合格": 2 } }))).toBe(false);
  });

  it("saves a marked loop, sends its bound, and still refuses an unmarked cycle", () => {
    const drafts: StepDraft[] = [
      { ...emptyDraft("draft"), title: "写稿", instruction: "写" },
      { ...emptyDraft("review"), title: "审稿", instruction: "审", kind: "branch", choices: ["合格", "不合格"] },
      { ...emptyDraft("send"), title: "发出", instruction: "发" },
    ];
    expect(cycleIn(drafts, loopEdges)).toBeNull();
    expect(draftProblem(drafts, loopEdges)).toBeNull();
    expect(graphInput(drafts, loopEdges).edges[2]).toEqual({
      from: "review",
      to: "draft",
      when: "不合格",
      max_loops: 2,
    });
    expect(graphInput(drafts, loopEdges).edges[0]).not.toHaveProperty("max_loops");

    const unmarked = loopEdges.map((edge) => ({ ...edge, max_loops: null }));
    expect(draftProblem(drafts, unmarked)).toContain("设成循环");
    // A loop mark on an edge that goes forward is not a loop.
    const sideways = [...loopEdges.slice(0, 2), { from: "review", to: "draft", when: "不合格", max_loops: null }, { from: "draft", to: "send", when: null, max_loops: 2 }];
    expect(draftProblem(drafts, sideways)).toContain("成环");
    expect(draftToText(drafts, loopEdges)).toContain("↺ 当「不合格」回到 写稿（最多循环 2 轮）");
  });
});

describe("the draft graph", () => {
  const node = (id: string, title: string): StepDraft => ({ ...emptyDraft(id), title, instruction: title });

  it("drops blank nodes and the edges that pointed at them", () => {
    const drafts = [node("a", "甲"), { ...emptyDraft("b"), title: "乙" }, node("c", "丙")];
    const input = graphInput(drafts, [
      { from: "a", to: "b", when: null },
      { from: "b", to: "c", when: null },
    ]);
    expect(input.steps.map((step) => step.id)).toEqual(["a", "c"]);
    expect(input.edges).toEqual([]);
  });

  it("never asks twice on a question or a branch node", () => {
    const drafts: StepDraft[] = [
      { ...node("a", "问一句"), kind: "ask_user", gate: "confirm" },
      { ...node("b", "选一条"), kind: "branch", gate: "confirm", choices: ["是", "否"] },
    ];
    const input = graphInput(drafts, []);
    expect(input.steps.map((step) => step.gate)).toEqual(["auto", "auto"]);
    expect(input.steps[1]!.choices).toEqual(["是", "否"]);
  });

  it("refuses a graph with a loose node, a mute branch or a cycle", () => {
    const loose: StepDraft[] = [node("a", "甲"), node("b", "乙")];
    expect(draftProblem(loose, [{ from: "a", to: "b", when: null }])).toBeNull();
    expect(draftProblem([node("a", "甲"), node("b", "乙")], [])).toContain("还没有连线");

    const branch: StepDraft[] = [
      { ...node("a", "要发吗"), kind: "branch", choices: ["发"] },
      node("b", "发出去"),
    ];
    expect(draftProblem(branch, [{ from: "a", to: "b", when: "发" }])).toContain("至少要两个选项");
    const silent: StepDraft[] = [
      { ...node("a", "要发吗"), kind: "branch", choices: ["发", "不发"] },
      node("b", "发出去"),
    ];
    expect(draftProblem(silent, [{ from: "b", to: "a", when: null }])).toContain("还没有连出去");
    expect(draftProblem(silent, [{ from: "a", to: "b", when: null }])).toContain("要选一个选项");

    const graph = [{ from: "a", to: "b", when: null }, { from: "b", to: "a", when: null }];
    expect(cycleIn([node("a", "甲"), node("b", "乙")], graph)).toEqual(["a", "b", "a"]);
    expect(draftProblem([node("a", "甲"), node("b", "乙")], graph)).toContain("成环");
  });

  it("renders the graph as text, branch labels included", () => {
    const drafts: StepDraft[] = [
      { ...node("a", "要发吗"), kind: "branch", choices: ["发", "不发"] },
      node("b", "发出去"),
    ];
    const text = draftToText(drafts, [{ from: "a", to: "b", when: "发" }]);
    expect(text).toContain("1. [分支] 要发吗");
    expect(text).toContain("（选项：发 / 不发）");
    expect(text).toContain("→ 当「发」接着 发出去");
  });

  it("hands out fresh node ids", () => {
    const id = newNodeId(["n1"]);
    expect(id).not.toBe("n1");
    expect(newNodeId([id])).not.toBe(id);
  });
});

describe("stepWord", () => {
  it("calls the branch that could not get a worker queued", () => {
    const arms = ["p", "q", "r", "s", "t"];
    const busy = run({
      edges: arms.map((id) => ({ from: "a", to: id, when: null })),
      steps: [
        state("a", 0, "done"),
        ...arms.map((id, index) => state(id, index + 1, index < 4 ? "running" : "pending")),
      ],
    });
    expect(runningSteps(busy).map((step) => step.node_id)).toEqual(["p", "q", "r", "s"]);
    expect(stepWord(busy, busy.steps[5]!)).toBe("排队");
    expect(stepWord(busy, busy.steps[1]!)).toBe("进行中");
    expect(stepWord(busy, busy.steps[4]!)).toBe("进行中");
  });

  it("does not call a ready branch queued while a worker is still free", () => {
    const walked = run({
      steps: [
        state("a", 0, "done"),
        state("b", 1, "running"),
        state("c", 2, "pending"),
        state("d", 3, "pending"),
      ],
    });
    // The second arm starts at once, so it is only 未开始 for the moment it takes to begin.
    expect(stepWord(walked, walked.steps[2]!)).toBe("未开始");
    // The join is not ready: one of its parents is still running.
    expect(stepWord(walked, walked.steps[3]!)).toBe("未开始");
  });

  it("queues a second question behind the open one", () => {
    const asked = run({
      status: "waiting_user",
      edges: [
        { from: "a", to: "ask1", when: null },
        { from: "a", to: "ask2", when: null },
      ],
      steps: [
        state("a", 0, "done"),
        state("ask1", 1, "waiting", { kind: "ask_user" }),
        state("ask2", 2, "pending", { kind: "ask_user" }),
      ],
    });
    // One page answers one question at a time.
    expect(stepWord(asked, asked.steps[1]!)).toBe("等你回答");
    expect(stepWord(asked, asked.steps[2]!)).toBe("排队");
  });

  it("calls every ready node queued while the whole run is still queued", () => {
    const waiting = run({
      status: "queued",
      edges: [],
      steps: [state("a", 0, "pending"), state("b", 1, "pending")],
    });
    expect(stepWord(waiting, waiting.steps[0]!)).toBe("排队");
    expect(stepWord(waiting, waiting.steps[1]!)).toBe("排队");
  });

  it("leaves an untaken branch not started", () => {
    const branched = run({
      cursor: 1,
      edges: [
        { from: "a", to: "b", when: "左" },
        { from: "a", to: "c", when: "右" },
      ],
      steps: [
        state("a", 0, "done", { kind: "branch", choice: "左" }),
        state("b", 1, "pending"),
        state("c", 2, "pending"),
      ],
    });
    expect(stepWord(branched, branched.steps[1]!)).toBe("未开始");
    expect(stepWord(branched, branched.steps[2]!)).toBe("未开始");
  });

  it("does not call anything queued once the run is over", () => {
    const done = run({
      status: "done",
      cursor: 1,
      steps: [state("a", 0, "done"), state("b", 1, "pending")],
    });
    expect(stepWord(done, done.steps[1]!)).toBe("未开始");
  });
});
