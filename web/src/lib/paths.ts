/** A path this short already fits the row; leave its verbatim value alone. */
const SHORT_PATH = 40;

/**
 * A tool row may name an absolute path. The row truncates from the end, which hides the one
 * part that tells two writes apart — the file's own name. Shorten the path in JS instead, to
 * `…/parent/file.ts`, so the row's CSS ellipsis never has to cut a Chinese sentence in half
 * (an RTL `direction` trick would reorder its punctuation).
 */
export function shortenPathSummary(text: string): string {
  if (!text.includes("/")) return text;
  return text
    .split(/(\s+)/)
    .map((part) => {
      if (!part.startsWith("/") && !part.startsWith("~/")) return part;
      // Keep a trailing comma, bracket or clause where it was: it belongs to the sentence.
      const cut = part.search(/[，。；：、)\]】」』]/);
      const suffix = cut >= 0 ? part.slice(cut) : "";
      const bare = cut >= 0 ? part.slice(0, cut) : part;
      if (bare.length <= SHORT_PATH) return part;
      const segments = bare.split("/").filter(Boolean);
      const name = segments.at(-1) ?? bare;
      const parent = segments.at(-2);
      const end = parent ? `${parent}/${name}` : name;
      return `…/${end}${suffix}`;
    })
    .join("");
}
