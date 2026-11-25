// One bot's whole work log: every activity it ran, newest first, split into days. Opened from
// the bot's right-click menu; each row opens the activity's detail sheet.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Activity, Agent } from "../api/types";
import { errorMessage } from "../api/client";
import {
  ACTIVITY_STATUS_META,
  activityGlyph,
  groupActivitiesByDay,
  isLiveActivity,
  type ActivityGlyph,
} from "../lib/activities";
import { clockTime } from "../lib/time";
import { store, useTeam } from "../state/store";
import { Avatar } from "./Avatar";
import { Dialog } from "./Dialog";
import { Icon, type IconName } from "./Icon";

const PAGE = 30;

/** The round glyph's drawing, chosen by the kind of work the row's own words name. */
const KIND_ICON: Record<ActivityGlyph, IconName> = {
  search: "search",
  file: "file",
  terminal: "terminal",
  browser: "globe",
  chat: "chat",
  work: "tasks",
};

/** One row's second line: what it concluded, else what it is doing, else nothing. */
function rowLine(activity: Activity): string {
  const summary = activity.summary.trim();
  if (summary) return summary;
  const step = activity.current_step.trim();
  if (step) return step;
  return activity.latest_text.trim();
}

export function WorkLog({
  agentID,
  active,
  onClose,
  onOpenActivity,
}: {
  agentID: string;
  /** Whether this sheet is the shown tab: Escape belongs to it only then. */
  active: boolean;
  onClose: () => void;
  onOpenActivity: (activityID: string) => void;
}) {
  const agent: Agent | undefined = useTeam((s) => s.workspace?.agents.find((item) => item.id === agentID));
  const activities = useTeam((s) => s.activities);
  const [items, setItems] = useState<Activity[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The newest read wins: a mount, a second page and a StrictMode remount never interleave. */
  const readSeq = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);

  const read = useCallback(
    async (before: string | null, signal?: AbortSignal) => {
      const seq = ++readSeq.current;
      setLoading(true);
      try {
        const page = await (await store.readyClient()).agentActivities(agentID, { limit: PAGE, before }, signal);
        if (signal?.aborted || seq !== readSeq.current) return;
        setItems((current) => {
          const seen = new Set(current.map((activity) => activity.id));
          return [...current, ...page.activities.filter((activity) => !seen.has(activity.id))];
        });
        // The tab is always read from its newest day: a body that opened at a restored scroll
        // offset would otherwise start below the 「今天」 header, which reads as a missing label.
        if (before === null) {
          window.requestAnimationFrame(() =>
            listRef.current?.closest<HTMLElement>(".dialog-body")?.scrollTo(0, 0),
          );
        }
        setHasMore(page.has_more);
        setError(null);
      } catch (failure) {
        if (!signal?.aborted && seq === readSeq.current) setError(errorMessage(failure));
      } finally {
        if (seq === readSeq.current) setLoading(false);
      }
    },
    [agentID],
  );

  useEffect(() => {
    const controller = new AbortController();
    setItems([]);
    setHasMore(false);
    setError(null);
    void read(null, controller.signal);
    return () => controller.abort();
  }, [read]);

  // Escape closes the page, but only while it is the shown one and not while typing.
  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, onClose]);

  // The sheet's own body scrolls, not this list: reaching its bottom asks for the next page.
  useEffect(() => {
    const scroller = listRef.current?.closest<HTMLElement>(".dialog-body") ?? null;
    if (!scroller || !hasMore) return;
    const onScroll = () => {
      const gap = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      if (gap < 160 && !loading) {
        const last = items[items.length - 1];
        if (last) void read(last.started_at);
      }
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [hasMore, items, loading, read]);

  const groups = useMemo(() => groupActivitiesByDay(items), [items]);

  return (
    <Dialog title={agent ? `${agent.name} · 工作记录` : "工作记录"} onClose={onClose} wide className="work-log-dialog">
      <div className="work-log" ref={listRef}>
        {agent && (
          <header className="work-log-head">
            <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={40} name={agent.name} agentID={agent.id} />
            <span className="work-log-head-text">
              <span className="work-log-name">{agent.name}</span>
              <span className="work-log-count">
                {items.length > 0 ? `最近 ${items.length} 件工作` : "这里会留下它做过的每件工作"}
              </span>
            </span>
          </header>
        )}

        {error && (
          <div className="inline-error" role="alert">
            <span>{error}</span>
            <button type="button" className="link-button" onClick={() => void read(items.at(-1)?.started_at ?? null)}>
              重试
            </button>
          </div>
        )}

        {items.length === 0 && !loading && !error && <p className="hint">还没有工作记录</p>}

        <div className="work-log-days">
          {groups.map((group) => (
            <section key={group.label} className="work-log-day">
              <h3 className="work-log-day-label">{group.label}</h3>
              <ul className="work-log-list">
                {group.activities.map((activity) => {
                  // A record the stream is still updating reads live; the list itself is a snapshot.
                  const current = activities[activity.id] ?? activity;
                  const meta = ACTIVITY_STATUS_META[current.status];
                  const glyph = activityGlyph(current);
                  return (
                    <li key={current.id}>
                      <button
                        type="button"
                        className={`work-log-row ${meta.tone} kind-${glyph}`}
                        onClick={() => onOpenActivity(current.id)}
                      >
                        <span className="work-log-row-icon" aria-hidden="true">
                          {isLiveActivity(current.status) ? (
                            <span className="activity-step-spinner" />
                          ) : (
                            <Icon name={KIND_ICON[glyph]} size={17} />
                          )}
                        </span>
                        <span className="work-log-row-text">
                          <span className="work-log-row-title">{current.title}</span>
                          <span className="work-log-row-line">{rowLine(current) || meta.label}</span>
                          <span className="work-log-row-time">
                            {clockTime(current.started_at)}
                            {isLiveActivity(current.status) && " · 进行中"}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>

        {loading && <p className="hint">正在读取…</p>}
      </div>
    </Dialog>
  );
}
