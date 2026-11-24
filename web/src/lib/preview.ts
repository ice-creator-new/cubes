// One-line preview for a conversation row. Relay markers (`in`/`out`) carry no text of their
// own — the private exchange stays private — so they never replace the previous preview.

import type { Message } from "../api/types";

/** The new row preview, or null when this message must leave the previous one alone. */
export function messagePreview(message: Message): string | null {
  if (message.relay && message.relay.direction !== "summary") return null;
  const text = message.kind === "action" ? (message.action?.summary ?? message.content) : message.content;
  return text.replace(/\s+/g, " ").trim() || null;
}
