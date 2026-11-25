// What a group's chat shows of one bot's work: while the bot is working, its rows fold into one
// activity card; once it is done they fold into one chip above its answer. Every step lives in
// the activity's detail. Pure so the rule and the wording can be tested without a chat.

import type { Activity, ActivityStatus, Message } from "../api/types";
import { elapsedMs } from "./taskProgress";

/** How a status reads and which colour family it takes in the sheet and on the card. */
export const ACTIVITY_STATUS_META: Record<
  ActivityStatus,
  { label: string; tone: "live" | "waiting" | "done" | "stopped" | "error" }
> = {
  running: { label: "进行中", tone: "live" },
  awaiting: { label: "等你确认", tone: "waiting" },
  completed: { label: "已完成", tone: "done" },
  stopped: { label: "已停下", tone: "stopped" },
  error: { label: "出错", tone: "error" },
  limited: { label: "额度用完", tone: "stopped" },
};

/** The tone each status reads in, as the app's status pills name their colours. */
export const ACTIVITY_PILL: Record<"live" | "waiting" | "done" | "stopped" | "error", string> = {
  live: "accent",
  waiting: "warn",
  done: "ok",
  stopped: "ink",
  error: "danger",
};

/** Whether the activity still runs (or waits on the user): its card replaces its rows. */
export function isLiveActivity(status: ActivityStatus): boolean {
  return status === "running" || status === "awaiting";
}

/** One line under a live card: what it is doing, else its newest sentence, else a quiet wait. */
export function activityStepLine(activity: Activity): string {
  // The card's author line already names the bot; 「「匠师」正在思考（5 秒）」 reads twice.
  // The counter in 「正在思考（8 秒）」 ticks every few seconds; the card already shows the
  // elapsed time, and a line that changes each tick would restart its shimmer each time.
  const step = activity.current_step
    .trim()
    .replace(/^「[^」]*」/, "")
    .replace(/（\d+ 秒）$/, "");
  if (step) return step;
  const text = activity.latest_text.trim();
  if (text) return text;
  if (activity.status === "awaiting") return "等你确认后继续";
  if (activity.status === "running") return activity.steps > 0 ? "正在整理结果" : "正在开始";
  return activity.summary.trim();
}

/**
 * Whether the card should draw the thinking glyph: no call has run yet, or the run is between
 * calls in a model call (「正在思考」). Otherwise it draws the newest call's own tool icon.
 */
export function activityThinking(activity: Activity): boolean {
  if (!activity.current_tool) return true;
  return /正在思考/.test(activity.current_step);
}

/** A duration as one compact phrase: seconds, then minutes with their seconds, then hours. */
export function activityTime(ms: number): string {
  const seconds = Math.floor(Math.max(0, ms) / 1000);
  // An instant turn did not take 「0 秒」: it took less than one.
  if (seconds < 1) return "不到 1 秒";
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

/** A running card's small print: how many steps so far and how long it has been at it. */
export function activityProgressLine(activity: Activity, now: number): string {
  const time = activityTime(elapsedMs(activity.started_at, now, activity.finished_at));
  return `${activity.steps} 步 · ${time}`;
}

/**
 * A finished activity as the one chip above its answer. A stop or a failure says what happened
 * rather than how long it took; a finished one counts its steps and its time.
 */
export function activityChipText(activity: Activity, now: number): string {
  if (activity.status === "stopped" || activity.status === "limited") {
    return activity.summary.trim() || ACTIVITY_STATUS_META[activity.status].label;
  }
  if (activity.status === "error") {
    const summary = activity.summary.trim();
    return summary ? `出错：${summary}` : "出错了";
  }
  if (activity.status === "awaiting") return "等你确认";
  return `用了 ${activity.steps} 步 · ${activityTime(elapsedMs(activity.started_at, now, activity.finished_at))}`;
}

/** Whether a row of a group's timeline stays visible next to its activity. */
function keepsRow(message: Message, activity: Activity | undefined): boolean {
  // What the user has to act on stays in place: a consent card or a question card.
  if (message.kind === "action" && message.action?.status === "awaiting") return true;
  if (!message.activity_id) return true;
  if (activity?.answer_message_id === message.id) return true;
  // A response that made no tool call is the answer, even before the activity's payload lands
  // (a page reload reads the rows before any new event).
  return message.kind === "agent" && message.leads_to_tools === false && message.content.trim() !== "";
}

/** One activity's place in the visible timeline: its card or chip sits before `at`. */
export interface ActivitySlot {
  at: number;
  activityID: string;
}

/** The rows a group shows, and where each activity's card or chip belongs among them. */
export interface GroupPlan {
  messages: Message[];
  slots: ActivitySlot[];
}

/**
 * Fold one group's rows into what the chat shows. Rows written inside an activity are hidden
 * except its answer; each activity then leaves one slot where its rows were (a live card) or
 * right above its answer (a finished chip). Live work that has not written a row yet keeps a
 * slot at the end, where its rows will land.
 */
export function groupPlan(
  messages: Message[],
  activities: Record<string, Activity>,
  conversationID: string,
): GroupPlan {
  const kept: Message[] = [];
  /** Where each activity's first hidden row would have been, and where its answer landed. */
  const firstAt = new Map<string, number>();
  const answerAt = new Map<string, number>();
  const order: string[] = [];

  for (const message of messages) {
    const id = message.activity_id ?? null;
    if (id !== null && !order.includes(id)) order.push(id);
    const activity = id !== null ? activities[id] : undefined;
    if (id !== null && !keepsRow(message, activity)) {
      if (!firstAt.has(id)) firstAt.set(id, kept.length);
      continue;
    }
    // A live activity's card goes above whatever stays visible of it (the consent card the
    // user acts on), not after it.
    if (id !== null && activity && isLiveActivity(activity.status) && !firstAt.has(id)) {
      firstAt.set(id, kept.length);
    }
    if (id !== null && message.kind === "agent" && !answerAt.has(id)) answerAt.set(id, kept.length);
    kept.push(message);
  }

  const slots: ActivitySlot[] = [];
  for (const id of order) {
    const activity = activities[id];
    // A payload that has not arrived yet is read as live: its card is fetched right away.
    const live = activity ? isLiveActivity(activity.status) : true;
    const at = live
      ? (firstAt.get(id) ?? kept.length)
      : (answerAt.get(id) ?? firstAt.get(id) ?? kept.length);
    slots.push({ at, activityID: id });
  }
  // A bot that started working but has not written anything yet.
  for (const activity of Object.values(activities)) {
    if (activity.conversation_id !== conversationID) continue;
    if (!isLiveActivity(activity.status) || order.includes(activity.id)) continue;
    slots.push({ at: kept.length, activityID: activity.id });
  }
  slots.sort((left, right) => left.at - right.at);
  return { messages: kept, slots };
}

/** One day's activities in the work log, under its label. */
export interface ActivityDayGroup {
  /** 今天 / 昨天, else 「M月D日」 (with the year when it is not this one). */
  label: string;
  activities: Activity[];
}

/** Midnight of the reader's own calendar day: local, never UTC and never a fixed 24 hours. */
function dayStart(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** What one calendar day reads as, relative to the reader's today. */
function dayLabel(day: number, today: number, yesterday: number, now: Date): string {
  if (day === today) return "今天";
  if (day === yesterday) return "昨天";
  const date = new Date(day);
  const monthDay = `${date.getMonth() + 1}月${date.getDate()}日`;
  return date.getFullYear() === now.getFullYear() ? monthDay : `${date.getFullYear()}年${monthDay}`;
}

/**
 * The work log's rows split into the reader's own calendar days, newest day first. Each older
 * day keeps its own header (「11月23日」), so a long log never collapses into one flat 「更早」.
 * Yesterday is yesterday by the calendar, not by subtracting 24 hours (a DST day is not 24h).
 */
export function groupActivitiesByDay(activities: Activity[], now = new Date()): ActivityDayGroup[] {
  const today = dayStart(now);
  const yesterdayDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  yesterdayDate.setDate(yesterdayDate.getDate() - 1);
  const yesterday = dayStart(yesterdayDate);
  const days = new Map<number, Activity[]>();
  for (const activity of activities) {
    const day = dayStart(new Date(activity.started_at));
    const rows = days.get(day);
    if (rows) rows.push(activity);
    else days.set(day, [activity]);
  }
  return [...days.entries()]
    .sort(([left], [right]) => right - left)
    .map(([day, rows]) => ({ label: dayLabel(day, today, yesterday, now), activities: rows }));
}

/** The kind of work a row's round glyph stands for, as far as its own words tell. */
export type ActivityGlyph = "search" | "file" | "terminal" | "browser" | "chat" | "work";

/**
 * Which glyph a work log row draws. An activity payload carries no tool names, so this reads
 * the turn's own words (title, summary, live step): the kind is an affordance, not a fact.
 */
function glyphOf(text: string): ActivityGlyph | null {
  if (/脚本|部署|回滚|命令|终端|运行|构建|安装|测试/.test(text)) return "terminal";
  if (/发布页|网页|浏览器|截图|网站|链接/.test(text)) return "browser";
  if (/搜索|查找|查询|调研|竞品|资料/.test(text)) return "search";
  if (/文件|文档|目录|检查单|日志|笔记|清单|名单|埋点|表格|数据/.test(text)) return "file";
  if (/沟通|回复|询问|联系|讨论|评估|会议|协商|汇报/.test(text)) return "chat";
  return null;
}

export function activityGlyph(activity: Activity): ActivityGlyph {
  // The title is the row's own name; the summary is only a fallback for a generic one.
  return (
    glyphOf(`${activity.title} ${activity.current_step}`.toLowerCase()) ??
    glyphOf(`${activity.summary} ${activity.latest_text}`.toLowerCase()) ??
    (activity.steps > 0 ? "work" : "chat")
  );
}
