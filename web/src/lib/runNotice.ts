import type { RunStatus } from "../api/types";

/**
 * The line under a finished run that did not complete. A limit is not an error, and the
 * engine already wrote why the run stopped as a quiet notice in the thread, so a limited run
 * gets `null` here: the chat only keeps the way to its trace, never a second copy of the
 * reason.
 */
export function runNotice(
  status: Extract<RunStatus, "stopped" | "error" | "limited">,
  error: string | null,
): { title: string; detail: string } | null {
  if (status === "limited") return null;
  const detail = (error ?? "").trim();
  if (status === "stopped") return { title: "上一次任务已停止", detail };
  return { title: "上一次任务出错", detail };
}
