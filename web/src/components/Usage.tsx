// The 用量 panel (opened from the rail's 更多 menu and the team panel), plus the shared budget
// editor used by both its inline budget cells and the member editor. Everything is plain CSS
// bars coloured with the bots' own body colours, and reads straight from `store.loadUsage`.

import { useEffect, useRef, useState } from "react";
import type { Agent, TokenBudget, UsagePeriod, UsageReport } from "../api/types";
import { COLORS, resolveAppearance } from "../bot/catalog";
import { fillOf, swatchOf } from "../bot/palette";
import { useSegmentedThumb } from "../lib/motion";
import { PERIOD_LABELS, budgetTone, bucketLabel, deltaText, formatTokens, presetsFor } from "../lib/team";
import { agentByID, store, useTeam, type UsageState } from "../state/store";
import { Avatar } from "./Avatar";
import { Collapse } from "./Collapse";
import { Dialog } from "./Dialog";

/** The fixed, stable colour set for the chart; these are the twelve bot body colours. */
const SERIES_PALETTE = COLORS.map((color) => fillOf(color));
/** The quiet colour for the "其他" bucket and unknown purposes. */
const OTHER_COLOR = fillOf("slate");

const PURPOSE_LABELS: Record<string, string> = {
  reply: "回复",
  compact: "上下文压缩",
  "memory-extract": "记忆整理",
  review: "主管验收",
  dispatch: "分发",
};

const PURPOSE_COLORS = [fillOf("sky"), fillOf("jade"), fillOf("tangerine"), fillOf("iris"), fillOf("plum")];

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

function trimWan(n: number): string {
  const rounded = Math.round(n * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

/** The spend in a chosen budget period: the report's total when the periods match, otherwise the
 *  agent's own budget spend when it already has a budget for that period. */
function currentSpend(agent: Agent | undefined, usage: UsageState | null, period: UsagePeriod | undefined): number | null {
  if (!agent || !usage?.report || !period) return null;
  const row = usage.report.agents.find((item) => item.agent_id === agent.id);
  if (!row) return null;
  if (usage.report.period === period) return row.total_tokens;
  if (agent.token_budget?.period === period) return row.budget_used;
  return null;
}

/** Which bucket holds "now" so it can be marked and later ones drawn as faint slots. */
function currentBucketIndex(series: UsageReport["series"], until: string): number {
  const now = Date.now();
  for (let i = 0; i < series.length; i++) {
    const start = new Date(series[i]!.start).getTime();
    const end = i + 1 < series.length ? new Date(series[i + 1]!.start).getTime() : new Date(until).getTime();
    if (now >= start && now < end) return i;
  }
  return -1;
}

/** A sparse x label: hour for day, weekday for week, a day number for month. */
function xLabel(startISO: string, period: UsagePeriod): string | null {
  const start = new Date(startISO);
  if (period === "day") {
    const hour = start.getHours();
    return [0, 6, 12, 18].includes(hour) ? `${hour}时` : null;
  }
  if (period === "week") return WEEKDAYS[start.getDay()] ?? "";
  const day = start.getDate();
  return [1, 8, 15, 22, 29].includes(day) ? String(day) : null;
}

export function UsagePanel({ onClose }: { onClose: () => void }) {
  return (
    <Dialog title="用量" onClose={onClose} wide className="usage-dialog">
      <UsageDashboard />
    </Dialog>
  );
}

function UsageDashboard() {
  const state = useTeam((s) => s);
  const workspace = state.workspace;
  const usage = state.usage;
  const period = usage?.period ?? "week";
  const report = usage?.report;
  const live = report && report.period === period ? report : null;
  const loading = usage?.loading ?? false;

  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, period);

  const [editingID, setEditingID] = useState<string | null>(null);
  const [draft, setDraft] = useState<TokenBudget | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!usage) void store.loadUsage("week");
  }, [usage]);

  const agents = workspace?.agents ?? [];
  const agentName = (id: string) => agents.find((a) => a.id === id)?.name ?? id;

  // Colour the top agents by their own body colour; everything else folds into "其他".
  const agentTotals = new Map<string, number>();
  for (const bucket of live?.series ?? []) {
    for (const [id, value] of Object.entries(bucket.by_agent)) {
      agentTotals.set(id, (agentTotals.get(id) ?? 0) + value);
    }
  }
  const sorted = [...agentTotals.entries()]
    .filter(([id]) => agents.some((a) => a.id === id))
    .sort((a, b) => b[1] - a[1]);
  const TOP_N = 5;
  const topIDs = sorted.slice(0, TOP_N).map(([id]) => id);
  const hasOther = agentTotals.size > topIDs.length;
  const colorOf = new Map<string, string>();
  topIDs.forEach((id, i) => {
    const agent = agents.find((a) => a.id === id);
    const fill = agent ? swatchOf(resolveAppearance(agent.appearance, agent.avatar_seed)).fill : null;
    colorOf.set(id, fill ?? SERIES_PALETTE[i % SERIES_PALETTE.length] ?? OTHER_COLOR);
  });

  const series = live?.series ?? [];
  const maxTotal = Math.max(1, ...series.map((bucket) => bucket.total_tokens));
  const currentIdx = live ? currentBucketIndex(series, live.until) : -1;

  const totals = live?.totals ?? {
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    total_tokens: 0,
    model_calls: 0,
  };
  const delta = live ? deltaText(totals.total_tokens, live.previous_total, period) : null;
  const cachePct = totals.input_tokens > 0 ? Math.round((totals.cached_tokens / totals.input_tokens) * 100) : null;

  const rows = [...(live?.agents ?? [])].sort((a, b) => b.total_tokens - a.total_tokens);
  const purposeEntries = Object.entries(live?.by_purpose ?? {}).filter(([, value]) => value > 0);
  const purposeTotal = purposeEntries.reduce((sum, [, value]) => sum + value, 0);
  const purposeColorOf = new Map<string, string>();
  purposeEntries.forEach(([key], i) => {
    const known = ["reply", "compact", "memory-extract", "review", "dispatch"].indexOf(key);
    purposeColorOf.set(key, known >= 0 ? (PURPOSE_COLORS[known] ?? OTHER_COLOR) : SERIES_PALETTE[(i + 3) % SERIES_PALETTE.length] ?? OTHER_COLOR);
  });

  const openEditor = (id: string, budget: TokenBudget | null) => {
    setEditingID(id);
    setDraft(budget);
    setSaveError(null);
  };
  const save = async (id: string) => {
    setSaving(true);
    setSaveError(null);
    const failure = await store.patchAgent(id, { tokenBudget: draft });
    setSaving(false);
    if (failure) setSaveError(failure);
    else setEditingID(null);
  };

  return (
    <div className="usage">
      <div className="usage-head">
        <div
          className={thumb ? "segmented usage-periods has-thumb" : "segmented usage-periods"}
          role="radiogroup"
          aria-label="统计周期"
          ref={segmented}
        >
          {thumb && (
            <span
              className={ready ? "segmented-thumb" : "segmented-thumb instant"}
              aria-hidden="true"
              style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
            />
          )}
          {(["day", "week", "month"] as const).map((p) => (
            <button
              key={p}
              type="button"
              role="radio"
              data-seg={p}
              aria-checked={period === p}
              className={period === p ? "on" : ""}
              onClick={() => void store.loadUsage(p)}
            >
              {PERIOD_LABELS[p]}
            </button>
          ))}
        </div>
      </div>

      {!live ? (
        <p className="muted">{loading ? "正在读取用量…" : "还没有用量数据。"}</p>
      ) : (
        <>
          <div className="usage-stats">
            <div className="usage-stat">
              <span className="usage-stat-label">总用量</span>
              <span className="usage-stat-value">{formatTokens(totals.total_tokens)}</span>
              {delta && <span className={`usage-stat-delta ${delta.tone}`}>{delta.text}</span>}
            </div>
            <div className="usage-stat">
              <span className="usage-stat-label">输入 / 输出</span>
              <span className="usage-stat-value">{formatTokens(totals.input_tokens)}</span>
              <span className="usage-stat-meta">输出 {formatTokens(totals.output_tokens)}</span>
            </div>
            <div className="usage-stat">
              <span className="usage-stat-label">缓存命中</span>
              <span className="usage-stat-value">{cachePct === null ? "—" : `${cachePct}%`}</span>
            </div>
            <div className="usage-stat">
              <span className="usage-stat-label">模型调用</span>
              <span className="usage-stat-value">{totals.model_calls} 次</span>
            </div>
          </div>

          {totals.total_tokens === 0 ? (
            <p className="usage-empty">这段时间还没有用量。</p>
          ) : (
            <>
              <div
                className="usage-chart"
                role="group"
                aria-label={`${PERIOD_LABELS[period]}用量柱状图，共 ${formatTokens(totals.total_tokens)} tokens，${sorted.length} 位成员`}
              >
                <div className="usage-plot">
                  {series.map((bucket, i) => {
                    const segments: { color: string; tokens: number }[] = [];
                    for (const id of topIDs) {
                      const value = bucket.by_agent[id] ?? 0;
                      if (value > 0) segments.push({ color: colorOf.get(id) ?? OTHER_COLOR, tokens: value });
                    }
                    const topSum = segments.reduce((sum, seg) => sum + seg.tokens, 0);
                    const other = bucket.total_tokens - topSum;
                    if (other > 0.0005) segments.push({ color: OTHER_COLOR, tokens: other });

                    const topAgents = Object.entries(bucket.by_agent)
                      .sort((a, b) => b[1] - a[1])
                      .slice(0, 3)
                      .map(([id, value]) => `${agentName(id)} ${formatTokens(value)}`);
                    const label = bucketLabel(bucket.start, period);
                    const barAria = [label, formatTokens(bucket.total_tokens), ...topAgents].join("，");
                    const isFuture = currentIdx >= 0 && i > currentIdx;
                    const isCurrent = i === currentIdx;
                    const classes = ["usage-col", isFuture ? "future" : "", isCurrent ? "current" : "", i >= series.length / 2 ? "flip" : ""];
                    return (
                      <div key={bucket.start} className={classes.filter(Boolean).join(" ")}>
                        <div className="usage-bar" role="img" tabIndex={0} aria-label={barAria}>
                          {segments.map((seg, j) => (
                            <span
                              key={j}
                              className="usage-bar-seg"
                              style={{ height: `${(seg.tokens / maxTotal) * 100}%`, background: seg.color }}
                            />
                          ))}
                          <span className="usage-bar-tip" role="tooltip">
                            <strong>{label}</strong>
                            <span>{formatTokens(bucket.total_tokens)}</span>
                            {topAgents.map((line) => (
                              <span key={line}>{line}</span>
                            ))}
                          </span>
                        </div>
                        <span className="usage-x-label" aria-hidden="true">
                          {xLabel(bucket.start, period) ?? ""}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>

              {(topIDs.length > 0 || hasOther) && (
                <div className="usage-legend">
                  {topIDs.map((id) => (
                    <span key={id} className="usage-legend-item">
                      <i style={{ background: colorOf.get(id) }} aria-hidden="true" />
                      {agentName(id)}
                    </span>
                  ))}
                  {hasOther && (
                    <span className="usage-legend-item">
                      <i style={{ background: OTHER_COLOR }} aria-hidden="true" />
                      其他
                    </span>
                  )}
                </div>
              )}

              <div className="usage-purpose">
                <span className="field-label">按用途</span>
                {purposeTotal > 0 ? (
                  <>
                    <div className="usage-purpose-bar" aria-hidden="true">
                      {purposeEntries.map(([key, value]) => (
                        <span
                          key={key}
                          className="usage-purpose-seg"
                          style={{ width: `${(value / purposeTotal) * 100}%`, background: purposeColorOf.get(key) }}
                        />
                      ))}
                    </div>
                    <div className="usage-purpose-legend">
                      {purposeEntries.map(([key, value]) => (
                        <span key={key} className="usage-purpose-item">
                          <i style={{ background: purposeColorOf.get(key) }} aria-hidden="true" />
                          {PURPOSE_LABELS[key] ?? key} {formatTokens(value)}
                        </span>
                      ))}
                    </div>
                  </>
                ) : (
                  <p className="muted">还没有按用途的记录。</p>
                )}
              </div>
            </>
          )}

          <ul className="usage-agents">
            {rows.map((row) => {
              const agent = agentByID(workspace, row.agent_id);
              if (!agent) return null;
              const sharePct = totals.total_tokens > 0 ? (row.total_tokens / totals.total_tokens) * 100 : 0;
              const budget = agent.token_budget;
              const used = row.budget_used ?? 0;
              const tone = budgetTone(used, budget);
              return (
                <li key={agent.id} className="usage-agent">
                  <div className="usage-agent-row">
                    <Avatar
                      seed={agent.avatar_seed}
                      appearance={agent.appearance}
                      size={28}
                      name={agent.name}
                      agentID={agent.id}
                      track="off"
                    />
                    <span className="usage-agent-name">{agent.name}</span>
                    <span className="usage-agent-share" aria-hidden="true">
                      <span style={{ width: `${sharePct}%` }} />
                    </span>
                    <span className="usage-agent-total">{formatTokens(row.total_tokens)}</span>
                    {budget ? (
                      <button
                        type="button"
                        className={`usage-budget has-budget ${tone}`}
                        onClick={() => openEditor(agent.id, budget)}
                        aria-label={`编辑 ${agent.name} 的用量预算`}
                      >
                        <span className="usage-budget-text">
                          {PERIOD_LABELS[budget.period]} {formatTokens(used)} / {formatTokens(budget.limit)}
                        </span>
                        <span className="usage-budget-track" aria-hidden="true">
                          <span className="usage-budget-fill" style={{ width: `${Math.min(100, (used / budget.limit) * 100)}%` }} />
                        </span>
                      </button>
                    ) : (
                      <button type="button" className="usage-budget link" onClick={() => openEditor(agent.id, null)}>
                        设预算
                      </button>
                    )}
                  </div>
                  <Collapse open={editingID === agent.id}>
                    {editingID === agent.id && (
                      <div className="usage-budget-editor">
                        <BudgetEditor value={draft} onChange={setDraft} agentID={agent.id} />
                        {saveError && (
                          <p className="form-error" role="alert">
                            {saveError}
                          </p>
                        )}
                        <div className="usage-budget-editor-actions">
                          <button type="button" className="ghost-button" onClick={() => setEditingID(null)}>
                            取消
                          </button>
                          <button type="button" className="primary-button" disabled={saving} onClick={() => void save(agent.id)}>
                            {saving ? "保存中…" : "保存"}
                          </button>
                        </div>
                      </div>
                    )}
                  </Collapse>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}

/**
 * The shared budget editor: a period segmented (不限/每天/每周/每月), a 万 amount with quick-fill
 * chips sized to the period, and a live preview of the current spend against the entered limit.
 * `value` seeds the draft and `onChange` reports every edit.
 */
export function BudgetEditor({
  value,
  onChange,
  agentID,
}: {
  value: TokenBudget | null;
  onChange: (budget: TokenBudget | null) => void;
  agentID: string;
}) {
  const usage = useTeam((s) => s.usage);
  const agent = useTeam((s) => agentByID(s.workspace, agentID));
  const period: "none" | UsagePeriod = value?.period ?? "none";
  const [wanText, setWanText] = useState<string>(() => (value ? trimWan(value.limit / 10_000) : "1"));
  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, period);

  const limitOf = (wan: number) => Math.min(1_000_000_000, Math.max(1000, Math.round(wan * 10_000)));

  const pickPeriod = (next: "none" | UsagePeriod) => {
    if (next === "none") {
      onChange(null);
      return;
    }
    const wan = Number(wanText);
    onChange({ period: next, limit: limitOf(Number.isFinite(wan) && wan > 0 ? wan : 1) });
  };

  const spend = value ? currentSpend(agent, usage, value.period) : null;
  const ratio = value && spend !== null ? spend / value.limit : 0;
  const over = ratio >= 1;
  const preview = value && spend !== null
    ? over
      ? "已经超过，保存后下一次回复前会先问你"
      : `${PERIOD_LABELS[value.period]}已用 ${formatTokens(spend)}，约 ${Math.round(ratio * 100)}%`
    : null;

  return (
    <div className="budget-editor">
      <div className={thumb ? "segmented has-thumb" : "segmented"} role="radiogroup" aria-label="用量预算周期" ref={segmented}>
        {thumb && (
          <span
            className={ready ? "segmented-thumb" : "segmented-thumb instant"}
            aria-hidden="true"
            style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
          />
        )}
        {(["none", "day", "week", "month"] as const).map((p) => (
          <button
            key={p}
            type="button"
            role="radio"
            data-seg={p}
            aria-checked={period === p}
            className={period === p ? "on" : ""}
            onClick={() => pickPeriod(p)}
          >
            {{ none: "不限", day: "每天", week: "每周", month: "每月" }[p]}
          </button>
        ))}
      </div>

      {period !== "none" && (
        <>
          <div className="budget-amount-row">
            <span className="budget-amount">
              <input
                type="number"
                className="field"
                value={wanText}
                min={0.1}
                step={0.1}
                max={100000}
                onChange={(event) => {
                  setWanText(event.target.value);
                  const wan = Number(event.target.value);
                  if (Number.isFinite(wan) && wan > 0) onChange({ period, limit: limitOf(wan) });
                }}
                aria-label="预算上限（万 token）"
              />
              <span className="budget-unit">万</span>
            </span>
            <span className="budget-presets">
              {presetsFor(period).map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className={wanText === trimWan(preset) ? "preset-chip on" : "preset-chip"}
                  onClick={() => {
                    setWanText(String(preset));
                    onChange({ period, limit: limitOf(preset) });
                  }}
                >
                  {preset}万
                </button>
              ))}
            </span>
          </div>

          {preview && (
            <div className={`budget-preview${over ? " over" : ""}`}>
              <span className="budget-preview-track" aria-hidden="true">
                <span className="budget-preview-fill" style={{ width: `${Math.min(100, ratio * 100)}%` }} />
              </span>
              <span className="budget-preview-text">{preview}</span>
            </div>
          )}
        </>
      )}

      <p className="hint">超过后，它下一次回复前会先问你；定时任务里直接停下。</p>
    </div>
  );
}
