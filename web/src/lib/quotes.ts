// A reply quotes the question it answers only when that question is no longer the one right
// above it: the user spoke again in between, so the answer's "you" needs naming. In order, the
// bubble stays quiet — the question sits directly above and the author line already says 回复.

import type { Message } from "../api/types";
import { quoteExcerpt } from "./turns";

/** A reminder is a quiet system row, not the user speaking again. */
function isUserTurn(message: Message): boolean {
  return message.kind === "user" && !message.reminder;
}

export interface BubbleQuote {
  /** The user message this answer replies to; the quote line jumps there. */
  id: string;
  /** Always 你: only the user's own messages are quoted. */
  label: string;
  /** One line of the question, markdown flattened and cut. */
  excerpt: string;
}

/**
 * The quote an answer row owes, or ``null`` when it follows its question directly.
 *
 * ``answerIndex`` is where the answer sits in ``messages`` (an activity chip that produced no
 * answer passes the index its slot stands at). An answer to an earlier user message while a
 * newer one exists is exactly the out-of-order case: two questions, two answers, each quoting
 * the one it belongs to.
 */
export function outOfOrderQuote(
  messages: readonly Message[],
  answerIndex: number,
  questionID: string | null | undefined,
  limit = 60,
): BubbleQuote | null {
  if (!questionID || answerIndex <= 0) return null;
  const questionIndex = messages.findIndex((message) => message.id === questionID);
  if (questionIndex < 0 || questionIndex >= answerIndex) return null;
  const question = messages[questionIndex]!;
  if (!isUserTurn(question)) return null;
  const interleaved = messages
    .slice(questionIndex + 1, answerIndex)
    .some(isUserTurn);
  if (!interleaved) return null;
  const excerpt = quoteExcerpt(question.content, limit) || ((question.attachments?.length ?? 0) > 0 ? "（图片）" : "");
  if (!excerpt) return null;
  return { id: question.id, label: "你", excerpt };
}
