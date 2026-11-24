// A terminal step's folded row: 「运行「<command>」，完成」. A plain end ellipsis hides the end
// of the command, which is often what tells two runs apart (the file it reads, the port it
// serves, the `| tail` it pipes into). So the row keeps both ends: the start shrinks with an
// ellipsis, and a fixed tail — the command's last characters plus the status — always shows.
// The split is text; where the cut lands follows the row's real width, in CSS.

/** How many of the command's last characters always stay visible on the folded row. */
export const COMMAND_TAIL_CHARS = 24;

/** The two halves a row draws: `head` shrinks with an ellipsis, `tail` never does. */
export interface MiddleSplit {
  head: string;
  tail: string;
}

/**
 * The folded row for a terminal step, from the backend's sentence and the whole command.
 * `summary` reads 「运行「<shortened>」<status>」; its shortened command is replaced by the whole
 * one (collapsed to one line), and the status after the quote is kept as it was.
 * Returns null when the summary is not a command sentence, so the row falls back to its text.
 */
export function commandRow(summary: string, command: string, tailChars = COMMAND_TAIL_CHARS): MiddleSplit | null {
  const open = summary.indexOf("「");
  const close = summary.lastIndexOf("」");
  if (open < 0 || close <= open) return null;
  const whole = command.split(/\s+/).filter(Boolean).join(" ");
  if (!whole) return null;
  const verb = summary.slice(0, open + 1);
  const status = summary.slice(close);
  const chars = Array.from(whole);
  if (chars.length <= tailChars) return { head: verb, tail: `${whole}${status}` };
  return {
    head: `${verb}${chars.slice(0, chars.length - tailChars).join("")}`,
    tail: `${chars.slice(chars.length - tailChars).join("")}${status}`,
  };
}
