// The composer's context bar: how full the window is, and the stacked breakdown above it.
// Everything here is pure so the panel's arithmetic can be tested without a DOM.

import type { ContextCategory } from "../api/types";

/** One colored stretch of the stacked bar, in percentages of the whole window. */
export interface ContextSegment {
  id: ContextCategory["id"];
  label: string;
  tokens: number;
  /** Where the stretch starts, 0–100. */
  left: number;
  /** How wide it is, 0–100; a request already past the window is clipped at the end. */
  width: number;
}

export interface ContextLayout {
  segments: ContextSegment[];
  /** Room between the used total and the auto-compaction marker. */
  free: { left: number; width: number };
  /** Everything from the marker to the window's end, drawn hatched. */
  buffer: { left: number; width: number };
  /** The marker itself, as a percentage. */
  marker: number;
}

export type FillTone = "accent" | "warn" | "danger";

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, value));
}

function trim(value: number, digits: number): string {
  return value.toFixed(digits).replace(/\.?0+$/, "");
}

/** A token count as the panel writes it: 999, 8.6K, 557K, 1.2M. */
export function tokenLabel(tokens: number): string {
  const value = Math.max(0, Math.round(Number.isFinite(tokens) ? tokens : 0));
  if (value >= 1_000_000) return `${trim(value / 1_000_000, 2)}M`;
  if (value >= 10_000) return `${Math.round(value / 1_000)}K`;
  if (value >= 1_000) return `${trim(value / 1_000, 1)}K`;
  return String(value);
}

/** A whole-number share, e.g. 56%. */
export function percentLabel(percent: number): string {
  return `${Math.round(clampPercent(percent))}%`;
}

/** One row's share of the window: 0.9%, 12%, or <0.1% when it rounds away. */
export function shareLabel(tokens: number, window: number): string {
  if (!(window > 0) || !(tokens > 0)) return "0%";
  const value = (tokens * 100) / window;
  if (value < 0.1) return "<0.1%";
  if (value >= 10) return `${Math.round(value)}%`;
  return `${value.toFixed(1).replace(/\.0$/, "")}%`;
}

/** What is left before compaction, or that the point has been reached. */
export function remainingLabel(tokens: number, threshold: number): string {
  const left = threshold - tokens;
  return left > 0 ? `还剩 ${tokenLabel(left)} 后自动压缩` : "已到自动压缩阈值";
}

/** Accent well below the point, warn as it nears, danger at or over it. */
export function fillTone(percent: number, thresholdPercent: number): FillTone {
  if (percent >= thresholdPercent) return "danger";
  if (percent >= 60) return "warn";
  return "accent";
}

/**
 * The stacked bar: one stretch per category, the free room, the hatched buffer and the
 * marker. Widths are shares of the model's whole window, so the marker sits at the same
 * place whatever the categories are.
 */
export function contextLayout(
  categories: ContextCategory[],
  window: number,
  threshold: number,
): ContextLayout {
  const scale = window > 0 ? 100 / window : 0;
  let cursor = 0;
  const segments: ContextSegment[] = [];
  for (const category of categories) {
    if (!(category.tokens > 0)) continue;
    const width = Math.min(Math.max(category.tokens * scale, 0), Math.max(0, 100 - cursor));
    segments.push({
      id: category.id,
      label: category.label,
      tokens: category.tokens,
      left: cursor,
      width,
    });
    cursor += width;
  }
  const marker = clampPercent((threshold > 0 ? threshold * scale : 0));
  const freeEnd = Math.min(cursor, marker);
  return {
    segments,
    free: { left: freeEnd, width: Math.max(0, marker - freeEnd) },
    buffer: { left: marker, width: Math.max(0, 100 - marker) },
    marker,
  };
}
