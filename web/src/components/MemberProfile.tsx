// A member's read-only 档案 tab: all-time tallies, one period's usage, favourite tools, memory
// count and recent runs. Each recent run opens the run-trace sheet.

import { useEffect, useRef, useState } from "react";
import type { Agent, ProfilePeriod, ProfileReport, RunStatus } from "../api/types";
import { errorMessage } from "../api/client";
import { useSegmentedThumb } from "../lib/motion";
import type { RunRef } from "../lib/tabs";
import { formatTokens, relativeTime } from "../lib/team";
import { store } from "../state/store";
import { TOOL_LABEL } from "./ChatView";
import { Icon } from "./Icon";

const PERIODS: readonly (readonly [ProfilePeriod, string])[] = [
  ["day", "今天"],
  ["week", "本周"],
  ["month", "本月"],
];

const STATUS_WORDS: Record<RunStatus, string> = {
  running: "进行中",
  completed: "已完成",
  stopped: "已停止",
  error: "出错",
  limited: "额度用完",
  queued: "排队中",
};

export function MemberProfile({ agent, onOpenRun }: { agent: Agent; onOpenRun: (run: RunRef) => void }) {
  const [period, setPeriod] = useState<ProfilePeriod>("week");
  const [report, setReport] = useState<ProfileReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, period);

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    store.client
      .agentProfile(agent.id, period, controller.signal)
      .then((next) => {
        if (!alive) return;
        setReport(next);
        setError(null);
      })
      .catch((failure) => {
        if (alive && !controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [agent.id, period]);

  if (error) return <p className="form-error" role="alert">{error}</p>;
  if (!report) return <p className="hint">正在读取档案…</p>;

  const tools = report.top_tools;

  return (
    <div className="member-profile">
      <div className="profile-stats">
        <div className="profile-stat">
          <span className="profile-stat-value">{report.replies}</span>
          <span className="profile-stat-label">回复</span>
        </div>
        <div className="profile-stat">
          <span className="profile-stat-value">{report.runs}</span>
          <span className="profile-stat-label">参与任务</span>
          <span className="profile-stat-sub">失败 {report.runs_failed}</span>
        </div>
        <div className="profile-stat">
          <span className="profile-stat-value">{report.tasks_done}</span>
          <span className="profile-stat-label">完成任务</span>
          <span className="profile-stat-sub">进行中 {report.tasks_open}</span>
        </div>
        <div className="profile-stat">
          <span className="profile-stat-value">{report.reviews.passed}</span>
          <span className="profile-stat-label">验收通过</span>
          <span className="profile-stat-sub">打回 {report.reviews.revised}</span>
        </div>
      </div>

      <div className="profile-usage">
        <div
          className={thumb ? "segmented has-thumb" : "segmented"}
          role="radiogroup"
          aria-label="用量周期"
          ref={segmented}
        >
          {thumb && (
            <span
              className={ready ? "segmented-thumb" : "segmented-thumb instant"}
              aria-hidden="true"
              style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
            />
          )}
          {PERIODS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              data-seg={value}
              aria-checked={period === value}
              className={period === value ? "on" : ""}
              onClick={() => setPeriod(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="profile-usage-value">
          {formatTokens(report.usage.total_tokens)} · 调用模型 {report.usage.model_calls} 次
        </span>
      </div>

      {tools.length > 0 && (
        <div className="profile-tools">
          <div className="field-label">常用工具</div>
          <div className="profile-tool-chips">
            {tools.map((tool) => (
              <span key={tool.tool_name} className="profile-tool-chip">
                {TOOL_LABEL[tool.tool_name] ?? tool.tool_name}
                <span className="profile-tool-count">{tool.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="profile-memories">
        <div className="field-label">记忆</div>
        <span className="profile-memories-value">{report.memories} 条</span>
      </div>

      <div className="profile-runs">
        <div className="field-label">最近参与</div>
        {report.recent_runs.length === 0 ? (
          <p className="hint">还没有参与过任务。</p>
        ) : (
          report.recent_runs.map((run) => (
            <button
              key={run.run_id}
              type="button"
              className="profile-run-row"
              onClick={() => onOpenRun({ id: run.run_id, conversation: run.conversation_id, at: run.created_at })}
            >
              <span className={`profile-run-dot ${run.status}`} aria-hidden="true" />
              <span className="profile-run-text">
                <span className="profile-run-title">{run.conversation_title}</span>
                <span className="profile-run-goal">{run.goal}</span>
              </span>
              <span className="profile-run-meta">
                {STATUS_WORDS[run.status]}
                <span className="profile-run-when">{relativeTime(run.created_at)}</span>
              </span>
              <Icon name="chevron" size={12} />
            </button>
          ))
        )}
      </div>
    </div>
  );
}
