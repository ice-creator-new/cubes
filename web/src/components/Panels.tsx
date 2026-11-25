// Right-side panels and dialogs: team directory, member editor, new member, group settings,
// and backend/appearance settings. All edit through the store and show server errors as-is.

import { useEffect, useRef, useState } from "react";
import type {
  Agent,
  Appearance,
  PermissionMode,
  SendMode,
  TaskItem,
  TaskStatus,
  TokenBudget,
  ToolPermissions,
} from "../api/types";
import { asHexColor, resolveAppearance } from "../bot/catalog";
import { THEME_ACCENTS, type Accent } from "../lib/accent";
import {
  EFFORT_LABELS,
  FALLBACK_HINT,
  FOLLOW_DEFAULT,
  agentModelValue,
  defaultEffortLabel,
  followDefaultLabel,
  modelEfforts,
  modelGroups,
  modelOptionText,
  modelValue,
  parseModelValue,
} from "../lib/modelOptions";
import { listTimestamp } from "../lib/time";
import { SEND_MODES } from "../lib/sendMode";
import { MODE_LABELS, PERMISSION_MODES, followModeLabel } from "../lib/permissions";
import { PERIOD_LABELS, activityOf, budgetState, budgetTone, formatTokens, relativeTime, supervisorName } from "../lib/team";
import { desktop, isDesktop, type AppIconMode } from "../lib/platform";
import { DEFAULT_GLASS, glassAvailable, glassSupport, MAX_BLUR, type Glass, type GlassSupport } from "../lib/glass";
import type { RunRef } from "../lib/tabs";
import { prefersReducedMotion, useFlip, usePresence, useSegmentedThumb } from "../lib/motion";
import { agentByID, store, teamMoodFor, useTeam } from "../state/store";
import { Avatar, GroupAvatar } from "./Avatar";
import { AppearanceEditor, ColorField } from "./AppearanceEditor";
import { Collapse } from "./Collapse";
import { CategorySheet, type Category } from "./CategorySheet";
import { ContextMenu } from "./ContextMenu";
import { Dialog } from "./Dialog";
import { Icon, type IconName } from "./Icon";
import { MessageBody } from "./MessageBody";
import { ProviderSettings } from "./ProviderSettings";
import { BudgetEditor } from "./Usage";
import { TeamSettingsSection } from "./TeamSettings";
import { PermissionSettingsSection } from "./PermissionSettings";
import { TeamPortActions } from "./TeamPort";
import { MemberProfile } from "./MemberProfile";
import { Switch } from "./Connectors";

export { Collapse } from "./Collapse";
export { Dialog } from "./Dialog";

/** Groups run todo → doing → blocked → done, so finished work sits last. */
const TASK_GROUPS: readonly (readonly [TaskStatus, string, IconName])[] = [
  ["todo", "待办", "todo"],
  ["doing", "进行中", "doing"],
  ["blocked", "受阻", "blocked"],
  ["done", "已完成", "done"],
];

const STATUS_LABELS = Object.fromEntries(
  TASK_GROUPS.map(([status, label]) => [status, label]),
) as Record<TaskStatus, string>;

const STATUS_ICONS = Object.fromEntries(
  TASK_GROUPS.map(([status, , icon]) => [status, icon]),
) as Record<TaskStatus, IconName>;

/** The "everything allowed" tool permissions; null on the agent means this exact value. */
const DEFAULT_PERMISSIONS: ToolPermissions = { code: true, files: true, computer: true, connectors: "all", skills_disabled: [] };

export function TeamPanel({
  onClose,
  onOpenMember,
  onCreate,
  onMessage,
  onOpenConversation,
  onOpenUsage,
}: {
  onClose: () => void;
  onOpenMember: (id: string) => void;
  onCreate: () => void;
  onMessage: (agent: Agent) => void;
  onOpenConversation: (id: string) => void;
  onOpenUsage: () => void;
}) {
  const state = useTeam((s) => s);
  const workspace = state.workspace;
  const supervisor = agentByID(workspace, workspace?.supervisor_id);
  const members = (workspace?.agents ?? []).filter((a) => a.kind === "member");
  const usage = state.usage;
  const period = usage?.period ?? "week";
  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, period);

  useEffect(() => {
    if (!usage) void store.loadUsage("week");
  }, [usage]);

  const workingCount = workspace
    ? workspace.agents.filter((agent) => activityOf(agent.id, workspace).kind === "working").length
    : 0;

  const row = (agent: Agent) => {
    const activity = workspace ? activityOf(agent.id, workspace) : { kind: "idle" as const };
    const usageRow = usage?.report?.agents.find((item) => item.agent_id === agent.id);
    const budget = agent.token_budget;
    const used = usageRow?.budget_used ?? 0;
    const bar = budgetState(used, budget);
    const tone = budgetTone(used, budget);
    const lastReply = usageRow?.last_reply_at ?? null;
    const budgetTitle = budget
      ? `${PERIOD_LABELS[budget.period]} ${formatTokens(used)} / ${formatTokens(budget.limit)}`
      : undefined;
    return (
      <div key={agent.id} className="team-row">
        <div
          className="team-row-main"
          role="button"
          tabIndex={0}
          onClick={() => onOpenMember(agent.id)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              onOpenMember(agent.id);
            }
          }}
        >
          <Avatar
            seed={agent.avatar_seed}
            appearance={agent.appearance}
            size={40}
            name={agent.name}
            agentID={agent.id}
            mood={teamMoodFor(state, agent.id)}
          />
          <span className="team-row-text">
            <span className="team-row-name">{agent.name}</span>
            {activity.kind === "working" ? (
              <button
                type="button"
                className="team-row-status link"
                onClick={(event) => {
                  event.stopPropagation();
                  onOpenConversation(activity.conversationID);
                }}
              >
                {activity.waitingTurn
                  ? `在「${activity.conversationTitle}」排队`
                  : `在「${activity.conversationTitle}」：${activity.step}`}
              </button>
            ) : (
              <span className="team-row-status">空闲{lastReply ? ` · 上次回复 ${relativeTime(lastReply)}` : ""}</span>
            )}
          </span>
        </div>
        <span className="team-row-usage" title={budgetTitle}>
          <span className="team-row-total">
            <span className="team-row-total-word">{PERIOD_LABELS[period]}</span>
            <span className="team-row-total-num">{formatTokens(usageRow?.total_tokens ?? 0)}</span>
          </span>
          {budget && (
            <span className="team-row-budget">
              <span className="team-row-budget-text">
                {formatTokens(used)} / {formatTokens(budget.limit)}
              </span>
              <span className="budget-bar wide" aria-hidden="true">
                <span
                  className={tone === "over" ? "budget-fill over" : tone === "warn" ? "budget-fill warn" : "budget-fill"}
                  style={{ width: `${Math.min(100, Math.round(bar.ratio * 100))}%` }}
                />
              </span>
            </span>
          )}
        </span>
        <button type="button" className="ghost-button" onClick={() => onMessage(agent)}>
          发消息
        </button>
      </div>
    );
  };

  return (
    <Dialog title="团队" onClose={onClose} wide>
      <div className="team-header">
        <span className="team-summary">{workingCount > 0 ? `现在 ${workingCount} 位在工作` : "大家都在休息"}</span>
        <div className="team-header-right">
          <TeamPortActions onImported={() => void store.loadWorkspace()} />
          <button type="button" className="link-button" onClick={onOpenUsage}>
            查看用量
          </button>
          <div
            className={thumb ? "segmented team-periods has-thumb" : "segmented team-periods"}
            role="radiogroup"
            aria-label="统计周期"
            ref={segmented}
          >
            {thumb && (
              <span
                className={ready ? "segmented-thumb" : "segmented-thumb instant"}
                aria-hidden="true"
                style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
              />
            )}
            {(["day", "week", "month"] as const).map((p) => (
              <button
                key={p}
                type="button"
                role="radio"
                data-seg={p}
                aria-checked={period === p}
                className={period === p ? "on" : ""}
                onClick={() => void store.loadUsage(p)}
              >
                {PERIOD_LABELS[p]}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="team-section-label">负责人</div>
      {supervisor && row(supervisor)}
      <div className="team-section-head">
        <span className="team-section-label">成员</span>
        <button type="button" className="ghost-button" onClick={onCreate}>
          <Icon name="plus" size={15} />
          添加成员
        </button>
      </div>
      {members.length ? (
        members.map(row)
      ) : (
        <p className="muted">{supervisor?.name ?? "负责人"}会按需创建成员，你也可以先加一位。</p>
      )}
    </Dialog>
  );
}

type MemberCategory = "basics" | "model" | "permissions" | "behavior" | "look" | "profile" | "danger";

/** The categories down the left of a member's editor: what the form acts on, in reading order. */
const MEMBER_CATEGORIES: readonly Omit<Category<MemberCategory>, "unsaved">[] = [
  { id: "basics", label: "基本", icon: "user", description: "名字、职责说明，以及它已经参与的群组。" },
  { id: "model", label: "模型", icon: "model", description: "回复用哪个模型，以及它这段时间能花多少额度。" },
  {
    id: "permissions",
    label: "权限",
    icon: "shield",
    description: "它动手做的哪些事要先问你，以及能用哪些能力。",
  },
  { id: "behavior", label: "行为", icon: "chat", description: "它正忙着回复时，你发的消息怎么处理。" },
  { id: "look", label: "形象", icon: "palette", description: "它在侧栏、消息和团队列表里长什么样。" },
  { id: "profile", label: "档案", icon: "usage", description: "只读：这段时间的回复、任务和常用工具。" },
  // The supervisor is never deleted, so it has no category to do it in.
  { id: "danger", label: "危险操作", icon: "trash", description: "删除这个成员；和它的私聊会一起删掉。" },
];

type MemberEditorProps = {
  agentID: string;
  /** The sidebar's context menu opens straight on the confirmation step. */
  startConfirming?: boolean;
  onClose: () => void;
  onMessage: (agent: Agent) => void;
  /** Called once the server has removed the member and the directory is refreshed. */
  onDeleted: () => void;
  /** Opens the run-trace sheet for one of this member's recent runs. */
  onOpenRun: (run: RunRef) => void;
};

/** A tab restored at launch mounts before the workspace arrives: the form waits for the member,
 * so its fields are seeded from the real record instead of staying empty. */
export function MemberEditor(props: MemberEditorProps) {
  const known = useTeam((s) => Boolean(agentByID(s.workspace, props.agentID)));
  return known ? <MemberEditorForm {...props} /> : null;
}

function MemberEditorForm({
  agentID,
  startConfirming = false,
  onClose,
  onMessage,
  onDeleted,
  onOpenRun,
}: MemberEditorProps) {
  const workspace = useTeam((s) => s.workspace);
  const providers = useTeam((s) => s.providers);
  const usage = useTeam((s) => s.usage);
  const skills = useTeam((s) => s.skills);
  const teamMode = useTeam((s) => s.teamSettings?.send_mode) ?? "steer";
  const teamPermissionMode = useTeam((s) => s.permissionSettings?.mode) ?? "ask";
  const agent = agentByID(workspace, agentID);
  const [name, setName] = useState(agent?.name ?? "");
  const [instructions, setInstructions] = useState(agent?.instructions ?? "");
  const [modelPick, setModelPick] = useState(() => (agent ? agentModelValue(agent) : FOLLOW_DEFAULT));
  const [effort, setEffort] = useState(() => agent?.model_ref?.effort ?? "");
  const [budget, setBudget] = useState<TokenBudget | null>(() => agent?.token_budget ?? null);
  const [permissions, setPermissions] = useState<ToolPermissions>(() => {
    const stored = agent ? (agent.tool_permissions ?? DEFAULT_PERMISSIONS) : DEFAULT_PERMISSIONS;
    return { ...stored, skills_disabled: stored.skills_disabled ?? [] };
  });
  const [look, setLook] = useState<Appearance>(() => resolveAppearance(agent?.appearance, agent?.avatar_seed));
  // Null follows the team setting, the same way its model pick follows the default.
  const [sendMode, setSendMode] = useState<SendMode | null>(() => agent?.send_mode ?? null);
  const [permissionMode, setPermissionMode] = useState<PermissionMode | null>(
    () => agent?.permission_mode ?? null,
  );
  const [resetLook, setResetLook] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(startConfirming);
  const [deleting, setDeleting] = useState(false);
  // The step stays rendered through its collapse, so the dialog below never jumps.
  const stepShown = usePresence(confirming ? true : null, 200);
  const stepRef = useRef<HTMLDivElement>(null);
  // The send-mode pick has a thumb; 「跟随团队设置」 is a fourth slot with a key of its own.
  const modeGroup = useRef<HTMLDivElement>(null);
  const { thumb: modeThumb, ready: modeReady } = useSegmentedThumb(modeGroup, sendMode ?? "follow");
  // Opened from the rail's menu, the editor lands on the delete step: bring it in whole, above
  // the sticky action row — once it has finished unfolding, so the scroll lands on the whole
  // step instead of an empty box.
  useEffect(() => {
    if (!confirming) return;
    const timer = window.setTimeout(() => {
      stepRef.current?.scrollIntoView({
        block: "center",
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    }, 200);
    return () => window.clearTimeout(timer);
  }, [confirming]);
  // The budget hint shows the member's current spend when the report is already known.
  useEffect(() => {
    if (usage) return;
    void store.loadUsage("week");
  }, [usage]);
  if (!agent) return null;
  const fallbackLook = resolveAppearance(null, agent.avatar_seed);
  const storedLook = agent.appearance ?? fallbackLook;
  const lookChanged =
    look.shape !== storedLook.shape || look.color !== storedLook.color || (look.eye ?? null) !== (storedLook.eye ?? null);
  const appearancePatch: Appearance | null | undefined = resetLook
    ? agent.appearance
      ? null
      : undefined
    : lookChanged
      ? { shape: look.shape, color: look.color, ...(look.eye ? { eye: look.eye } : {}) }
      : undefined;
  const pickGroups = modelGroups(providers);
  // A pick the list no longer offers still shows itself, so the user sees what they chose.
  const listed = pickGroups.some((group) =>
    group.models.some((model) => modelValue({ provider_id: group.provider.id, model }) === modelPick),
  );
  const stalePick = modelPick !== FOLLOW_DEFAULT && !listed ? parseModelValue(modelPick) : null;
  const pickedRef = parseModelValue(modelPick);
  // Reasoning effort is only offered for a CLI model: an HTTP service has no such setting.
  const efforts = modelEfforts(providers, pickedRef);
  const effortOptions =
    effort && !efforts.includes(effort) ? [...efforts, effort] : efforts;
  const modelChanged = modelPick !== agentModelValue(agent) || effort !== (agent.model_ref?.effort ?? "");
  const budgetChanged =
    (budget === null) !== (agent.token_budget === null) ||
    (budget !== null &&
      agent.token_budget !== null &&
      (budget.limit !== agent.token_budget.limit || budget.period !== agent.token_budget.period));
  const storedPermissions = agent.tool_permissions ?? DEFAULT_PERMISSIONS;
  const storedDisabled = (storedPermissions.skills_disabled ?? []).slice().sort();
  const currentDisabled = (permissions.skills_disabled ?? []).slice().sort();
  const skillsDisabledChanged =
    currentDisabled.length !== storedDisabled.length ||
    currentDisabled.some((name, index) => name !== storedDisabled[index]);
  const permissionsChanged =
    permissions.code !== storedPermissions.code ||
    permissions.files !== storedPermissions.files ||
    permissions.computer !== storedPermissions.computer ||
    permissions.connectors !== storedPermissions.connectors ||
    skillsDisabledChanged;
  // All four packs back at the defaults (and no skills disabled) means "everything allowed".
  const isDefaultPermissions =
    permissions.code &&
    permissions.files &&
    permissions.computer &&
    permissions.connectors === "all" &&
    (permissions.skills_disabled ?? []).length === 0;
  const toolPermissionsPatch: ToolPermissions | null | undefined = permissionsChanged
    ? isDefaultPermissions
      ? null
      : permissions
    : undefined;
  const nameChanged = name.trim() !== agent.name;
  const instructionsChanged = instructions.trim() !== agent.instructions;
  const sendModeChanged = sendMode !== (agent.send_mode ?? null);
  const permissionModeChanged = permissionMode !== (agent.permission_mode ?? null);
  const changed =
    nameChanged ||
    instructionsChanged ||
    appearancePatch !== undefined ||
    modelChanged ||
    budgetChanged ||
    permissionsChanged ||
    sendModeChanged ||
    permissionModeChanged;
  // The categories carry a dot of their own, so a hidden edit is still findable from the list.
  const changedIn: Partial<Record<MemberCategory, boolean>> = {
    basics: nameChanged || instructionsChanged,
    model: modelChanged || budgetChanged,
    permissions: permissionsChanged || permissionModeChanged,
    behavior: sendModeChanged,
    look: appearancePatch !== undefined,
  };
  const categories = MEMBER_CATEGORIES.filter(
    (choice) => choice.id !== "danger" || agent.kind !== "supervisor",
  ).map((choice) => ({ ...choice, unsaved: changedIn[choice.id] }));
  /** The delete step is its own category, and the rail's menu opens the editor right on it. */
  const firstCategory: MemberCategory | undefined = startConfirming ? "danger" : undefined;
  const groups = (workspace?.conversations ?? []).filter((c) => c.kind === "group" && c.member_ids.includes(agent.id));
  const pickedPermission = PERMISSION_MODES.find((option) => option.id === permissionMode) ?? null;
  const modeOptions: { id: SendMode | null; label: string }[] = [
    { id: null, label: "跟随团队设置" },
    ...SEND_MODES,
  ];

  return (
    <Dialog
      title={agent.kind === "supervisor" ? "负责人" : "成员"}
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="ghost-button" onClick={() => onMessage(agent)}>
            发消息
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={!changed || saving || confirming || !name.trim() || !instructions.trim()}
            onClick={async () => {
              setSaving(true);
              const failure = await store.patchAgent(agent.id, {
                name,
                instructions,
                appearance: appearancePatch,
                ...(modelChanged
                  ? {
                      modelRef: pickedRef && effort ? { ...pickedRef, effort } : pickedRef,
                    }
                  : {}),
                ...(budgetChanged ? { tokenBudget: budget } : {}),
                ...(permissionsChanged ? { toolPermissions: toolPermissionsPatch } : {}),
                ...(sendMode !== (agent.send_mode ?? null) ? { sendMode } : {}),
                ...(permissionModeChanged ? { permissionMode } : {}),
              });
              setSaving(false);
              if (failure) setError(failure);
              else onClose();
            }}
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <CategorySheet
        navLabel="成员编辑类别"
        idPrefix="member"
        categories={categories}
        storageKey={`cubes.member.category.${agent.id}`}
        initial={firstCategory}
      >
        {(active) => (
          <>
            {active.id === "basics" && (
              <>
                <section className="settings-section">
                  <div className="settings-heading">
                    <h4>名字和头像</h4>
                    <p>名字出现在消息署名和团队列表里，头像按名字和颜色生成。</p>
                  </div>
                  <div className="member-head">
                    <Avatar seed={agent.avatar_seed} appearance={look} size={56} name={agent.name} agentID={agent.id} />
                    <div className="member-head-text">
                      <input
                        className="field title-field"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        aria-label="名称"
                      />
                    </div>
                  </div>
                </section>

                <section className="settings-section">
                  <div className="settings-heading">
                    <h4>职责说明</h4>
                    <p>{supervisorName(workspace)}分派任务时会把这段说明一起交给它。改动只影响之后的回复。</p>
                  </div>
                  <textarea
                    id="instructions"
                    className="field instructions"
                    rows={4}
                    aria-label="职责说明"
                    value={instructions}
                    onChange={(e) => setInstructions(e.target.value)}
                  />
                </section>

                {groups.length > 0 && (
                  <section className="settings-section">
                    <div className="settings-heading">
                      <h4>参与的群组</h4>
                      <p>这些群组里能把它叫出来；去群组设置里调整成员。</p>
                    </div>
                    <p className="muted">{groups.map((g) => g.title).join("、")}</p>
                  </section>
                )}
              </>
            )}

            {active.id === "model" && (
              <>
                <section className="settings-section">
                  <div className="settings-heading">
                    <h4>选择模型</h4>
                    <p>不选就跟随团队默认；用本机命令行引擎时还能再调思考强度。</p>
                  </div>
                  <label className="field-label" htmlFor="member-model">
                    模型
                  </label>
                  <select
                    id="member-model"
                    className="field select"
                    value={modelPick}
                    onChange={(event) => {
                      setModelPick(event.target.value);
                      // Another model may not take the effort this one did: start from its own default.
                      setEffort("");
                    }}
                  >
                    <option value={FOLLOW_DEFAULT}>{followDefaultLabel(providers)}</option>
                    {stalePick && (
                      <option value={modelPick}>{`${stalePick.model}（已不可用）`}</option>
                    )}
                    {pickGroups.map((group) => (
                      <optgroup key={group.provider.id} label={group.provider.name}>
                        {group.models.map((model) => (
                          <option key={model} value={modelValue({ provider_id: group.provider.id, model })}>
                            {modelOptionText(group.provider, model)}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                  {efforts.length > 0 && (
                    <>
                      <label className="field-label" htmlFor="member-effort">
                        思考强度
                      </label>
                      <select
                        id="member-effort"
                        className="field select"
                        value={effort}
                        onChange={(event) => setEffort(event.target.value)}
                      >
                        <option value="">{defaultEffortLabel(providers, pickedRef)}</option>
                        {effortOptions.map((level) => (
                          <option key={level} value={level}>
                            {`${level}（${EFFORT_LABELS[level] ?? level}）`}
                          </option>
                        ))}
                      </select>
                      <p className="hint">只在用本机命令行引擎时生效：越高想得越久，额度也用得越多。</p>
                    </>
                  )}
                  {agent.model_status === "fallback" && <p className="hint model-fallback">{FALLBACK_HINT}</p>}
                </section>

                <section className="settings-section">
                  <div className="settings-heading">
                    <h4>用量预算</h4>
                    <p>超过上限就先停下来问你，不会悄悄把额度花光。</p>
                  </div>
                  <BudgetEditor value={budget} onChange={setBudget} agentID={agent.id} />
                </section>
              </>
            )}

            {active.id === "permissions" && (
              <>
                <section className="settings-section">
                  <div className="settings-heading">
                    <h4>审批方式</h4>
                    <p>写文件、运行代码、使用电脑、连接器写入和定时任务，按这里的方式决定要不要先问你。</p>
                  </div>
                  <select
                    className="field select"
                    aria-label="审批方式"
                    value={permissionMode ?? "follow"}
                    onChange={(event) => {
                      const value = event.target.value;
                      setPermissionMode(value === "follow" ? null : (value as PermissionMode));
                      setError(null);
                    }}
                  >
                    <option value="follow">{followModeLabel(teamPermissionMode)}</option>
                    {PERMISSION_MODES.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <p className="hint">
                    {permissionMode === null
                      ? `跟随团队：现在是「${MODE_LABELS[teamPermissionMode]}」。`
                      : `${pickedPermission?.ahead ?? ""}${pickedPermission?.stillAsks ?? ""}`}
                  </p>
                </section>

                <section className="settings-section">
                  <div className="settings-heading">
                    <h4>能用的能力</h4>
                    <p>关掉的能力它就用不了；全局没打开的，这里打开也用不了。</p>
                  </div>
                  <div className="member-permissions">
                    <div className="connector-option">
                      <span className="connector-option-text">
                        <span className="connector-option-label">运行代码</span>
                        <span className="connector-option-note">python 和 shell 工具。</span>
                      </span>
                      <Switch
                        checked={permissions.code}
                        label={permissions.code ? "关掉运行代码" : "打开运行代码"}
                        onChange={() => setPermissions({ ...permissions, code: !permissions.code })}
                      />
                    </div>
                    <div className="connector-option">
                      <span className="connector-option-text">
                        <span className="connector-option-label">读写文件</span>
                        <span className="connector-option-note">工作区里的文件浏览、读取和写入。</span>
                      </span>
                      <Switch
                        checked={permissions.files}
                        label={permissions.files ? "关掉读写文件" : "打开读写文件"}
                        onChange={() => setPermissions({ ...permissions, files: !permissions.files })}
                      />
                    </div>
                    <div className="connector-option">
                      <span className="connector-option-text">
                        <span className="connector-option-label">电脑</span>
                        <span className="connector-option-note">这台 Mac 的屏幕、终端，或连上来的云端电脑。</span>
                      </span>
                      <Switch
                        checked={permissions.computer}
                        label={permissions.computer ? "关掉沙盒电脑" : "打开沙盒电脑"}
                        onChange={() => setPermissions({ ...permissions, computer: !permissions.computer })}
                      />
                    </div>
                    <label className="connector-option">
                      <span className="connector-option-text">
                        <span className="connector-option-label">连接器</span>
                        <span className="connector-option-note">外部服务（GitHub / 飞书 / 本地 MCP）。</span>
                      </span>
                      <select
                        className="field select"
                        value={permissions.connectors}
                        aria-label="连接器权限"
                        onChange={(event) => setPermissions({ ...permissions, connectors: event.target.value as ToolPermissions["connectors"] })}
                      >
                        <option value="all">全部</option>
                        <option value="read">只读</option>
                        <option value="none">不能用</option>
                      </select>
                    </label>
                    {skills && skills.length > 0 && (
                      <div className="member-skill-block">
                        <span className="connector-option-label">技能</span>
                        <span className="connector-option-note">关掉的技能，这个成员就用不了。</span>
                        <div className="member-skill-list">
                          {skills.map((skill) => {
                            const disabled = (permissions.skills_disabled ?? []).includes(skill.name);
                            return (
                              <div key={skill.id} className="member-skill-row">
                                <span className="member-skill-name" title={skill.name}>
                                  {skill.name}
                                </span>
                                <Switch
                                  checked={!disabled}
                                  label={disabled ? `打开「${skill.name}」` : `关掉「${skill.name}」`}
                                  onChange={() => {
                                    const list = permissions.skills_disabled ?? [];
                                    setPermissions({
                                      ...permissions,
                                      skills_disabled: disabled ? list.filter((name) => name !== skill.name) : [...list, skill.name],
                                    });
                                  }}
                                />
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                </section>
              </>
            )}

            {active.id === "behavior" && (
              <section className="settings-section">
                <div className="settings-heading">
                  <h4>忙时消息</h4>
                  <p>它正在回复时你发的消息怎么处理；换一种只影响这个成员。</p>
                </div>
                <div
                  className={modeThumb ? "segmented has-thumb" : "segmented"}
                  role="radiogroup"
                  aria-label="这个机器人忙时你发的消息"
                  ref={modeGroup}
                >
                  {modeThumb && (
                    <span
                      className={modeReady ? "segmented-thumb" : "segmented-thumb instant"}
                      aria-hidden="true"
                      style={{ width: modeThumb.width, transform: `translateX(${modeThumb.x}px)` }}
                    />
                  )}
                  {modeOptions.map((option) => (
                    <button
                      key={option.id ?? "follow"}
                      type="button"
                      role="radio"
                      data-seg={option.id ?? "follow"}
                      aria-checked={(sendMode ?? null) === option.id}
                      className={(sendMode ?? null) === option.id ? "on" : ""}
                      onClick={() => {
                        setSendMode(option.id);
                        setError(null);
                      }}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <p className="hint">
                  {sendMode === null
                    ? `跟随团队设置：现在是「${SEND_MODES.find((option) => option.id === teamMode)?.label ?? "插进这一轮"}」。`
                    : SEND_MODES.find((option) => option.id === sendMode)?.hint}
                </p>
              </section>
            )}

            {active.id === "look" && (
              <section className="settings-section">
                <div className="settings-heading with-switch">
                  <div className="settings-heading-text">
                    <h4>外观</h4>
                    <p>形状、颜色和眼睛；恢复默认会回到按名字和颜色生成的样子。</p>
                  </div>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => {
                      setLook(fallbackLook);
                      setResetLook(true);
                    }}
                  >
                    恢复默认
                  </button>
                </div>
                <AppearanceEditor
                  seed={agent.avatar_seed}
                  name={agent.name}
                  agentID={agent.id}
                  head={false}
                  value={look}
                  onChange={(next) => {
                    setLook(next);
                    setResetLook(false);
                  }}
                />
              </section>
            )}

            {active.id === "danger" && (
              <section className="settings-section">
                <div className="settings-heading">
                  <h4>删除这个成员</h4>
                  <p>删除后无法恢复：和它的私聊、它参与的机器人私聊会一起删掉，群组里的历史发言会保留。</p>
                </div>
                <Collapse open={!confirming}>
                  <button type="button" className="ghost-button danger-ghost" onClick={() => setConfirming(true)}>
                    <Icon name="trash" size={15} />
                    删除成员
                  </button>
                </Collapse>
                <Collapse open={confirming}>
                  {stepShown.value && (
                    <div className="confirm-step" role="group" aria-label="确认删除成员" ref={stepRef}>
                      <p className="confirm-prompt">再确认一次：删除「{agent.name}」？</p>
                      <div className="confirm-actions">
                        <button
                          type="button"
                          className="danger-button"
                          disabled={deleting}
                          onClick={async () => {
                            setDeleting(true);
                            setError(null);
                            const failure = await store.deleteAgent(agent.id);
                            if (failure) {
                              setError(failure);
                              setDeleting(false);
                              return;
                            }
                            onDeleted();
                          }}
                        >
                          <Icon name="trash" size={15} />
                          {deleting ? "删除中…" : "删除"}
                        </button>
                        <button
                          type="button"
                          className="ghost-button"
                          disabled={deleting}
                          autoFocus
                          onClick={() => setConfirming(false)}
                        >
                          取消
                        </button>
                      </div>
                    </div>
                  )}
                </Collapse>
              </section>
            )}

            {active.id === "profile" && <MemberProfile agent={agent} onOpenRun={onOpenRun} />}

            {error && <p className="form-error" role="alert">{error}</p>}
          </>
        )}
      </CategorySheet>
    </Dialog>
  );
}

export function CreateMember({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  return (
    <Dialog
      title="新成员"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ghost-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={saving || !name.trim() || !instructions.trim()}
            onClick={async () => {
              setSaving(true);
              const result = await store.createMember(name, instructions);
              setSaving(false);
              if (result.error) setError(result.error);
              else onClose();
            }}
          >
            {saving ? "添加中…" : "添加"}
          </button>
        </>
      }
    >
      <label className="field-label" htmlFor="new-name">
        名称
      </label>
      <input id="new-name" className="field" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      <label className="field-label" htmlFor="new-instructions">
        职责说明
      </label>
      <textarea id="new-instructions" className="field" rows={6} value={instructions} onChange={(e) => setInstructions(e.target.value)} placeholder="它擅长什么、依据什么工作、输出什么" />
      {error && <p className="form-error" role="alert">{error}</p>}
    </Dialog>
  );
}

type GroupSettingsProps = {
  conversationID: string;
  /** The sidebar's context menu opens straight on the confirmation step. */
  startConfirming?: boolean;
  onClose: () => void;
  /** Called once the server has dissolved the group and the directory is refreshed. */
  onDissolved: () => void;
};

/** Like the member editor: the form waits for the group so its fields start from the record. */
export function GroupSettings(props: GroupSettingsProps) {
  const known = useTeam((s) => Boolean(s.workspace?.conversations.some((c) => c.id === props.conversationID)));
  return known ? <GroupSettingsForm {...props} /> : null;
}

function GroupSettingsForm({
  conversationID,
  startConfirming = false,
  onClose,
  onDissolved,
}: GroupSettingsProps) {
  const state = useTeam((s) => s);
  const workspace = state.workspace;
  const group = workspace?.conversations.find((c) => c.id === conversationID);
  const [title, setTitle] = useState(group?.title ?? "");
  const [purpose, setPurpose] = useState(group?.purpose ?? "");
  const [rules, setRules] = useState(group?.rules ?? "");
  const [fact, setFact] = useState("");
  const groupFacts = (state.memories?.memories ?? []).filter(
    (memory) => memory.scope === "group" && memory.agent_id === conversationID && memory.status === "active",
  );
  const [selected, setSelected] = useState<Record<string, true>>(() =>
    Object.fromEntries((group?.member_ids ?? []).map((id) => [id, true])),
  );
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(startConfirming);
  const [dissolving, setDissolving] = useState(false);
  // The step stays rendered through its collapse, so the dialog below never jumps.
  const stepShown = usePresence(confirming ? true : null, 200);
  const stepRef = useRef<HTMLDivElement>(null);
  // Opened from the rail's menu, the step sits at the bottom of a long dialog: bring it in
  // once it has unfolded.
  useEffect(() => {
    if (!confirming) return;
    const timer = window.setTimeout(() => {
      stepRef.current?.scrollIntoView({
        block: "center",
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    }, 200);
    return () => window.clearTimeout(timer);
  }, [confirming]);
  const running = Boolean(workspace?.active_runs.some((r) => r.conversation_id === conversationID));
  if (!group) return null;
  const supervisorID = workspace?.supervisor_id ?? null;
  // Members plus the supervisor: the supervisor is optional in a group and removable here too.
  const members = (workspace?.agents ?? []).filter((a) => a.kind === "member" || a.id === supervisorID);
  const memberCount = Object.keys(selected).length;

  return (
    <Dialog
      title="群组设置"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ghost-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={running || !title.trim() || !purpose.trim() || memberCount === 0}
            onClick={async () => {
              const failure = await store.patchGroup(group.id, {
                title: title.trim(),
                purpose: purpose.trim(),
                rules: rules.trim(),
                member_ids: Object.keys(selected),
              });
              if (failure) setError(failure);
              else onClose();
            }}
          >
            保存
          </button>
        </>
      }
    >
      <label className="field-label" htmlFor="group-title">
        名称
      </label>
      <input id="group-title" className="field" value={title} onChange={(e) => setTitle(e.target.value)} />
      <label className="field-label" htmlFor="group-purpose">
        目的
      </label>
      <textarea id="group-purpose" className="field" rows={3} value={purpose} onChange={(e) => setPurpose(e.target.value)} />
      <label className="field-label" htmlFor="group-rules">
        群规则
      </label>
      <textarea
        id="group-rules"
        className="field"
        rows={3}
        value={rules}
        placeholder="例如：回答先给结论；不超过五句；用表格列对比。"
        onChange={(e) => setRules(e.target.value)}
      />
      <p className="hint">只在这个群里生效，每位成员在这里发言前都会读到。</p>
      <div className="field-label">群约定（{groupFacts.length}）</div>
      <ul className="group-facts">
        {groupFacts.map((memory) => (
          <li key={memory.id}>
            <span>{memory.content}</span>
            <button
              type="button"
              className="icon-button"
              aria-label={`删除：${memory.content}`}
              onClick={async () => {
                const failure = await store.deleteMemory(memory.id);
                if (failure) setError(failure);
              }}
            >
              <Icon name="trash" size={14} />
            </button>
          </li>
        ))}
      </ul>
      <form
        className="group-fact-add"
        onSubmit={async (event) => {
          event.preventDefault();
          const content = fact.trim();
          if (!content) return;
          const failure = await store.addMemory({ content, scope: "group", agent_id: conversationID });
          if (failure) setError(failure);
          else setFact("");
        }}
      >
        <input
          className="field"
          value={fact}
          placeholder="加一条只在这个群里记住的事"
          aria-label="新的群约定"
          onChange={(e) => setFact(e.target.value)}
        />
        <button type="submit" className="ghost-button" disabled={!fact.trim()}>
          添加
        </button>
      </form>
      <div className="field-label">成员（{memberCount}）</div>
      <div className="member-toggles">
        {members.map((agent) => (
          <label key={agent.id} className={selected[agent.id] ? "member-toggle on" : "member-toggle"}>
            <input
              type="checkbox"
              checked={Boolean(selected[agent.id])}
              disabled={running}
              onChange={(e) => {
                const next = { ...selected };
                if (e.target.checked) next[agent.id] = true;
                else delete next[agent.id];
                setSelected(next);
              }}
            />
            <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={24} name={agent.name} agentID={agent.id} />
            <span>{agent.name}</span>
          </label>
        ))}
      </div>
      {memberCount === 0 && (
        <p className="form-error" role="alert">
          群里至少要留一位成员。
        </p>
      )}
      {running && <p className="hint">这个群正在回复，结束后才能改成员。</p>}
      <div className="danger-zone">
        <div className="field-label">危险操作</div>
        <Collapse open={!confirming}>
          <button
            type="button"
            className="ghost-button danger-ghost"
            disabled={running}
            onClick={() => setConfirming(true)}
          >
            <Icon name="trash" size={15} />
            解散群组
          </button>
        </Collapse>
        <Collapse open={confirming}>
          {stepShown.value && (
            <div className="confirm-step" role="group" aria-label="确认解散群组" ref={stepRef}>
              <p className="confirm-prompt">
                解散「{group.title}」后，群组和它的聊天记录会一起删除，成员和他们的私聊都会保留。
              </p>
              <div className="confirm-actions">
                <button
                  type="button"
                  className="danger-button"
                  disabled={dissolving || running}
                  onClick={async () => {
                    setDissolving(true);
                    setError(null);
                    const failure = await store.dissolveGroup(group.id);
                    if (failure) {
                      setError(failure);
                      setDissolving(false);
                      return;
                    }
                    onDissolved();
                  }}
                >
                  <Icon name="trash" size={15} />
                  {dissolving ? "解散中…" : "解散"}
                </button>
                <button type="button" className="ghost-button" disabled={dissolving} autoFocus onClick={() => setConfirming(false)}>
                  取消
                </button>
              </div>
            </div>
          )}
        </Collapse>
        {running && <p className="hint">这个群正在回复，结束后才能解散。</p>}
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
    </Dialog>
  );
}

/** A starting point for a group: name, purpose and rules filled in; members stay the user's pick. */
const GROUP_TEMPLATES: readonly {
  id: string;
  label: string;
  title: string;
  purpose: string;
  rules: string;
  /** Whether the template suggests the supervisor too; the user can still change it. */
  supervisor: boolean;
}[] = [
  {
    id: "review",
    label: "方案评审",
    title: "方案评审",
    purpose: "把方案拿出来，大家从各自的角度挑问题、给改进意见。",
    rules: "每人只从自己的职责出发说；先说结论，再说理由；不重复别人说过的。",
    supervisor: true,
  },
  {
    id: "project",
    label: "项目推进",
    title: "项目推进",
    purpose: "把一个目标拆成几块，各自负责一块，按时交回结果。",
    rules: "接活时说清会交什么、什么时候交；卡住了直接说卡在哪。",
    supervisor: true,
  },
  {
    id: "brainstorm",
    label: "头脑风暴",
    title: "头脑风暴",
    purpose: "围绕一个话题尽量多地想点子，不急着下结论。",
    rules: "每人先各自给 3 个想法；不评价别人的想法，补充或变形可以。",
    supervisor: false,
  },
];

export function NewGroupDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const workspace = useTeam((s) => s.workspace);
  const [template, setTemplate] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [purpose, setPurpose] = useState("");
  const [rules, setRules] = useState("");
  const [opening, setOpening] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [includeSupervisor, setIncludeSupervisor] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDuty, setNewDuty] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const supervisor = agentByID(workspace, workspace?.supervisor_id ?? null);
  const members = (workspace?.agents ?? []).filter((a) => a.kind === "member");
  const needle = query.trim().toLowerCase();
  const listed = needle
    ? members.filter((a) => a.name.toLowerCase().includes(needle) || a.instructions.toLowerCase().includes(needle))
    : members;
  const chosen = selected.map((id) => members.find((a) => a.id === id)).filter((a): a is Agent => Boolean(a));
  const toggle = (id: string) =>
    setSelected((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  const applyTemplate = (id: string) => {
    const picked = GROUP_TEMPLATES.find((item) => item.id === id);
    if (!picked) return;
    setTemplate(id);
    setTitle(picked.title);
    setPurpose(picked.purpose);
    setRules(picked.rules);
    setIncludeSupervisor(picked.supervisor);
  };
  const addMember = async () => {
    setError(null);
    const result = await store.createMember(newName, newDuty);
    if (result.error || !result.id) {
      setError(result.error ?? "没能添加成员。");
      return;
    }
    setSelected((current) => [...current, result.id as string]);
    setNewName("");
    setNewDuty("");
    setAdding(false);
  };
  const create = async () => {
    setSaving(true);
    setError(null);
    const result = await store.createGroup({
      title: title.trim(),
      purpose: purpose.trim(),
      member_ids: selected,
      rules: rules.trim(),
      include_supervisor: includeSupervisor,
    });
    if (!result.id) {
      setSaving(false);
      setError(result.error ?? "没能建群。");
      return;
    }
    const first = opening.trim();
    if (first) {
      const failure = await store.send(result.id, first, []);
      if (failure) store.notify(`群已经建好，但第一条消息没发出去：${failure}`);
    }
    setSaving(false);
    onCreated(result.id);
  };
  const preview = includeSupervisor && supervisor ? [supervisor, ...chosen] : chosen;

  return (
    <Dialog
      title="新建群组"
      wide
      className="new-group-dialog"
      onClose={onClose}
      footer={
        <>
          <span className="new-group-summary">
            {preview.length > 0 && <GroupAvatar members={preview} size={26} />}
            {chosen.length === 0
              ? "还没选成员"
              : includeSupervisor && supervisor
                ? `负责人、${chosen.length} 位成员`
                : `${chosen.length} 位成员`}
          </span>
          <button type="button" className="ghost-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={saving || !title.trim() || !purpose.trim() || chosen.length === 0}
            onClick={() => void create()}
          >
            {saving ? "创建中…" : opening.trim() ? "创建并发送" : "创建"}
          </button>
        </>
      }
    >
      <div className="field-label">从模板开始（可选）</div>
      <div className="group-templates" role="radiogroup" aria-label="群组模板">
        {GROUP_TEMPLATES.map((item) => (
          <button
            key={item.id}
            type="button"
            role="radio"
            aria-checked={template === item.id}
            className={template === item.id ? "group-template on" : "group-template"}
            onClick={() => applyTemplate(item.id)}
          >
            <span className="group-template-name">{item.label}</span>
            <span className="group-template-purpose">{item.purpose}</span>
          </button>
        ))}
      </div>

      <div className="new-group-grid">
        <div>
          <label className="field-label" htmlFor="new-group-title">
            名称
          </label>
          <input
            id="new-group-title"
            className="field"
            autoFocus
            value={title}
            placeholder="例如：增长方案评审"
            onChange={(e) => setTitle(e.target.value)}
          />
          <label className="field-label" htmlFor="new-group-purpose">
            目的
          </label>
          <textarea
            id="new-group-purpose"
            className="field"
            rows={3}
            value={purpose}
            placeholder="这个群为什么存在：大家在这里一起做什么。分发和成员都会看它。"
            onChange={(e) => setPurpose(e.target.value)}
          />
          <label className="field-label" htmlFor="new-group-rules">
            群规则（可选）
          </label>
          <textarea
            id="new-group-rules"
            className="field"
            rows={2}
            value={rules}
            placeholder="例如：先给结论；不超过五句。"
            onChange={(e) => setRules(e.target.value)}
          />
        </div>

        <div className="new-group-people">
          <div className="field-label">成员</div>
          {supervisor && (
            <div className="new-group-supervisor">
              <div className="new-group-supervisor-text">
                <span className="new-group-supervisor-name">加入主管（{supervisor.name}）</span>
                <span className="hint">
                  主管不干活，负责整理进度、检查结果、催办停住的任务。不加也能正常分派，群里有一个看不见的分发会决定谁来回答。
                </span>
              </div>
              <Switch
                checked={includeSupervisor}
                label={includeSupervisor ? "不加主管" : "加入主管"}
                onChange={() => setIncludeSupervisor((value) => !value)}
              />
            </div>
          )}
          {members.length > 5 && (
            <input
              className="field new-group-search"
              value={query}
              placeholder="按名字或职责找成员"
              aria-label="找成员"
              onChange={(e) => setQuery(e.target.value)}
            />
          )}
          <div className="new-group-list">
            {listed.map((agent) => {
              const on = selected.includes(agent.id);
              return (
                <label key={agent.id} className={on ? "new-group-person on" : "new-group-person"}>
                  <input type="checkbox" checked={on} onChange={() => toggle(agent.id)} />
                  <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={26} name={agent.name} agentID={agent.id} />
                  <span className="new-group-person-text">
                    <span className="new-group-person-name">{agent.name}</span>
                    <span className="new-group-person-duty">{agent.instructions.split("\n")[0]}</span>
                  </span>
                  <span className="new-group-check" aria-hidden="true">
                    {on && <Icon name="check" size={13} />}
                  </span>
                </label>
              );
            })}
            {listed.length === 0 && members.length > 0 && <p className="hint">没有找到「{query.trim()}」</p>}
          </div>
          <Collapse open={!adding}>
            <button type="button" className="ghost-button new-group-add" onClick={() => setAdding(true)}>
              <Icon name="plus" size={14} />
              {members.length === 0 ? "先加一位成员" : "新成员，直接加进来"}
            </button>
          </Collapse>
          <Collapse open={adding}>
            <div className="new-group-member-form">
              <input
                className="field"
                value={newName}
                placeholder="名字"
                aria-label="新成员名字"
                onChange={(e) => setNewName(e.target.value)}
              />
              <textarea
                className="field"
                rows={2}
                value={newDuty}
                placeholder="职责：它擅长什么、交出什么"
                aria-label="新成员职责"
                onChange={(e) => setNewDuty(e.target.value)}
              />
              <div className="confirm-actions">
                <button
                  type="button"
                  className="primary-button"
                  disabled={!newName.trim() || !newDuty.trim()}
                  onClick={() => void addMember()}
                >
                  添加并选中
                </button>
                <button type="button" className="ghost-button" onClick={() => setAdding(false)}>
                  取消
                </button>
              </div>
            </div>
          </Collapse>
        </div>
      </div>

      <label className="field-label" htmlFor="new-group-opening">
        第一条消息（可选）
      </label>
      <textarea
        id="new-group-opening"
        className="field"
        rows={2}
        value={opening}
        placeholder={chosen.length > 1 ? "建好就发出去，例如：@所有人 先各自说说对这个方案的看法" : "建好就发出去，团队马上开始"}
        onChange={(e) => setOpening(e.target.value)}
      />
      {error && <p className="form-error" role="alert">{error}</p>}
    </Dialog>
  );
}

function PersonChip({ id, fallback = "未署名" }: { id: string | null; fallback?: string }) {
  const workspace = useTeam((s) => s.workspace);
  const agent = agentByID(workspace, id);
  // A non-null id with no row means the member was deleted; its notes and tasks remain.
  if (!agent) return <span className="person-name">{id ? "已删除的成员" : fallback}</span>;
  return (
    <span className="person">
      <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={18} name={agent.name} agentID={agent.id} />
      <span className="person-name">{agent.name}</span>
    </span>
  );
}

/** One task: a round done-box on the left, the status menu on the right. */
function TaskRow({ task }: { task: TaskItem }) {
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  // The menu closes on the pointerdown that precedes this button's click; without the gap the
  // click would immediately reopen it instead of toggling it shut.
  const closedAt = useRef(0);
  const done = task.status === "done";
  return (
    <li className={`task-row ${task.status}`} data-flip-key={task.id}>
      <button
        type="button"
        role="checkbox"
        aria-checked={done}
        aria-label={done ? "标记为未完成" : "标记为已完成"}
        className={done ? "task-check on" : "task-check"}
        onClick={() => void store.updateTask(task.id, { status: done ? "todo" : "done" })}
      >
        <Icon name={done ? "check" : "todo"} size={done ? 12 : 18} />
      </button>
      <div className="task-main">
        <span className="task-title">{task.title}</span>
        {task.detail && <span className="task-detail">{task.detail}</span>}
        {task.note && <span className="task-note">{task.note}</span>}
      </div>
      <PersonChip id={task.assignee_id} fallback="未指派" />
      <button
        type="button"
        className={`task-status ${task.status}`}
        aria-haspopup="menu"
        aria-expanded={menu !== null}
        aria-label={`当前状态：${STATUS_LABELS[task.status]}，点击修改`}
        onClick={(event) => {
          if (menu || Date.now() - closedAt.current < 250) return;
          const rect = event.currentTarget.getBoundingClientRect();
          setMenu({ x: rect.left, y: rect.bottom + 6 });
        }}
      >
        <Icon name={STATUS_ICONS[task.status]} size={12} />
        {STATUS_LABELS[task.status]}
        <Icon name="caret-down" size={10} />
      </button>
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={TASK_GROUPS.map(([status, label, icon]) => ({
            label,
            icon,
            disabled: status === task.status,
            onSelect: () => void store.updateTask(task.id, { status }),
          }))}
          onClose={() => {
            closedAt.current = Date.now();
            setMenu(null);
          }}
        />
      )}
    </li>
  );
}

/** Window on the notes and tasks the team keeps while it works: notes can be deleted, a task's
 * status can be set by hand. */
export function Workbench({ onClose }: { onClose: () => void }) {
  const state = useTeam((s) => s);
  const workspace = state.workspace;
  const notes = state.notes;
  const tasks = state.tasks;
  const [tab, setTab] = useState<"notes" | "tasks">("notes");
  const [openNoteID, setOpenNoteID] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // The delete step stays rendered through its collapse.
  const deleteStep = usePresence(confirmingDelete ? true : null, 200);
  const tabs = useRef<HTMLDivElement>(null);
  const taskScroll = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(tabs, tab);
  // A task that changes group glides to its new place; the panel only fades on the very first
  // paint of the dialog.
  // The tasks list is only mounted on its tab, so the tab belongs in the trigger list: the
  // first pass on that tab records where everything sits.
  useFlip(taskScroll, [tasks, tab]);
  const [panelIn, setPanelIn] = useState(false);

  const noteStamp = notes?.find((entry) => entry.id === openNoteID)?.updated_at ?? "";

  useEffect(() => {
    void store.loadWorkbench();
  }, []);

  // Re-read the body whenever the list says this note moved (an edit, a fresh preview).
  // A note that just left the list is not fetched again: it would 404 on the way out.
  useEffect(() => {
    if (openNoteID && notes?.some((entry) => entry.id === openNoteID)) void store.loadNote(openNoteID);
  }, [openNoteID, noteStamp, notes]);

  useEffect(() => {
    if (!notes) return;
    if (openNoteID && notes.some((entry) => entry.id === openNoteID)) return;
    setOpenNoteID(notes[0]?.id ?? null);
  }, [notes, openNoteID]);

  const note = openNoteID ? state.noteBodies[openNoteID] : undefined;
  const memberNames = (workspace?.agents ?? []).map((agent) => agent.name);
  const pick = (next: "notes" | "tasks") => {
    if (next === tab) return;
    setPanelIn(true);
    setTab(next);
  };
  const selectAt = (next: "notes" | "tasks") => {
    pick(next);
    document.getElementById(`workbench-tab-${next}`)?.focus();
  };
  // A new note never inherits the previous note's half-open confirmation.
  useEffect(() => {
    setConfirmingDelete(false);
    setDeleteError(null);
  }, [openNoteID]);

  const removeOpenNote = async () => {
    if (!note) return;
    const at = (notes ?? []).findIndex((entry) => entry.id === note.id);
    setDeleting(true);
    setDeleteError(null);
    const failure = await store.deleteNote(note.id);
    setDeleting(false);
    if (failure) {
      setDeleteError(failure);
      return;
    }
    // The list already dropped the row, so the same index now names the following note.
    const left = (store.getState().notes ?? []);
    setOpenNoteID(left[at]?.id ?? left[left.length - 1]?.id ?? null);
  };

  return (
    <Dialog
      title="工作台"
      onClose={onClose}
      wide
      className="workbench-dialog"
      closeOnly
      footer={
        <button type="button" className="ghost-button" onClick={onClose}>
          关闭
        </button>
      }
    >
      <div
        className={thumb ? "segmented workbench-tabs has-thumb" : "segmented workbench-tabs"}
        role="tablist"
        aria-label="工作台"
        ref={tabs}
      >
        {thumb && (
          <span
            className={ready ? "segmented-thumb" : "segmented-thumb instant"}
            aria-hidden="true"
            style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
          />
        )}
        {(["notes", "tasks"] as const).map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            id={`workbench-tab-${value}`}
            data-seg={value}
            aria-selected={tab === value}
            aria-controls={`workbench-panel-${value}`}
            tabIndex={tab === value ? 0 : -1}
            className={tab === value ? "on" : ""}
            onClick={() => pick(value)}
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              selectAt(value === "notes" ? "tasks" : "notes");
            }}
          >
            {value === "notes" ? "笔记" : "任务"}
          </button>
        ))}
      </div>

      {state.workbenchError && (
        <div className="inline-error" role="alert">
          <span>{state.workbenchError}</span>
          <button type="button" className="link-button" onClick={() => void store.loadWorkbench()}>
            重新读取
          </button>
        </div>
      )}

      {tab === "notes" && (
        <div
          key={tab}
          className={panelIn ? "workbench-panel panel-in" : "workbench-panel"}
          role="tabpanel"
          id="workbench-panel-notes"
          aria-labelledby="workbench-tab-notes"
        >
          {notes === null ? (
            state.workbenchError ? null : <p className="muted">正在读取笔记…</p>
          ) : notes.length === 0 ? (
            <p className="workbench-empty">还没有笔记</p>
          ) : (
            <div className="workbench-split">
              <div className="note-list">
                {notes.map((entry) => (
                  <button
                    key={entry.id}
                    type="button"
                    className={entry.id === openNoteID ? "note-item on" : "note-item"}
                    aria-current={entry.id === openNoteID ? "true" : undefined}
                    onClick={() => setOpenNoteID(entry.id)}
                  >
                    <span className="note-item-title">{entry.title}</span>
                    <span className="note-item-preview">{entry.preview}</span>
                    <span className="note-item-meta">
                      <PersonChip id={entry.author_id} />
                      <span className="note-item-time">{listTimestamp(entry.updated_at)}</span>
                    </span>
                  </button>
                ))}
              </div>
              <div className="note-detail">
                {note ? (
                  <>
                    <div className="note-detail-head">
                      <div>
                        <h3>{note.title}</h3>
                        <div className="note-item-meta">
                          <PersonChip id={note.author_id} />
                          <span className="note-item-time">{listTimestamp(note.updated_at)}</span>
                        </div>
                      </div>
                      {!confirmingDelete && (
                        <button
                          type="button"
                          className="ghost-button danger-ghost note-delete"
                          onClick={() => setConfirmingDelete(true)}
                        >
                          <Icon name="trash" size={14} />
                          删除笔记
                        </button>
                      )}
                    </div>
                    <Collapse open={confirmingDelete}>
                      {deleteStep.value && (
                        <div className="confirm-step note-confirm" role="group" aria-label="确认删除笔记">
                          <p className="confirm-prompt">删除「{note.title}」？删除后无法恢复。</p>
                          <div className="confirm-actions">
                            <button
                              type="button"
                              className="danger-button"
                              disabled={deleting}
                              onClick={() => void removeOpenNote()}
                            >
                              <Icon name="trash" size={15} />
                              {deleting ? "删除中…" : "删除"}
                            </button>
                            <button
                              type="button"
                              className="ghost-button"
                              disabled={deleting}
                              autoFocus
                              onClick={() => setConfirmingDelete(false)}
                            >
                              取消
                            </button>
                          </div>
                        </div>
                      )}
                    </Collapse>
                    {deleteError && (
                      <p className="form-error" role="alert">
                        {deleteError}
                      </p>
                    )}
                    <MessageBody text={note.content} markdown memberNames={memberNames} onMention={() => undefined} />
                  </>
                ) : (
                  <p className="muted">正在读取…</p>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "tasks" && (
        <div
          key={tab}
          className={panelIn ? "workbench-panel panel-in" : "workbench-panel"}
          role="tabpanel"
          id="workbench-panel-tasks"
          aria-labelledby="workbench-tab-tasks"
        >
          {tasks === null ? (
            state.workbenchError ? null : <p className="muted">正在读取任务…</p>
          ) : tasks.length === 0 ? (
            <p className="workbench-empty">还没有任务</p>
          ) : (
            <div className="workbench-scroll" ref={taskScroll}>
              {TASK_GROUPS.map(([status, label]) => {
                const rows = tasks.filter((task) => task.status === status);
                // 待办 and 已完成 are anchors the user always wants to see, empty or not.
                if (!rows.length && status !== "todo" && status !== "done") return null;
                return (
                  <section key={status} className="task-group">
                    <h3 className="task-group-label">
                      {label}
                      <span className="task-count">{rows.length}</span>
                    </h3>
                    {rows.length ? (
                      <ul className="task-list">
                        {rows.map((task) => (
                          <TaskRow key={task.id} task={task} />
                        ))}
                      </ul>
                    ) : (
                      <p className="task-group-empty">{status === "todo" ? "没有待办任务" : "还没有完成的任务"}</p>
                    )}
                  </section>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}

export type ThemeChoice = "system" | "light" | "dark";

/**
 * 「设置 → 连接」: a desktop window can talk to the backend the app starts, or to one running on a
 * server. The choice lives in the shell, so a switch reloads the window into the other backend.
 */
function RemoteBackend() {
  const remote = useTeam((s) => s.remote);
  const baseURL = useTeam((s) => s.baseURL);
  const [side, setSide] = useState<"local" | "remote">("local");
  const [address, setAddress] = useState("https://");
  const [token, setToken] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, side);

  if (remote) {
    return (
      <section className="settings-section">
        <div className="settings-heading">
          <h4>远端服务器</h4>
          <p>这个窗口用的是服务器上的后端，对话和团队都在那边。</p>
        </div>
        <dl className="settings-values">
          <div>
            <dt>地址</dt>
            <dd>{baseURL}</dd>
          </div>
        </dl>
        <button
          type="button"
          className="primary-button"
          disabled={pending}
          onClick={async () => {
            setPending(true);
            // Success reloads the window; a failure lands back here with a notice.
            await store.useLocalBackend();
            setPending(false);
          }}
        >
          {pending ? "正在改回…" : "改回本机"}
        </button>
        <p className="hint">定时任务只在服务器的后端运行时才会执行。</p>
      </section>
    );
  }

  return (
    <section className="settings-section">
      <div className="settings-heading">
        <h4>后端</h4>
        <p>后端由应用自己启动，或者连到一台服务器。</p>
      </div>
      <div
        className={thumb ? "segmented has-thumb" : "segmented"}
        role="radiogroup"
        aria-label="后端"
        ref={segmented}
      >
        {thumb && (
          <span
            className={ready ? "segmented-thumb" : "segmented-thumb instant"}
            aria-hidden="true"
            style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
          />
        )}
        {(["local", "remote"] as const).map((value) => (
          <button
            key={value}
            type="button"
            role="radio"
            data-seg={value}
            aria-checked={side === value}
            className={side === value ? "on" : ""}
            onClick={() => setSide(value)}
          >
            {value === "local" ? "本机" : "远端服务器"}
          </button>
        ))}
      </div>

      {side === "local" ? (
        <p className="hint">数据留在这台电脑上。</p>
      ) : (
        <>
          <label className="field-label" htmlFor="remote-url">
            地址
          </label>
          <input
            id="remote-url"
            className="field"
            value={address}
            spellCheck={false}
            placeholder="https://cubes.example.com"
            onChange={(event) => setAddress(event.target.value)}
          />
          <label className="field-label" htmlFor="remote-token">
            访问令牌
          </label>
          <div className="inline-field">
            <input
              id="remote-token"
              className="field"
              type={revealed ? "text" : "password"}
              autoComplete="off"
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
            <button type="button" className="link-button" onClick={() => setRevealed(!revealed)}>
              {revealed ? "隐藏" : "显示"}
            </button>
          </div>
          <button
            type="button"
            className="primary-button"
            disabled={pending}
            onClick={async () => {
              setPending(true);
              setError(null);
              // Only a failure returns: a working address reloads the window into the server.
              const failure = await store.connectRemote(address, token);
              setPending(false);
              if (failure) setError(failure);
            }}
          >
            {pending ? "正在连接…" : "测试并连接"}
          </button>
          {error ? (
            <p className="form-error" role="alert">
              {error}
            </p>
          ) : (
            <p className="hint">先试一次连接，确认令牌可用才会切换。</p>
          )}
        </>
      )}
    </section>
  );
}

/** 「设置 → 常规」: the theme and the accent colour. App owns both, so they outlast the window. */
function GeneralSettings({
  appearance,
  onAppearance,
  accent,
  onAccent,
}: {
  appearance: ThemeChoice;
  onAppearance: (value: ThemeChoice) => void;
  accent: Accent;
  onAccent: (value: Accent) => void;
}) {
  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, appearance);
  // The dock/taskbar picture lives in the shell, not the backend: read it once, save on tap.
  const [iconMode, setIconMode] = useState<AppIconMode>("auto");
  useEffect(() => {
    let live = true;
    void desktop?.appIconInfo?.().then((info) => {
      if (live && info) setIconMode(info.mode);
    });
    return () => {
      live = false;
    };
  }, []);
  const saveAppIcon = (value: AppIconMode) => {
    setIconMode(value);
    void desktop?.setAppIcon?.(value);
  };
  const pickAppIcon = () => {
    void desktop?.pickAppIcon?.().then((info) => {
      if (info) setIconMode(info.mode);
    });
  };
  return (
    <>
      <section className="settings-section">
        <div className="settings-heading">
          <h4>外观</h4>
          <p>跟随系统，或者固定成浅色、深色。</p>
        </div>
        <div
          className={thumb ? "segmented has-thumb" : "segmented"}
          role="radiogroup"
          aria-label="外观"
          ref={segmented}
        >
          {thumb && (
            <span
              className={ready ? "segmented-thumb" : "segmented-thumb instant"}
              aria-hidden="true"
              style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
            />
          )}
          {(["system", "light", "dark"] as const).map((value) => (
            <button
              key={value}
              type="button"
              role="radio"
              data-seg={value}
              aria-checked={appearance === value}
              className={appearance === value ? "on" : ""}
              onClick={() => onAppearance(value)}
            >
              {{ system: "跟随系统", light: "浅色", dark: "深色" }[value]}
            </button>
          ))}
        </div>
      </section>

      {isDesktop && (
        <section className="settings-section">
          <div className="settings-heading">
            <h4>应用图标</h4>
            <p>Dock 和任务栏上的那张脸。跟着明暗自动换，或者固定一张，也可以自己选图。</p>
          </div>
          <div className="segmented" role="radiogroup" aria-label="应用图标">
            {(["auto", "light", "dark"] as const).map((value) => (
              <button
                key={value}
                type="button"
                role="radio"
                data-seg={value}
                aria-checked={iconMode === value}
                className={iconMode === value ? "on" : ""}
                onClick={() => saveAppIcon(value)}
              >
                {{ auto: "跟随系统", light: "浅色", dark: "深色" }[value]}
              </button>
            ))}
            <button
              type="button"
              role="radio"
              data-seg="custom"
              aria-checked={iconMode === "custom"}
              className={iconMode === "custom" ? "on" : ""}
              onClick={pickAppIcon}
            >
              自定义…
            </button>
          </div>
        </section>
      )}

      <section className="settings-section">
        <div className="settings-heading">
          <h4>主题色</h4>
          <p>气泡和界面里的强调色都跟着它变。</p>
        </div>
        {/* Six presets, or any colour: the bubble and every accent in the interface take it. */}
        <div className="accent-picker">
          <div className="accent-swatches" role="radiogroup" aria-label="主题色">
            {THEME_ACCENTS.map((choice) => (
              <button
                key={choice.id}
                type="button"
                role="radio"
                aria-checked={accent === choice.id}
                aria-label={choice.label}
                title={choice.label}
                className={accent === choice.id ? "accent-swatch on" : "accent-swatch"}
                style={{ "--swatch": choice.swatch } as React.CSSProperties}
                onClick={() => onAccent(choice.id)}
              >
                <span className="accent-swatch-name">{choice.label}</span>
              </button>
            ))}
          </div>
          <ColorField
            label="自定义"
            name="自定义主题色"
            value={asHexColor(accent) ?? (THEME_ACCENTS.find((choice) => choice.id === accent)?.swatch ?? "#2f6fd0")}
            on={asHexColor(accent) !== null}
            onChange={onAccent}
          />
        </div>
        <p className="hint">
          自己挑颜色时，浅色配深字，饱和一点的颜色自动配白字——这时气泡会稍微调深，白字才看得清，也不会像黑字那样发闷；用作线条和文字的那一份会调深或调浅，保证在页面上看得清。选中气泡里的文字有清楚的底色。
        </p>
      </section>
    </>
  );
}

type SettingsCategory = "general" | "models" | "team" | "permissions" | "connection";

/** 常规 → 窗口材质 (macOS desktop only): Liquid Glass follows the system, frosted takes two dials. */
function GlassSettings({ value, onChange }: { value: Glass; onChange: (value: Glass) => void }) {
  const [support, setSupport] = useState<GlassSupport | null>(null);
  useEffect(() => {
    let live = true;
    void glassSupport().then((found) => live && setSupport(found));
    return () => {
      live = false;
    };
  }, []);
  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, value.kind);
  // An older macOS cannot draw Liquid Glass: the choice stays visible, but frosted is what shows.
  const liquidMissing = support !== null && !support.liquid;
  const options = [
    { id: "liquid", label: "液态玻璃" },
    { id: "frosted", label: "毛玻璃" },
  ] as const;
  return (
    <section className="settings-section">
      <div className="settings-heading">
        <h4>窗口材质</h4>
        <p>侧边栏和顶部标签栏后面透出来的那层玻璃。</p>
      </div>
      <div
        className={thumb ? "segmented has-thumb" : "segmented"}
        role="radiogroup"
        aria-label="窗口材质"
        ref={segmented}
      >
        {thumb && (
          <span
            className={ready ? "segmented-thumb" : "segmented-thumb instant"}
            aria-hidden="true"
            style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
          />
        )}
        {options.map((option) => (
          <button
            key={option.id}
            type="button"
            role="radio"
            data-seg={option.id}
            aria-checked={value.kind === option.id}
            className={value.kind === option.id ? "on" : ""}
            disabled={option.id === "liquid" && liquidMissing}
            onClick={() => onChange({ ...value, kind: option.id })}
          >
            {option.label}
          </button>
        ))}
      </div>
      {value.kind === "liquid" ? (
        <p className="hint">
          {liquidMissing
            ? "液态玻璃需要 macOS 26 或更新的系统，这台 Mac 上会显示成毛玻璃。"
            : "系统自带的液态玻璃，颜色和明暗跟随系统，不需要调。"}
        </p>
      ) : (
        <div className="glass-dials">
          <label className="glass-dial">
            <span className="glass-dial-label">透明度</span>
            <input
              type="range"
              min={0}
              max={100}
              step={1}
              value={value.transparency}
              onChange={(event) => onChange({ ...value, transparency: Number(event.target.value) })}
            />
            <span className="glass-dial-value">{value.transparency}%</span>
          </label>
          <label className="glass-dial">
            <span className="glass-dial-label">模糊度</span>
            <input
              type="range"
              min={0}
              max={MAX_BLUR}
              step={1}
              value={value.blur}
              onChange={(event) => onChange({ ...value, blur: Number(event.target.value) })}
            />
            <span className="glass-dial-value">{value.blur}</span>
          </label>
          <p className="hint">
            透明度越高，桌面和后面的窗口透出来越多；模糊度为 0 时就是一层清透的玻璃。
            {(value.transparency !== DEFAULT_GLASS.transparency || value.blur !== DEFAULT_GLASS.blur) && (
              <>
                {" "}
                <button
                  type="button"
                  className="link-button"
                  onClick={() => onChange({ ...DEFAULT_GLASS, kind: "frosted" })}
                >
                  恢复默认
                </button>
              </>
            )}
          </p>
        </div>
      )}
    </section>
  );
}

/** The categories down the left of 设置: a long sheet split by what the settings act on. */
const SETTINGS_CATEGORIES: readonly Category<SettingsCategory>[] = [
  {
    id: "general",
    label: "常规",
    icon: "palette",
    description: glassAvailable ? "外观、主题色和窗口材质，跟着这台设备记住。" : "外观和主题色，跟着这台设备记住。",
  },
  { id: "models", label: "模型", icon: "model", description: "回复用哪些模型服务，默认和备用模型是哪个。" },
  { id: "team", label: "团队", icon: "team", description: "主管怎么验收、忙时消息怎么处理、定时简报。" },
  {
    id: "permissions",
    label: "权限",
    icon: "shield",
    description: "哪些操作要先问你，以及成员能碰哪些文件夹。",
  },
  { id: "connection", label: "连接", icon: "globe", description: "这个界面连的是哪个后端。" },
];

/** Remembered per window, so 设置 reopens on the category the user left it on. */
const SETTINGS_CATEGORY_KEY = "cubes.settings.category";

/**
 * 「设置」: categories down the left, the chosen one's settings beside them. Each category keeps
 * one reading column, so a long sheet (model services especially) stays findable.
 */
export function Settings({
  onClose,
  appearance,
  onAppearance,
  accent,
  onAccent,
  glass,
  onGlass,
}: {
  onClose: () => void;
  appearance: ThemeChoice;
  onAppearance: (value: ThemeChoice) => void;
  accent: Accent;
  onAccent: (value: Accent) => void;
  glass: Glass;
  onGlass: (value: Glass) => void;
}) {
  const state = useTeam((s) => s);
  const [address, setAddress] = useState(state.baseURL);
  const [error, setError] = useState<string | null>(null);
  const health = state.health;
  // The embedded backend's address is fixed, so 连接 only exists in a desktop window or in a
  // browser that talks to a server.
  const categories = SETTINGS_CATEGORIES.filter(
    (choice) => choice.id !== "connection" || isDesktop || !state.embedded,
  );

  return (
    <Dialog title="设置" onClose={onClose} wide>
      <CategorySheet
        navLabel="设置类别"
        idPrefix="settings"
        categories={categories}
        storageKey={SETTINGS_CATEGORY_KEY}
      >
        {(active) => (
          <>
            {active.id === "general" && (
              <GeneralSettings
                appearance={appearance}
                onAppearance={onAppearance}
                accent={accent}
                onAccent={onAccent}
              />
            )}

            {active.id === "general" && glassAvailable && <GlassSettings value={glass} onChange={onGlass} />}

            {active.id === "models" && <ProviderSettings />}

            {active.id === "team" && <TeamSettingsSection />}

            {active.id === "permissions" && <PermissionSettingsSection />}

            {active.id === "connection" && (
              <>
                {isDesktop ? (
                  <RemoteBackend />
                ) : (
                  <section className="settings-section">
                    <div className="settings-heading">
                      <h4>后端地址</h4>
                      <p>换地址后会重新读取那边的对话和团队。</p>
                    </div>
                    <div className="inline-field">
                      <input
                        id="backend"
                        className="field"
                        aria-label="后端地址"
                        value={address}
                        onChange={(e) => setAddress(e.target.value)}
                        spellCheck={false}
                      />
                      <button
                        type="button"
                        className="primary-button"
                        onClick={() => {
                          const failure = store.setBaseURL(address);
                          setError(failure);
                        }}
                      >
                        连接
                      </button>
                    </div>
                    {error && (
                      <p className="form-error" role="alert">
                        {error}
                      </p>
                    )}
                  </section>
                )}
                {!health && state.healthError && <p className="hint">{state.healthError}</p>}
                {health?.auth === false && (
                  <p className="hint">这个后端没有登录或访问鉴权，只在本机或受信任的网络里使用。</p>
                )}
              </>
            )}
          </>
        )}
      </CategorySheet>
    </Dialog>
  );
}
