// A group's work, kept out of the thread. The chat header carries one small chip: it speaks only
// when something is open (「2 项进行中」, 「1 项停下了」) and is a quiet icon otherwise. The chip
// opens a list with one entry per request the user sent, the members who took it, and the parts
// they handed on. A stopped request can be picked up again from there.

import { useEffect, useRef, useState } from "react";
import type { GroupTask } from "../api/types";
import { agentByID, store, useTeam } from "../state/store";
import { groupByRequest, workSummary, type RequestWork } from "../lib/groupTasks";
import { isTaskDismissed, taskStatusMeta } from "../lib/taskProgress";
import { usePresence } from "../lib/motion";
import { Avatar } from "./Avatar";
import { Icon } from "./Icon";

const STATUS_LABEL: Record<GroupTask["status"], string> = {
  queued: "排队中",
  running: "进行中",
  awaiting: "等你确认",
  done: "完成",
  failed: "没做成",
  stopped: "停下了",
};

export function GroupTasks({
  conversationID,
  onOpenConversation,
}: {
  conversationID: string;
  /** Opens a task's thread, where its turns happen. */
  onOpenConversation: (id: string) => void;
}) {
  const tasks = useTeam((s) => s.groupTasks[conversationID]);
  const [open, setOpen] = useState(false);
  const shell = useRef<HTMLDivElement>(null);
  const popover = usePresence(open ? true : null, 140);

  useEffect(() => {
    void store.loadGroupTasks(conversationID);
  }, [conversationID]);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!shell.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Put-down and archived rows stay in the timeline but not in this list.
  const visible = (tasks ?? []).filter((task) => !isTaskDismissed(task));
  if (!tasks || visible.length === 0) return null;
  const work = groupByRequest(visible);
  const { running, awaiting, queued, stopped } = workSummary(work);
  const label =
    [
      running > 0 ? `${running} 项进行中` : "",
      awaiting > 0 ? `${awaiting} 项等你确认` : "",
      queued > 0 ? `${queued} 项排队` : "",
      stopped > 0 ? `${stopped} 项停下了` : "",
    ]
      .filter(Boolean)
      .join(" · ") || null;
  const tone = running > 0 ? "running" : awaiting > 0 ? "awaiting" : queued > 0 ? "queued" : stopped > 0 ? "stopped" : "";

  return (
    <div className="work-chip-shell" ref={shell}>
      <button
        type="button"
        className={["work-chip", tone, label ? "" : "quiet"].filter(Boolean).join(" ")}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={label ? `群里的工作：${label}` : "群里的工作"}
        title={label ? undefined : "群里的工作"}
        onClick={() => setOpen((value) => !value)}
      >
        {tone === "running" || tone === "awaiting" ? (
          <span className="work-dot" aria-hidden="true" />
        ) : (
          <Icon name="tasks" size={15} />
        )}
        {label && <span>{label}</span>}
      </button>
      {popover.value && (
        <div className={popover.open ? "work-pop" : "work-pop closing"} role="dialog" aria-label="群里的工作">
          <div className="work-pop-head">群里的工作</div>
          <ul className="work-list">
            {work.map((item) => (
              <WorkItem
                key={item.key}
                item={item}
                conversationID={conversationID}
                onJump={() => setOpen(false)}
                onOpenConversation={onOpenConversation}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function WorkItem({
  item,
  conversationID,
  onJump,
  onOpenConversation,
}: {
  item: RequestWork;
  conversationID: string;
  onJump: () => void;
  onOpenConversation: (id: string) => void;
}) {
  const jump = (messageID: string | null) => {
    if (!messageID) return;
    store.focusMessage(conversationID, messageID);
    onJump();
  };
  return (
    <li className={`work-item ${item.status}`}>
      <button
        type="button"
        className="work-request"
        disabled={!item.requestID}
        title={item.requestID ? "跳到这条消息" : undefined}
        onClick={() => jump(item.requestID)}
      >
        <span className="work-request-title">{item.title}</span>
        <span className="work-request-time">{shortTime(item.createdAt)}</span>
      </button>
      <ul className="work-owners">
        {item.owners.map((task) => (
          <WorkTaskRow
            key={task.id}
            task={task}
            part={false}
            conversationID={conversationID}
            onJump={jump}
            onOpenConversation={onOpenConversation}
          />
        ))}
        {item.parts.map((task) => (
          <WorkTaskRow
            key={task.id}
            task={task}
            part
            conversationID={conversationID}
            onJump={jump}
            onOpenConversation={onOpenConversation}
          />
        ))}
      </ul>
    </li>
  );
}

function WorkTaskRow({
  task,
  part,
  conversationID,
  onJump,
  onOpenConversation,
}: {
  task: GroupTask;
  part: boolean;
  conversationID: string;
  onJump: (messageID: string | null) => void;
  onOpenConversation: (id: string) => void;
}) {
  const workspace = useTeam((s) => s.workspace);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const owner = agentByID(workspace, task.owner_id);
  const giver = part && task.assigned_by ? agentByID(workspace, task.assigned_by) : undefined;
  const meta = taskStatusMeta(task.status);
  const canResume = task.status === "stopped" || task.status === "failed";
  const control = async (action: () => Promise<string | null>) => {
    setPending(true);
    setError(null);
    const failure = await action();
    setPending(false);
    if (failure) setError(failure);
  };
  return (
    <li className="work-row">
      <button
        type="button"
        className={part ? "work-owner part" : "work-owner"}
        disabled={!task.result_message_id}
        title={task.result_message_id ? "跳到这条回答" : undefined}
        onClick={() => onJump(task.result_message_id)}
      >
        {owner ? (
          <Avatar seed={owner.avatar_seed} appearance={owner.appearance} size={18} name={owner.name} agentID={owner.id} />
        ) : (
          <span className="work-gone" aria-hidden="true" />
        )}
        <span className="work-owner-text">
          <span className="work-owner-name">{owner?.name ?? "已删除的成员"}</span>
          {part && (
            <span className="work-owner-note">
              {giver ? `${giver.name}分给它：` : ""}
              {task.title}
            </span>
          )}
          {!part && task.rounds > 1 && <span className="work-owner-note">做了 {task.rounds} 轮</span>}
        </span>
        <span className={`work-status ${meta.tone}`}>{STATUS_LABEL[task.status]}</span>
      </button>
      <span className="work-row-actions">
        {canResume && (
          <button
            type="button"
            className="work-thread"
            disabled={pending}
            title="在这个任务自己的过程里接着做"
            onClick={() => void control(() => store.resumeTask(conversationID, task.id))}
          >
            接着做
          </button>
        )}
        {canResume && (
          <button
            type="button"
            className="work-thread"
            disabled={pending}
            title="不做了：从群里收起来，任务卡片还留在记录里"
            onClick={() => void control(() => store.dismissTask(conversationID, task.id))}
          >
            不做了
          </button>
        )}
        {task.thread_id && (
          <button
            type="button"
            className="work-thread"
            title="看这个任务的过程"
            onClick={() => {
              onOpenConversation(task.thread_id!);
              onJump(null);
            }}
          >
            过程
          </button>
        )}
      </span>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

function shortTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  const hm = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  return sameDay ? hm : `${date.getMonth() + 1}月${date.getDate()}日 ${hm}`;
}
