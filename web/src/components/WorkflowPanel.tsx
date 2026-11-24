// The rail beside a walk's thread: what the plan looks like right now, how far it has come, the
// controls, and the node you last touched. The drawing is the panel's canvas cut down to status
// (`WorkflowGraph`); everything the walk says in words stays in the thread next to it. Pure
// props — no store, no network.

import { useMemo, useState, type JSX, type KeyboardEvent } from "react";
import type { WorkflowRun } from "../api/types";
import {
  canPause,
  canResume,
  isLive,
  pendingQuestion,
  progress,
  RUN_LABELS,
  runHeadline,
  runningSteps,
  stepWord,
} from "../lib/workflow";
import { Icon } from "./Icon";
import { WorkflowGraph } from "./WorkflowGraph";

export interface WorkflowPanelProps {
  run: WorkflowRun;
  /** A pill when closed: the page keeps the thread, the float keeps its headline. */
  open: boolean;
  onToggle: () => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onRetry: (nodeID: string) => void;
  onSkip: (nodeID: string) => void;
  onAnswer: (text: string) => void;
  onOpenWorkflow: () => void;
}

export function WorkflowPanel(props: WorkflowPanelProps): JSX.Element {
  const { run, open, onToggle, onPause, onResume, onStop, onRetry, onSkip, onAnswer, onOpenWorkflow } = props;

  const [selected, setSelected] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");

  const live = isLive(run.status);
  const running = runningSteps(run);
  const question = pendingQuestion(run);
  const { done, total } = progress(run);
  const picked = useMemo(
    () => run.steps.find((step) => step.node_id === selected) ?? null,
    [run.steps, selected],
  );

  const submit = () => {
    const text = answer.trim();
    if (!text) return;
    onAnswer(text);
    setAnswer("");
  };

  const onAnswerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    submit();
  };

  return (
    <div className={`wf-float-panel status-${run.status}${open ? "" : " is-closed"}`}>
      <button
        type="button"
        className="wf-float-head"
        aria-expanded={open}
        title={open ? "收起流转浮窗" : "展开流转浮窗"}
        onClick={onToggle}
      >
        <span className="wf-pulse" aria-hidden="true" />
        <span className="wf-float-title">
          <span className="wf-float-name">{run.workflow_name || "工作流"}</span>
          <span className="wf-float-sub">
            {done}/{total} 步 · {RUN_LABELS[run.status]}
          </span>
        </span>
        <span className="wf-float-caret" aria-hidden="true">
          <Icon name="chevron" size={13} />
        </span>
      </button>

      {!open ? null : (
        <div className="wf-float-body">
      {live && (
        <div className="wf-rail-actions">
          {canPause(run) && (
            <button type="button" className="wf-btn" onClick={onPause}>
              <Icon name="pause" size={13} />
              暂停
            </button>
          )}
          {canResume(run) && (
            <button type="button" className="wf-btn" onClick={onResume}>
              <Icon name="run" size={13} />
              继续
            </button>
          )}
          <button type="button" className="wf-btn is-danger" onClick={onStop}>
            <Icon name="stop" size={13} />
            停止
          </button>
        </div>
      )}

      <div className="wf-rail-graph">
        <WorkflowGraph
          run={run}
          selected={selected}
          onSelect={(nodeID) => setSelected((value) => (value === nodeID ? null : nodeID))}
        />
      </div>

      <div className="wf-rail-headline">{runHeadline(run)}</div>

      {picked && (
        <div className="wf-node-detail">
          <div className="wf-node-detail-head">
            <span className="wf-node-detail-title">
              第 {picked.index + 1} 步 · {picked.title}
            </span>
            <span className="wf-node-detail-status">
              {stepWord(run, picked)}
              {(picked.round ?? 0) > 0 && ` · 第 ${(picked.round ?? 0) + 1} 轮`}
              {picked.choice && ` · 选了「${picked.choice}」`}
            </span>
          </div>
          <div className="wf-node-detail-body">{picked.output || "（这一步还没有输出）"}</div>
          {live && picked.status !== "done" && picked.status !== "skipped" && (
            <div className="wf-node-detail-controls">
              <button type="button" className="wf-chip" onClick={() => onRetry(picked.node_id)}>
                重跑这一步
              </button>
              <button type="button" className="wf-chip" onClick={() => onSkip(picked.node_id)}>
                跳过
              </button>
            </div>
          )}
        </div>
      )}

      {question && (
        <div className="wf-question">
          <div className="wf-question-title">{question.title}</div>
          <textarea
            className="wf-input"
            rows={2}
            value={answer}
            placeholder="在这里回答，也可以在下面输入框发送"
            onChange={(event) => setAnswer(event.target.value)}
            onKeyDown={onAnswerKeyDown}
          />
          <button type="button" className="wf-btn is-primary" disabled={!answer.trim()} onClick={submit}>
            提交回答
          </button>
        </div>
      )}

      {run.status === "failed" && run.summary && <div className="wf-failure">{run.summary}</div>}

      <footer className="wf-float-foot">
        <button type="button" className="wf-link" onClick={onOpenWorkflow}>
          <Icon name="workflows" size={12} />
          打开工作流定义
        </button>
        {running.length > 0 && live && (
          <span className="wf-float-step">
            {running.length > 1 ? "正在并行：" : "正在做："}
            {running.map((step) => step.title).join("、")}
          </span>
        )}
      </footer>
        </div>
      )}
    </div>
  );
}
