// One bot's work as the group chat shows it: a card while it runs, one quiet chip above its
// answer once it is done. The card's only job is to say what is happening; every step lives
// behind it, in the activity's detail sheet.

import type { Activity, Agent } from "../api/types";
import {
  ACTIVITY_STATUS_META,
  activityChipText,
  activityProgressLine,
  activityStepLine,
  activityThinking,
  isLiveActivity,
} from "../lib/activities";
import { useSecondTick } from "../lib/motion";
import { AuthorLine } from "./AuthorLine";
import { TOOL_ICON } from "../lib/toolIcons";
import { Icon, type IconName } from "./Icon";

/** The card's glyph: the brain while it thinks, else the icon its newest call's row draws. */
function cardIcon(activity: Activity | undefined): IconName {
  if (!activity || activityThinking(activity)) return "memory";
  return TOOL_ICON[activity.current_tool] ?? "info";
}

const CHIP_ICON: Record<string, IconName> = {
  done: "check",
  stopped: "pause",
  error: "close",
  waiting: "pause",
  live: "doing",
};

/**
 * One activity where its rows would be. A live one is a card under the bot's face; a finished
 * one is a chip right above the answer it produced. Without a payload yet (a reload read the
 * rows first) the card still shows, and the store fetches what it says.
 */
export function ActivitySlotView({
  activity,
  activityID,
  agent,
  fallbackName,
  fallbackSeed,
  showAuthor,
  onOpen,
}: {
  activity: Activity | undefined;
  activityID: string;
  /** The bot, for its face and its name; the payload alone can name it too. */
  agent: Agent | undefined;
  /** The turn's own row names the speaker before the activity payload lands. */
  fallbackName?: string;
  fallbackSeed?: string | null;
  /** Whether this is the first row of the bot's burst: the card draws the author line. */
  showAuthor: boolean;
  onOpen: (activityID: string) => void;
}) {
  const live = activity ? isLiveActivity(activity.status) : true;
  const now = useSecondTick(live);
  if (activity && !live) {
    const meta = ACTIVITY_STATUS_META[activity.status];
    const chip = (
      <button
        type="button"
        className={`activity-chip ${meta.tone}`}
        title={activity.title}
        onClick={() => onOpen(activityID)}
      >
        <Icon name={CHIP_ICON[meta.tone] ?? "history"} size={12} />
        <span className="activity-chip-text">{activityChipText(activity, now)}</span>
        <Icon name="chevron" size={11} />
      </button>
    );
    // A finished turn that left no answer stands on its own: who stopped is still named above.
    if (!showAuthor) return chip;
    return (
      <article className="turn agent activity-turn">
        <AuthorLine
          agent={agent}
          fallbackName={activity.agent_name || fallbackName}
          fallbackSeed={fallbackSeed}
          mood="idle"
        />
        {chip}
      </article>
    );
  }
  const title = activity?.title.trim() || "正在工作";
  return (
    <article className="turn agent activity-turn">
      {showAuthor && (
        <AuthorLine
          agent={agent}
          fallbackName={activity?.agent_name || fallbackName}
          fallbackSeed={fallbackSeed}
          mood="idle"
          note={activity?.status === "awaiting" ? "等你确认" : undefined}
        />
      )}
      <ActivityCardBody activity={activity} title={title} now={now} onOpen={() => onOpen(activityID)} />

    </article>
  );
}

/** The live card itself: a tinted kind tile, the title, and one shimmering step line. */
function ActivityCardBody({
  activity,
  title,
  now,
  onOpen,
}: {
  activity: Activity | undefined;
  title: string;
  now: number;
  onOpen: () => void;
}) {
  const working = activity === undefined || activity.status === "running";
  const icon = cardIcon(activity);
  const line = activity ? activityStepLine(activity) : "正在开始";
  const meta = activity ? ACTIVITY_STATUS_META[activity.status] : ACTIVITY_STATUS_META.running;
  return (
    <button
      type="button"
      className={`activity-card ${meta.tone}${working ? " working" : ""}`}
      aria-label={`${title}，${meta.label}。打开工作记录`}
      onClick={onOpen}
    >
      <span className="activity-card-icon" aria-hidden="true">
        <Icon name={icon} size={14} />
      </span>
      <span className="activity-card-body">
        <span className="activity-card-head">
          <span className="activity-card-title">{title}</span>
          {activity && <span className="activity-card-meta">{activityProgressLine(activity, now)}</span>}
        </span>
        <span className="activity-card-step">
          {/* A new step remounts the line, so its fade is the transition, not a jump. */}
          <span key={line} className={`activity-card-line${working ? " shimmer" : ""}`}>
            {line}
          </span>
        </span>
      </span>
      <span className="activity-card-chevron" aria-hidden="true">
        <Icon name="chevron" size={12} />
      </span>
    </button>
  );
}
