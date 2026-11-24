// A scheduled task's newest run, opened from its row in the 「定时」 panel. The run worked in a
// hidden workspace nobody chats in, so this is the only way to see it: read-only, the same
// timeline a chat uses, refreshed while the run is still working.

import { useEffect, useState } from "react";
import type { Agent, ConversationDetail, Schedule, ScheduleRunDetail } from "../api/types";
import { errorMessage } from "../api/client";
import { lastResultText } from "../lib/schedules";
import { store, useTeam } from "../state/store";
import { Timeline } from "./ChatView";
import { Dialog } from "./Panels";

/** How often a working run is re-read; its rows only change as fast as the model answers. */
const REFRESH_MS = 2000;
/** Stable empty list: a fresh `[]` from a selector would churn the store subscription forever. */
const NO_AGENTS: Agent[] = [];

export function ScheduleRunSheet({
  schedule,
  onClose,
  onOpenConversation,
}: {
  schedule: Schedule;
  onClose: () => void;
  onOpenConversation: (id: string) => void;
}) {
  const agents = useTeam((s) => s.workspace)?.agents ?? NO_AGENTS;
  const [detail, setDetail] = useState<ScheduleRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = schedule.running;
  // The row's run id moves when a new fire starts; reading again then shows the new one.
  const runKey = live?.run_id ?? schedule.last_run_id ?? "";

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const controller = new AbortController();
    const read = async () => {
      try {
        const next = await (await store.readyClient()).scheduleRun(schedule.id, controller.signal);
        if (!alive) return;
        setDetail(next);
        setError(null);
        if (next.run?.status === "running") timer = window.setTimeout(read, REFRESH_MS);
      } catch (failure) {
        if (alive && !controller.signal.aborted) setError(errorMessage(failure));
      }
    };
    void read();
    return () => {
      alive = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [schedule.id, runKey]);

  const run = detail?.run ?? null;
  const running = run?.status === "running";
  const status = !run
    ? ""
    : run.status === "running" || run.status === "queued"
      ? live?.step || "正在进行"
      : lastResultText(run.status, run.error).replace(/^上次/, "这次") || "已结束";
  // The timeline only needs the rows; the conversation itself is hidden, so a stand-in names it.
  const timeline: ConversationDetail | null = detail?.run
    ? {
        conversation: {
          id: detail.run.conversation_id,
          kind: "direct",
          title: `定时任务「${schedule.title}」`,
          purpose: "",
          member_ids: [],
          last_message: null,
          created_at: detail.run.created_at,
          updated_at: detail.run.updated_at,
        },
        messages: detail.messages,
        latest_run: detail.run,
        legacy_roles: [],
        has_more: false,
        references: [],
      }
    : null;

  return (
    <Dialog title={`运行详情：${schedule.title}`} onClose={onClose} wide className="schedule-run-dialog">
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {!detail && !error && <p className="hint">正在读取这次运行…</p>}
      {detail && !run && <p className="hint">这个定时任务还没有运行过。</p>}
      {run && (
        <p className={running ? "schedule-run-status running" : `schedule-run-status ${run.status}`} role="status">
          {status}
          {run.model_calls > 0 && (
            <span className="schedule-run-counts">
              调用模型 {run.model_calls} 次，工具 {run.tool_calls} 次
            </span>
          )}
        </p>
      )}
      {timeline && (
        <div className="schedule-run-timeline chat-scroll">
          <div className="column">
            <Timeline
              detail={timeline}
              workspaceAgents={agents}
              memberNames={agents.map((agent) => agent.name)}
              onMention={() => {}}
              onOpenMember={() => {}}
              onOpenConversation={(id) => {
                onClose();
                onOpenConversation(id);
              }}
              onReply={() => {}}
              onOpenMemories={() => {}}
              onJump={() => {}}
              flashID={null}
              arrivals={new Set()}
              canReply={false}
              privateChat={false}
              running={running}
              step={running ? live?.step ?? run.current_step : ""}
            />
          </div>
        </div>
      )}
    </Dialog>
  );
}
