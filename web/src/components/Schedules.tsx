// 「定时任务」panel, opened from the rail: prompts the backend runs by itself in a conversation
// at the due time. A row shows the rule and its next run in the rule's own timezone, what the
// last run came to, and the four controls; the form opens above it as its own sheet.

import { useEffect, useRef, useState } from "react";
import type { Conversation, Schedule, ScheduleKind } from "../api/types";
import { useSegmentedThumb } from "../lib/motion";
import {
  lastResultText,
  nextRunText,
  schedulePayload,
  formFromSchedule,
  type ScheduleForm,
} from "../lib/schedules";
import { supervisorName } from "../lib/team";
import { isScheduleTarget } from "../lib/threads";
import { store, useTeam } from "../state/store";
import { Switch } from "./Connectors";
import { Icon } from "./Icon";
import { Modal } from "./Dialog";
import { Dialog } from "./Panels";
import { ScheduleRunSheet } from "./ScheduleRun";
import { WebhookSection } from "./WebhookSection";

const KINDS: readonly ScheduleKind[] = ["once", "interval", "cron"];
const KIND_LABELS: Record<ScheduleKind, string> = { once: "一次", interval: "每隔", cron: "cron", reminder: "提醒" };

/** Enabled first, then the paused ones; inside each, whichever runs soonest comes first. */
function ordered(list: Schedule[]): Schedule[] {
  const rank = (schedule: Schedule) => (schedule.enabled ? 0 : 1);
  return [...list].sort((a, b) => {
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    const left = a.next_run_at ? Date.parse(a.next_run_at) : Number.POSITIVE_INFINITY;
    const right = b.next_run_at ? Date.parse(b.next_run_at) : Number.POSITIVE_INFINITY;
    if (left !== right) return left - right;
    return Date.parse(a.created_at) - Date.parse(b.created_at);
  });
}

export function SchedulesPanel({
  onClose,
  onOpenConversation,
}: {
  onClose: () => void;
  /** Jumps to the conversation a rule runs in; the panel steps aside for the chat. */
  onOpenConversation: (id: string) => void;
}) {
  const schedules = useTeam((s) => s.schedules);
  const schedulesError = useTeam((s) => s.schedulesError);
  const workspace = useTeam((s) => s.workspace);
  const [editor, setEditor] = useState<{ schedule: Schedule | null } | null>(null);
  const [viewingID, setViewingID] = useState<string | null>(null);
  const [confirmingID, setConfirmingID] = useState<string | null>(null);
  const [working, setWorking] = useState<{ id: string; kind: "toggle" | "run" | "stop" | "delete" } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void store.loadSchedules();
  }, []);

  const list = schedules?.schedules ?? [];
  // A bot's own reminder is one-shot and lives in the chat it was made in: own heading, and
  // only the controls that make sense there (run detail and cancel).
  const reminders = list.filter((schedule) => schedule.reminder);
  const tasks = list.filter((schedule) => !schedule.reminder);
  const known = new Set((workspace?.conversations ?? []).map((conversation) => conversation.id));

  const viewing = list.find((schedule) => schedule.id === viewingID) ?? null;
  const act = async (id: string, kind: "toggle" | "run" | "stop" | "delete", work: () => Promise<string | null>) => {
    setWorking({ id, kind });
    setError(null);
    const failure = await work();
    setWorking(null);
    setConfirmingID(null);
    if (failure) setError(failure);
  };

  const row = (schedule: Schedule) => {
    const busy = working?.id === schedule.id;
    const live = schedule.running;
    const next = schedule.enabled ? nextRunText(schedule.next_run_at, schedule.timezone) : "";
    const result = live ? "" : lastResultText(schedule.last_status, schedule.last_error);
    const canOpen = known.has(schedule.conversation_id);
    const classes = ["schedule-card", schedule.enabled ? "" : "paused", live ? "running" : ""];
    return (
      <li key={schedule.id} className={classes.filter(Boolean).join(" ")}>
        <div className="schedule-head">
          <span className="schedule-title">{schedule.title}</span>
          {!schedule.reminder && (
            <Switch
              checked={schedule.enabled}
              disabled={busy}
              label={schedule.enabled ? "暂停这个定时任务" : "启用这个定时任务"}
              onChange={() =>
                void act(schedule.id, "toggle", () => store.updateSchedule(schedule.id, { enabled: !schedule.enabled }))
              }
            />
          )}
        </div>

        <div className="schedule-facts">
          <span>{schedule.describe}</span>
          <span>在「{schedule.conversation_title}」</span>
          {schedule.queued ? (
            <span className="schedule-queued">排队中，上一次做完后马上开始</span>
          ) : schedule.enabled ? (
            next && <span>{next}</span>
          ) : (
            <span>已暂停</span>
          )}
        </div>

        {live && (
          <p className="schedule-live" role="status">
            <span className="schedule-live-dot" aria-hidden="true" />
            <span className="schedule-live-text">进行中：{live.step || "正在开始"}</span>
          </p>
        )}

        {result && (
          <button
            type="button"
            className={schedule.last_status === "error" ? "schedule-result error" : "schedule-result"}
            disabled={!canOpen}
            onClick={() => onOpenConversation(schedule.conversation_id)}
            title={canOpen ? "打开它发总结的会话" : "这个会话已经不在了"}
          >
            {result}
          </button>
        )}

        {confirmingID === schedule.id ? (
          <div className="confirm-step" role="group" aria-label={`确认删除${schedule.title}`}>
            <p className="confirm-prompt">删除「{schedule.title}」后，它就不会再自动运行了。</p>
            <div className="confirm-actions">
              <button
                type="button"
                className="danger-button"
                disabled={busy}
                onClick={() => void act(schedule.id, "delete", () => store.deleteSchedule(schedule.id))}
              >
                <Icon name="trash" size={15} />
                {busy ? "删除中…" : "删除"}
              </button>
              <button type="button" className="ghost-button" disabled={busy} onClick={() => setConfirmingID(null)}>
                取消
              </button>
            </div>
          </div>
        ) : (
          <div className="schedule-actions">
            {(live || schedule.last_run_id) && (
              <button
                type="button"
                className="schedule-action"
                onClick={() => setViewingID(schedule.id)}
              >
                <Icon name="history" size={14} />
                运行详情
              </button>
            )}
            {!schedule.reminder && (live ? (
              <button
                type="button"
                className="schedule-action"
                disabled={busy}
                onClick={() => void act(schedule.id, "stop", () => store.stopSchedule(schedule.id))}
              >
                <Icon name="stop" size={14} />
                {working?.id === schedule.id && working.kind === "stop" ? "正在停止…" : "停止"}
              </button>
            ) : (
              <button
                type="button"
                className="schedule-action"
                disabled={busy}
                onClick={() => void act(schedule.id, "run", () => store.runSchedule(schedule.id))}
              >
                <Icon name="run" size={14} />
                {working?.id === schedule.id && working.kind === "run" ? "正在开始…" : "立即运行"}
              </button>
            ))}
            {!schedule.reminder && (
              <button
                type="button"
                className="schedule-action"
                disabled={busy}
                onClick={() => {
                  setEditor({ schedule });
                  setError(null);
                }}
              >
                <Icon name="rename" size={14} />
                编辑
              </button>
            )}
            <button
              type="button"
              className="schedule-action danger"
              disabled={busy}
              onClick={() => setConfirmingID(schedule.id)}
            >
              <Icon name="trash" size={14} />
              {schedule.reminder ? "取消提醒" : "删除"}
            </button>
          </div>
        )}
      </li>
    );
  };

  return (
    <>
      {/* The editor and a run's detail are dialogs over the panel, even while it is a tab. */}
      {editor && (
        <Modal>
          <ScheduleEditor
            schedule={editor.schedule}
            conversations={workspace?.conversations ?? []}
            defaultTimezone={schedules?.timezone ?? ""}
            onClose={() => setEditor(null)}
          />
        </Modal>
      )}
      {viewing && (
        <Modal>
          <ScheduleRunSheet
            schedule={viewing}
            onClose={() => setViewingID(null)}
            onOpenConversation={onOpenConversation}
          />
        </Modal>
      )}
      <Dialog title="定时任务" onClose={onClose} wide>
        <p className="hint">到点后团队在后台、你看不到的地方去做，会话里不显示过程和中间步骤；做完由建这个任务的机器人发一小段标着任务名的总结。</p>

        {schedulesError && !schedules && (
          <p className="form-error" role="alert">
            {schedulesError}
          </p>
        )}

        {!schedules && !schedulesError && <p className="hint">正在读取定时任务…</p>}

        {schedules && list.length === 0 && (
          <p className="hint">
            还没有定时任务。你可以在对话里直接让{supervisorName(workspace)}按时提醒或做事，也可以在这里新建一个。
          </p>
        )}

        {reminders.length > 0 && (
          <section className="schedule-section" aria-label="提醒">
            <p className="schedule-section-label">提醒</p>
            <ul className="provider-list schedule-list">{ordered(reminders).map(row)}</ul>
          </section>
        )}

        {tasks.length > 0 && (
          <section className="schedule-section" aria-label="定时任务">
            {reminders.length > 0 && <p className="schedule-section-label">定时任务</p>}
            <ul className="provider-list schedule-list">{ordered(tasks).map(row)}</ul>
          </section>
        )}

        <button
          type="button"
          className="connector-add"
          onClick={() => {
            setEditor({ schedule: null });
            setError(null);
          }}
        >
          <Icon name="plus" size={15} />
          新建定时任务
        </button>

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </Dialog>
    </>
  );
}

/** The create / edit sheet over the panel; it closes itself once the server agreed. */
function ScheduleEditor({
  schedule,
  conversations,
  defaultTimezone,
  onClose,
}: {
  schedule: Schedule | null;
  conversations: Conversation[];
  defaultTimezone: string;
  onClose: () => void;
}) {
  const options = conversations.filter(isScheduleTarget);
  const supervisorID = useTeam((s) => s.workspace?.direct_conversation_id ?? null);
  const [form, setForm] = useState<ScheduleForm>(() =>
    schedule
      ? formFromSchedule(schedule)
      : {
          title: "",
          prompt: "",
          conversation_id: supervisorID ?? options[0]?.id ?? "",
          kind: "once",
          at: "",
          every_minutes: "30",
          cron: "0 9 * * *",
          timezone: defaultTimezone || Intl.DateTimeFormat().resolvedOptions().timeZone,
          use_computer: false,
        },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const kinds = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(kinds, form.kind);
  const patch = (values: Partial<ScheduleForm>) => setForm((current) => ({ ...current, ...values }));

  const save = async () => {
    const payload = schedulePayload(form);
    if (!payload.ok) {
      setError(payload.error);
      return;
    }
    setError(null);
    setBusy(true);
    const failure = schedule
      ? await store.updateSchedule(schedule.id, payload.body)
      : await store.createSchedule(payload.body);
    setBusy(false);
    if (failure) setError(failure);
    else onClose();
  };

  const groups = options.filter((conversation) => conversation.kind === "group");
  const directs = options
    .filter((conversation) => conversation.kind !== "group")
    .sort((a, b) => {
      if (a.id === supervisorID) return -1;
      if (b.id === supervisorID) return 1;
      return 0;
    });
  const missing = !options.some((conversation) => conversation.id === form.conversation_id);

  return (
    <Dialog
      title={schedule ? `编辑「${schedule.title}」` : "新建定时任务"}
      onClose={onClose}
      className="schedule-form-dialog"
      footer={
        <>
          <button type="button" className="ghost-button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={busy} onClick={() => void save()}>
            {busy ? "正在保存…" : schedule ? "保存" : "新建"}
          </button>
        </>
      }
    >
      <div className="provider-form schedule-form">
        <div className="connector-field">
          <label className="field-label" htmlFor="schedule-title">
            标题
          </label>
          <input
            id="schedule-title"
            className="field"
            value={form.title}
            placeholder="早上看一遍行业新闻"
            onChange={(event) => patch({ title: event.target.value })}
          />
        </div>

        <div className="connector-field">
          <label className="field-label" htmlFor="schedule-prompt">
            要做的事
          </label>
          <textarea
            id="schedule-prompt"
            className="field"
            rows={3}
            value={form.prompt}
            placeholder="把今天的行业新闻整理成五条要点，发在这里。"
            onChange={(event) => patch({ prompt: event.target.value })}
          />
          <p className="hint">这段话会以你的名义发进会话，团队照常回复。</p>
        </div>

        <div className="connector-field">
          <label className="field-label" htmlFor="schedule-conversation">
            在哪个会话运行
          </label>
          <select
            id="schedule-conversation"
            className="field select"
            value={form.conversation_id}
            onChange={(event) => patch({ conversation_id: event.target.value })}
          >
            {missing && (
              <option value={form.conversation_id} disabled>
                {schedule?.conversation_title ?? "还没有可以运行的会话"}
              </option>
            )}
            {directs.length > 0 && (
              <optgroup label="单独对话">
                {directs.map((conversation) => (
                  <option key={conversation.id} value={conversation.id}>
                    {conversation.title}
                  </option>
                ))}
              </optgroup>
            )}
            {groups.length > 0 && (
              <optgroup label="群组">
                {groups.map((conversation) => (
                  <option key={conversation.id} value={conversation.id}>
                    {conversation.title}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </div>

        <div className="connector-field">
          <span className="field-label">重复</span>
          <div
            className={thumb ? "segmented has-thumb" : "segmented"}
            role="radiogroup"
            aria-label="重复方式"
            ref={kinds}
          >
            {thumb && (
              <span
                className={ready ? "segmented-thumb" : "segmented-thumb instant"}
                aria-hidden="true"
                style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
              />
            )}
            {KINDS.map((kind) => (
              <button
                key={kind}
                type="button"
                role="radio"
                data-seg={kind}
                aria-checked={form.kind === kind}
                className={form.kind === kind ? "on" : ""}
                onClick={() => patch({ kind })}
              >
                {KIND_LABELS[kind]}
              </button>
            ))}
          </div>
        </div>

        {form.kind === "once" && (
          <div className="connector-field">
            <label className="field-label" htmlFor="schedule-at">
              运行时间
            </label>
            <input
              id="schedule-at"
              className="field"
              type="datetime-local"
              value={form.at}
              onChange={(event) => patch({ at: event.target.value })}
            />
            <p className="hint">按下面填的时区计算；只运行一次，跑完就自动停用。</p>
          </div>
        )}

        {form.kind === "interval" && (
          <div className="connector-field">
            <label className="field-label" htmlFor="schedule-every">
              间隔分钟
            </label>
            <input
              id="schedule-every"
              className="field"
              type="number"
              min={5}
              step={5}
              inputMode="numeric"
              value={form.every_minutes}
              onChange={(event) => patch({ every_minutes: event.target.value })}
            />
            <p className="hint">最少 5 分钟；上一轮还没跑完时不会叠加，等它结束再算下一次。</p>
          </div>
        )}

        {form.kind === "cron" && (
          <div className="connector-field">
            <label className="field-label" htmlFor="schedule-cron">
              cron 表达式
            </label>
            <input
              id="schedule-cron"
              className="field"
              value={form.cron}
              spellCheck={false}
              placeholder="0 9 * * 1-5"
              onChange={(event) => patch({ cron: event.target.value })}
            />
            <p className="hint">5 段：分 时 日 月 周。例如 0 9 * * 1-5 是工作日九点。</p>
          </div>
        )}

        <div className="connector-field">
          <label className="field-label" htmlFor="schedule-timezone">
            时区
          </label>
          <input
            id="schedule-timezone"
            className="field"
            value={form.timezone}
            spellCheck={false}
            placeholder="Asia/Shanghai"
            onChange={(event) => patch({ timezone: event.target.value })}
          />
        </div>

        <div className="connector-option">
          <span className="connector-option-text">
            <span className="connector-option-label">允许使用沙盒电脑</span>
            <span className="connector-option-note">打开后到点直接操作那台虚拟电脑；不打开的话，第一次要用时会在目标会话里问你。</span>
          </span>
          <Switch
            checked={form.use_computer}
            disabled={busy}
            label={form.use_computer ? "不允许使用沙盒电脑" : "允许使用沙盒电脑"}
            onChange={() => patch({ use_computer: !form.use_computer })}
          />
        </div>

        {/* A new schedule has no id yet, so its hook can only exist after it is created. */}
        {schedule && <WebhookSection targetKind="schedule" targetID={schedule.id} />}

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}
