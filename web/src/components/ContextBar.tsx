// The composer's context ring: its own border fills clockwise from the top centre with the share
// of the window the next request would use, fed by the same request a real model call would
// send. Hovering thickens it slightly and names the room left; clicking opens the omp-style
// breakdown, where older turns can also be folded into the rolling summary now.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ContextMeter, Run } from "../api/types";
import { errorMessage } from "../api/client";
import {
  contextLayout,
  fillTone,
  percentLabel,
  remainingLabel,
  shareLabel,
  tokenLabel,
} from "../lib/contextBar";
import { TOOL_LABEL } from "../lib/toolLabels";
import { store, useTeam } from "../state/store";
import { Icon } from "./Icon";

/** How long the before → after line stays after a fold. */
const OUTCOME_MS = 6_000;
/** The ring's centre line sits this far inside the card, on top of its 1px edge. */
const RING_INSET = 0.75;

interface Box {
  width: number;
  height: number;
  radius: number;
}

function ringRadius({ width, height, radius }: Box): number {
  return Math.max(0, Math.min(radius, height / 2, width / 2) - RING_INSET);
}

/** The path's length in pixels: dashes are set in real lengths, not through `pathLength`,
 *  whose dash scaling older WebKit builds get wrong. */
export function ringLength(box: Box): number {
  const r = ringRadius(box);
  const straight = 2 * (box.width - 2 * RING_INSET - 2 * r) + 2 * (box.height - 2 * RING_INSET - 2 * r);
  return Math.max(0, straight) + 2 * Math.PI * r;
}

/**
 * The card's border as one closed path, starting where the top-left corner meets the top edge
 * and running clockwise: the first stretch fills left to right along the top, as the old bar
 * did, then wraps round, so p percent full is a dash of p percent of `ringLength`.
 */
export function ringPath(box: Box): string {
  const { width, height } = box;
  const inset = RING_INSET;
  const left = inset;
  const top = inset;
  const right = width - inset;
  const bottom = height - inset;
  const r = ringRadius(box);
  const arc = `A ${r} ${r} 0 0 1`;
  return [
    `M ${left + r} ${top}`,
    `H ${right - r}`,
    `${arc} ${right} ${top + r}`,
    `V ${bottom - r}`,
    `${arc} ${right - r} ${bottom}`,
    `H ${left + r}`,
    `${arc} ${left} ${bottom - r}`,
    `V ${top + r}`,
    `${arc} ${left + r} ${top}`,
    "Z",
  ].join(" ");
}

interface Outcome {
  before: number;
  after: number;
  auto: boolean;
}

export function ContextBar({
  conversationID,
  run,
  busy,
}: {
  conversationID: string;
  /** The live run: a finished reply changes the request, so the bar re-reads it. */
  run: Run | undefined;
  /** True while a reply is streaming: compaction is refused then. */
  busy: boolean;
}) {
  const [meter, setMeter] = useState<ContextMeter | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [hot, setHot] = useState<string | null>(null);
  const event = useTeam((state) => state.contextEvents[conversationID]);
  const host = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const reads = useRef(0);
  const [box, setBox] = useState<Box | null>(null);

  // The ring traces the card it sits in; the card grows with the text and eases its corners
  // when it turns multi-line, so it is measured whenever its size changes.
  useLayoutEffect(() => {
    const card = host.current?.parentElement;
    if (!card) return;
    const measure = () => {
      const radius = parseFloat(getComputedStyle(card).borderTopLeftRadius) || 0;
      setBox((previous) =>
        previous &&
        previous.width === card.offsetWidth &&
        previous.height === card.offsetHeight &&
        previous.radius === radius
          ? previous
          : { width: card.offsetWidth, height: card.offsetHeight, radius },
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(card);
    // The corner radius eases over a transition after the height changes.
    card.addEventListener("transitionend", measure);
    return () => {
      observer.disconnect();
      card.removeEventListener("transitionend", measure);
    };
  }, []);

  const refresh = async () => {
    const read = ++reads.current;
    try {
      const next = await store.conversationContext(conversationID);
      if (read !== reads.current) return;
      setMeter(next);
      setError(null);
    } catch (failure) {
      if (read !== reads.current) return;
      setError(errorMessage(failure));
    }
  };

  // A conversation change starts from nothing; a run's own end brings the next request.
  useEffect(() => {
    setMeter(null);
    setOutcome(null);
    setError(null);
    setOpen(false);
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationID]);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, run?.status]);

  // The backend folds after a run and during a turn; its event is the bar's only clock.
  useEffect(() => {
    if (!event) return;
    if (event.state === "compacting") {
      setOutcome(null);
      return;
    }
    void refresh();
    if (event.compacted && event.before !== undefined && event.after !== undefined) {
      setOutcome({ before: event.before, after: event.after, auto: event.auto });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [event]);

  useEffect(() => {
    if (!outcome) return;
    const timer = window.setTimeout(() => setOutcome(null), OUTCOME_MS);
    return () => window.clearTimeout(timer);
  }, [outcome]);

  // An open panel owns Escape and closes on any click outside it (including the bar).
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!host.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  useEffect(() => {
    if (open) panel.current?.focus();
  }, [open]);

  const compactNow = async () => {
    setRequesting(true);
    setError(null);
    setOutcome(null);
    try {
      const result = await store.compactConversation(conversationID);
      setMeter(result);
      if (result.compacted) setOutcome({ before: result.before, after: result.after, auto: false });
      else setError("这段对话还太短，没有可以压缩的内容。");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setRequesting(false);
    }
  };

  const compacting = requesting || event?.state === "compacting";
  const autoCompacting = !requesting && event?.state === "compacting" && event.auto;
  const exactPercent = meter && meter.window > 0 ? (meter.tokens * 100) / meter.window : 0;
  const tone = meter ? fillTone(exactPercent, meter.threshold_percent) : "accent";
  const layout = meter ? contextLayout(meter.categories, meter.window, meter.threshold) : null;
  const path = box ? ringPath(box) : "";
  const length = box ? ringLength(box) : 0;

  const tooltip = !meter
    ? error ?? "上下文用量"
    : compacting
      ? autoCompacting
        ? "正在自动压缩…"
        : "正在压缩…"
      : outcome
        ? `已压缩：${tokenLabel(outcome.before)} → ${tokenLabel(outcome.after)}`
        : `上下文 ${percentLabel(meter.percent)} · ${remainingLabel(meter.tokens, meter.threshold)}`;

  const disabledReason = busy
    ? "这一轮回复结束后才能压缩"
    : compacting
      ? "正在压缩…"
      : null;

  return (
    <div className={`context-bar${open ? " is-open" : ""}`} ref={host}>
      <button
        ref={trigger}
        type="button"
        className={`context-ring tone-${tone}${compacting ? " is-compacting" : ""}`}
        aria-label={tooltip}
        aria-expanded={open}
        aria-haspopup="dialog"
        disabled={!meter}
        onClick={() => setOpen((value) => !value)}
      >
        {box && (
          <svg
            className="context-ring-svg"
            width={box.width}
            height={box.height}
            viewBox={`0 0 ${box.width} ${box.height}`}
            aria-hidden="true"
          >
            {/* Only a band along the border takes the pointer, so the field and the buttons
                inside the card stay clickable; the band is the hover and click target. */}
            <path className="context-ring-hit" d={path} />
            {meter && exactPercent > 0 && (
              <path
                className="context-ring-fill"
                d={path}
                strokeDasharray={`${length} ${length}`}
                strokeDashoffset={length * (1 - Math.min(100, exactPercent) / 100)}
              />
            )}
            {meter && (
              <path
                className="context-ring-marker"
                d={path}
                strokeDasharray={`3 ${Math.max(0, length - 3)}`}
                strokeDashoffset={-(length * Math.min(100, meter.threshold_percent)) / 100 + 1.5}
              />
            )}
          </svg>
        )}
        <span className="context-tooltip" role="tooltip">
          {tooltip}
        </span>
      </button>
      {error && !meter && (
        <p className="context-bar-error" role="alert">
          {error}
        </p>
      )}

      {open && meter && layout && (
        <div
          className="context-panel"
          role="dialog"
          aria-modal="false"
          aria-labelledby="context-panel-title"
          tabIndex={-1}
          ref={panel}
        >
          <header className="context-panel-head">
            <h2 id="context-panel-title">上下文</h2>
            <span className="context-panel-model" title={`${meter.agent_name} · ${meter.model}`}>
              {meter.model} · {meter.provider}
            </span>
            <button
              type="button"
              className="context-panel-close"
              aria-label="关闭上下文面板"
              title="关闭（Esc）"
              onClick={() => {
                setOpen(false);
                trigger.current?.focus();
              }}
            >
              <Icon name="close" size={13} />
            </button>
          </header>

          <div className="context-panel-usage">
            <div className="context-panel-total" aria-label={tooltip}>
              <strong>{tokenLabel(meter.tokens)}</strong>
              <span>
                {" "}
                / {tokenLabel(meter.window)} tokens · {percentLabel(meter.percent)}
              </span>
            </div>
            <div className="context-panel-left">
              {compacting
                ? autoCompacting
                  ? "正在自动压缩…"
                  : "正在压缩…"
                : remainingLabel(meter.tokens, meter.threshold)}
              <span className="context-threshold">
                （阈值 {percentLabel(meter.threshold_percent)}）
              </span>
            </div>
          </div>

          <div className="context-stack" aria-hidden="true">
            <span
              className="context-stack-buffer"
              style={{ left: `${layout.buffer.left}%`, width: `${layout.buffer.width}%` }}
            />
            {layout.segments.map((segment) => (
              <span
                key={segment.id}
                className={`context-seg context-cat-${segment.id}${
                  hot && hot !== segment.id ? " is-dim" : ""
                }${hot === segment.id ? " is-hot" : ""}`}
                style={{ left: `${segment.left}%`, width: `${segment.width}%` }}
              />
            ))}
            <span className="context-stack-marker" style={{ left: `${layout.marker}%` }} />
          </div>

          <ul className="context-rows">
            {meter.categories.map((category) => (
              <li key={category.id}>
                <div
                  className={`context-row${hot === category.id ? " is-hot" : ""}`}
                  onMouseEnter={() => setHot(category.id)}
                  onMouseLeave={() => setHot(null)}
                >
                  <span
                    className={`context-swatch context-cat-${category.id}`}
                    aria-hidden="true"
                  />
                  <span className="context-row-label">{category.label}</span>
                  <span className="context-row-leader" aria-hidden="true" />
                  <span className="context-row-value">
                    {tokenLabel(category.tokens)} · {shareLabel(category.tokens, meter.window)}
                  </span>
                </div>
                {category.items && category.items.length > 0 && (
                  <ul className="context-subrows">
                    {category.items.map((item) => (
                      <li
                        key={item.name}
                        className="context-subrow"
                        title={item.name}
                        onMouseEnter={() => setHot(category.id)}
                        onMouseLeave={() => setHot(null)}
                      >
                        <span className="context-row-indent" aria-hidden="true" />
                        <span className="context-row-label">
                          {TOOL_LABEL[item.name] ?? item.name}
                        </span>
                        <span className="context-row-leader" aria-hidden="true" />
                        <span className="context-row-value">
                          {tokenLabel(item.tokens)} · {shareLabel(item.tokens, meter.window)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
            <li>
              <div className="context-row is-static">
                <span className="context-swatch hatched" aria-hidden="true" />
                <span className="context-row-label">自动压缩缓冲</span>
                <span className="context-row-leader" aria-hidden="true" />
                <span className="context-row-value">
                  {tokenLabel(meter.buffer)} · {shareLabel(meter.buffer, meter.window)}
                </span>
              </div>
            </li>
            <li>
              <div className="context-row is-static">
                <span className="context-swatch hollow" aria-hidden="true" />
                <span className="context-row-label">空闲空间</span>
                <span className="context-row-leader" aria-hidden="true" />
                <span className="context-row-value">
                  {tokenLabel(meter.free)} · {shareLabel(meter.free, meter.window)}
                </span>
              </div>
            </li>
          </ul>

          {outcome && (
            <p className="context-outcome" role="status">
              {outcome.auto ? "已自动压缩" : "已压缩"}：{tokenLabel(outcome.before)} →{" "}
              {tokenLabel(outcome.after)}
            </p>
          )}
          {error && (
            <p className="context-error" role="alert">
              {error}
            </p>
          )}

          <footer className="context-panel-foot">
            <p className="context-footnote">
              各类别是按同一个请求估算的份额。
              {meter.measured > meter.estimated
                ? ` 模型上次报告 ${tokenLabel(meter.measured)} tokens，已按比例缩放。`
                : ""}
            </p>
            <button
              type="button"
              className="context-compact"
              disabled={Boolean(disabledReason)}
              title={disabledReason ?? "把较早的对话折叠成摘要，腾出上下文"}
              onClick={() => void compactNow()}
            >
              {compacting ? (
                <>
                  <span className="spinner" aria-hidden="true" /> 压缩中…
                </>
              ) : (
                "立即压缩"
              )}
            </button>
          </footer>
        </div>
      )}
    </div>
  );
}
