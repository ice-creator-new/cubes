// One bot's work, opened from its card, its chip or the work log: what it was asked, what it
// answered, and every step it took. The body is the chat's own timeline — sandbox folds, tool
// groups, commentary, images, consent cards — fed by the rows this turn wrote, so a record
// reads exactly like the turn did. The live ones update from the activity event while the
// sheet stays open.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  Activity,
  ActivityDetail,
  Agent,
  Conversation,
  ConversationDetail,
  Message,
} from "../api/types";
import { errorMessage } from "../api/client";
import {
  ACTIVITY_PILL,
  ACTIVITY_STATUS_META,
  activityStepLine,
  activityTime,
  isLiveActivity,
} from "../lib/activities";
import { elapsedMs } from "../lib/taskProgress";
import { momentStamp } from "../lib/time";
import { useSecondTick } from "../lib/motion";
import { store, useTeam } from "../state/store";
import { Timeline } from "./ChatView";
import { Dialog } from "./Dialog";
import { Icon } from "./Icon";
import { MessageAttachments } from "./Images";
import { MessageBody } from "./MessageBody";

/** How long after an event the sheet re-reads: the stream already sends at most one a second. */
const REFRESH_MS = 700;
/** No bot in the workspace yet: stable, so the timeline selector never churns. */
const NO_AGENTS: Agent[] = [];
const NO_ARRIVALS = new Set<string>();
const noop = () => undefined;

/** One section's rows as the chat would show them, with the answer left to the header. */
function sectionTimeline(
  detail: ActivityDetail,
  section: ActivityDetail["sections"][number],
  index: number,
): ConversationDetail {
  const activity = detail.activity;
  const answerID = index === 0 ? activity.answer_message_id : null;
  const messages = answerID ? section.messages.filter((message) => message.id !== answerID) : section.messages;
  const conversation: Conversation = {
    id: activity.conversation_id,
    kind: "direct",
    title: detail.conversation_title,
    purpose: "",
    member_ids: [],
    last_message: null,
    created_at: activity.started_at,
    updated_at: activity.finished_at ?? activity.started_at,
  };
  return {
    conversation,
    messages,
    latest_run: null,
    legacy_roles: [],
    has_more: false,
    references: [],
  };
}

export function ActivitySheet({
  activityID,
  active,
  onClose,
  onOpenConversation,
  onLoaded,
}: {
  activityID: string;
  /** Whether this sheet is the shown tab: Escape belongs to it only then. */
  active: boolean;
  onClose: () => void;
  onOpenConversation: (id: string) => void;
  /** The payload names its title and chat, so the tab can be labelled without another read. */
  onLoaded?: (activity: Activity, conversationTitle: string) => void;
}) {
  const live = useTeam((s) => s.activities[activityID]);
  const agents = useTeam((s) => s.workspace?.agents) ?? NO_AGENTS;
  const [detail, setDetail] = useState<ActivityDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const onLoadedRef = useRef(onLoaded);
  onLoadedRef.current = onLoaded;

  const read = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const next = await (await store.readyClient()).activityDetail(activityID, signal);
        if (signal?.aborted) return;
        setDetail(next);
        setError(null);
        onLoadedRef.current?.(next.activity, next.conversation_title);
      } catch (failure) {
        if (!signal?.aborted) setError(errorMessage(failure));
      }
    },
    [activityID],
  );

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setError(null);
    void read(controller.signal);
    return () => controller.abort();
  }, [read]);

  // Every event for this activity refreshes the sheet a beat later, so a burst of steps folds
  // into one read and the answer lands as soon as it is written.
  useEffect(() => {
    if (!live || !isLiveActivity(live.status)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => void read(controller.signal), REFRESH_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [live, read]);

  // Escape closes the sheet, but only while it is the page on screen and not while typing.
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

  const activity = live ?? detail?.activity ?? null;
  const meta = activity ? ACTIVITY_STATUS_META[activity.status] : null;
  const ticking = activity ? isLiveActivity(activity.status) : false;
  const now = useSecondTick(ticking);
  const running = activity ? isLiveActivity(activity.status) : false;
  const answer = detail?.answer ?? null;

  const memberNames = useMemo(() => agents.map((agent) => agent.name), [agents]);

  // Each section is the chat's timeline over that turn's own rows; the main answer is drawn
  // once, in the header above, so it never appears twice.
  const sections = useMemo(() => {
    if (!detail) return [];
    return detail.sections.map((section, index) => ({
      section,
      index,
      timeline: sectionTimeline(detail, section, index),
      running:
        index === 0
          ? isLiveActivity(detail.activity.status)
          : section.steps.some((step) => step.status === "running"),
    }));
  }, [detail]);

  // A hand-off row in the main section opens the section it caused on this page. A child's
  // own hand-offs lead outside the page, so the row keeps its usual target.
  const handoffTargets = useMemo(() => {
    const targets = new Map<string, string>();
    if (!detail) return targets;
    const known = new Set(detail.sections.map((section) => section.activity_id));
    for (const step of detail.sections[0]?.steps ?? []) {
      if (step.target_activity_id && known.has(step.target_activity_id)) {
        targets.set(step.message_id, step.target_activity_id);
      }
    }
    return targets;
  }, [detail]);

  // A hand-off scrolls to the section it led to and marks it for a moment.
  const jumpToSection = (sectionID: string) => {
    const element = document.getElementById(`activity-section-${sectionID}`);
    if (!element) return;
    element.scrollIntoView({ block: "start", behavior: "smooth" });
    setFlash(sectionID);
    window.setTimeout(() => setFlash((current) => (current === sectionID ? null : current)), 1200);
  };

  const openHandoff = (message: Message) => {
    const target = handoffTargets.get(message.id);
    if (!target) return false;
    jumpToSection(target);
    return true;
  };

  const openMember = (agentID: string) => {
    const child = detail?.sections.find(
      (section, index) => index > 0 && section.agent_id === agentID,
    );
    if (child) jumpToSection(child.activity_id);
  };

  const title = activity?.title.trim() || "工作记录";
  const summary = activity
    ? activity.summary.trim() || activity.current_step.trim() || activity.latest_text.trim()
    : "";
  const started = activity ? momentStamp(activity.started_at) : "";
  const took = activity?.finished_at
    ? activityTime(elapsedMs(activity.started_at, Date.now(), activity.finished_at))
    : activity
      ? activityTime(elapsedMs(activity.started_at, now))
      : "";

  return (
    <Dialog title="工作记录" onClose={onClose} wide className="activity-dialog">
      <div className="activity-sheet">
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {!detail && !error && !activity && <p className="hint">正在读取这份记录…</p>}

        <p className="activity-sheet-eyebrow">工作记录</p>

        {activity && meta && (
          <header className="activity-sheet-head">
            <span className={`run-status-pill ${ACTIVITY_PILL[meta.tone]}`}>{meta.label}</span>
            {running && <span className="activity-card-pulse" aria-hidden="true" />}
            <p className="activity-sheet-meta">
              <span>{started}</span>
              {took && <span>{running ? "已用" : "用时"} {took}</span>}
              <span>{activity.steps} 步</span>
              {detail && (
                <button
                  type="button"
                  className="link-button"
                  onClick={() => onOpenConversation(detail.activity.conversation_id)}
                >
                  {detail.conversation_title || "这个会话"}
                </button>
              )}
            </p>
          </header>
        )}

        {activity && <h2 className="activity-sheet-title">{title}</h2>}

        {summary && <p className="activity-sheet-summary">{summary}</p>}

        {activity && <hr className="activity-divider" />}

        {answer && (
          <div className="activity-sheet-answer">
            <MessageBody text={answer.content} markdown memberNames={memberNames} onMention={noop} />
            {answer.attachments && answer.attachments.length > 0 && (
              <MessageAttachments attachments={answer.attachments} from={answer.author_name} />
            )}
          </div>
        )}
        {!answer && running && (
          <p className="activity-sheet-waiting" role="status">
            <span className="activity-card-pulse" aria-hidden="true" />
            还在写，写完会出现在这里
          </p>
        )}

        {detail && detail.sections.length > 0 && (
          <>
            <hr className="activity-divider" />
            <div className="activity-sections">
              {sections.map(({ section, index, timeline, running: sectionRunning }) => (
                <section
                  key={section.id}
                  id={`activity-section-${section.id}`}
                  className={flash === section.id ? "activity-section flash" : "activity-section"}
                >
                  {/* The activity's own steps come first; every delegated one keeps its bot's name. */}
                  <h3 className="activity-section-label">{index === 0 ? "主线" : section.label}</h3>
                  <div className="activity-timeline chat-scroll">
                    <div className="column">
                      <Timeline
                        detail={timeline}
                        workspaceAgents={agents}
                        memberNames={memberNames}
                        onMention={noop}
                        onOpenMember={openMember}
                        onOpenConversation={onOpenConversation}
                        onOpenMemories={noop}
                        onHandoff={openHandoff}
                        onReply={noop}
                        onJump={noop}
                        flashID={null}
                        arrivals={NO_ARRIVALS}
                        canReply={false}
                        privateChat={false}
                        running={sectionRunning}
                        step={index === 0 && sectionRunning ? activity?.current_step ?? "" : ""}
                      />
                    </div>
                  </div>
                  {section.messages.length === 0 &&
                    (sectionRunning ? (
                      // Nothing written yet is a turn still thinking, not one that left no trace.
                      <p className="activity-sheet-waiting" role="status">
                        <Icon name="memory" size={13} />
                        {(index === 0 && activity ? activityStepLine(activity) : "") || "正在思考"}
                      </p>
                    ) : (
                      <p className="hint">这一轮没有留下过程。</p>
                    ))}
                </section>
              ))}
            </div>
          </>
        )}

        {detail && detail.sections.length === 0 && <p className="hint">这次没有留下过程记录。</p>}
      </div>
    </Dialog>
  );
}
