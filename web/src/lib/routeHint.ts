// How a group's routed user message reads under its bubble: one faint line, and a tooltip that
// says who decided and why. Pure so the wording can be tested without a chat.

import type { RouteDecision, RouteIntent } from "../api/types";

/** The tooltip prefix each decider owns; the reason follows it as one sentence. */
export const ROUTE_BY_PREFIX: Record<RouteDecision["by"], string> = {
  dispatcher: "自动判断：",
  fallback: "没判断出来，交给了上一位说话的人",
  mention: "你点了名",
  user: "你改过",
};

export interface RouteHintView {
  /** The faint line under the bubble, e.g. 「→ 匠师、乙」. */
  text: string;
  /** The hover tooltip: who decided, then the short reason. */
  title: string;
  /** One line per intent, when the message carried several asks. */
  lines?: string[];
}

/** The hint line and tooltip for one route. Unknown speaker ids are dropped, not shown raw. */
export function routeHint(
  route: RouteDecision,
  nameOf: (id: string) => string | undefined,
  taskTitle?: (id: string) => string | undefined,
): RouteHintView {
  // Still deciding: the hint says so instead of naming anyone; the dots animate in the UI.
  if (route.pending) return { text: "正在决定谁来接", title: "正在判断这条消息该交给谁" };
  const names = (ids: string[]) => ids.map((id) => nameOf(id) ?? "").filter((name) => name !== "");
  const speakers = names(route.speakers);
  if (route.intents && route.intents.length > 1) {
    const lines = route.intents.map((intent) => intentLine(intent, names, taskTitle));
    return { text: lines.join(" · "), title: routeTitle(route), lines };
  }
  let text: string;
  switch (route.kind) {
    case "answer":
      text = speakers.length > 0 ? `→ ${speakers.join("、")}` : "→ 等成员回答";
      break;
    case "invite":
      text = speakers.length > 0 ? `→ 邀请 ${speakers.join("、")}` : "→ 邀请新人";
      break;
    case "steer":
      text = `→ 并入「${taskTitle?.(route.task_id ?? "") ?? "那件事"}」`;
      break;
    case "resume":
      text = `→ 接着做「${taskTitle?.(route.task_id ?? "") ?? "那件事"}」`;
      break;
    case "status":
      text = "→ 进度";
      break;
    case "none":
      text = "→ 不需要回复";
      break;
  }
  return { text, title: routeTitle(route) };
}

/** One intent's line: the slice in quotes, then who it went to. */
function intentLine(
  intent: RouteIntent,
  names: (ids: string[]) => string[],
  taskTitle?: (id: string) => string | undefined,
): string {
  const quoted = intent.text ? `“${intent.text}”` : "这一部分";
  if (intent.kind === "invite") {
    const invited = names(intent.invite).join("、") || "新人";
    const takers = names(intent.speakers).join("、") || invited;
    return `邀请 ${invited} → ${takers}`;
  }
  if (intent.kind === "steer") {
    return `${quoted} → 并入「${taskTitle?.(intent.task_id ?? "") ?? "那件事"}」`;
  }
  if (intent.kind === "resume") {
    return `${quoted} → 接着做「${taskTitle?.(intent.task_id ?? "") ?? "那件事"}」`;
  }
  if (intent.kind === "status") return `${quoted} → 进度`;
  const speakers = names(intent.speakers).join("、") || "等成员回答";
  return `${quoted} → ${speakers}`;
}

/** The tooltip sentence: the decider's prefix, then the reason when there is one. */
export function routeTitle(route: RouteDecision): string {
  const prefix = ROUTE_BY_PREFIX[route.by];
  const reason = (route.reason ?? "").trim();
  if (!reason) return prefix.endsWith("：") ? prefix.slice(0, -1) : prefix;
  return prefix.endsWith("：") ? `${prefix}${reason}` : `${prefix}：${reason}`;
}

/** A failed dispatch, in two or three words a settings line can carry. */
export function dispatchFailureWord(error: string | null): string {
  if (error === "TimeoutError") return "超时";
  if (error === "UpstreamError") return "模型出错";
  if (error === "Unreadable") return "结果看不懂";
  return "原因不明";
}
