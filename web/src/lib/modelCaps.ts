// Model capabilities as the settings sheet shows them: how a window reads, what a chip's
// tooltip says, and how the image chip cycles through auto → on → off. Pure, so the wording
// and the cycling can be tested without a dialog.

import type { CapSource, ModelCaps, ModelOverride } from "../api/types";

/** The four layers, in the order the backend resolves them. */
export const CAP_SOURCE_LABELS: Record<CapSource, string> = {
  user: "你设置的",
  provider: "服务商提供",
  catalog: "models.dev",
  default: "默认值",
};

export function capSourceLabel(source: CapSource | undefined): string {
  return CAP_SOURCE_LABELS[source ?? "default"];
}

/** What one model shows now: the detected value with the user's live override on top. */
export interface CapView {
  images: boolean;
  video: boolean;
  audio: boolean;
  pdf: boolean;
  context_window: number;
  max_output: number;
  reasoning: boolean;
  reasoning_levels: string[];
  sources: Record<string, CapSource>;
}

/**
 * A model's chips. `caps` is what the backend resolved last time it was read; `override` is
 * the unsaved edit in the form, so a click shows its effect before saving.
 */
export function capabilityView(caps: ModelCaps | undefined, override: ModelOverride | undefined): CapView {
  const base: CapView = {
    images: caps?.images ?? false,
    video: caps?.video ?? false,
    audio: caps?.audio ?? false,
    pdf: caps?.pdf ?? false,
    context_window: caps?.context_window ?? 0,
    max_output: caps?.max_output ?? 0,
    reasoning: caps?.reasoning ?? false,
    reasoning_levels: caps?.reasoning_levels ?? [],
    sources: { ...(caps?.sources ?? {}) },
  };
  if (typeof override?.images === "boolean") {
    base.images = override.images;
    base.sources.images = "user";
  }
  if (typeof override?.context === "number" && override.context > 0) {
    base.context_window = override.context;
    base.sources.context_window = "user";
  }
  if (typeof override?.output === "number" && override.output > 0) {
    base.max_output = override.output;
    base.sources.max_output = "user";
  }
  return base;
}

/** The image chip's next state: auto → 开 → 关 → auto, written as an override patch. */
export function cycleImages(override: ModelOverride | undefined): ModelOverride {
  const next: ModelOverride = { ...(override ?? {}) };
  if (override?.images === true) next.images = false;
  else if (override?.images === false) {
    next.images = null;
    if (next.context == null && next.output == null) delete next.images;
  } else next.images = true;
  return next;
}

/** A token count as the chips read it: 1M, 200K, 128K, 4096. */
export function formatTokens(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) return "—";
  if (tokens >= 1_000_000) {
    const millions = (tokens / 1_000_000).toFixed(1).replace(/\.0$/, "");
    return `${millions}M`;
  }
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}K`;
  return String(tokens);
}

/** The override with an auto field removed; an entry with nothing left disappears. */
export function withOverrideField(
  overrides: Record<string, ModelOverride>,
  model: string,
  patch: ModelOverride,
): Record<string, ModelOverride> {
  const merged: ModelOverride = { ...(overrides[model] ?? {}) };
  for (const key of ["images", "context", "output"] as const) {
    if (!(key in patch)) continue;
    const value = patch[key];
    if (value === null || value === undefined) delete merged[key];
    else Object.assign(merged, { [key]: value });
  }
  const next = { ...overrides };
  if (merged.images === undefined && merged.context === undefined && merged.output === undefined) {
    delete next[model];
  } else {
    next[model] = merged;
  }
  return next;
}
