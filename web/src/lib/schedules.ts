// Scheduled tasks: the pure parts of the panel — a rule's next run written the way a person
// reads a clock in the schedule's own timezone, the last result in one Chinese line, and the
// form turned into the payload the server expects. Everything else (cron evaluation, the
// actual firing) belongs to the backend.

import type { ScheduleInput, ScheduleKind, ScheduleLastStatus } from "../api/types";

/** The same breakpoints the server enforces; shown before a request is even made. */
export const MIN_INTERVAL_MINUTES = 5;

/** One instant as the wall clock of a zone: its calendar parts and its `HH:MM`. */
function zoneParts(date: Date, timeZone: string): { year: number; month: number; day: number; clock: string } | null {
  try {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone,
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(date);
    const pick = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((part) => part.type === type)?.value);
    const year = pick("year");
    const month = pick("month");
    const day = pick("day");
    const hour = pick("hour");
    const minute = pick("minute");
    if (![year, month, day, hour, minute].every(Number.isFinite)) return null;
    // Some engines render midnight as 24:00.
    return { year, month, day, clock: `${String(hour % 24).padStart(2, "0")}:${String(minute).padStart(2, "0")}` };
  } catch {
    return null;
  }
}

/**
 * A UTC instant written in the schedule's zone: 「今天 09:00」, 「明天 09:00」, 「10月24日 09:00」,
 * or with the year once it is a different one. Null when the string or the zone is unusable.
 */
export function formatZoneTime(iso: string | null, timeZone: string, now = new Date()): string | null {
  if (!iso) return null;
  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) return null;
  const there = zoneParts(target, timeZone);
  const here = zoneParts(now, timeZone);
  if (!there || !here) return null;
  if (there.year === here.year && there.month === here.month && there.day === here.day) {
    return `今天 ${there.clock}`;
  }
  const tomorrow = new Date(now.getTime() + 86_400_000);
  const next = zoneParts(tomorrow, timeZone);
  if (next && next.year === there.year && next.month === there.month && next.day === there.day) {
    return `明天 ${there.clock}`;
  }
  const day = `${there.month}月${there.day}日 ${there.clock}`;
  return there.year === here.year ? day : `${there.year}年${day}`;
}

/** The row's next line: 「下次 10月24日 09:00」, or nothing when the rule has no next run. */
export function nextRunText(nextRunAt: string | null, timeZone: string, now = new Date()): string {
  const when = formatZoneTime(nextRunAt, timeZone, now);
  return when ? `下次 ${when}` : "";
}

/** What the last run came to, in one quiet Chinese line; the error rides along when there is one. */
export function lastResultText(status: ScheduleLastStatus | null, error: string | null): string {
  switch (status) {
    case "completed":
      return "上次已完成";
    case "error":
      return error ? `上次出错：${error}` : "上次出错";
    case "stopped":
      return "上次被停止";
    case "limited":
      return "上次额度用完";
    case "missed":
      return "上次错过（后端当时没运行）";
    default:
      return "";
  }
}

/** The form as the user holds it: numbers stay strings until the payload is built. */
export interface ScheduleForm {
  title: string;
  prompt: string;
  conversation_id: string;
  kind: ScheduleKind;
  /** `datetime-local` value: "YYYY-MM-DDTHH:MM", wall time in `timezone`. */
  at: string;
  every_minutes: string;
  cron: string;
  timezone: string;
  use_computer: boolean;
}

/** Either the body to send, or one Chinese sentence naming what is missing. */
export type SchedulePayload = { ok: true; body: ScheduleInput } | { ok: false; error: string };

/**
 * The form to the server's shape: exactly one of `at` / `every_minutes` / `cron` is filled, the
 * others go out as null so a rule that changed kind leaves nothing behind.
 */
export function schedulePayload(form: ScheduleForm): SchedulePayload {
  const title = form.title.trim();
  if (!title) return { ok: false, error: "请给这个定时任务起个名字。" };
  const prompt = form.prompt.trim();
  if (!prompt) return { ok: false, error: "请写下到时候要它做什么。" };
  const conversationID = form.conversation_id.trim();
  if (!conversationID) return { ok: false, error: "请选择在哪个会话里运行。" };
  const timezone = form.timezone.trim();
  if (!timezone) return { ok: false, error: "请填写时区，例如 Asia/Shanghai。" };

  const body: ScheduleInput = {
    title,
    prompt,
    conversation_id: conversationID,
    kind: form.kind,
    timezone,
    use_computer: form.use_computer,
    at: null,
    every_minutes: null,
    cron: null,
  };

  if (form.kind === "once") {
    const at = form.at.trim();
    if (!at) return { ok: false, error: "请选择运行时间。" };
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(at)) return { ok: false, error: "运行时间看不懂，请重新选择。" };
    body.at = at;
  } else if (form.kind === "interval") {
    const text = form.every_minutes.trim();
    if (!text) return { ok: false, error: `请填写间隔分钟数，至少 ${MIN_INTERVAL_MINUTES} 分钟。` };
    const minutes = Number(text);
    if (!Number.isInteger(minutes) || minutes <= 0) {
      return { ok: false, error: "间隔要写成一个整数分钟数。" };
    }
    if (minutes < MIN_INTERVAL_MINUTES) {
      return { ok: false, error: `间隔至少 ${MIN_INTERVAL_MINUTES} 分钟。` };
    }
    body.every_minutes = minutes;
  } else {
    const cron = form.cron.trim().split(/\s+/).filter(Boolean).join(" ");
    if (!cron) return { ok: false, error: "请填写 cron 表达式，例如 0 9 * * 1-5。" };
    if (cron.split(" ").length !== 5) {
      return { ok: false, error: "cron 表达式要写成 5 段：分 时 日 月 周，例如 0 9 * * 1-5。" };
    }
    body.cron = cron;
  }

  return { ok: true, body };
}

/** The values a form starts from when an existing schedule is opened for editing. */
export function formFromSchedule(schedule: {
  title: string;
  prompt: string;
  conversation_id: string;
  kind: ScheduleKind;
  at: string | null;
  every_minutes: number | null;
  cron: string | null;
  timezone: string;
  use_computer: boolean;
}): ScheduleForm {
  return {
    title: schedule.title,
    prompt: schedule.prompt,
    conversation_id: schedule.conversation_id,
    kind: schedule.kind,
    at: schedule.at ?? "",
    every_minutes: schedule.every_minutes === null ? "" : String(schedule.every_minutes),
    cron: schedule.cron ?? "",
    timezone: schedule.timezone,
    use_computer: schedule.use_computer,
  };
}
