// LaTeX in replies, made safe for remark-math before the Markdown is parsed:
// - models write `\[ … \]` and `\( … \)` as often as `$$ … $$` and `$ … $`, but remark-math only
//   reads the dollar forms, so the bracket forms are turned into dollars;
// - a `$` that does not open a formula (prices: 「$5 到 $10」) is escaped, by Pandoc's rule: a
//   closing `$` must follow a non-space and must not be followed by a digit.
// Code, fenced or inline, is left exactly as written by both steps.

/** A fenced code block, an inline code span, or one of the two bracket math forms. */
const TOKEN =
  /(?<fenced>^ {0,3}(?<fence>`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^ {0,3}\k<fence>[ \t]*$|(?![\s\S])))|(?<span>(?<ticks>`+)[\s\S]*?\k<ticks>)|\\\[(?<display>[\s\S]+?)\\\]|\\\((?<inline>[\s\S]+?)\\\)/gm;

interface Groups {
  display?: string;
  inline?: string;
}

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const MATH_FENCE = /^ {0,3}\$\$/;
const SPAN = /(`+)[\s\S]*?\1/g;

/** `source` ready for remark-math: bracket math as dollars, stray dollars escaped, code untouched. */
export function normalizeMath(source: string): string {
  const bracketed =
    source.includes("\\[") || source.includes("\\(")
      ? source.replace(TOKEN, (match: string, ...rest: unknown[]) => {
          const { display, inline } = rest[rest.length - 1] as Groups;
          if (display !== undefined) return `\n$$\n${display.trim()}\n$$\n`;
          if (inline !== undefined) return `$${inline.trim()}$`;
          return match;
        })
      : source;
  return bracketed.includes("$") ? escapeStrayDollars(bracketed) : bracketed;
}

/** Every line outside fences and display-math blocks, with stray single dollars escaped. */
function escapeStrayDollars(source: string): string {
  let fence: string | null = null;
  let math = false;
  return source
    .split(/(?<=\n)/)
    .map((line) => {
      const content = line.replace(/\n$/, "");
      const marker = FENCE.exec(content)?.[1];
      if (marker !== undefined) {
        if (fence === null) fence = marker;
        else if (marker[0] === fence[0] && marker.length >= fence.length && content.trim() === marker) fence = null;
        return line;
      }
      if (fence !== null) return line;
      if (MATH_FENCE.test(content)) {
        const rest = content.trim().slice(2);
        if (math) math = false;
        else if (!rest.includes("$$")) math = true;
        return line;
      }
      if (math) {
        if (content.trimEnd().endsWith("$$")) math = false;
        return line;
      }
      return escapeOutsideSpans(line);
    })
    .join("");
}

function escapeOutsideSpans(line: string): string {
  let out = "";
  let cursor = 0;
  for (const span of line.matchAll(SPAN)) {
    out += escapeDollars(line.slice(cursor, span.index));
    out += span[0];
    cursor = span.index + span[0].length;
  }
  return out + escapeDollars(line.slice(cursor));
}

/** Pair single `$` the way Pandoc does; any `$` left unpaired is written as `\$`. */
function escapeDollars(text: string): string {
  const singles: number[] = [];
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] !== "$" || text[i - 1] === "\\") continue;
    if (text[i + 1] === "$") {
      i += 1; // `$$` inline display math: left to remark-math
      continue;
    }
    singles.push(i);
  }
  if (singles.length === 0) return text;
  const stray = new Set<number>();
  let open = -1;
  for (const at of singles) {
    if (open < 0) {
      // An opener needs something right after it.
      if (/\s/.test(text[at + 1] ?? " ")) stray.add(at);
      else open = at;
      continue;
    }
    const closes = !/\s/.test(text[at - 1] ?? " ") && !/\d/.test(text[at + 1] ?? "");
    if (closes) {
      open = -1;
      continue;
    }
    // Not a closer: the opener was a price, and this `$` may open the next formula.
    stray.add(open);
    if (/\s/.test(text[at + 1] ?? " ")) {
      stray.add(at);
      open = -1;
    } else open = at;
  }
  if (open >= 0) stray.add(open);
  if (stray.size === 0) return text;
  let out = "";
  for (let i = 0; i < text.length; i += 1) out += stray.has(i) ? "\\$" : text[i];
  return out;
}
