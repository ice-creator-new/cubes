// HTTP + server-sent events against the Cubes backend. One base URL, set in settings.

import { SSEParser } from "./sse";
import type {
  ActivitiesPage,
  ActivityDetail,
  GroupTask,
  Action,
  Agent,
  ApprovalRule,
  CompactResult,
  ContextMeter,
  Attachment,
  Appearance,
  AskAnswer,
  Connector,
  ComputerPermissions,
  ComputerState,
  ConnectorInput,
  ConnectorsState,
  Conversation,
  ConversationDetail,
  ConversationRuns,
  Health,
  Memory,
  MemoriesState,
  MemoryScope,
  ModelCaps,
  ModelOverride,
  ModelRef,
  Note,
  NoteSummary,
  PermissionMode,
  PermissionSettings,
  ProfilePeriod,
  ProfileReport,
  Provider,
  ProviderForm,
  ProviderProbe,
  ProviderProtocol,
  ProvidersState,
  ReactionUpdate,
  Run,
  RunTrace,
  Schedule,
  ScheduleInput,
  ScheduleRunDetail,
  SchedulesState,
  SendMode,
  SidebarLayout,
  LocalSkill,
  Skill,
  Workflow,
  WorkflowDetail,
  WorkflowEdgeInput,
  WorkflowRun,
  WorkflowStepInput,
  SkillDetail,
  TaskItem,
  TaskStatus,
  TeamImportPlan,
  TeamSettings,
  TokenBudget,
  ToolPermissions,
  UsagePeriod,
  UsageReport,
  Webhook,
  WebhookWithSecret,
  Workspace,
} from "./types";

export const DEFAULT_BASE_URL = "http://127.0.0.1:8000";
const BASE_KEY = "cubes.backendURL";
/** What the stream never says about itself: it only stops when something went wrong. */
const BROKEN_STREAM = "实时更新断了，状态可能已经变化。";
const EVENT_NAMES: Record<string, true> = {
  snapshot: true,
  workspace: true,
  run: true,
  message_start: true,
  message_end: true,
  message_update: true,
  delta: true,
  sidebar: true,
  notes: true,
  tasks: true,
  connectors: true,
  computer: true,
  schedules: true,
  memories: true,
  team_settings: true,
  permission_settings: true,
  skills: true,
  reactions: true,
  context: true,
  workflows: true,
  workflow_run: true,
  "message_removed": true,
  decisions: true,
  activity: true,
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Accepts http/https with a host; a missing scheme means local http. Returns null when invalid. */
export function normalizeBaseURL(raw: string): string | null {
  let text = raw.trim();
  if (!text || /\s/.test(text)) return null;
  if (!text.includes("://")) text = `http://${text}`;
  try {
    const url = new URL(text);
    if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname) return null;
    return `${url.protocol}//${url.host}${url.pathname.replace(/\/+$/, "")}`;
  } catch {
    return null;
  }
}

export function loadBaseURL(): string {
  return normalizeBaseURL(localStorage.getItem(BASE_KEY) ?? "") ?? DEFAULT_BASE_URL;
}

export function saveBaseURL(url: string): void {
  localStorage.setItem(BASE_KEY, url);
}

/** FastAPI errors are `{"detail": "..."}`, sometimes a list of validation objects. */
async function detailOf(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      const messages = body.detail
        .map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : ""))
        .filter(Boolean);
      if (messages.length) return messages.join("；");
    }
  } catch {
    // Not JSON: fall through to the status line.
  }
  return `请求失败（HTTP ${response.status}）。`;
}

function transportMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "请求已取消。";
  return "连不上后端服务。请确认本地服务已启动，并且地址填写正确。";
}

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : transportMessage(error);
}

export class ApiClient {
  constructor(
    readonly baseURL: string,
    /** The desktop launch token; null in a browser, where the backend has no guard. */
    readonly token: string | null = null,
  ) {}

  private url(path: string): string {
    return `${this.baseURL}/${path}`;
  }

  /** Every call carries the launch token when there is one: requests, streams, avatars. */
  private headers(extra: Record<string, string>): Record<string, string> {
    return this.token ? { ...extra, Authorization: `Bearer ${this.token}` } : extra;
  }

  private async request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    let response: Response;
    try {
      response = await fetch(this.url(path), {
        method,
        headers: this.headers(
          body === undefined
            ? { Accept: "application/json" }
            : { Accept: "application/json", "Content-Type": "application/json" },
        ),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      });
    } catch (error) {
      throw new ApiError(transportMessage(error));
    }
    if (!response.ok) throw new ApiError(await detailOf(response), response.status);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  health() {
    return this.request<Health>("GET", "health");
  }

  workspace() {
    return this.request<Workspace>("GET", "workspace");
  }

  /** Names the supervisor and stores the look picked on the first-run screen. */
  completeOnboarding(name: string, appearance: Appearance) {
    return this.request<Workspace>("POST", "onboarding", { name, appearance });
  }

  /** Per-agent token totals for a window, plus each agent's own-budget spend. */
  usage(period: UsagePeriod) {
    return this.request<UsageReport>("GET", `usage?period=${encodeURIComponent(period)}`);
  }

  /** One conversation. With `limit` the newest page comes back, `before` stepping further back. */
  conversation(id: string, page?: { limit?: number; before?: number | null }, signal?: AbortSignal) {
    const params = new URLSearchParams();
    if (page?.limit !== undefined) params.set("limit", String(page.limit));
    if (page?.before !== undefined && page.before !== null) params.set("before", String(page.before));
    const query = params.toString();
    return this.request<ConversationDetail>(
      "GET",
      `conversations/${encodeURIComponent(id)}${query ? `?${query}` : ""}`,
      undefined,
      signal,
    );
  }

  /** Remember how far the user has read this chat; the server's unread dot clears. */
  markConversationRead(id: string, seq: number) {
    return this.request<void>("POST", `conversations/${encodeURIComponent(id)}/read`, { seq });
  }

  /** The team as one Markdown file; fetched as text, never as JSON. */
  async exportTeam(): Promise<string> {
    let response: Response;
    try {
      response = await fetch(this.url("team/export"), {
        headers: this.headers({ Accept: "text/markdown" }),
      });
    } catch (error) {
      throw new ApiError(transportMessage(error));
    }
    if (!response.ok) throw new ApiError(await detailOf(response), response.status);
    return await response.text();
  }

  /** Preview the file's changes, or apply them with `apply: true`. */
  importTeam(markdown: string, options: { apply?: boolean; overwriteSupervisor?: boolean } = {}) {
    return this.request<TeamImportPlan>("POST", "team/import", {
      markdown,
      apply: options.apply ?? false,
      overwrite_supervisor: options.overwriteSupervisor ?? false,
    });
  }

  createAgent(name: string, instructions: string) {
    return this.request<Agent>("POST", "agents", { name, instructions });
  }

  patchAgent(
    id: string,
    patch: {
      name?: string;
      instructions?: string;
      appearance?: Appearance | null;
      /** Null follows the workspace default; omitted leaves the stored pick alone. */
      model_ref?: ModelRef | null;
      /** Object to set a cap, null to clear it; omitted leaves the stored budget alone. */
      token_budget?: TokenBudget | null;
      /** Object to set the four packs, null to allow everything; omitted leaves them alone. */
      tool_permissions?: ToolPermissions | null;
      /** Null follows the team setting; omitted leaves the stored pick alone. */
      send_mode?: SendMode | null;
      /** Null follows the workspace's approval mode; omitted leaves the stored pick alone. */
      permission_mode?: PermissionMode | null;
    },
  ) {
    return this.request<Agent>("PATCH", `agents/${encodeURIComponent(id)}`, patch);
  }

  /** The supervisor's own row is refused by the server; everything else cascades there. */
  deleteAgent(id: string) {
    return this.request<void>("DELETE", `agents/${encodeURIComponent(id)}`);
  }

  confirmAction(id: string, approve: boolean, always = false) {
    return this.request<Action>("POST", `actions/${encodeURIComponent(id)}/confirm`, {
      approve,
      always,
    });
  }

  /** The remembered 「总是允许」 rules the settings page lists. */
  approvalRules() {
    return this.request<{ rules: ApprovalRule[] }>("GET", "approval-rules");
  }

  deleteApprovalRule(id: string) {
    return this.request<void>("DELETE", `approval-rules/${encodeURIComponent(id)}`);
  }

  /** How full the bot's context window is for this conversation. */
  conversationContext(id: string) {
    return this.request<ContextMeter>("GET", `conversations/${encodeURIComponent(id)}/context`);
  }

  /** Fold older turns into a summary now; answers the meter after the fold. */
  compactConversation(id: string) {
    return this.request<CompactResult>("POST", `conversations/${encodeURIComponent(id)}/compact`, {});
  }

  /** Answers an `ask_user` call: one entry per question, or a skip for all of them. */
  answerAction(id: string, answer: { answers: AskAnswer[]; skip: boolean }) {
    return this.request<Action>("POST", `actions/${encodeURIComponent(id)}/answer`, answer);
  }

  openDirect(agentID: string) {
    return this.request<Conversation>("POST", `agents/${encodeURIComponent(agentID)}/direct`, {});
  }

  /** The work one group took on, newest first. */
  groupTasks(id: string) {
    return this.request<{ conversation_id: string; tasks: GroupTask[] }>("GET", `conversations/${encodeURIComponent(id)}/tasks`);
  }

  /** 「现在就做」: starts a queued task beside its bot's live work. */
  startTaskNow(groupID: string, taskID: string) {
    return this.request<{ conversation_id?: string; tasks?: GroupTask[]; task?: GroupTask }>(
      "POST",
      `conversations/${encodeURIComponent(groupID)}/tasks/${encodeURIComponent(taskID)}/now`,
      {},
    );
  }

  /** Stops one task's thread run, or takes a queued one out of the line. */
  stopTask(groupID: string, taskID: string) {
    return this.request<{ conversation_id?: string; tasks?: GroupTask[]; task?: GroupTask }>(
      "POST",
      `conversations/${encodeURIComponent(groupID)}/tasks/${encodeURIComponent(taskID)}/stop`,
      {},
    );
  }

  /** Folds a queued task into its owner's running thread as a steer message. */
  mergeTask(groupID: string, taskID: string) {
    return this.request<{ conversation_id?: string; tasks?: GroupTask[]; task?: GroupTask }>(
      "POST",
      `conversations/${encodeURIComponent(groupID)}/tasks/${encodeURIComponent(taskID)}/merge`,
      {},
    );
  }

  /** The user opens a group; the supervisor joins only when asked. */
  createGroup(body: {
    title: string;
    purpose: string;
    member_ids: string[];
    rules?: string;
    include_supervisor?: boolean;
  }) {
    return this.request<Conversation>("POST", "conversations", body);
  }

  /** Re-answers one user message in a group with the speakers the user picked. */
  rerouteMessage(conversationID: string, messageID: string, speakers: string[]) {
    return this.request<{ conversation_id?: string; run_id?: string }>(
      "POST",
      `conversations/${encodeURIComponent(conversationID)}/messages/${encodeURIComponent(messageID)}/reroute`,
      { speakers },
    );
  }

  /** Pulls a team agent into a group and lets them answer the message that @-ed them. */
  inviteMember(conversationID: string, agentID: string, messageID: string | null, noticeID: string) {
    return this.request<{ conversation_id?: string; id?: string; member_ids?: string[] }>(
      "POST",
      `conversations/${encodeURIComponent(conversationID)}/invite`,
      { agent_id: agentID, message_id: messageID, notice_id: noticeID },
    );
  }

  /** Picks up one stopped or failed task again in its own thread. */
  resumeTask(groupID: string, taskID: string) {
    return this.request<{ conversation_id?: string; tasks?: GroupTask[]; task?: GroupTask }>(
      "POST",
      `conversations/${encodeURIComponent(groupID)}/tasks/${encodeURIComponent(taskID)}/resume`,
      {},
    );
  }

  /** Puts a stopped or failed task down: it leaves the lists but stays in the timeline. */
  dismissTask(groupID: string, taskID: string) {
    return this.request<{ conversation_id?: string; tasks?: GroupTask[]; task?: GroupTask }>(
      "POST",
      `conversations/${encodeURIComponent(groupID)}/tasks/${encodeURIComponent(taskID)}/dismiss`,
      {},
    );
  }

  /** Marks a finished task's result as seen, so it leaves the sidebar. */
  markTaskSeen(groupID: string, taskID: string) {
    return this.request<void>(
      "POST",
      `conversations/${encodeURIComponent(groupID)}/tasks/${encodeURIComponent(taskID)}/seen`,
      {},
    );
  }

  patchConversation(id: string, patch: { title?: string; purpose?: string; member_ids?: string[]; rules?: string }) {
    return this.request<Conversation>("PATCH", `conversations/${encodeURIComponent(id)}`, patch);
  }

  /** Dissolves a group and its history; anything that is not a group is refused by the server. */
  dissolveGroup(id: string) {
    return this.request<void>("DELETE", `conversations/${encodeURIComponent(id)}`);
  }

  /** The user adds their own `emoji` to a message; idempotent, and refuses a 21st distinct emoji. */
  addReaction(messageID: string, emoji: string) {
    return this.request<ReactionUpdate>(
      "PUT",
      `messages/${encodeURIComponent(messageID)}/reactions/${encodeURIComponent(emoji)}`,
    );
  }

  /** The user removes their own `emoji`; idempotent, answers the current list either way. */
  removeReaction(messageID: string, emoji: string) {
    return this.request<ReactionUpdate>(
      "DELETE",
      `messages/${encodeURIComponent(messageID)}/reactions/${encodeURIComponent(emoji)}`,
    );
  }

  startRun(
    conversationID: string,
    content: string,
    mentions: string[],
    replyToID?: string | null,
    attachmentIDs: string[] = [],
    mode?: SendMode,
  ) {
    const body: Record<string, unknown> = { content };
    if (mentions.length) body.mentions = mentions;
    if (replyToID) body.reply_to_id = replyToID;
    if (attachmentIDs.length) body.attachment_ids = attachmentIDs;
    if (mode) body.mode = mode;
    return this.request<Run>("POST", `conversations/${encodeURIComponent(conversationID)}/runs`, body);
  }

  /** Take back a send still waiting for its turn. A running one must be stopped instead. */
  cancelQueuedRun(runID: string) {
    return this.request<void>("DELETE", `runs/${encodeURIComponent(runID)}`);
  }

  /** Correct one of your own messages: the tail goes and a fresh run answers the new text. */
  editMessage(conversationID: string, messageID: string, content: string) {
    return this.request<Run>(
      "POST",
      `conversations/${encodeURIComponent(conversationID)}/messages/${encodeURIComponent(messageID)}/edit`,
      { content },
    );
  }

  /** Delete everything after one message; the conversation comes back as it now reads. */
  rewindMessage(conversationID: string, messageID: string) {
    return this.request<ConversationDetail>(
      "POST",
      `conversations/${encodeURIComponent(conversationID)}/messages/${encodeURIComponent(messageID)}/rewind`,
    );
  }

  /**
   * One attachment as the raw body; the server reads its kind from the bytes themselves. The
   * original filename rides along percent-encoded, since header values cannot hold a raw one.
   */
  async uploadAttachment(file: Blob, name = ""): Promise<Attachment> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "Content-Type": file.type || "application/octet-stream",
    };
    // A blob with no name (a pasted screenshot, say) gets its own type's default.
    if (name) headers["X-Attachment-Name"] = encodeURIComponent(name);
    let response: Response;
    try {
      response = await fetch(this.url("attachments"), {
        method: "POST",
        headers: this.headers(headers),
        body: file,
      });
    } catch (error) {
      throw new ApiError(transportMessage(error));
    }
    if (!response.ok) throw new ApiError(await detailOf(response), response.status);
    return (await response.json()) as Attachment;
  }

  /** The file behind one attachment, fetched with the launch token a plain URL could not carry. */
  async attachmentBlob(id: string): Promise<Blob> {
    let response: Response;
    try {
      response = await fetch(this.url(`attachments/${encodeURIComponent(id)}`), { headers: this.headers({}) });
    } catch (error) {
      throw new ApiError(transportMessage(error));
    }
    if (!response.ok) throw new ApiError(await detailOf(response), response.status);
    return response.blob();
  }

  stopRun(runID: string) {
    return this.request<Run>("POST", `runs/${encodeURIComponent(runID)}/stop`, {});
  }

  sidebar() {
    return this.request<SidebarLayout>("GET", "sidebar");
  }

  /** Whole-layout write: the server renumbers pin and in-folder order from array order. */
  putSidebar(body: { items: { conversation_id: string; pinned: boolean; folder_id: string | null }[]; folder_ids: string[] }) {
    return this.request<SidebarLayout>("PUT", "sidebar", body);
  }

  createFolder(name: string) {
    return this.request<SidebarLayout>("POST", "sidebar/folders", { name });
  }

  renameFolder(id: string, name: string) {
    return this.request<SidebarLayout>("PATCH", `sidebar/folders/${encodeURIComponent(id)}`, { name });
  }

  deleteFolder(id: string) {
    return this.request<SidebarLayout>("DELETE", `sidebar/folders/${encodeURIComponent(id)}`);
  }

  // ---- model services

  providers() {
    return this.request<ProvidersState>("GET", "providers");
  }

  createProvider(body: {
    name: string;
    protocol: ProviderProtocol;
    base_url: string;
    api_key?: string;
    models: string[];
    model_overrides?: Record<string, ModelOverride>;
  }) {
    return this.request<Provider>("POST", "providers", body);
  }

  /** An omitted `api_key` keeps the stored one; an empty string clears it. */
  updateProvider(
    id: string,
    patch: {
      name?: string;
      protocol?: ProviderProtocol;
      base_url?: string;
      api_key?: string;
      models?: string[];
      model_overrides?: Record<string, ModelOverride>;
    },
  ) {
    return this.request<Provider>("PATCH", `providers/${encodeURIComponent(id)}`, patch);
  }

  deleteProvider(id: string) {
    return this.request<void>("DELETE", `providers/${encodeURIComponent(id)}`);
  }

  setDefaultModel(ref: ModelRef) {
    return this.request<ModelRef>("PUT", "providers/default", ref);
  }

  /** The backup model; both fields empty turns it off again. */
  setFallbackModel(ref: ModelRef | null) {
    return this.request<ProvidersState>("PUT", "providers/fallback", ref ?? {});
  }

  /** One small real request; `ok` is false with a readable `detail` when it failed. */
  testProvider(form: ProviderForm & { model: string }) {
    return this.request<ProviderProbe>("POST", "providers/test", form);
  }

  /** The provider's own model list; a failed read is a 502 with a readable sentence. */
  fetchModels(form: ProviderForm) {
    return this.request<{ models: string[]; caps: Record<string, ModelCaps> }>("POST", "providers/models", form);
  }

  // ---- connectors

  connectors() {
    return this.request<ConnectorsState>("GET", "connectors");
  }

  /** The server connects and lists tools before answering, so this can take a minute. */
  createConnector(body: ConnectorInput) {
    return this.request<Connector>("POST", "connectors", body);
  }

  /** Omitted secrets and headers keep what is stored; sent sets replace the whole set. */
  updateConnector(id: string, patch: ConnectorInput) {
    return this.request<Connector>("PATCH", `connectors/${encodeURIComponent(id)}`, patch);
  }

  deleteConnector(id: string) {
    return this.request<void>("DELETE", `connectors/${encodeURIComponent(id)}`);
  }

  /** Answers 200 even when the new connection failed; the status then reads "error". */
  reconnectConnector(id: string) {
    return this.request<Connector>("POST", `connectors/${encodeURIComponent(id)}/reconnect`, {});
  }

  // ---- computer

  computer() {
    return this.request<ComputerState>("GET", "computer");
  }

  /** Stops whichever run is using the computer; answers the state after. */
  stopComputer() {
    return this.request<ComputerState>("POST", "computer/stop", {});
  }

  /** Which machine the bots may drive: the Mac's sandbox, a cloud agent, or either. */
  setComputerTarget(target: "local" | "cloud" | "mixed") {
    return this.request<ComputerState>("PATCH", "computer/target", { target });
  }

  /** Screen recording and accessibility on this Mac. Does not prompt. */
  computerPermissions() {
    return this.request<ComputerPermissions>("GET", "computer/permissions");
  }

  /** Asks macOS, then opens System Settings for whichever permission is still missing. */
  requestComputerPermissions() {
    return this.request<ComputerPermissions>("POST", "computer/permissions", {});
  }

  notes() {
    return this.request<{ notes: NoteSummary[] }>("GET", "notes");
  }
  note(id: string) {
    return this.request<Note>("GET", `notes/${encodeURIComponent(id)}`);
  }

  /** Deletes one note for good; the server answers 204. */
  deleteNote(id: string) {
    return this.request<void>("DELETE", `notes/${encodeURIComponent(id)}`);
  }

  tasks() {
    return this.request<{ tasks: TaskItem[] }>("GET", "tasks");
  }

  // ---- schedules

  schedules() {
    return this.request<SchedulesState>("GET", "schedules");
  }

  createSchedule(body: ScheduleInput) {
    return this.request<Schedule>("POST", "schedules", body);
  }

  /** Partial of the create body; omitted fields keep what the server stored. */
  updateSchedule(id: string, patch: ScheduleInput) {
    return this.request<Schedule>("PATCH", `schedules/${encodeURIComponent(id)}`, patch);
  }

  /** Starts it now, or queues it when its conversation is busy; answers the rule as it became. */
  runSchedule(id: string) {
    return this.request<Schedule>("POST", `schedules/${encodeURIComponent(id)}/run`, {});
  }

  /** The newest fire as it happened (or is happening): read-only, every row it wrote. */
  scheduleRun(id: string, signal?: AbortSignal) {
    return this.request<ScheduleRunDetail>("GET", `schedules/${encodeURIComponent(id)}/run`, undefined, signal);
  }

  /** Stops the fire working now; a stopped scheduled run posts no summary. */
  stopSchedule(id: string) {
    return this.request<Schedule>("POST", `schedules/${encodeURIComponent(id)}/stop`, {});
  }

  deleteSchedule(id: string) {
    return this.request<void>("DELETE", `schedules/${encodeURIComponent(id)}`);
  }

  // ---- webhooks

  /** The hook a schedule or a workflow owns, if it has one; the secret never comes back here. */
  webhook(targetKind: "schedule" | "workflow", targetID: string) {
    const params = new URLSearchParams({ target_kind: targetKind, target_id: targetID });
    return this.request<{ webhook: Webhook | null }>("GET", `webhooks?${params.toString()}`);
  }

  /** Create the hook; the answer is the one time the secret is readable, so show it then. */
  createWebhook(targetKind: "schedule" | "workflow", targetID: string) {
    return this.request<WebhookWithSecret>("POST", "webhooks", {
      target_kind: targetKind,
      target_id: targetID,
    });
  }

  /** Replace the secret, keeping the URL; the old secret stops working at once. */
  rotateWebhook(id: string) {
    return this.request<WebhookWithSecret>("POST", `webhooks/${encodeURIComponent(id)}/rotate`, {});
  }

  deleteWebhook(id: string) {
    return this.request<void>("DELETE", `webhooks/${encodeURIComponent(id)}`);
  }

  // ---- memories

  /** Every row, active and retired, newest first, plus the two switches. */
  memories() {
    return this.request<MemoriesState>("GET", "memories");
  }

  /** A memory the user typed here: the server marks its origin as "user". */
  addMemory(body: { content: string; topic?: string; scope: MemoryScope; agent_id?: string }) {
    return this.request<Memory>("POST", "memories", body);
  }

  /** Editing the content retires the old row: the answer is the new active one. */
  updateMemory(id: string, patch: { content?: string; topic?: string; pinned?: boolean }) {
    return this.request<Memory>("PATCH", `memories/${encodeURIComponent(id)}`, patch);
  }

  /** Removes the row for good; the server answers 204. */
  deleteMemory(id: string) {
    return this.request<void>("DELETE", `memories/${encodeURIComponent(id)}`);
  }

  /** The event stream then pushes the whole state back. */
  setMemorySettings(patch: { paused?: boolean; auto_extract?: boolean }) {
    return this.request<MemoriesState>("PATCH", "memories/settings", patch);
  }

  // ---- skills

  /** Every skill, sorted by name. */
  skills() {
    return this.request<{ skills: Skill[] }>("GET", "skills");
  }

  /** One skill with its SKILL.md body and file list. */
  skill(id: string) {
    return this.request<SkillDetail>("GET", `skills/${encodeURIComponent(id)}`);
  }

  /** A skill the user wrote here; source `user`, enabled right away. */
  createSkill(body: { name: string; description: string; body: string }) {
    return this.request<SkillDetail>("POST", "skills", body);
  }

  /** A partial edit (enabled / description / body); the name is immutable. */
  updateSkill(id: string, patch: { enabled?: boolean; description?: string; body?: string }) {
    return this.request<SkillDetail>("PATCH", `skills/${encodeURIComponent(id)}`, patch);
  }

  /** Removes the folder and the row for good; the server answers 204. */
  deleteSkill(id: string) {
    return this.request<void>("DELETE", `skills/${encodeURIComponent(id)}`);
  }

  /** A zip or a single SKILL.md as the raw body; lands disabled until the user enables it. */
  async uploadSkill(file: Blob, contentType: string): Promise<SkillDetail> {
    let response: Response;
    try {
      response = await fetch(this.url("skills/upload"), {
        method: "POST",
        headers: this.headers({ Accept: "application/json", "Content-Type": contentType }),
        body: file,
      });
    } catch (error) {
      throw new ApiError(transportMessage(error));
    }
    if (!response.ok) throw new ApiError(await detailOf(response), response.status);
    return (await response.json()) as SkillDetail;
  }

  /** Imports a skill from a GitHub URL; lands disabled until the user enables it. */
  importSkill(url: string) {
    return this.request<SkillDetail>("POST", "skills/github", { url });
  }

  /** Skills other agents keep on this machine; nothing is copied until `importLocalSkill`. */
  localSkills() {
    return this.request<{ skills: LocalSkill[] }>("GET", "skills/local");
  }

  /** Copies one listed folder in; lands disabled until the user enables it. */
  importLocalSkill(path: string) {
    return this.request<SkillDetail>("POST", "skills/local", { path });
  }

  // ---- workflows

  /** Every workflow in the workspace, newest edit first. */
  workflows() {
    return this.request<{ workflows: Workflow[] }>("GET", "workflows");
  }

  /** One workflow with its `WORKFLOW.md` rendering and its last walks. */
  workflow(id: string) {
    return this.request<WorkflowDetail>("GET", `workflows/${encodeURIComponent(id)}`);
  }

  createWorkflow(body: {
    name: string;
    description?: string;
    steps: WorkflowStepInput[];
    edges?: WorkflowEdgeInput[];
    owner_agent_id?: string;
    enabled?: boolean;
  }) {
    return this.request<Workflow>("POST", "workflows", body);
  }

  updateWorkflow(
    id: string,
    patch: {
      name?: string;
      description?: string;
      steps?: WorkflowStepInput[];
      edges?: WorkflowEdgeInput[];
      enabled?: boolean;
      owner_agent_id?: string;
    },
  ) {
    return this.request<Workflow>("PATCH", `workflows/${encodeURIComponent(id)}`, patch);
  }

  deleteWorkflow(id: string) {
    return this.request<void>("DELETE", `workflows/${encodeURIComponent(id)}`);
  }

  /** Start a walk: it runs in a conversation of its own, one step at a time. */
  runWorkflow(id: string, input = "") {
    return this.request<{ workflow_run: WorkflowRun; conversation_id: string }>(
      "POST",
      `workflows/${encodeURIComponent(id)}/run`,
      { input },
    );
  }

  workflowRun(id: string) {
    return this.request<WorkflowRun>("GET", `workflow-runs/${encodeURIComponent(id)}`);
  }

  /** The walk a chat is serving right now; `null` when none is live. */
  conversationWorkflowRun(conversationId: string) {
    return this.request<{ workflow_run: WorkflowRun | null }>(
      "GET",
      `conversations/${encodeURIComponent(conversationId)}/workflow-run`,
    );
  }

  pauseWorkflowRun(id: string) {
    return this.request<WorkflowRun>("POST", `workflow-runs/${encodeURIComponent(id)}/pause`);
  }

  resumeWorkflowRun(id: string) {
    return this.request<WorkflowRun>("POST", `workflow-runs/${encodeURIComponent(id)}/resume`);
  }

  stopWorkflowRun(id: string) {
    return this.request<WorkflowRun>("POST", `workflow-runs/${encodeURIComponent(id)}/stop`);
  }

  /** The answer to an `ask_user` step; it becomes that step's result. */
  answerWorkflowRun(id: string, input: string) {
    return this.request<WorkflowRun>("POST", `workflow-runs/${encodeURIComponent(id)}/answer`, {
      input,
    });
  }

  /** `retry` or `skip` one node; a live walk applies it at its next boundary. */
  controlWorkflowNode(id: string, nodeID: string, command: "retry" | "skip") {
    return this.request<WorkflowRun>(
      "POST",
      `workflow-runs/${encodeURIComponent(id)}/nodes/${encodeURIComponent(nodeID)}/${command}`,
    );
  }

  // ---- team settings / runs / profile

  /** Workspace-level team behaviour: the review and digest switches. */
  teamSettings() {
    return this.request<TeamSettings>("GET", "settings/team");
  }

  /** Partial, nested allowed; the server answers the whole state. */
  patchTeamSettings(patch: Partial<TeamSettings>) {
    return this.request<TeamSettings>("PATCH", "settings/team", patch);
  }

  /** The approval mode and the folders bots may use; older servers answer 404. */
  permissionSettings() {
    return this.request<PermissionSettings>("GET", "settings/permissions");
  }

  /** The server answers the whole state; a refused folder is a 422 with the reason. */
  patchPermissionSettings(patch: { mode?: PermissionMode; folders?: string[] }) {
    return this.request<PermissionSettings>("PATCH", "settings/permissions", patch);
  }

  /** Fires the digest now; 202 with a run id, 409 when one is already working. */
  runDigestNow() {
    return this.request<{ run_id: string }>("POST", "settings/team/digest/run", {});
  }

  /** The run plus its hand-off graph and every message it wrote, oldest first. */
  runTrace(runID: string, signal?: AbortSignal) {
    return this.request<RunTrace>("GET", `runs/${encodeURIComponent(runID)}/trace`, undefined, signal);
  }

  /** A conversation's runs, newest first. */
  conversationRuns(id: string) {
    return this.request<ConversationRuns>("GET", `conversations/${encodeURIComponent(id)}/runs`);
  }

  /** A member's all-time tallies plus one period's usage. */
  agentProfile(id: string, period: ProfilePeriod, signal?: AbortSignal) {
    return this.request<ProfileReport>(
      "GET",
      `agents/${encodeURIComponent(id)}/profile?period=${encodeURIComponent(period)}`,
      undefined,
      signal,
    );
  }

  /** One bot's pieces of work, across every conversation, newest first. */
  agentActivities(id: string, page?: { limit?: number; before?: string | null }, signal?: AbortSignal) {
    const params = new URLSearchParams();
    if (page?.limit !== undefined) params.set("limit", String(page.limit));
    if (page?.before) params.set("before", page.before);
    const query = params.toString();
    return this.request<ActivitiesPage>(
      "GET",
      `agents/${encodeURIComponent(id)}/activities${query ? `?${query}` : ""}`,
      undefined,
      signal,
    );
  }

  /** One activity with its answer and its steps, sections labelled. */
  activityDetail(id: string, signal?: AbortSignal) {
    return this.request<ActivityDetail>("GET", `activities/${encodeURIComponent(id)}`, undefined, signal);
  }

  /** Hand-set status and/or progress note; the server answers with the updated task. */
  updateTask(id: string, patch: { status?: TaskStatus; note?: string }) {
    return this.request<TaskItem>("PATCH", `tasks/${encodeURIComponent(id)}`, patch);
  }

  /**
   * One long-lived stream, read with fetch so the launch token can be sent as a header. It never
   * ends by itself, so `onClose` always means a fault the user should see: the first error closes
   * it and is reported once, with no silent reconnect.
   */
  events(handlers: {
    onEvent: (name: string, data: unknown) => void;
    onClose: (message: string) => void;
  }): () => void {
    const controller = new AbortController();
    const parser = new SSEParser();
    let closed = false;
    const close = (message?: string) => {
      if (closed) return;
      closed = true;
      controller.abort();
      if (message) handlers.onClose(message);
    };

    void (async () => {
      let response: Response;
      try {
        response = await fetch(this.url("events"), {
          headers: this.headers({ Accept: "text/event-stream" }),
          signal: controller.signal,
        });
      } catch {
        close(BROKEN_STREAM);
        return;
      }
      if (!response.ok || !response.body) {
        close(BROKEN_STREAM);
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
            if (!EVENT_NAMES[frame.event]) continue;
            let data: unknown;
            try {
              data = JSON.parse(frame.data);
            } catch {
              close("事件数据无法解析。请重新连接。");
              return;
            }
            handlers.onEvent(frame.event, data);
            if (closed) return;
          }
        }
      } catch {
        // A read that failed, or the abort from close(); either way the stream is over.
      }
      close(BROKEN_STREAM);
    })();

    return () => close();
  }
}
