// The text areas of the connector form: one header per line (`名称: 值`), one environment
// variable per line (`NAME=value`), one argument per line. Values are taken whole, so a URL in a
// header value or an `=` inside an environment value survives. Only the shape of a line is
// checked here; the server has the last word on what its service accepts.

/** What a pair text area parsed to: the pairs, or the first line that could not be read. */
export interface ParsedPairs {
  values: Record<string, string>;
  /** A Chinese sentence naming the offending line; null when every line was read. */
  error: string | null;
}

/** `Authorization: Bearer a:b` -> one header; the split is at the first colon only. */
export function parseHeaders(text: string): ParsedPairs {
  const values: Record<string, string> = {};
  const lines = text.split(/\r?\n/);
  for (const [index, raw] of lines.entries()) {
    if (raw.trim() === "") continue;
    const colon = raw.indexOf(":");
    if (colon < 0) return { values, error: `第 ${index + 1} 行缺少冒号，请写成「名称: 值」。` };
    const name = raw.slice(0, colon).trim();
    if (!name) return { values, error: `第 ${index + 1} 行没写请求头名称。` };
    values[name] = raw.slice(colon + 1).trim();
  }
  return { values, error: null };
}

/** `API_KEY=a=b` -> one variable; the split is at the first equals sign only. */
export function parseEnv(text: string): ParsedPairs {
  const values: Record<string, string> = {};
  const lines = text.split(/\r?\n/);
  for (const [index, raw] of lines.entries()) {
    if (raw.trim() === "") continue;
    const equals = raw.indexOf("=");
    if (equals < 0) return { values, error: `第 ${index + 1} 行缺少等号，请写成「NAME=value」。` };
    const name = raw.slice(0, equals).trim();
    if (!name) return { values, error: `第 ${index + 1} 行没写变量名。` };
    values[name] = raw.slice(equals + 1).trim();
  }
  return { values, error: null };
}

/** One argument per line; blank lines are dropped, everything else is kept as typed. */
export function parseArgs(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
}
