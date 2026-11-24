// Pure helpers for the skills panel: the upload's content type and the one-line credit under a
// skill's name. Kept separate so the panel and its tests can share them without the store.

import type { Skill } from "../api/types";

/** The raw-body content type: a `.md` uploads as a single SKILL.md, anything else as a zip. */
export function skillUploadContentType(name: string): "application/zip" | "text/markdown" {
  return name.toLowerCase().endsWith(".md") ? "text/markdown" : "application/zip";
}

/** The GitHub host a source URL points at, or `GitHub` when it cannot be read. */
export function skillSourceHost(url: string | null): string {
  if (!url) return "GitHub";
  try {
    return new URL(url).host;
  } catch {
    return "GitHub";
  }
}

/** The one-line credit under a skill's name, in the user's words. */
export function skillSourceLine(skill: Skill): string {
  switch (skill.source) {
    case "user":
      return "自己写的";
    case "agent":
      return skill.author_name ? `由「${skill.author_name}」写的` : "机器人写的";
    case "upload":
      return "上传的";
    case "local":
      return "从本机导入的";
    case "github":
      return `来自 ${skillSourceHost(skill.source_url)}`;
  }
}

/** The unfinished `/名称` right before the caret, when the skill picker should be showing. */
export interface SlashQuery {
  text: string;
  start: number;
}

// A skill name as typed so far: the spec's lowercase letters, digits and hyphens.
const PARTIAL_NAME = /^[a-z0-9-]*$/i;

/** `/` opens the picker only at the start of the draft or after whitespace, so `a/b` and URLs never do. */
export function slashQuery(text: string, caret = text.length): SlashQuery | null {
  const head = text.slice(0, caret);
  const marker = head.lastIndexOf("/");
  if (marker < 0) return null;
  const before = head[marker - 1];
  if (before !== undefined && !/\s/u.test(before)) return null;
  const suffix = head.slice(marker + 1);
  if (!PARTIAL_NAME.test(suffix)) return null;
  return { text: suffix, start: marker };
}

/** Replaces the open `/…` with `/名称 `; returns the new text and caret. */
export function insertSlash(draft: string, name: string, caret = draft.length): { text: string; caret: number } {
  const query = slashQuery(draft, caret);
  const start = query ? query.start : caret;
  const replacement = `/${name} `;
  return { text: draft.slice(0, start) + replacement + draft.slice(caret), caret: start + replacement.length };
}

export interface SlashToken {
  name: string;
  start: number;
  end: number;
}

/** Every `/名称` in sent text that names one of `names` (start of text or after whitespace). */
export function slashTokens(text: string, names: ReadonlySet<string>): SlashToken[] {
  const tokens: SlashToken[] = [];
  for (const match of text.matchAll(/(^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)(?![a-z0-9-])/giu)) {
    const name = match[2]!;
    if (!names.has(name)) continue;
    const start = match.index! + match[1]!.length;
    tokens.push({ name, start, end: start + 1 + name.length });
  }
  return tokens;
}
