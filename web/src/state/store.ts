// Single source of truth for the web client: workspace directory, the open conversation,
// per-conversation runs, one app-wide event stream, and short avatar mood windows.

import { useSyncExternalStore } from "react";
import { ApiClient, ApiError, errorMessage, loadBaseURL, normalizeBaseURL, saveBaseURL } from "../api/client";
import type {
  Activity,
  GroupTask,
  Action,
  Agent,
  ApprovalRule,
  AskAnswer,
  Attachment,
  CompactResult,
  ContextEvent,
  ContextMeter,
  ComputerState,
  Connector,
  ConnectorInput,
  ConnectorsState,
  Conversation,
  ConversationDetail,
  Delta,
  Health,
  Memory,
  MemoriesState,
  MemorySettings,
  MemoryScope,
  Message,
  ModelCaps,
  ModelOverride,
  ModelRef,
  Note,
  NoteSummary,
  PermissionMode,
  PermissionSettings,
  Provider,
  ProvidersState,
  ProviderProbe,
  ReactionUpdate,
  Run,
  Schedule,
  ScheduleInput,
  SchedulesState,
  SendMode,
  SidebarFolder,
  SidebarItem,
  SidebarLayout,
  LocalSkill,
  Skill,
  SkillDetail,
  Workflow,
  WorkflowDetail,
  WorkflowRun,
  Snapshot,
  TaskItem,
  TaskProgress,
  TaskStatus,
  TeamImportPlan,
  TeamSettings,
  TokenBudget,
  ToolPermissions,
  UsagePeriod,
  UsageReport,
  Workspace,
} from "../api/types";
import type { Appearance } from "../api/types";
import { asMoodTag } from "../bot/catalog";
import type { GestureKind } from "../bot/engine";
import { noteActivity } from "../bot/activity";
import { backendInfo, clearRemote, restartBackend as askRestartBackend, setRemote } from "../lib/desktop";
import { isDesktop } from "../lib/platform";
import type { Mood } from "../lib/mood";
import { messagePreview } from "../lib/preview";
import { PAGE_SIZE, acceptsLiveMessage, mergeNewestPage, prependOlderPage } from "../lib/pages";
import { skillUploadContentType } from "../lib/skills";
import type { ReplyTarget } from "../lib/turns";
import { hasOwnReaction, toggleOwnReaction } from "../lib/reactions";
import {
  groupThreadsFromTasks,
  mergeGroupThreadProgress,
  mergeTaskProgress,
  patchGroupThreadProgress,
} from "../lib/taskProgress";
import { isLive } from "../lib/workflow";
import { isLiveActivity } from "../lib/activities";

/** Where each task thread's group is remembered, so a restored thread tab still knows its way
 *  back before any group's tasks have been read. */
const THREAD_GROUPS_KEY = "cubes.thread-groups";

function readThreadGroups(): Record<string, string> {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(THREAD_GROUPS_KEY) ?? "{}");
    if (raw === null || typeof raw !== "object") return {};
    return Object.fromEntries(
      Object.entries(raw).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
    );
  } catch {
    return {};
  }
}

function saveThreadGroups(threadGroups: Record<string, string>) {
  try {
    localStorage.setItem(THREAD_GROUPS_KEY, JSON.stringify(threadGroups));
  } catch {
    // A full or disabled storage only costs the restored tab its label.
  }
}

/** The last `GET /usage` answer and how it was fetched; null until something reads it. */
export interface UsageState {
  period: UsagePeriod;
  report: UsageReport | null;
  loading: boolean;
  error: string | null;
}

/** One message being corrected in the composer, and the draft it took over. */
export interface EditTarget {
  messageID: string;
  /** What the composer held before the edit began: Esc puts it back. */
  previous: string;
}

export interface TeamState {
  baseURL: string;
  /** True when the address and launch token came from the desktop shell, not from settings. */
  embedded: boolean;
  /** True when that address is a server picked in 「设置 → 连接」, not the embedded backend. */
  remote: boolean;
  /** Set when the desktop shell could not start its backend; the whole window shows it. */
  embeddedFailure: { error: string; logPath: string } | null;
  /** False in the desktop shell until it has said where the backend is. Nothing may read before:
   *  the client still points at the default address, where nothing (or a stale server) answers. */
  backendSettled: boolean;
  health: Health | null;
  healthError: string | null;
  /** Every model service, and the model bots follow by default; null until the first read. */
  providers: ProvidersState | null;
  providersError: string | null;
  /** The account-wide external services every bot can use; null until the first read. */
  connectors: ConnectorsState | null;
  connectorsError: string | null;
  /** The shared computer; null until the first read. The token only comes with the GET. */
  computer: ComputerState | null;
  computerError: string | null;
  workspace: Workspace | null;
  workspaceError: string | null;
  loadingWorkspace: boolean;
  selectedID: string | null;
  /** Set when the sidebar opens a chat in order to land on one waiting message. */
  focus: { conversationID: string; messageID: string } | null;
  detail: ConversationDetail | null;
  detailError: string | null;
  streamError: string | null;
  sending: Record<string, true>;
  stopping: Record<string, true>;
  /** Per conversation: the message the next send answers. */
  replying: Record<string, ReplyTarget>;
  /** Per conversation: the message the composer is correcting, and the draft it displaced. */
  editing: Record<string, EditTarget>;
  /** Per conversation: the last compaction state the backend published. */
  contextEvents: Record<string, ContextEvent>;
  moods: Record<string, Mood>;
  /** One-shot avatar gestures, newest per member; `at` is monotonic so a face can detect it. */
  gestures: Record<string, { kind: GestureKind; at: number }>;
  notice: string | null;
  sidebar: SidebarLayout | null;
  notes: NoteSummary[] | null;
  tasks: TaskItem[] | null;
  /** Each group's tasks, newest first, by conversation id; a group is read when it opens. */
  groupTasks: Record<string, GroupTask[]>;
  /** One bot's pieces of work, by activity id: the live ones feed a group's cards and sheets. */
  activities: Record<string, Activity>;
  /** Thread details opened inline from a card, kept live from the same message events. */
  threadDetails: Record<string, ConversationDetail>;
  /** Which group a task thread belongs to, once known: the thread's way back to its group. */
  threadGroups: Record<string, string>;
  /** Every scheduled task, with the backend's default zone and its limits; null until read. */
  schedules: SchedulesState | null;
  schedulesError: string | null;
  /** Long-term memories and the two switches; null until the first read. */
  memories: MemoriesState | null;
  memoriesError: string | null;
  /** Workspace-level team behaviour (review + digest); null until the first read. */
  teamSettings: TeamSettings | null;
  teamSettingsError: string | null;
  /** The approval mode and the folders bots may use; null until the first read. */
  permissionSettings: PermissionSettings | null;
  permissionSettingsError: string | null;
  /** The remembered 「总是允许」 rules; null until the settings page reads them. */
  approvalRules: ApprovalRule[] | null;
  /** Agent skills, sorted by name; null until the first read. */
  skills: Skill[] | null;
  skillsError: string | null;
  /** Every workflow, newest edit first; null until the first read. */
  workflows: Workflow[] | null;
  workflowsError: string | null;
  /** Per chat: the walk it is serving (or just finished), live from the event stream. */
  workflowRuns: Record<string, WorkflowRun>;
  /** Set when the workbench could not be read at all; the dialog offers a retry. */
  workbenchError: string | null;
  /** Full note bodies, read on demand when the workbench opens one. */
  noteBodies: Record<string, Note>;
  /** Per-agent token usage for one period; read by the team panel and member editor. */
  usage: UsageState | null;
}

type Listener = () => void;

class TeamStore {
  private state: TeamState;
  private listeners = new Set<Listener>();
  private api: ApiClient;
  private server = 0;
  private detailToken = 0;
  /** Conversations whose older page is being read right now. */
  private olderLoads = new Set<string>();
  /** Threads whose last page is being read for an inline card. */
  private threadLoads = new Set<string>();
  private workspaceToken = 0;
  private closeStream: (() => void) | null = null;
  private moodTimers = new Map<string, number>();
  private gestureSeq = 0;
  private noteTokens = new Map<string, number>();
  private usageToken = 0;
  /** Highest sequence already reported as read per conversation, so the POST is not repeated. */
  private readMarks = new Map<string, number>();
  /** The shell is asked once per page load, before the first request goes out. */
  private adopted: Promise<boolean> | null = null;
  /** Activities whose payload is being read after a reload named them before any event did. */
  private activityLoads = new Set<string>();

  constructor() {
    const baseURL = loadBaseURL();
    this.api = new ApiClient(baseURL);
    this.state = {
      baseURL,
      embedded: false,
      remote: false,
      embeddedFailure: null,
      backendSettled: !isDesktop,
      health: null,
      healthError: null,
      providers: null,
      providersError: null,
      connectors: null,
      connectorsError: null,
      computer: null,
      computerError: null,
      workspace: null,
      workspaceError: null,
      loadingWorkspace: false,
      selectedID: null,
      focus: null,
      detail: null,
      detailError: null,
      streamError: null,
      sending: {},
      stopping: {},
      replying: {},
      editing: {},
      contextEvents: {},
      moods: {},
      gestures: {},
      notice: null,
      sidebar: null,
      notes: null,
      tasks: null,
      groupTasks: {},
      /** Every activity the stream has told us about, by id; a group's chat draws its live ones. */
      activities: {},
      threadDetails: {},
      threadGroups: readThreadGroups(),
      schedules: null,
      schedulesError: null,
      memories: null,
      memoriesError: null,
      teamSettings: null,
      teamSettingsError: null,
      permissionSettings: null,
      permissionSettingsError: null,
      approvalRules: null,
      skills: null,
      skillsError: null,
      workflows: null,
      workflowsError: null,
      workflowRuns: {},
      workbenchError: null,
      noteBodies: {},
      usage: null,
    };
  }

  // MARK: subscription

  subscribe = (listener: Listener) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getState = () => this.state;

  private set(patch: Partial<TeamState>) {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }

  get client() {
    return this.api;
  }

  /**
   * The client once the backend is settled. A view that reads on its own (a restored run tab
   * mounting before `boot` has asked the shell) must wait for this: until the shell answers, the
   * client still points at the default address, where an unrelated or stale server may answer.
   */
  async readyClient(): Promise<ApiClient> {
    await this.adoptEmbeddedBackend();
    return this.api;
  }

  // MARK: backend

  /** Validates, persists and applies a new backend address. Returns an error message or null. */
  setBaseURL(raw: string): string | null {
    if (this.state.embedded) return "桌面版的后端由应用自己启动，地址不能修改。";
    const url = normalizeBaseURL(raw);
    if (!url) return "地址无效。请使用 http:// 或 https:// 开头，并包含主机名，例如 http://127.0.0.1:8000。";
    if (url === this.state.baseURL) return null;
    saveBaseURL(url);
    this.server += 1;
    this.stopStream();
    drafts.clearAll();
    for (const timer of this.moodTimers.values()) window.clearTimeout(timer);
    this.moodTimers.clear();
    this.api = new ApiClient(url);
    this.set({
      baseURL: url,
      health: null,
      healthError: null,
      providers: null,
      providersError: null,
      connectors: null,
      connectorsError: null,
      computer: null,
      computerError: null,
      workspace: null,
      workspaceError: null,
      selectedID: null,
      detail: null,
      detailError: null,
      streamError: null,
      sending: {},
      stopping: {},
      replying: {},
      editing: {},
      contextEvents: {},
      moods: {},
      gestures: {},
      notice: null,
      sidebar: null,
      notes: null,
      tasks: null,
      schedules: null,
      schedulesError: null,
      memories: null,
      memoriesError: null,
      teamSettings: null,
      teamSettingsError: null,
      permissionSettings: null,
      permissionSettingsError: null,
      approvalRules: null,
      skills: null,
      skillsError: null,
      workflows: null,
      workflowsError: null,
      workflowRuns: {},
      workbenchError: null,
      noteBodies: {},
    });
    void this.boot();
    return null;
  }

  async boot() {
    if (!(await this.adoptEmbeddedBackend())) return;
    await Promise.all([
      this.checkHealth(),
      this.loadProviders(),
      this.loadConnectors(),
      this.loadComputer(),
      this.loadWorkspace(),
      this.loadSidebar(),
      this.loadSchedules(),
      this.loadMemories(),
      this.loadTeamSettings(),
      this.loadPermissionSettings(),
      this.loadSkills(),
      this.loadWorkflows(),
    ]);
    this.startStream();
  }

  /**
   * Inside the desktop shell the address and the launch token come from IPC, and the settings'
   * backend address is ignored; a null answer means a browser, which is unchanged.
   */
  private adoptEmbeddedBackend(): Promise<boolean> {
    // StrictMode runs the mount effect twice, and views that read on their own wait here too: all
    // share one answer and one switch of the client, or a caller could still be sent to the
    // default address.
    this.adopted ??= backendInfo().then((info) => {
      if (info && "error" in info) {
        this.set({ embeddedFailure: info, backendSettled: true });
        return false;
      }
      if (info) {
        this.api = new ApiClient(info.baseURL, info.token);
        this.set({ baseURL: info.baseURL, embedded: true, remote: info.remote, embeddedFailure: null });
      }
      this.set({ backendSettled: true });
      return true;
    });
    return this.adopted;
  }

  /** The shell's failure page asks it to start the backend again, then reloads into it. */
  async restartBackend(): Promise<void> {
    const info = await askRestartBackend();
    if (info && "baseURL" in info) {
      window.location.reload();
      return;
    }
    this.set({ embeddedFailure: info ?? this.state.embeddedFailure });
  }

  /**
   * 「设置 → 连接 → 远端服务器」: probe the address with the token before the shell stores it, so a
   * typo is reported here instead of turning into an unreachable window after the reload.
   * Returns a Chinese sentence to show, or null once the shell accepted the setting.
   */
  async connectRemote(raw: string, token: string): Promise<string | null> {
    if (!isDesktop) return null;
    const url = normalizeBaseURL(raw);
    if (!url) return "地址无效。请使用 https:// 开头，并包含主机名。";
    const secret = token.trim();
    if (!secret) return "请填写访问令牌。";
    let health: Health;
    try {
      health = await new ApiClient(url, secret).health();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return "访问令牌不对。";
      // A backend that answered with another error knows why; a dead socket or an HTML page
      // from something else in the way is usually a wrong address, CORS or TLS.
      if (error instanceof ApiError && error.status !== undefined) return error.message;
      return "连不上这个地址。请确认服务已经启动、地址正确，服务器放行了桌面端的来源。";
    }
    if (health?.status !== "ok" || typeof health.configured !== "boolean") {
      return "这个地址不是 Cubes 后端。";
    }
    try {
      await setRemote(url, secret);
    } catch (error) {
      return typeof error === "string" && error ? error : "保存远端设置失败。";
    }
    // The shell now answers with the server on every start: reload into it rather than patching
    // the client in place, so the window and the saved setting can never disagree.
    window.location.reload();
    return null;
  }

  /** 「改回本机」: drop the server setting, start the embedded backend and reload into it. */
  async useLocalBackend(): Promise<void> {
    if (!isDesktop) return;
    try {
      await clearRemote();
    } catch {
      this.set({ notice: "改回本机失败，请重试。" });
      return;
    }
    window.location.reload();
  }

  async checkHealth() {
    const server = this.server;
    try {
      const health = await this.api.health();
      if (server === this.server) this.set({ health, healthError: null });
    } catch (error) {
      if (server === this.server) this.set({ health: null, healthError: errorMessage(error) });
    }
  }

  /** The model services and the default pick; provider edits push a workspace event instead. */
  async loadProviders() {
    const server = this.server;
    try {
      const providers = await this.api.providers();
      if (server === this.server) this.set({ providers, providersError: null });
    } catch (error) {
      if (server === this.server) this.set({ providersError: errorMessage(error) });
    }
  }

  /** Every connector and the presets the add dialog offers; status rides on the stream after. */
  async loadConnectors() {
    const server = this.server;
    try {
      const connectors = await this.api.connectors();
      if (server === this.server) this.set({ connectors, connectorsError: null });
    } catch (error) {
      if (server === this.server) this.set({ connectorsError: errorMessage(error) });
    }
  }

  /** The shared computer and its pairing token; live status rides on the stream after. */
  async loadComputer() {
    const server = this.server;
    try {
      const computer = await this.api.computer();
      if (server === this.server) this.set({ computer, computerError: null });
    } catch (error) {
      if (server === this.server) this.set({ computerError: errorMessage(error) });
    }
  }

  /** Switches whether bots may use the local sandbox, the cloud computer, or either. */
  async setComputerTarget(target: "local" | "cloud" | "mixed"): Promise<string | null> {
    const server = this.server;
    try {
      const next = await this.api.setComputerTarget(target);
      if (server !== this.server) return null;
      const token = this.state.computer?.token;
      this.set({ computer: token ? { ...next, token } : next, computerError: null });
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }


  /** Stops the run using the computer; its own run event follows on the stream. */
  async stopComputer(): Promise<string | null> {
    try {
      await this.api.stopComputer();
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  async loadWorkspace() {
    const server = this.server;
    const token = ++this.workspaceToken;
    this.set({ loadingWorkspace: true });
    try {
      const workspace = await this.api.workspace();
      if (server !== this.server || token !== this.workspaceToken) return;
      this.applyWorkspace(workspace);
      this.set({ workspaceError: null, loadingWorkspace: false });
    } catch (error) {
      if (server !== this.server || token !== this.workspaceToken) return;
      this.set({ workspaceError: errorMessage(error), loadingWorkspace: false });
    }
  }

  private applyWorkspace(incoming: Workspace) {
    const detail = this.state.detail;
    // The chat on screen is read, whatever the server says: a workspace fetched as a run ends can
    // answer before this window's read marker landed, and would put the dot back on the open chat
    // and list its run under 已完成.
    const open = detail?.conversation.id;
    const workspace =
      open === undefined
        ? incoming
        : {
            ...incoming,
            conversations: incoming.conversations.map((c) => (c.id === open ? { ...c, unread: false } : c)),
            finished_runs: (incoming.finished_runs ?? []).filter((run) => run.conversation_id !== open),
          };
    const conversation = detail ? workspace.conversations.find((c) => c.id === detail.conversation.id) : undefined;
    // A dissolved group takes its task threads with it; their remembered links go too.
    const threadGroups = this.pruneThreadGroups(
      new Set(workspace.conversations.filter((c) => c.kind === "group").map((c) => c.id)),
    );
    // First sight of a member, in any window: it glances around and settles.
    const known = this.state.workspace;
    this.set({ workspace, threadGroups, detail: detail && conversation ? { ...detail, conversation } : detail });
    if (!known) {
      for (const agent of workspace.agents) this.gesture(agent.id, "wake");
      return;
    }
    const seen = new Set(known.agents.map((agent) => agent.id));
    for (const agent of workspace.agents) if (!seen.has(agent.id)) this.gesture(agent.id, "wake");
  }

  /** Forget thread links whose group is no longer in the directory; returns what stayed. */
  private pruneThreadGroups(groupIDs: ReadonlySet<string>): Record<string, string> {
    const current = this.state.threadGroups;
    const kept = Object.fromEntries(Object.entries(current).filter(([, groupID]) => groupIDs.has(groupID)));
    if (Object.keys(kept).length === Object.keys(current).length) return current;
    saveThreadGroups(kept);
    return kept;
  }

  /** Fire a one-shot gesture on a member's face. `at` only ever grows, so no event is missed. */
  gesture(agentID: string, kind: GestureKind) {
    this.gestureSeq += 1;
    this.set({
      gestures: {
        ...this.state.gestures,
        [agentID]: { kind, at: performance.now() + this.gestureSeq / 1000 },
      },
    });
  }

  // MARK: sidebar layout

  async loadSidebar() {
    const server = this.server;
    try {
      const sidebar = await this.api.sidebar();
      if (server === this.server) this.set({ sidebar });
    } catch {
      // No layout endpoint (or a failed read): the rail falls back to a flat recency list.
      if (server === this.server) this.set({ sidebar: null });
    }
  }

  // MARK: conversation

  async open(conversationID: string) {
    if (this.state.selectedID === conversationID && this.state.detail) {
      // Coming back to the chat that stayed selected under a tool tab: what arrived meanwhile
      // is on screen now, so it is read now.
      this.markRead(conversationID, this.state.detail.messages[this.state.detail.messages.length - 1]?.seq ?? 0);
      return;
    }
    const server = this.server;
    const token = ++this.detailToken;
    this.set({ selectedID: conversationID, detail: null, detailError: null });
    try {
      const detail = await this.api.conversation(conversationID, { limit: PAGE_SIZE });
      if (server !== this.server || token !== this.detailToken) return;
      this.set({ detail });
      this.markRead(conversationID, detail.messages[detail.messages.length - 1]?.seq ?? 0);
    } catch (error) {
      if (server !== this.server || token !== this.detailToken) return;
      this.set({ detailError: errorMessage(error) });
    }
  }

  async reloadSelected() {
    const id = this.state.selectedID;
    if (!id) return;
    this.set({ detailError: null });
    await this.refreshDetail(id);
  }

  /**
   * Read the page just above the loaded window and put it in front. Returns the message to
   * show on failure, or null. At most one such read runs per conversation.
   */
  async loadOlder(conversationID: string): Promise<string | null> {
    const detail = this.state.detail;
    const before = detail?.conversation.id === conversationID ? detail.messages[0]?.seq : undefined;
    if (before === undefined || !detail?.has_more || this.olderLoads.has(conversationID)) return null;
    const server = this.server;
    this.olderLoads.add(conversationID);
    try {
      const page = await this.api.conversation(conversationID, { limit: PAGE_SIZE, before });
      if (server !== this.server || this.state.selectedID !== conversationID) return null;
      const current = this.state.detail;
      // A reload since the request started moved the window: this page no longer fits it.
      if (!current || current.conversation.id !== conversationID || current.messages[0]?.seq !== before) {
        return null;
      }
      this.set({ detail: { ...current, ...prependOlderPage(current, page) } });
      return null;
    } catch (error) {
      if (server !== this.server) return null;
      return errorMessage(error);
    } finally {
      this.olderLoads.delete(conversationID);
    }
  }

  closeConversation() {
    this.detailToken += 1;
    this.set({ selectedID: null, detail: null, detailError: null });
  }

  /** Tell the server (and this window) that a chat has been read through `seq`. */
  private markRead(conversationID: string, seq: number) {
    if (seq <= 0) return;
    if ((this.readMarks.get(conversationID) ?? 0) >= seq) return;
    this.readMarks.set(conversationID, seq);
    const workspace = this.state.workspace;
    const target = workspace?.conversations.find((item) => item.id === conversationID);
    if (workspace && target?.unread) {
      this.set({
        workspace: {
          ...workspace,
          conversations: workspace.conversations.map((item) =>
            item.id === conversationID ? { ...item, unread: false } : item,
          ),
        },
      });
    }
    void this.api.markConversationRead(conversationID, seq).catch(() => {
      // A failed marker only means the dot may come back on the next workspace read.
    });
  }

  /** Mark a reply as the message the next send answers, in that one conversation. */
  startReply(conversationID: string, target: ReplyTarget) {
    this.set({ replying: { ...this.state.replying, [conversationID]: target } });
  }

  cancelReply(conversationID: string) {
    if (!this.state.replying[conversationID]) return;
    const replying = { ...this.state.replying };
    delete replying[conversationID];
    this.set({ replying });
  }

  /** Put one of your own messages in the composer for correction, in that conversation. */
  startEdit(conversationID: string, messageID: string) {
    this.set({
      editing: {
        ...this.state.editing,
        [conversationID]: { messageID, previous: drafts.get(conversationID) },
      },
      // An edit answers nothing else: a reply target would silently ride on the new run.
      replying: Object.fromEntries(
        Object.entries(this.state.replying).filter(([id]) => id !== conversationID),
      ),
    });
  }

  /** Leave edit mode and put back the draft that was there before it. */
  cancelEdit(conversationID: string) {
    const target = this.state.editing[conversationID];
    if (!target) return;
    const editing = { ...this.state.editing };
    delete editing[conversationID];
    this.set({ editing });
    drafts.set(conversationID, target.previous);
  }

  private finishEdit(conversationID: string) {
    if (!this.state.editing[conversationID]) return;
    const editing = { ...this.state.editing };
    delete editing[conversationID];
    this.set({ editing });
  }

  /**
   * Send the corrected text for one of your own messages: the server drops the old row, the
   * whole tail after it and answers the new text with a fresh run. Returns the message to
   * show, or null when accepted; a failure keeps the composer as it was.
   */
  async editMessage(conversationID: string, messageID: string, content: string): Promise<string | null> {
    const text = content.trim();
    if (!text) return "先把这条消息改好再发。";
    if (this.state.sending[conversationID]) return "正在发送，请稍候。";
    const server = this.server;
    this.set({ sending: { ...this.state.sending, [conversationID]: true } });
    try {
      const run = await this.api.editMessage(conversationID, messageID, text);
      if (server !== this.server) return "后端地址已更改，请重新发送。";
      this.applyRun(run);
      drafts.clear(conversationID);
      this.finishEdit(conversationID);
      if (this.state.selectedID === conversationID) await this.refreshDetail(conversationID);
      return null;
    } catch (error) {
      if (server !== this.server) return "后端地址已更改，请重新发送。";
      if (error instanceof ApiError && error.status === 409) void this.loadWorkspace();
      return errorMessage(error);
    } finally {
      if (server === this.server) {
        const sending = { ...this.state.sending };
        delete sending[conversationID];
        this.set({ sending });
      }
    }
  }

  /**
   * Delete everything said after one message. Returns the message to show, or null once the
   * conversation has been replaced with what is left.
   */
  async rewind(conversationID: string, messageID: string): Promise<string | null> {
    const server = this.server;
    try {
      const detail = await this.api.rewindMessage(conversationID, messageID);
      if (server !== this.server) return null;
      if (this.state.selectedID === conversationID) this.set({ detail, detailError: null });
      // A reply bookmarked on a row that just went has nothing left to answer.
      const replyTarget = this.state.replying[conversationID];
      if (replyTarget && !detail.messages.some((message) => message.id === replyTarget.messageID)) {
        this.cancelReply(conversationID);
      }
      await this.loadWorkspace();
      return null;
    } catch (error) {
      if (server !== this.server) return null;
      if (error instanceof ApiError && error.status === 409) void this.loadWorkspace();
      return errorMessage(error);
    }
  }

  /**
   * Returns null when accepted, otherwise the message to show. The draft is kept on failure.
   * ``mode`` decides what a send does while a reply is already running here: join it
   * (``steer``, the default), wait for it (``queue``) or stop it (``interrupt``).
   */
  async send(
    conversationID: string,
    content: string,
    mentions: string[],
    replyToID?: string | null,
    attachmentIDs: string[] = [],
    mode?: SendMode,
  ): Promise<string | null> {
    const text = content.trim();
    if (!text && attachmentIDs.length === 0) return "请先写下要说的话。";
    if (this.state.sending[conversationID]) return "正在发送，请稍候。";
    const server = this.server;
    this.set({ sending: { ...this.state.sending, [conversationID]: true } });
    try {
      const run = await this.api.startRun(conversationID, text, mentions, replyToID, attachmentIDs, mode);
      if (server !== this.server) return "后端地址已更改，请重新发送。";
      this.applyRun(run);
      drafts.clear(conversationID);
      this.cancelReply(conversationID);
      // The user's own message is written by the server with the run; read it back.
      if (this.state.selectedID === conversationID) await this.refreshDetail(conversationID);
      return null;
    } catch (error) {
      if (server !== this.server) return "后端地址已更改，请重新发送。";
      if (error instanceof ApiError && error.status === 409) void this.loadWorkspace();
      return errorMessage(error);
    } finally {
      if (server === this.server) {
        const sending = { ...this.state.sending };
        delete sending[conversationID];
        this.set({ sending });
      }
    }
  }

  /** Re-read the newest page; rows already scrolled back to stay in front of it. */
  private async refreshDetail(conversationID: string) {
    const server = this.server;
    const token = ++this.detailToken;
    try {
      const page = await this.api.conversation(conversationID, { limit: PAGE_SIZE });
      if (server !== this.server || token !== this.detailToken || this.state.selectedID !== conversationID) return;
      const current = this.state.detail;
      if (current && current.conversation.id === conversationID) {
        this.set({ detail: { ...page, ...mergeNewestPage(current, page) } });
      } else {
        this.set({ detail: page });
      }
      this.markRead(conversationID, page.messages[page.messages.length - 1]?.seq ?? 0);
    } catch (error) {
      if (server === this.server && token === this.detailToken) this.set({ detailError: errorMessage(error) });
    }
  }

  async stop(conversationID: string) {
    const run = this.runFor(conversationID);
    if (!run || this.state.stopping[conversationID]) return;
    const server = this.server;
    this.set({ stopping: { ...this.state.stopping, [conversationID]: true } });
    try {
      const stopped = await this.api.stopRun(run.id);
      if (server !== this.server) return;
      this.applyRun(stopped);
      // Frames for the stopped run may never arrive: re-read what is on screen.
      if (this.state.selectedID) await this.refreshDetail(this.state.selectedID);
    } catch (error) {
      if (server === this.server) this.set({ notice: `无法停止：${errorMessage(error)}` });
    } finally {
      if (server === this.server) {
        const stopping = { ...this.state.stopping };
        delete stopping[conversationID];
        this.set({ stopping });
      }
    }
  }

  // MARK: team

  /** The team as one Markdown file: the caller downloads it. */
  async exportTeam(): Promise<{ text: string } | { error: string }> {
    try {
      return { text: await this.api.exportTeam() };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }

  /** Preview or apply a team file; an apply refreshes the directory and the workflow list. */
  async importTeam(
    markdown: string,
    options: { apply?: boolean; overwriteSupervisor?: boolean } = {},
  ): Promise<{ plan: TeamImportPlan } | { error: string }> {
    try {
      const plan = await this.api.importTeam(markdown, options);
      if (options.apply) {
        await Promise.all([this.loadWorkspace(), this.loadWorkflows()]);
      }
      return { plan };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }

  /** Name the supervisor and keep the look picked first; the shell appears on the reply. */
  async completeOnboarding(name: string, appearance: Appearance): Promise<string | null> {
    try {
      const workspace = await this.api.completeOnboarding(name.trim(), appearance);
      this.applyWorkspace(workspace);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Adds a member bot; answers its id, or the server's reason. */
  async createMember(name: string, instructions: string): Promise<{ id?: string; error?: string }> {
    try {
      const agent = await this.api.createAgent(name.trim(), instructions.trim());
      await this.loadWorkspace();
      return { id: agent.id };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }

  async patchAgent(
    id: string,
    patch: {
      name?: string;
      instructions?: string;
      appearance?: Appearance | null;
      /** Null sends the bot back to the default; undefined leaves its pick alone. */
      modelRef?: ModelRef | null;
      /** Object to set a cap, null to clear it; undefined leaves the stored budget alone. */
      tokenBudget?: TokenBudget | null;
      /** Object to set the four packs, null to allow everything; undefined leaves them alone. */
      toolPermissions?: ToolPermissions | null;
      /** Null follows the team setting; undefined leaves the stored pick alone. */
      sendMode?: SendMode | null;
      /** Null follows the workspace's approval mode; undefined leaves the stored pick alone. */
      permissionMode?: PermissionMode | null;
    },
  ): Promise<string | null> {
    try {
      await this.api.patchAgent(id, {
        ...(patch.name === undefined ? {} : { name: patch.name.trim() }),
        ...(patch.instructions === undefined ? {} : { instructions: patch.instructions.trim() }),
        ...(patch.appearance === undefined ? {} : { appearance: patch.appearance }),
        ...(patch.modelRef === undefined ? {} : { model_ref: patch.modelRef }),
        ...(patch.tokenBudget === undefined ? {} : { token_budget: patch.tokenBudget }),
        ...(patch.toolPermissions === undefined ? {} : { tool_permissions: patch.toolPermissions }),
        ...(patch.sendMode === undefined ? {} : { send_mode: patch.sendMode }),
        ...(patch.permissionMode === undefined ? {} : { permission_mode: patch.permissionMode }),
      });
      await this.loadWorkspace();
      if (patch.tokenBudget !== undefined && this.state.usage) void this.loadUsage(this.state.usage.period);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Reads per-agent usage for one period; the latest call wins, stale answers are dropped. */
  async loadUsage(period: UsagePeriod): Promise<void> {
    const server = this.server;
    const token = ++this.usageToken;
    this.set({ usage: { period, report: this.state.usage?.report ?? null, loading: true, error: null } });
    try {
      const report = await this.api.usage(period);
      if (server === this.server && token === this.usageToken) {
        this.set({ usage: { period, report, loading: false, error: null } });
      }
    } catch (error) {
      if (server === this.server && token === this.usageToken) {
        this.set({ usage: { period, report: null, loading: false, error: errorMessage(error) } });
      }
    }
  }

  // MARK: model services

  /**
   * Creates or updates one provider and re-reads the list. `apiKey` is only sent when the user
   * typed one, so editing a card never clears the stored key.
   */
  async saveProvider(
    input: {
      name: string;
      protocol: Provider["protocol"];
      baseUrl: string;
      models: string[];
      /** The user's per-model answers; `null` fields are left to detection. */
      modelOverrides: Record<string, ModelOverride>;
      apiKey?: string;
    },
    id?: string,
  ): Promise<string | null> {
    const server = this.server;
    const body = {
      name: input.name.trim(),
      protocol: input.protocol,
      base_url: input.baseUrl.trim(),
      models: input.models,
      model_overrides: input.modelOverrides,
      ...(input.apiKey === undefined ? {} : { api_key: input.apiKey }),
    };
    try {
      if (id) await this.api.updateProvider(id, body);
      else await this.api.createProvider(body);
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) await Promise.all([this.loadProviders(), this.loadWorkspace()]);
    return null;
  }

  async deleteProvider(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.deleteProvider(id);
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) await Promise.all([this.loadProviders(), this.loadWorkspace()]);
    return null;
  }

  /** The workspace default: every bot that follows it answers with this model. */
  async setDefaultModel(ref: ModelRef): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.setDefaultModel(ref);
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) {
      await Promise.all([this.loadProviders(), this.loadWorkspace(), this.checkHealth()]);
    }
    return null;
  }

  /** The optional workspace backup: a failed call retries here before the turn ends. */
  async setFallbackModel(ref: ModelRef | null): Promise<string | null> {
    const server = this.server;
    try {
      const providers = await this.api.setFallbackModel(ref);
      if (server === this.server) this.set({ providers, providersError: null });
    } catch (error) {
      return errorMessage(error);
    }
    return null;
  }

  // MARK: connectors

  /**
   * Creates or updates one connector and re-reads the list. The request can take a minute while
   * the server connects and lists tools, so the form stays open and shows its own busy state.
   */
  async saveConnector(input: ConnectorInput, id?: string): Promise<string | null> {
    const server = this.server;
    try {
      if (id) await this.api.updateConnector(id, input);
      else await this.api.createConnector(input);
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) await this.loadConnectors();
    return null;
  }

  async deleteConnector(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.deleteConnector(id);
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) await this.loadConnectors();
    return null;
  }

  /** The enable switch. Turning one on verifies the connection first, so it can be slow. */
  async setConnectorEnabled(id: string, enabled: boolean): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.updateConnector(id, { enabled });
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) await this.loadConnectors();
    return null;
  }

  /** Tries the stored settings again; a failure answers 200 and arrives on the list instead. */
  async reconnectConnector(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.reconnectConnector(id);
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) await this.loadConnectors();
    return null;
  }

  /** A probe makes no change to the store, so a failure is the caller's to show. */
  probeProvider(form: {
    id?: string | null;
    protocol: Provider["protocol"];
    base_url: string;
    api_key?: string | null;
    model: string;
  }): Promise<ProviderProbe> {
    return this.api.testProvider(form);
  }

  /** The provider's own model list, for the form's "fetch models" button. */
  providerModels(form: {
    id?: string | null;
    protocol: Provider["protocol"];
    base_url: string;
    api_key?: string | null;
  }): Promise<{ models: string[]; caps: Record<string, ModelCaps> }> {
    return this.api.fetchModels(form).then((body) => ({ models: body.models ?? [], caps: body.caps ?? {} }));
  }

  /** Upload one prepared attachment; the composer keeps the returned id until it sends. */
  uploadAttachment(file: Blob, name = ""): Promise<Attachment> {
    return this.api.uploadAttachment(file, name);
  }

  /** The file behind one attachment, fetched with the launch token. */
  attachmentBlob(id: string): Promise<Blob> {
    return this.api.attachmentBlob(id);
  }

  /** Opens a new group; answers its id, or the server's reason. */
  async createGroup(body: {
    title: string;
    purpose: string;
    member_ids: string[];
    rules?: string;
    include_supervisor?: boolean;
  }): Promise<{ id?: string; error?: string }> {
    try {
      const group = await this.api.createGroup(body);
      await this.loadWorkspace();
      return { id: group.id };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }

  async patchGroup(id: string, patch: { title?: string; purpose?: string; member_ids?: string[]; rules?: string }): Promise<string | null> {
    try {
      await this.api.patchConversation(id, patch);
      await this.loadWorkspace();
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /**
   * Removes a member and everything that hung off it. The caller decides where to stand
   * afterwards: the conversations it owned are gone from the directory once this returns.
   */
  async deleteAgent(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.deleteAgent(id);
      if (server !== this.server) return null;
      await this.loadWorkspace();
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /**
   * Dissolves a group and everything that belonged only to it. The caller decides where to
   * stand afterwards: the group is gone from the directory once this returns.
   */
  async dissolveGroup(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.dissolveGroup(id);
      if (server !== this.server) return null;
      await this.loadWorkspace();
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Open this chat and, once its messages are on screen, scroll to this one. */
  focusMessage(conversationID: string, messageID: string) {
    this.set({ focus: { conversationID, messageID } });
  }

  clearFocus() {
    if (this.state.focus) this.set({ focus: null });
  }


  /** Answers an awaiting action. SSE finishes the row; the reply updates it right away. */
  async confirmAction(actionID: string, approve: boolean, always = false): Promise<string | null> {
    const server = this.server;
    try {
      const action = await this.api.confirmAction(actionID, approve, always);
      if (always) void this.loadApprovalRules();
      if (server === this.server) this.applyAction(action);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Answers an `ask_user` call; SSE finishes the row, the reply updates it right away. */
  async answerAction(
    actionID: string,
    answer: { answers: AskAnswer[]; skip: boolean },
  ): Promise<string | null> {
    const server = this.server;
    try {
      const action = await this.api.answerAction(actionID, answer);
      if (server === this.server) this.applyAction(action);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Read the remembered rules; the settings page and the card's third button use them. */
  async loadApprovalRules(): Promise<void> {
    const server = this.server;
    try {
      const response = await this.api.approvalRules();
      if (server === this.server) this.set({ approvalRules: response.rules });
    } catch {
      if (server === this.server) this.set({ approvalRules: [] });
    }
  }

  async deleteApprovalRule(ruleID: string): Promise<string | null> {
    try {
      await this.api.deleteApprovalRule(ruleID);
      const rules = this.state.approvalRules;
      if (rules) this.set({ approvalRules: rules.filter((rule) => rule.id !== ruleID) });
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** The composer's meter; a failure is the caller's to show, so errors are rethrown. */
  conversationContext(conversationID: string): Promise<ContextMeter> {
    return this.api.conversationContext(conversationID);
  }

  /** Run the compaction now; answers the new meter and whether anything was folded. */
  compactConversation(conversationID: string): Promise<CompactResult> {
    return this.api.compactConversation(conversationID);
  }

  /** Opens (creating if needed) a member's direct chat; returns its id or an error. */
  async openDirect(agentID: string): Promise<{ id: string } | { error: string }> {
    try {
      const conversation = await this.api.openDirect(agentID);
      await this.loadWorkspace();
      return { id: conversation.id };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  }

  /** Take back a send still waiting for its turn; the row goes from the chat at once. */
  async cancelQueued(runID: string, conversationID: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.cancelQueuedRun(runID);
      if (server !== this.server) return null;
      const detail = this.state.detail;
      if (detail && detail.conversation.id === conversationID) {
        this.set({ detail: { ...detail, messages: detail.messages.filter((m) => m.run_id !== runID) } });
      }
      await this.loadWorkspace();
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  dismissNotice() {
    this.set({ notice: null });
  }

  /** Show the app-wide banner with one sentence; the next notice replaces it. */
  /** Reads one group's tasks; the stream keeps them current afterwards. */
  async loadGroupTasks(id: string): Promise<void> {
    try {
      const { tasks } = await this.api.groupTasks(id);
      this.setGroupTasks(id, tasks);
    } catch {
      // A missing list only hides the task strip; the chat itself still works.
    }
  }

  /**
   * One activity the chat named before its event arrived (a reload reads the rows first). The
   * stream's own payload wins when it lands while this is in flight, so an older read is dropped.
   */
  async loadActivity(id: string): Promise<void> {
    if (this.state.activities[id] || this.activityLoads.has(id)) return;
    this.activityLoads.add(id);
    try {
      const { activity } = await this.api.activityDetail(id);
      if (!this.state.activities[id]) {
        this.set({ activities: { ...this.state.activities, [activity.id]: activity } });
      }
    } catch {
      // A record that is gone stays a quiet card; the chat itself still works.
    } finally {
      this.activityLoads.delete(id);
    }
  }

  /**
   * The thread's last page, read once when a card is opened inline or waits on an action. The
   * stream's message events keep it current afterwards; an already-read thread is not re-read.
   */
  async loadThreadDetail(id: string): Promise<void> {
    if (this.state.threadDetails[id] || this.threadLoads.has(id)) return;
    this.threadLoads.add(id);
    const server = this.server;
    try {
      const detail = await this.api.conversation(id, { limit: 40 });
      if (server !== this.server) return;
      this.set({ threadDetails: { ...this.state.threadDetails, [id]: detail } });
    } catch {
      // A card that cannot read its thread still shows the live line from `task_progress`.
    } finally {
      this.threadLoads.delete(id);
    }
  }

  /** Stops one task's thread run, or takes a queued one out of the line. */
  async stopTask(groupID: string, taskID: string): Promise<string | null> {
    try {
      const body = await this.api.stopTask(groupID, taskID);
      // The server answers the group's rows; a bare task still needs no local guess.
      if (Array.isArray(body.tasks)) this.setGroupTasks(groupID, body.tasks);
      else void this.loadGroupTasks(groupID);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Folds a queued task into its owner's running thread as a steer message. */
  async mergeTask(groupID: string, taskID: string): Promise<string | null> {
    try {
      const body = await this.api.mergeTask(groupID, taskID);
      if (Array.isArray(body.tasks)) this.setGroupTasks(groupID, body.tasks);
      else void this.loadGroupTasks(groupID);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Picks one stopped or failed task up again, in its own thread. */
  async resumeTask(groupID: string, taskID: string): Promise<string | null> {
    try {
      const body = await this.api.resumeTask(groupID, taskID);
      if (Array.isArray(body.tasks)) this.setGroupTasks(groupID, body.tasks);
      else void this.loadGroupTasks(groupID);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Puts a stopped or failed task down; the card stays in the timeline, greyed. */
  async dismissTask(groupID: string, taskID: string): Promise<string | null> {
    try {
      const body = await this.api.dismissTask(groupID, taskID);
      if (Array.isArray(body.tasks)) this.setGroupTasks(groupID, body.tasks);
      else void this.loadGroupTasks(groupID);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /**
   * Re-answers one user message with the speakers the user picked. No new user message is
   * written; the run that message started is stopped server-side when it is still going.
   */
  async rerouteMessage(conversationID: string, messageID: string, speakers: string[]): Promise<string | null> {
    try {
      await this.api.rerouteMessage(conversationID, messageID, speakers);
      if (this.state.selectedID === conversationID) await this.refreshDetail(conversationID);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /**
   * Confirms a notice's 「拉进群」: adds the agent, then the invited bot answers the message
   * that @-ed them. Refreshes this chat so the button and the new reply both show.
   */
  async inviteMember(
    conversationID: string,
    agentID: string,
    messageID: string | null,
    noticeID: string,
  ): Promise<string | null> {
    try {
      await this.api.inviteMember(conversationID, agentID, messageID, noticeID);
      if (this.state.selectedID === conversationID) await this.refreshDetail(conversationID);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** A done task's result reached the viewport: drop it from the sidebar, once. */
  async markTaskSeen(groupID: string, taskID: string): Promise<void> {
    const tasks = this.state.groupTasks[groupID];
    const task = tasks?.find((row) => row.id === taskID);
    if (!tasks || !task || task.status !== "done" || task.seen_at) return;
    this.setGroupTasks(
      groupID,
      tasks.map((row) => (row.id === taskID ? { ...row, seen_at: new Date().toISOString() } : row)),
    );
    try {
      await this.api.markTaskSeen(groupID, taskID);
    } catch {
      // The sidebar hint only: the timeline and the card stay right either way.
    }
  }

  /** Store one group's tasks, remembering which group each task's thread belongs to. */
  private setGroupTasks(id: string, tasks: GroupTask[]) {
    const threadGroups = { ...this.state.threadGroups };
    for (const task of tasks) if (task.thread_id) threadGroups[task.thread_id] = id;
    const workspace = this.state.workspace;
    this.set({
      groupTasks: { ...this.state.groupTasks, [id]: tasks },
      threadGroups,
      // The sidebar's live lines follow the same rows the cards read, so both stay in step.
      ...(workspace
        ? { workspace: { ...workspace, group_threads: groupThreadsFromTasks(workspace.group_threads, id, tasks) } }
        : {}),
    });
    saveThreadGroups(threadGroups);
  }

  /**
   * Find the group a thread belongs to, for its page's way back. Loaded tasks are searched first;
   * a thread opened before its group (a restored tab) asks the groups one by one until one owns
   * it. A group with no thread rows leaves nothing behind but its tasks.
   */
  async locateThread(threadID: string): Promise<void> {
    if (this.state.threadGroups[threadID]) return;
    const server = this.server;
    const groups = (this.state.workspace?.conversations ?? []).filter((c) => c.kind === "group");
    for (const group of groups) {
      let tasks = this.state.groupTasks[group.id];
      if (!tasks) {
        try {
          tasks = (await this.api.groupTasks(group.id)).tasks;
        } catch {
          continue;
        }
        if (server !== this.server) return;
        this.setGroupTasks(group.id, tasks);
      }
      if (tasks.some((task) => task.thread_id === threadID)) {
        const threadGroups = { ...this.state.threadGroups, [threadID]: group.id };
        this.set({ threadGroups });
        saveThreadGroups(threadGroups);
        return;
      }
    }
  }

  notify(message: string) {
    this.set({ notice: message });
  }


  // MARK: sidebar actions

  /** Optimistic whole-layout write; a failed PUT restores the layout that was on screen. */
  private async saveSidebar(next: SidebarLayout): Promise<string | null> {
    const server = this.server;
    const previous = this.state.sidebar;
    this.set({ sidebar: next });
    try {
      const saved = await this.api.putSidebar({
        items: orderedItems(next).map((item) => ({
          conversation_id: item.conversation_id,
          pinned: item.pinned,
          folder_id: item.folder_id,
        })),
        folder_ids: next.folders.map((folder) => folder.id),
      });
      if (server !== this.server) return null;
      this.set({ sidebar: saved });
      return null;
    } catch (error) {
      if (server !== this.server) return null;
      const message = errorMessage(error);
      this.set({ sidebar: previous, notice: `侧栏布局没有保存：${message}` });
      return message;
    }
  }

  /** New pins go to the end of the pinned row. */
  pin(conversationID: string) {
    const layout = this.state.sidebar ?? { folders: [], items: [] };
    const pinPosition = 1 + Math.max(0, ...layout.items.filter((i) => i.pinned).map((i) => i.pin_position ?? 0));
    const entry: SidebarItem = { conversation_id: conversationID, pinned: true, pin_position: pinPosition, folder_id: null, position: 0 };
    const items = layout.items.some((i) => i.conversation_id === conversationID)
      ? layout.items.map((i) => (i.conversation_id === conversationID ? { ...i, pinned: true, pin_position: pinPosition } : i))
      : [...layout.items, entry];
    void this.saveSidebar({ ...layout, items });
  }

  unpin(conversationID: string) {
    const layout = this.state.sidebar;
    if (!layout) return;
    const items = layout.items.map((i) =>
      i.conversation_id === conversationID ? { ...i, pinned: false, pin_position: null } : i,
    );
    void this.saveSidebar({ ...layout, items });
  }

  /** Moving also clears the pin: the entry has to land where the user pointed. */
  moveToFolder(conversationID: string, folderID: string | null) {
    const layout = this.state.sidebar ?? { folders: [], items: [] };
    const position = 1 + Math.max(0, ...layout.items.filter((i) => i.folder_id === folderID).map((i) => i.position ?? 0));
    const entry: SidebarItem = { conversation_id: conversationID, pinned: false, pin_position: null, folder_id: folderID, position };
    const items = layout.items.some((i) => i.conversation_id === conversationID)
      ? layout.items.map((i) => (i.conversation_id === conversationID ? entry : i))
      : [...layout.items, entry];
    void this.saveSidebar({ ...layout, items });
  }

  async createFolder(name: string): Promise<SidebarFolder | null> {
    const trimmed = name.trim();
    if (!trimmed) return null;
    const known = new Set(this.state.sidebar?.folders.map((folder) => folder.id) ?? []);
    try {
      const layout = await this.api.createFolder(trimmed);
      this.set({ sidebar: layout });
      return layout.folders.find((folder) => !known.has(folder.id)) ?? null;
    } catch (error) {
      this.set({ notice: `新建分组失败：${errorMessage(error)}` });
      return null;
    }
  }

  /** Returns null on success, otherwise the message already shown in the banner. */
  async renameFolder(id: string, name: string): Promise<string | null> {
    const trimmed = name.trim();
    if (!trimmed) return null;
    try {
      const layout = await this.api.renameFolder(id, trimmed);
      this.set({ sidebar: layout });
      return null;
    } catch (error) {
      const message = errorMessage(error);
      this.set({ notice: `重命名分组失败：${message}` });
      return message;
    }
  }

  async deleteFolder(id: string): Promise<string | null> {
    try {
      const layout = await this.api.deleteFolder(id);
      this.set({ sidebar: layout });
      return null;
    } catch (error) {
      const message = errorMessage(error);
      this.set({ notice: `删除分组失败：${message}` });
      return message;
    }
  }

  // MARK: workbench

  /** Reads notes and tasks once; live updates arrive on the event stream afterwards. */
  async loadWorkbench() {
    const server = this.server;
    let failure: string | null = null;
    try {
      const body = await this.api.notes();
      if (server === this.server) this.set({ notes: body.notes ?? [] });
    } catch (error) {
      failure = errorMessage(error);
    }
    try {
      const body = await this.api.tasks();
      if (server === this.server) this.set({ tasks: body.tasks ?? [] });
    } catch (error) {
      failure ??= errorMessage(error);
    }
    if (server === this.server) this.set({ workbenchError: failure });
  }

  // MARK: schedules

  /** Every rule, the backend's default zone and its limits; changes then ride the stream. */
  async loadSchedules() {
    const server = this.server;
    try {
      const schedules = await this.api.schedules();
      if (server === this.server) this.set({ schedules, schedulesError: null });
    } catch (error) {
      if (server === this.server) this.set({ schedulesError: errorMessage(error) });
    }
  }

  /** Creates one; the panel closes only when null comes back. */
  async createSchedule(body: ScheduleInput): Promise<string | null> {
    try {
      await this.api.createSchedule(body);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Partial edit, e.g. just `enabled` for the switch. The stream pushes the new list after. */
  async updateSchedule(id: string, patch: ScheduleInput): Promise<string | null> {
    try {
      await this.api.updateSchedule(id, patch);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Runs it now, or queues it while its conversation is busy. */
  async runSchedule(id: string): Promise<string | null> {
    try {
      await this.api.runSchedule(id);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Stops the run a schedule is working on now. */
  async stopSchedule(id: string): Promise<string | null> {
    try {
      await this.api.stopSchedule(id);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Deletes it; a schedule that is already running keeps that run. */
  async deleteSchedule(id: string): Promise<string | null> {
    try {
      await this.api.deleteSchedule(id);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  // MARK: memories

  /** The whole table, every row the server kept; changes then ride the event stream. */
  async loadMemories(): Promise<string | null> {
    const server = this.server;
    try {
      const memories = await this.api.memories();
      if (server === this.server) this.set({ memories, memoriesError: null });
      return null;
    } catch (error) {
      const message = errorMessage(error);
      if (server === this.server) this.set({ memoriesError: message });
      return message;
    }
  }

  /** The review and digest switches; changes then ride the `team_settings` stream event. */
  async loadTeamSettings(): Promise<string | null> {
    const server = this.server;
    try {
      const teamSettings = await this.api.teamSettings();
      if (server === this.server) this.set({ teamSettings, teamSettingsError: null });
      return null;
    } catch (error) {
      const message = errorMessage(error);
      if (server === this.server) this.set({ teamSettingsError: message });
      return message;
    }
  }

  /** Partial, nested allowed; the server answers the whole state, which the stream re-broadcasts. */
  async setTeamSettings(patch: Partial<TeamSettings>): Promise<string | null> {
    const server = this.server;
    try {
      const teamSettings = await this.api.patchTeamSettings(patch);
      if (server === this.server) this.set({ teamSettings, teamSettingsError: null });
      return null;
    } catch (error) {
      const message = errorMessage(error);
      if (server === this.server) this.set({ teamSettingsError: message });
      return message;
    }
  }

  /** The approval mode and folders; changes then ride the `permission_settings` stream event. */
  async loadPermissionSettings(): Promise<string | null> {
    const server = this.server;
    try {
      const permissionSettings = await this.api.permissionSettings();
      if (server === this.server) this.set({ permissionSettings, permissionSettingsError: null });
      return null;
    } catch (error) {
      const message = errorMessage(error);
      if (server === this.server) this.set({ permissionSettingsError: message });
      return message;
    }
  }

  /** The server answers the whole state (or the Chinese reason a folder was refused). */
  async patchPermissionSettings(patch: {
    mode?: PermissionMode;
    folders?: string[];
  }): Promise<string | null> {
    const server = this.server;
    try {
      const permissionSettings = await this.api.patchPermissionSettings(patch);
      if (server === this.server) this.set({ permissionSettings, permissionSettingsError: null });
      return null;
    } catch (error) {
      const message = errorMessage(error);
      if (server === this.server) this.set({ permissionSettingsError: message });
      return message;
    }
  }

  /** Fires the digest now; returns an error message, or null when it started. */
  async runDigestNow(): Promise<string | null> {
    try {
      await this.api.runDigestNow();
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Adds one the user typed here; the stream confirms the list right after. */
  async addMemory(body: {
    content: string;
    topic?: string;
    scope: MemoryScope;
    agent_id?: string;
  }): Promise<string | null> {
    const server = this.server;
    try {
      const memory = await this.api.addMemory(body);
      if (server === this.server) this.placeMemory(memory);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /**
   * A partial edit or the pin switch. Editing the content makes the server answer a new row:
   * the old one is retired here at once, so the row never looks duplicated while the stream
   * catches up.
   */
  async updateMemory(
    id: string,
    patch: { content?: string; topic?: string; pinned?: boolean },
  ): Promise<string | null> {
    const server = this.server;
    try {
      const memory = await this.api.updateMemory(id, patch);
      if (server === this.server) this.placeMemory(memory, id);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Deletes one for good; the row disappears locally only after the server agreed. */
  async deleteMemory(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.deleteMemory(id);
    } catch (error) {
      return errorMessage(error);
    }
    const current = this.state.memories;
    if (server === this.server && current) {
      this.set({
        memories: { ...current, memories: current.memories.filter((memory) => memory.id !== id) },
      });
    }
    return null;
  }

  /** Both switches; the server answers the whole state. */
  async setMemorySettings(patch: { paused?: boolean; auto_extract?: boolean }): Promise<string | null> {
    const server = this.server;
    try {
      const memories = await this.api.setMemorySettings(patch);
      if (server === this.server) {
        this.set({ memories, memoriesError: null });
      }
      return null;
    } catch (error) {
      const message = errorMessage(error);
      if (server === this.server) this.set({ memoriesError: message });
      return message;
    }
  }

  /**
   * Puts an answered row into the list without waiting for the stream. `replaces` names the row
   * an edit answered: when the server handed back a new id, the old row is retired in its place.
   */
  private placeMemory(memory: Memory, replaces?: string) {
    const current = this.state.memories;
    if (!current) return;
    const replaced = replaces !== memory.id ? current.memories.find((row) => row.id === replaces) : undefined;
    const rows = current.memories.filter((row) => row.id !== memory.id && row.id !== replaced?.id);
    const retired: Memory | null = replaced
      ? {
          ...replaced,
          status: "retired",
          superseded_by: memory.id,
          valid_to: memory.valid_from,
          updated_at: memory.updated_at,
        }
      : null;
    this.set({
      memories: { ...current, memories: [memory, ...(retired ? [retired] : []), ...rows] },
      memoriesError: null,
    });
  }

  /** Full note body; re-read whenever the workbench asks again so edits show up. */
  async loadNote(id: string) {
    const server = this.server;
    const token = (this.noteTokens.get(id) ?? 0) + 1;
    this.noteTokens.set(id, token);
    try {
      const note = await this.api.note(id);
      if (server !== this.server || token !== this.noteTokens.get(id)) return;
      this.set({ noteBodies: { ...this.state.noteBodies, [note.id]: note } });
    } catch (error) {
      if (server === this.server) this.set({ notice: `没有读到这条笔记：${errorMessage(error)}` });
    }
  }

  // MARK: skills

  /** Every skill, sorted by name; changes then ride the `skills` stream event. */
  async loadSkills() {
    const server = this.server;
    try {
      const body = await this.api.skills();
      if (server === this.server) this.set({ skills: body.skills ?? [], skillsError: null });
    } catch (error) {
      if (server === this.server) this.set({ skillsError: errorMessage(error) });
    }
  }

  async loadWorkflows() {
    const server = this.server;
    try {
      const body = await this.api.workflows();
      if (server === this.server) {
        this.set({ workflows: body.workflows ?? [], workflowsError: null });
      }
    } catch (error) {
      if (server === this.server) this.set({ workflowsError: errorMessage(error) });
    }
  }

  /** One workflow with its markdown and its last walks; read on demand by the editor. */
  async workflowDetail(id: string): Promise<WorkflowDetail> {
    return this.api.workflow(id);
  }

  /** The walk a chat is serving, read once when the chat opens; the stream keeps it current. */
  async loadWorkflowRun(conversationID: string) {
    const server = this.server;
    try {
      const body = await this.api.conversationWorkflowRun(conversationID);
      // A read that started before the stream's newest event must not overwrite it: the
      // events are the authority once anything for this chat has arrived.
      if (
        server === this.server &&
        body.workflow_run &&
        !this.state.workflowRuns[conversationID]
      ) {
        this.applyWorkflowRun(body.workflow_run);
      }
    } catch {
      // A chat that has no live walk simply has none.
    }
  }

  /** One walk's controls; every answer is the whole run, so the strip is always current. */
  async pauseWorkflow(workflowRunID: string) {
    this.applyWorkflowRun(await this.api.pauseWorkflowRun(workflowRunID));
    this.refreshWorkflowSoon(workflowRunID);
  }

  async resumeWorkflow(workflowRunID: string) {
    this.applyWorkflowRun(await this.api.resumeWorkflowRun(workflowRunID));
    this.refreshWorkflowSoon(workflowRunID);
  }

  async stopWorkflow(workflowRunID: string) {
    this.applyWorkflowRun(await this.api.stopWorkflowRun(workflowRunID));
    this.refreshWorkflowSoon(workflowRunID);
  }

  async answerWorkflow(workflowRunID: string, answer: string) {
    this.applyWorkflowRun(await this.api.answerWorkflowRun(workflowRunID, answer));
    this.refreshWorkflowSoon(workflowRunID);
  }

  async controlWorkflowNode(workflowRunID: string, nodeID: string, command: "retry" | "skip") {
    this.applyWorkflowRun(await this.api.controlWorkflowNode(workflowRunID, nodeID, command));
    this.refreshWorkflowSoon(workflowRunID);
  }

  /** Re-read one walk a moment after a control.

   *  A control's response is a snapshot taken before the engine acts, and the step it moves
   *  may well change ten milliseconds later; if the stream happens to be reconnecting at that
   *  instant that change would never reach this window. One extra read costs nothing and the
   *  strip can only end up more current.
   */
  private refreshWorkflowSoon(workflowRunID: string) {
    window.setTimeout(() => {
      void this.api
        .workflowRun(workflowRunID)
        .then((run) => this.applyWorkflowRun(run))
        .catch(() => undefined);
    }, 400);
  }

  /** Start a walk of one workflow; returns the tab it opened. */
  async runWorkflow(workflowID: string, input = ""): Promise<{ workflow_run: WorkflowRun; conversation_id: string }> {
    const started = await this.api.runWorkflow(workflowID, input);
    this.applyWorkflowRun(started.workflow_run);
    void this.loadWorkspace();
    return started;
  }

  /** One skill with its SKILL.md body and file list; read on demand, never cached. */
  async skillDetail(id: string): Promise<SkillDetail> {
    return this.api.skill(id);
  }

  /** Creates one; the panel closes only when null comes back. */
  async createSkill(body: { name: string; description: string; body: string }): Promise<string | null> {
    try {
      await this.api.createSkill(body);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Uploads a zip or a single SKILL.md; lands disabled until the user enables it. */
  async uploadSkill(file: File): Promise<string | null> {
    try {
      await this.api.uploadSkill(file, skillUploadContentType(file.name));
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Imports a skill from a GitHub URL; lands disabled until the user enables it. */
  async importSkillFromGitHub(url: string): Promise<string | null> {
    try {
      await this.api.importSkill(url);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** The skills other agents on this machine keep, for the import picker; read on demand. */
  async localSkills(): Promise<LocalSkill[]> {
    return (await this.api.localSkills()).skills ?? [];
  }

  /** Imports one folder `localSkills` listed; null on success, else the reason. */
  async importLocalSkill(path: string): Promise<string | null> {
    try {
      await this.api.importLocalSkill(path);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Partial edit, e.g. just `enabled` for the switch. The stream pushes the new list after. */
  async updateSkill(id: string, patch: { enabled?: boolean; description?: string; body?: string }): Promise<string | null> {
    try {
      await this.api.updateSkill(id, patch);
      return null;
    } catch (error) {
      return errorMessage(error);
    }
  }

  /** Deletes one; the row disappears locally only after the server agreed. */
  async deleteSkill(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.deleteSkill(id);
    } catch (error) {
      return errorMessage(error);
    }
    const current = this.state.skills;
    if (server === this.server && current) {
      this.set({ skills: current.filter((skill) => skill.id !== id) });
    }
    return null;
  }

  /**
   * Hand-set a task's status or note. The list moves at once; a refused patch puts the old
   * row back and leaves the reason in ``workbenchError`` for the dialog to show.
   */
  async updateTask(id: string, patch: { status?: TaskStatus; note?: string }): Promise<string | null> {
    const server = this.server;
    const before = this.state.tasks?.find((task) => task.id === id);
    const list = this.state.tasks;
    if (list && before) {
      this.set({ tasks: list.map((task) => (task.id === id ? { ...task, ...patch } : task)) });
    }
    try {
      const updated = await this.api.updateTask(id, patch);
      if (server === this.server) {
        this.set({
          tasks: (this.state.tasks ?? []).map((task) => (task.id === id ? updated : task)),
          workbenchError: null,
        });
      }
      return null;
    } catch (error) {
      const message = errorMessage(error);
      if (server === this.server) {
        this.set({
          tasks: this.state.tasks?.map((task) => (task.id === id && before ? before : task)) ?? null,
          workbenchError: `任务没有改成：${message}`,
        });
      }
      return message;
    }
  }

  /** Deletes one note; the row disappears locally only after the server agreed. */
  async deleteNote(id: string): Promise<string | null> {
    const server = this.server;
    try {
      await this.api.deleteNote(id);
    } catch (error) {
      return errorMessage(error);
    }
    if (server === this.server) {
      const bodies = { ...this.state.noteBodies };
      delete bodies[id];
      this.set({
        notes: (this.state.notes ?? []).filter((note) => note.id !== id),
        noteBodies: bodies,
      });
    }
    return null;
  }

  // MARK: live stream

  startStream() {
    if (this.closeStream) return;
    const server = this.server;
    this.closeStream = this.api.events({
      onEvent: (name, data) => {
        if (server === this.server) this.applyEvent(name, data);
      },
      onClose: (message) => {
        if (server !== this.server) return;
        this.closeStream = null;
        this.set({ streamError: message });
      },
    });
    this.set({ streamError: null });
  }

  private stopStream() {
    this.closeStream?.();
    this.closeStream = null;
  }

  /** Manual recovery: reconnect, re-read the directory and the open conversation. */
  async retryStream() {
    this.stopStream();
    this.startStream();
    await this.loadWorkspace();
    if (this.state.selectedID) await this.refreshDetail(this.state.selectedID);
  }

  /** One walk, keyed by its conversation: a run only ever speaks for its own tab. */
  private applyWorkflowRun(run: WorkflowRun, replace = false) {
    const previous = this.state.workflowRuns[run.conversation_id];
    if (!replace && previous && run.updated_at < previous.updated_at) return;
    this.set({ workflowRuns: { ...this.state.workflowRuns, [run.conversation_id]: run } });
  }

  private applyEvent(name: string, data: unknown) {
    noteActivity();
    switch (name) {
      case "snapshot": {
        const snapshot = data as Snapshot;
        // A reconnect missed whatever moved meanwhile. A fold still shown as running may
        // have finished while the stream was down, so its state is not carried over.
        this.set({ contextEvents: {} });
        // The same for activities: one still marked live is re-read when a card names it, so a
        // card never claims a bot is working after the stream went quiet.
        const settledActivities = Object.fromEntries(
          Object.entries(this.state.activities).filter(([, activity]) => !isLiveActivity(activity.status)),
        );
        this.set({ activities: settledActivities });
        this.applyWorkspace(snapshot.workspace);
        if (snapshot.sidebar) this.set({ sidebar: snapshot.sidebar });
        // A reconnect missed whatever moved meanwhile: the snapshot is the newest truth and
        // replaces the walk it names, so a waiting step never shows as still running.
        if (snapshot.workflow_run) this.applyWorkflowRun(snapshot.workflow_run, true);
        else if (snapshot.conversation) {
          // Nothing live for this chat any more: a walk still shown as live finished while the
          // stream was down, and showing it as running would be a lie.
          const chatID = snapshot.conversation.conversation.id;
          const stale = this.state.workflowRuns[chatID];
          if (stale && isLive(stale.status)) {
            const { [chatID]: _gone, ...rest } = this.state.workflowRuns;
            this.set({ workflowRuns: rest });
          }
        }
        break;
      }
      case "workspace":
        this.applyWorkspace(data as Workspace);
        // Provider edits ride on this event: the payload never carries the list itself.
        void this.loadProviders();
        break;
      case "decisions":
        if (this.state.workspace) {
          this.set({ workspace: { ...this.state.workspace, decisions: data as Workspace["decisions"] } });
        }
        break;
      case "connectors": {
        // The event carries the current list and statuses; presets only ever come from the GET.
        const list: unknown = data !== null && typeof data === "object" && "connectors" in data ? data.connectors : null;
        if (!Array.isArray(list)) break;
        const presets = this.state.connectors?.presets;
        this.set({
          connectors: { presets: presets ?? [], connectors: list as Connector[] },
          connectorsError: null,
        });
        if (!presets) void this.loadConnectors();
        break;
      }
      case "computer": {
        // The event never carries the token; keep the one the GET returned.
        if (data === null || typeof data !== "object" || !("connected" in data)) break;
        const token = this.state.computer?.token;
        this.set({ computer: { ...(data as ComputerState), token }, computerError: null });
        if (token === undefined) void this.loadComputer();
        break;
      }
      case "sidebar":
        this.set({ sidebar: data as SidebarLayout });
        break;
      case "run":
        this.applyRun(data as Run);
        break;
      case "context": {
        // Compaction runs on the backend after a run; the bar follows it live, no polling.
        const event = data as ContextEvent;
        if (event && typeof event.conversation_id === "string") {
          this.set({
            contextEvents: { ...this.state.contextEvents, [event.conversation_id]: event },
          });
        }
        break;
      }
      case "message_start":
      case "message_end":
        this.applyMessage(data as Message, name === "message_start");
        break;
      case "message_update":
        // A row that changed while it streams, e.g. text that turned out to lead to tools.
        // Not an arrival: no gesture, no unread mark, no mood.
        this.applyMessage(data as Message, false);
        break;
      case "activity": {
        const activity = data as Activity;
        if (
          activity === null ||
          typeof activity !== "object" ||
          typeof activity.id !== "string" ||
          typeof activity.conversation_id !== "string" ||
          typeof activity.status !== "string"
        ) {
          break;
        }
        this.set({ activities: { ...this.state.activities, [activity.id]: activity } });
        break;
      }
      case "delta":
        this.applyDelta(data as Delta);
        break;
      case "notes": {
        // The event carries the whole list: bare, or wrapped the way the REST route answers.
        const list: unknown = Array.isArray(data) ? data : data !== null && typeof data === "object" && "notes" in data ? data.notes : null;
        if (Array.isArray(list)) this.set({ notes: list.filter(isNoteSummary) });
        break;
      }
      case "group_tasks": {
        if (data === null || typeof data !== "object" || !("conversation_id" in data) || !("tasks" in data)) break;
        const { conversation_id: id, tasks } = data as { conversation_id: unknown; tasks: unknown };
        if (typeof id !== "string" || !Array.isArray(tasks)) break;
        this.setGroupTasks(id, tasks as GroupTask[]);
        break;
      }
      case "task_progress": {
        const progress = data as TaskProgress;
        if (
          progress === null ||
          typeof progress !== "object" ||
          typeof progress.conversation_id !== "string" ||
          typeof progress.task_id !== "string"
        ) {
          break;
        }
        const groupID = progress.conversation_id;
        const tasks = this.state.groupTasks[groupID];
        const merged = tasks ? mergeTaskProgress(tasks, progress) : null;
        const task = merged?.find((row) => row.id === progress.task_id);
        const workspace = this.state.workspace;
        // The sidebar's live line moves even for a group this window has not opened, as long as
        // the workspace already listed its thread.
        const groupThreads = !workspace
          ? null
          : task
            ? mergeGroupThreadProgress(workspace.group_threads, groupID, task)
            : patchGroupThreadProgress(workspace.group_threads, progress);
        this.set({
          ...(merged ? { groupTasks: { ...this.state.groupTasks, [groupID]: merged } } : {}),
          ...(workspace && groupThreads ? { workspace: { ...workspace, group_threads: groupThreads } } : {}),
        });
        break;
      }
      case "group_work_done": {
        // One line per finished piece of group work, only for a group the user is not looking at.
        if (data === null || typeof data !== "object") break;
        const done = data as { conversation_id?: unknown; title?: unknown; done?: unknown; failed?: unknown; stopped?: unknown };
        if (typeof done.conversation_id !== "string" || done.conversation_id === this.state.selectedID) break;
        this.set({ notice: groupWorkLine(String(done.title ?? "群组"), Number(done.done) || 0, Number(done.failed) || 0, Number(done.stopped) || 0) });
        break;
      }
      case "tasks": {
        const list: unknown = Array.isArray(data) ? data : data !== null && typeof data === "object" && "tasks" in data ? data.tasks : null;
        if (Array.isArray(list)) this.set({ tasks: list.filter(isTaskItem) });
        break;
      }
      case "schedules": {
        // The event carries the whole state, the same object GET /schedules answers.
        if (data === null || typeof data !== "object" || !("schedules" in data)) break;
        const list: unknown = (data as { schedules: unknown }).schedules;
        if (!Array.isArray(list)) break;
        const previous = this.state.schedules;
        const timezone = (data as { timezone?: unknown }).timezone;
        const limits = (data as { limits?: unknown }).limits;
        this.set({
          schedules: {
            schedules: list.filter(isSchedule),
            timezone: typeof timezone === "string" ? timezone : previous?.timezone ?? "",
            limits:
              limits !== null && typeof limits === "object" && "max_enabled" in limits && "min_interval_minutes" in limits
                ? (limits as SchedulesState["limits"])
                : previous?.limits ?? { max_enabled: 20, min_interval_minutes: 5 },
          },
          schedulesError: null,
        });
        break;
      }
      case "memories": {
        // The event carries the whole state, the same object GET /memories answers.
        if (data === null || typeof data !== "object" || !("memories" in data)) break;
        const list = data.memories;
        if (!Array.isArray(list)) break;
        const settings = "settings" in data ? data.settings : undefined;
        const previous = this.state.memories?.settings;
        this.set({
          memories: {
            memories: list.filter(isMemory),
            settings:
              isMemorySettings(settings) ? settings : previous ?? { paused: false, auto_extract: true, core_chars: 4000 },
          },
          memoriesError: null,
        });
        break;
      }
      case "approval_rules": {
        const rules = (data as { rules?: ApprovalRule[] }).rules;
        if (Array.isArray(rules)) this.set({ approvalRules: rules });
        break;
      }
      case "team_settings": {
        if (isTeamSettings(data)) this.set({ teamSettings: data, teamSettingsError: null });
        break;
      }
      case "permission_settings": {
        if (isPermissionSettings(data)) {
          this.set({ permissionSettings: data, permissionSettingsError: null });
        }
        break;
      }
      case "skills": {
        // The event carries the whole list, the same object GET /skills answers.
        const list: unknown =
          data !== null && typeof data === "object" && "skills" in data ? data.skills : null;
        if (Array.isArray(list)) this.set({ skills: list.filter(isSkill), skillsError: null });
        break;
      }
      case "workflows": {
        // The event carries the whole list, the same object GET /workflows answers.
        const list: unknown =
          data !== null && typeof data === "object" && "workflows" in data ? data.workflows : null;
        if (Array.isArray(list)) this.set({ workflows: list.filter(isWorkflow), workflowsError: null });
        break;
      }
      case "workflow_run": {
        if (isWorkflowRun(data)) this.applyWorkflowRun(data);
        break;
      }
      case "reactions": {
        // `{conversation_id, message_id, reactions}`; skipped by the server for hidden chats.
        if (
          data !== null &&
          typeof data === "object" &&
          "message_id" in data &&
          "conversation_id" in data &&
          "reactions" in data
        ) {
          this.applyReactions(data as ReactionUpdate);
        }
        break;
      }
      case "message_removed": {
        // A streaming reply that turned out to be reaction-only: drop the row and its quotes.
        if (
          data !== null &&
          typeof data === "object" &&
          "message_id" in data &&
          "conversation_id" in data
        ) {
          this.applyMessageRemoved(data as { conversation_id: string; message_id: string });
        }
        break;
      }
    }
  }

  private applyRun(run: Run) {
    const workspace = this.state.workspace;
    if (!workspace) return;
    const before = workspace.active_runs.find((item) => item.id === run.id);
    // A member taking the turn thinks first: the body becomes the middle of three dots.
    if (run.status === "running" && run.current_agent_id && before?.current_agent_id !== run.current_agent_id) {
      this.gesture(run.current_agent_id, "think");
    }
    const others = workspace.active_runs.filter((item) => item.id !== run.id);
    const active_runs = run.status === "running" ? [...others, run] : others;
    // A run that just finished spent tokens: refresh the team panel's totals if they are on screen.
    if (before?.status === "running" && run.status !== "running" && this.state.usage) {
      void this.loadUsage(this.state.usage.period);
    }
    // A completed run joins the sidebar's 已完成 list until its chat is opened.
    if (before?.status === "running" && run.status === "completed") void this.loadWorkspace();
    const detail = this.state.detail;
    this.set({
      workspace: { ...workspace, active_runs },
      detail: detail && detail.conversation.id === run.conversation_id ? { ...detail, latest_run: run } : detail,
    });
  }

  private applyMessage(message: Message, started: boolean) {
    const workspace = this.state.workspace;
    if (workspace) {
      const preview = messagePreview(message);
      const conversations = workspace.conversations.map((c) =>
        c.id === message.conversation_id
          ? { ...c, ...(preview === null ? {} : { last_message: preview }), updated_at: message.created_at }
          : c,
      );
      this.set({ workspace: { ...workspace, conversations } });
    }
    const botReply =
      message.kind === "agent" &&
      (message.status === "complete" || message.status === "interrupted");
    // A bot reply raises the dot of the chat it landed in. A task's intermediate turns live in
    // its thread, so only that hidden chat is marked, never the group; the group hears from the
    // task once — its result reply (an `agent` row in the group) is what marks it unread.
    if (botReply) {
      if (message.conversation_id === this.state.selectedID) {
        this.markRead(message.conversation_id, message.seq);
      } else if (this.state.workspace) {
        const current = this.state.workspace;
        this.set({
          workspace: {
            ...current,
            conversations: current.conversations.map((c) =>
              c.id === message.conversation_id ? { ...c, unread: true } : c,
            ),
          },
        });
      }
    }
    if (started) {
      for (const id of message.mentions) {
        this.holdMood(id, "surprised", 1400);
        this.gesture(id, "notice");
      }
      if (message.kind === "agent" && message.author_id) this.gesture(message.author_id, "nod");
      // A private message between two bots lands on the other one as a pastille pop.
      const conversation = this.state.workspace?.conversations.find((c) => c.id === message.conversation_id);
      if (conversation?.kind === "private" && message.author_id) {
        for (const id of conversation.member_ids) {
          if (id !== message.author_id) this.gesture(id, "notice");
        }
      }
    }
    if (!started && message.kind === "agent" && message.author_id) {
      // A finished reply wears the mood the model tagged, for as long as it stays news.
      const mood = asMoodTag(message.mood);
      if (message.status === "complete" && mood) this.holdMood(message.author_id, mood, RECENT_MOOD_MS);
      if (message.status === "interrupted") this.holdMood(message.author_id, "error", 4000);
      if (message.status === "complete" && (mood === "happy" || mood === "proud")) {
        this.gesture(message.author_id, "cheer");
      } else if (message.status === "complete" && mood === "sorry") {
        this.gesture(message.author_id, "sigh");
      } else if (message.status === "interrupted") {
        this.gesture(message.author_id, "wince");
      }
    }
    const detail = this.state.detail;
    if (detail && detail.conversation.id === message.conversation_id) {
      const index = detail.messages.findIndex((m) => m.id === message.id);
      // Older than the window with history still unread above it: it comes with its page.
      if (index >= 0 || acceptsLiveMessage(detail, message)) {
        const messages =
          index >= 0
            ? detail.messages.map((m, i) => (i === index ? message : m))
            : [...detail.messages, message].sort((a, b) => a.seq - b.seq);
        this.set({ detail: { ...detail, messages } });
      }
    }
    // A card's inline thread lives in the same stream; keep its copy in step too.
    const thread = this.state.threadDetails[message.conversation_id];
    if (!thread) return;
    const at = thread.messages.findIndex((m) => m.id === message.id);
    const messages =
      at >= 0
        ? thread.messages.map((m, i) => (i === at ? message : m))
        : [...thread.messages, message].sort((a, b) => a.seq - b.seq);
    this.set({ threadDetails: { ...this.state.threadDetails, [message.conversation_id]: { ...thread, messages } } });
  }

  private applyDelta(delta: Delta) {
    const detail = this.state.detail;
    if (detail && detail.conversation.id === delta.conversation_id) {
      const messages = detail.messages.map((m) =>
        m.id === delta.message_id ? { ...m, content: m.content + delta.text } : m,
      );
      this.set({ detail: { ...detail, messages } });
    }
    const thread = this.state.threadDetails[delta.conversation_id];
    if (!thread) return;
    const messages = thread.messages.map((m) =>
      m.id === delta.message_id ? { ...m, content: m.content + delta.text } : m,
    );
    this.set({ threadDetails: { ...this.state.threadDetails, [delta.conversation_id]: { ...thread, messages } } });
  }

  /** Replace one action row wherever it is drawn: the open chat or an inline thread card. */
  private applyAction(action: Action) {
    const patch = (detail: ConversationDetail): ConversationDetail => ({
      ...detail,
      messages: detail.messages.map((m) => (m.action?.id === action.id ? { ...m, action } : m)),
    });
    const detail = this.state.detail;
    const thread = Object.values(this.state.threadDetails).find((row) =>
      row.messages.some((m) => m.action?.id === action.id),
    );
    this.set({
      ...(detail && detail.messages.some((m) => m.action?.id === action.id) ? { detail: patch(detail) } : {}),
      ...(thread
        ? { threadDetails: { ...this.state.threadDetails, [thread.conversation.id]: patch(thread) } }
        : {}),
    });
  }

  /** Update one message's reactions in the loaded conversation, if that message is on screen. */
  private applyReactions(update: ReactionUpdate) {
    const detail = this.state.detail;
    if (!detail || detail.conversation.id !== update.conversation_id) return;
    const messages = detail.messages.map((m) =>
      m.id === update.message_id ? { ...m, reactions: update.reactions } : m,
    );
    this.set({ detail: { ...detail, messages } });
  }

  /** Drop a reaction-only reply row, its quoted reference included. */
  private applyMessageRemoved(payload: { conversation_id: string; message_id: string }) {
    const detail = this.state.detail;
    if (!detail || detail.conversation.id !== payload.conversation_id) return;
    const messages = detail.messages.filter((m) => m.id !== payload.message_id);
    if (messages.length === detail.messages.length) return;
    const references = detail.references.filter((m) => m.id !== payload.message_id);
    this.set({ detail: { ...detail, messages, references } });
  }

  /**
   * The user toggles their own reaction on one message: the list flips optimistically, then the
   * server's answer (and the stream event that follows) replaces it. A failure restores the
   * previous list and shows the reason.
   */
  async toggleReaction(messageID: string, emoji: string) {
    const detail = this.state.detail;
    const message = detail?.messages.find((m) => m.id === messageID);
    if (!detail || !message) return;
    const previous = message.reactions ?? [];
    const adding = !hasOwnReaction(previous, emoji);
    const optimistic = toggleOwnReaction(previous, emoji);
    this.applyReactions({ message_id: messageID, conversation_id: message.conversation_id, reactions: optimistic });
    try {
      const saved = adding
        ? await this.api.addReaction(messageID, emoji)
        : await this.api.removeReaction(messageID, emoji);
      // The stream's `reactions` event carries the authoritative list regardless; apply the
      // response directly only when it has the documented shape, so a stray body cannot clobber.
      if (saved && Array.isArray(saved.reactions)) {
        this.applyReactions(saved);
      }
    } catch (error) {
      // Roll back to what was on screen before the tap.
      this.applyReactions({ message_id: messageID, conversation_id: message.conversation_id, reactions: previous });
      this.set({ notice: `表情回应没有保存：${errorMessage(error)}` });
    }
  }

  private holdMood(agentID: string, mood: Mood, ms: number) {
    const previous = this.moodTimers.get(agentID);
    if (previous !== undefined) window.clearTimeout(previous);
    this.set({ moods: { ...this.state.moods, [agentID]: mood } });
    const timer = window.setTimeout(() => {
      this.moodTimers.delete(agentID);
      const moods = { ...this.state.moods };
      delete moods[agentID];
      this.set({ moods });
    }, ms);
    this.moodTimers.set(agentID, timer);
  }

  // MARK: derived

  runFor(conversationID: string): Run | undefined {
    return this.state.workspace?.active_runs.find((run) => run.conversation_id === conversationID);
  }
}

/** Guards for the workbench events: shape checks enough to reject anything that is not a row. */
function isNoteSummary(value: unknown): value is NoteSummary {
  return value !== null && typeof value === "object" && "id" in value && "title" in value;
}

function isTaskItem(value: unknown): value is TaskItem {
  return value !== null && typeof value === "object" && "id" in value && "status" in value;
}

function isSchedule(value: unknown): value is Schedule {
  return value !== null && typeof value === "object" && "id" in value && "kind" in value && "enabled" in value;
}

function isWorkflow(value: unknown): value is Workflow {
  return (
    value !== null &&
    typeof value === "object" &&
    "id" in value &&
    "name" in value &&
    "steps" in value &&
    "enabled" in value
  );
}

function isWorkflowRun(value: unknown): value is WorkflowRun {
  return (
    value !== null &&
    typeof value === "object" &&
    "id" in value &&
    "conversation_id" in value &&
    "status" in value &&
    "steps" in value
  );
}

function isSkill(value: unknown): value is Skill {
  return value !== null && typeof value === "object" && "id" in value && "name" in value && "enabled" in value;
}

function isMemory(value: unknown): value is Memory {
  return (
    value !== null &&
    typeof value === "object" &&
    "id" in value &&
    "content" in value &&
    "scope" in value &&
    "status" in value
  );
}

function isMemorySettings(value: unknown): value is MemorySettings {
  return (
    value !== null &&
    typeof value === "object" &&
    "paused" in value &&
    "auto_extract" in value &&
    "core_chars" in value
  );
}

function isTeamSettings(value: unknown): value is TeamSettings {
  if (value === null || typeof value !== "object") return false;
  if (!("review" in value) || !("digest" in value)) return false;
  const review = value.review;
  const digest = value.digest;
  return (
    review !== null &&
    typeof review === "object" &&
    "enabled" in review &&
    "max_rounds" in review &&
    digest !== null &&
    typeof digest === "object" &&
    "enabled" in digest &&
    "frequency" in digest &&
    "time" in digest &&
    "weekday" in digest &&
    "step_budget" in value &&
    "step_budgets" in value
  );
}

function isPermissionSettings(value: unknown): value is PermissionSettings {
  if (value === null || typeof value !== "object") return false;
  if (!("mode" in value) || !("folders" in value)) return false;
  if (!("working_folder" in value) || !("home" in value)) return false;
  const mode = value.mode;
  return (
    Array.isArray(value.folders) &&
    (mode === "ask" || mode === "auto" || mode === "full") &&
    typeof value.working_folder === "string" &&
    typeof value.home === "string"
  );
}

/** PUT order: pinned entries first, then each folder in turn, then the ungrouped tail. */
function orderedItems(layout: SidebarLayout): SidebarItem[] {
  const pinned = layout.items
    .filter((item) => item.pinned)
    .sort((a, b) => (a.pin_position ?? 0) - (b.pin_position ?? 0));
  const pinnedIDs = new Set(pinned.map((item) => item.conversation_id));
  const rest: SidebarItem[] = [];
  for (const folder of [...layout.folders].sort((a, b) => a.position - b.position)) {
    rest.push(
      ...layout.items
        .filter((item) => item.folder_id === folder.id && !pinnedIDs.has(item.conversation_id))
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0)),
    );
  }
  rest.push(...layout.items.filter((item) => !item.pinned && !item.folder_id));
  return [...pinned, ...rest];
}

export const store = new TeamStore();

/**
 * Unsent text, per conversation, kept out of TeamState: a keystroke must not wake the whole
 * timeline, so only the composer that reads its own draft subscribes here.
 */
class DraftStore {
  private texts = new Map<string, string>();
  private listeners = new Set<Listener>();

  subscribe = (listener: Listener) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  get = (conversationID: string): string => this.texts.get(conversationID) ?? "";

  set(conversationID: string, text: string) {
    if (this.get(conversationID) === text) return;
    if (text) this.texts.set(conversationID, text);
    else this.texts.delete(conversationID);
    for (const listener of this.listeners) listener();
  }

  clear(conversationID: string) {
    this.set(conversationID, "");
  }

  clearAll() {
    if (this.texts.size === 0) return;
    this.texts.clear();
    for (const listener of this.listeners) listener();
  }
}

export const drafts = new DraftStore();

/** The unsent text of one conversation; typing re-renders the composer, nothing else. */
export function useDraft(conversationID: string): string {
  return useSyncExternalStore(drafts.subscribe, () => drafts.get(conversationID));
}

export function useTeam<T>(select: (state: TeamState) => T): T {
  return useSyncExternalStore(store.subscribe, () => select(store.getState()));
}

// MARK: selectors shared by several views

export function agentByID(workspace: Workspace | null, id: string | null | undefined): Agent | undefined {
  return id ? workspace?.agents.find((agent) => agent.id === id) : undefined;
}

export function membersOf(workspace: Workspace | null, conversation: Conversation): Agent[] {
  return conversation.member_ids
    .map((id) => agentByID(workspace, id))
    .filter((agent): agent is Agent => agent !== undefined);
}

const RECENT_MOOD_MS = 30_000;

/** The mood tagged on this member's latest finished reply, while it is under 30 seconds old. */
function latestReplyMood(state: TeamState, agentID: string): Mood | null {
  const messages = state.detail?.messages ?? [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!;
    if (message.author_id !== agentID || message.status !== "complete") continue;
    const mood = asMoodTag(message.mood);
    const age = Date.now() - Date.parse(message.created_at);
    return mood && Number.isFinite(age) && age <= RECENT_MOOD_MS ? mood : null;
  }
  return null;
}

/** Face for a member right now: held windows, then the live run, then the latest tagged reply. */
export function moodFor(state: TeamState, agentID: string, conversationID?: string): Mood {
  const held = state.moods[agentID];
  if (held) return held;
  if (conversationID) {
    const detail = state.detail;
    if (detail?.conversation.id === conversationID) {
      const streaming = detail.messages.find((m) => m.status === "streaming");
      if (streaming?.author_id === agentID) return "speaking";
    }
    const run = state.workspace?.active_runs.find((r) => r.conversation_id === conversationID);
    if (run && (run.current_agent_id === agentID || run.pending_agent_ids.includes(agentID))) return "thinking";
  }
  return latestReplyMood(state, agentID) ?? "idle";
}

/** Face for a member in the team panel: thinking when it owns any run's turn, else held mood. */
export function teamMoodFor(state: TeamState, agentID: string): Mood {
  if (state.workspace?.active_runs.some((run) => run.current_agent_id === agentID)) return "thinking";
  return state.moods[agentID] ?? "idle";
}

/** 「方案小组」的工作结束了：完成 2 项，1 项停下。 */
export function groupWorkLine(title: string, done: number, failed: number, stopped: number): string {
  const parts = [done && `完成 ${done} 项`, failed && `${failed} 项没做成`, stopped && `${stopped} 项停下`].filter(Boolean);
  return `「${title}」的工作结束了${parts.length ? `：${parts.join("，")}` : ""}。`;
}
