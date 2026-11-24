// How a group's routed user message reads under its bubble: one faint line, and a tooltip that
// says who decided and why. Pure so the wording can be tested without a chat.

import type { RouteDecision } from "../api/types";

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
}

/** The hint line and tooltip for one route. Unknown speaker ids are dropped, not shown raw. */
export function routeHint(
  route: RouteDecision,
  nameOf: (id: string) => string | undefined,
  taskTitle?: (id: string) => string | undefined,
): RouteHintView {
  const speakers = route.speakers.map((id) => nameOf(id) ?? "").filter((name) => name !== "");
  let text: string;
  switch (route.kind) {
    case "answer":
      text = speakers.length > 0 ? `→ ${speakers.join("、")}` : "→ 等成员回答";
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

/** The tooltip sentence: the decider's prefix, then the reason when there is one. */
export function routeTitle(route: RouteDecision): string {
  const prefix = ROUTE_BY_PREFIX[route.by];
  const reason = (route.reason ?? "").trim();
  if (!reason) return prefix.endsWith("：") ? prefix.slice(0, -1) : prefix;
  return prefix.endsWith("：") ? `${prefix}${reason}` : `${prefix}：${reason}`;
}
