// Pure helpers for the team panel: who is doing what, token formatting, and budget state.

import type { RunLane, TokenBudget, UsagePeriod, Workspace } from "../api/types";

export type AgentActivity =
  | {
      kind: "working";
      conversationID: string;
      conversationTitle: string;
      step: string;
      waitingTurn: boolean;
      /** Which of its lanes this is; a bot working and answering at once reports work. */
      lane: RunLane;
    }
  | { kind: "idle" };

/** The run this member is part of: its own turn first, then a queued slot; idle when neither. */
export function activityOf(agentID: string, workspace: Workspace): AgentActivity {
  // A member can hold two lanes at once (working and answering): the work lane is the headline.
  const current =
    workspace.active_runs.find((run) => run.current_agent_id === agentID && run.lane !== "reply") ??
    workspace.active_runs.find((run) => run.current_agent_id === agentID);
  const run =
    current ??
    workspace.active_runs.find((run) => run.pending_agent_ids.includes(agentID) && run.lane !== "reply") ??
    workspace.active_runs.find((run) => run.pending_agent_ids.includes(agentID));
  if (!run) return { kind: "idle" };
  const conversation = workspace.conversations.find((item) => item.id === run.conversation_id);
  return {
    kind: "working",
    conversationID: run.conversation_id,
    conversationTitle: conversation?.title ?? "",
    step: run.current_step,
    waitingTurn: current === undefined,
    lane: run.lane,
  };
}

/** Token counts for humans: below 1 万 plain, then 万/亿 with one decimal, dropping `.0`. */
export function formatTokens(n: number): string {
  const tokens = Math.max(0, Math.round(n));
  if (tokens < 10_000) return String(tokens);
  // Tenths of 万: 99 999 500 would round to 10000 万, so it is shown as 1 亿 instead.
  if (Math.round(tokens / 1_000) < 100_000) return `${trimOneDecimal(tokens / 10_000)} 万`;
  return `${trimOneDecimal(tokens / 100_000_000)} 亿`;
}

function trimOneDecimal(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

/** How far into a budget this member is, and whether it has crossed the line. */
export function budgetState(used: number, budget: TokenBudget | null): { ratio: number; over: boolean } {
  if (!budget || budget.limit <= 0) return { ratio: 0, over: false };
  return { ratio: used / budget.limit, over: used >= budget.limit };
}

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

/** A short, human "how long ago": 刚刚 / N 分钟前 / N 小时前 / 昨天 / M月D日. */
export function relativeTime(iso: string, now = new Date()): string {
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  const diff = now.getTime() - value.getTime();
  if (diff < MINUTE) return "刚刚";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}分钟前`;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (value >= today) return `${Math.floor(diff / HOUR)}小时前`;
  const yesterday = new Date(today.getTime() - DAY);
  if (value >= yesterday) return "昨天";
  return `${value.getMonth() + 1}月${value.getDate()}日`;
}

/** The short word for a period, for "本周 1.2 万 / 2 万" and the budget hint. */
export const PERIOD_LABELS: Record<UsagePeriod, string> = {
  day: "今天",
  week: "本周",
  month: "本月",
};

/** The word for a period's previous window, for "比上周 +12%". */
const DELTA_PERIOD_LABELS: Record<UsagePeriod, string> = {
  day: "昨天",
  week: "上周",
  month: "上月",
};

export type DeltaTone = "up" | "down" | "flat" | "none";

/**
 * How this period's spend moved against the previous one. `flat` when the change is under 1%,
 * `none` when there was no previous spend to compare against.
 */
export function deltaText(current: number, previous: number, period: UsagePeriod): { text: string; tone: DeltaTone } {
  const label = DELTA_PERIOD_LABELS[period];
  if (previous <= 0) return { text: `${label}没有用量`, tone: "none" };
  const change = (current - previous) / previous;
  if (Math.abs(change) < 0.01) return { text: `与${label}持平`, tone: "flat" };
  const percent = Math.round(Math.abs(change) * 100);
  return change > 0
    ? { text: `比${label} +${percent}%`, tone: "up" }
    : { text: `比${label} −${percent}%`, tone: "down" };
}

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

/**
 * A bucket's short label for the chart tooltip, formatted in the browser's local time:
 * day → "14:00–15:00", week → "10月28日 周二", month → "10月28日".
 */
export function bucketLabel(startISO: string, period: UsagePeriod): string {
  const start = new Date(startISO);
  if (period === "day") {
    const end = new Date(start.getTime() + 3_600_000);
    return `${pad(start.getHours())}:00–${pad(end.getHours())}:00`;
  }
  const date = `${start.getMonth() + 1}月${start.getDate()}日`;
  if (period === "week") return `${date} 周${WEEKDAYS[start.getDay()]}`;
  return date;
}

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

export type BudgetTone = "none" | "ok" | "warn" | "over";

/** How far into a budget this member is: `warn` at 80% and over the line past the limit. */
export function budgetTone(used: number, budget: TokenBudget | null): BudgetTone {
  if (!budget || budget.limit <= 0) return "none";
  if (used >= budget.limit) return "over";
  if (used / budget.limit >= 0.8) return "warn";
  return "ok";
}

/** Quick-fill amounts (in 万) for the budget editor, sized to how long a period covers. */
export function presetsFor(period: UsagePeriod): number[] {
  if (period === "day") return [5, 20, 50];
  if (period === "week") return [20, 100, 300];
  return [100, 500, 1000];
}

/**
 * The supervisor's own name, for labels that name the bot rather than the role.
 *
 * 主管/负责人 stays right where it describes the job (验收、简报、团队列表的「负责人」行);
 * anything that says who will do something uses this, so renaming the bot changes every
 * sentence about it. Before the directory arrives the role word is the honest fallback.
 */
export function supervisorName(workspace: Workspace | null | undefined): string {
  return workspace?.agents.find((agent) => agent.id === workspace.supervisor_id)?.name ?? "负责人";
}
