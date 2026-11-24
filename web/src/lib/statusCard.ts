// The dispatcher's progress card, read as rows: owner, what it is, how far it got, and its newest
// line. Pure so the card's wording is testable without a chat or a backend.

import type { StatusCard, StatusCardTask } from "../api/types";
import { taskStatusMeta } from "./taskProgress";

export interface StatusCardRow {
  /** Stable list key: owner + title, since one member may hold more than one finished task. */
  key: string;
  ownerID: string | null;
  ownerName: string;
  title: string;
  /** 「进行中」 / 「等你确认」 … together with the tone class the dot uses. */
  label: string;
  tone: string;
  /** 「第 3 步 · 当前：跑测试」, or a closed task's own line. */
  stepLine: string;
  latest: string;
  waitingFor: string;
}

export interface StatusCardView {
  rows: StatusCardRow[];
  idleNames: string[];
}

/**
 * The card's rows in payload order. `owner` may be an id or a name (a payload that resolved it
 * first); an id is swapped for the member's name, anything else is shown as itself.
 */
export function statusCardRows(
  card: StatusCard,
  nameOf: (id: string) => string | undefined,
): StatusCardView {
  const rows = card.tasks.map((task, index) => {
    const resolved = nameOf(task.owner);
    const meta = taskStatusMeta(task.status);
    return {
      key: `${task.owner}-${task.title}-${index}`,
      ownerID: resolved ? task.owner : null,
      ownerName: resolved ?? task.owner,
      title: task.title,
      label: meta.label,
      tone: meta.tone,
      stepLine: statusStepLine(task),
      latest: task.latest.trim(),
      waitingFor: (task.waiting_for ?? "").trim(),
    };
  });
  return {
    rows,
    idleNames: card.idle.map((who) => nameOf(who) ?? who).filter((name) => name !== ""),
  };
}

/** The progress line for one row: queued and closed tasks say their own thing. */
export function statusStepLine(task: StatusCardTask): string {
  const current = task.current.trim();
  if (task.status === "queued") return "排队中";
  if (task.status === "done") return task.step > 0 ? `共 ${task.step} 步` : "已完成";
  if (task.status === "failed" || task.status === "stopped") {
    return task.step > 0 ? `停在第 ${task.step + 1} 步` : "";
  }
  return `第 ${task.step + 1} 步${current ? ` · 当前：${current}` : ""}`;
}
