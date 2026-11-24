// Mirrors of the backend's snake_case contract (backend/app/models.py).

import type { Appearance } from "../bot/catalog";

export type { Appearance };

export type AgentKind = "supervisor" | "member";
export type ConversationKind = "direct" | "group" | "private" | "workflow" | "thread";
/**
 * `task` is the group's one live card; it is not prose, so no reply tools ride on it.
 * `status` is the dispatcher's progress card: drawn from task data, written by no member.
 */
export type MessageKind = "user" | "agent" | "action" | "notice" | "task" | "status";
export type MessageStatus = "streaming" | "complete" | "interrupted" | "queued";
export type RunStatus = "running" | "completed" | "stopped" | "error" | "limited" | "queued";
export type ActionStatus = "running" | "completed" | "error" | "awaiting";
/** `in`/`out` are bare markers; `summary` is the bot's own report to the user. */
export type RelayDirection = "in" | "out" | "summary";

/** A watched bot-to-bot message mirrored into one participant's direct chat. */
export interface Relay {
  conversation_id: string;
  direction: RelayDirection;
  peer_id: string;
  peer_name: string;
}

/** The user question a bot-to-bot message was sent for, in the conversation it came from. */
export interface Origin {
  message_id: string;
  conversation_id: string;
  author_name: string;
  content: string;
}

/** One answer a bot offers when it asks the user a question. */
export interface ActionOption {
  label: string;
  description: string;
}

/** One question in an `ask_user` call: a chip label, a sentence, and its options. */
export interface ActionQuestion {
  question: string;
  header: string;
  options: ActionOption[];
  multi: boolean;
  /** Label of the suggested option; the picker preselects it. */
  recommended?: string | null;
}

/** One question's answer: the picked labels, and the words typed under 「其他」. */
export interface AskAnswer {
  question_index: number;
  labels: string[];
  other: string | null;
}

/** What an awaiting action asks the user: a yes/no card, or an `ask_user` question. */
export interface ActionConfirmation {
  prompt: string;
  confirm_label: string;
  deny_label: string;
  /** Absent on an older payload, which is a card. */
  kind?: "confirm" | "question";
  questions?: ActionQuestion[];
  /** The key a 「总是允许」 answer remembers; absent on cards that always ask. */
  rule?: string;
}

/** One source a search returned. */
export interface ActionDetailItem {
  title: string;
  url: string;
  snippet: string;
}

/** One parallel subtask's progress, as a `run_subtasks` row reports it. */
export interface ActionStep {
  title: string;
  status: "running" | "done" | "failed";
  output: string;
}

/** One line of a written file: kept, added or removed, as a unified diff without headers. */
export interface ActionDiffLine {
  op: "+" | "-" | " ";
  text: string;
}

/** What a finished tool actually did, in the user's terms. */
export interface ActionDetail {
  kind: "search" | "page" | "text" | "subtasks" | "diff";
  query?: string | null;
  provider?: string | null;
  items?: ActionDetailItem[];
  title?: string | null;
  url?: string | null;
  text?: string | null;
  /** A browser step: the numbered elements the model was shown, for the opened panel. */
  snapshot?: string | null;
  /** A terminal step: the whole command as it ran; `text` is then its output. */
  command?: string | null;
  steps?: ActionStep[];
  /** A file write: where it landed, how, and the lines that changed. */
  path?: string | null;
  display_path?: string | null;
  mode?: "create" | "overwrite" | "append" | null;
  added?: number | null;
  removed?: number | null;
  lines?: ActionDiffLine[];
  truncated?: boolean | null;
  /** Why the diff is not the whole story (a binary or oversized original). */
  note?: string | null;
}

export type TaskStatus = "todo" | "doing" | "done" | "blocked";

/** A note as it appears in the workbench list. */
export interface NoteSummary {
  id: string;
  title: string;
  preview: string;
  author_id: string | null;
  updated_at: string;
}

export interface Note {
  id: string;
  title: string;
  content: string;
  author_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskItem {
  id: string;
  title: string;
  /** Empty string when the bot gave no detail. */
  detail: string;
  status: TaskStatus;
  assignee_id: string | null;
  author_id: string | null;
  note: string;
  created_at: string;
  updated_at: string;
}

/** Which wire protocol a provider speaks; the last two run a local CLI on the user's subscription. */
export type ProviderProtocol = "openai-chat" | "anthropic-messages" | "claude-code" | "codex";

/** One picked model: a provider, and one of the models it lists. */
export interface ModelRef {
  provider_id: string;
  model: string;
  /** The reasoning effort a CLI engine runs with; only Claude Code / Codex take one. */
  effort?: string | null;
}

/** `custom` when the pick still resolves, `fallback` when its provider or model is gone. */
export type ModelStatus = "default" | "custom" | "fallback";

/** How much a bot may do without asking: 手动审批 / 自动审批 / 完全权限. */
export type PermissionMode = "ask" | "auto" | "full";

/** `GET/PATCH /settings/permissions` and the `permission_settings` stream event. */
export interface PermissionSettings {
  /** The workspace default; a bot with its own pick follows that instead. */
  mode: PermissionMode;
  /** Extra folders the file tools may reach, absolute paths; the working folder always is. */
  folders: string[];
  /** Where relative paths land; read-only here. */
  working_folder: string;
  /** The user's home folder, so the page can show `~/…` instead of the long path. */
  home: string;
}

/** The window a usage report or budget covers, aligned to the backend's local timezone. */
export type UsagePeriod = "day" | "week" | "month";

/** A per-agent usage cap: at most `limit` tokens per `period`. */
export interface TokenBudget {
  limit: number;
  period: UsagePeriod;
}

export interface Agent {
  id: string;
  name: string;
  instructions: string;
  kind: AgentKind;
  avatar_seed: string;
  appearance: Appearance | null;
  /** Null follows the workspace default. */
  model_ref: ModelRef | null;
  model_status: ModelStatus;
  /** When set, the model pauses before its next reply once this period's spend crosses `limit`. */
  token_budget: TokenBudget | null;
  /** Per-member tool packs; null means every pack is allowed (the default). */
  tool_permissions: ToolPermissions | null;
  /** How a send meets this member mid-reply; null follows the team setting. */
  send_mode: SendMode | null;
  /** This bot's own approval mode; null follows the workspace default. */
  permission_mode: PermissionMode | null;
  created_at: string;
  updated_at: string;
}

export interface Action {
  id: string;
  run_id: string;
  tool_call_id: string;
  tool_name: string;
  status: ActionStatus;
  summary: string;
  target_agent_id: string | null;
  target_conversation_id: string | null;
  error: string | null;
  /** Set while status is "awaiting"; absent on every other action. */
  confirmation?: ActionConfirmation | null;
  /** What the finished tool did, when it has something worth showing. */
  detail?: ActionDetail | null;
}

/** What the group's invisible dispatcher decided about one user message. */
export interface RouteDecision {
  /** Ordered agent ids for `answer`; empty for the other kinds. */
  speakers: string[];
  kind: "answer" | "steer" | "resume" | "status" | "none";
  /** The live task a `steer` or `resume` points at. */
  task_id?: string | null;
  /** One short Chinese sentence, the hint's tooltip. */
  reason: string;
  /** Who decided: the dispatcher, the fallback rule, an explicit @, or the user's own reroute. */
  by: "dispatcher" | "fallback" | "mention" | "user";
}

/** One task's row in a `kind: "status"` progress card. */
export interface StatusCardTask {
  /** The owner's agent id (or its name on a payload that resolves it first). */
  owner: string;
  title: string;
  status: GroupTask["status"];
  /** Steps done so far. */
  step: number;
  /** What it is doing now, short. */
  current: string;
  /** Its newest prose line. */
  latest: string;
  waiting_for: string | null;
}

/** The progress card the backend posts for a `status` question, with no model call per member. */
export interface StatusCard {
  tasks: StatusCardTask[];
  /** Members with nothing running, as ids or names. */
  idle: string[];
}

export interface Message {
  id: string;
  conversation_id: string;
  author_id: string | null;
  author_name: string;
  avatar_seed: string | null;
  kind: MessageKind;
  content: string;
  status: MessageStatus;
  seq: number;
  created_at: string;
  action: Action | null;
  mentions: string[];
  reply_to_id: string | null;
  /** On a `kind: "task"` row: the group task whose live state the card draws. */
  task_id?: string | null;
  mood: string | null;
  relay: Relay | null;
  /** Set on a private-chat message a bot sent while answering a user question elsewhere. */
  origin?: Origin | null;
  /** A user message sent while the previous reply was still running, and cut it off. */
  interrupts?: boolean;
  /** The run that wrote this message; null for messages from before runs carried an id. */
  run_id: string | null;
  /** Set on the answer a scheduled run left: which task produced it, by the name it had then. */
  schedule?: { id: string; title: string } | null;
  /** Attachments this message carries, in order: images, then PDFs and text files. */
  attachments?: Attachment[];
  /** Emoji reactions, oldest pill first; empty/absent when nobody reacted. */
  reactions?: Reaction[];
  /** A self-reminder's trigger: drawn as a quiet system row, not a user bubble. */
  reminder?: boolean;
  /**
   * Whether the model response that wrote this text also emitted tool calls. `true` means the
   * text is narration for the steps that follow; `false` means it is the answer; `null`/absent
   * on rows written before the field existed and on non-agent rows.
   */
  leads_to_tools?: boolean | null;
  /** On a user message in a group: how the dispatcher routed it. */
  route?: RouteDecision | null;
  /** On a `kind: "status"` row: the progress the card draws, straight from task data. */
  status_card?: StatusCard | null;
}

/** One emoji folded to its pill: the count of actors, whether the user is among them, and who. */
export interface Reaction {
  emoji: string;
  count: number;
  mine: boolean;
  actors: { id: string; name: string }[];
}

/** What `PUT`/`DELETE` on one message's reactions answer, and what the `reactions` event sends. */
export interface ReactionUpdate {
  message_id: string;
  conversation_id: string;
  reactions: Reaction[];
}

/** One attachment: an image, a PDF, or a text/code file; the bytes come from `GET /attachments/{id}`. */
export interface Attachment {
  id: string;
  mime: string;
  size: number;
  /** The name it was uploaded with; "" for an image sent before names were kept. */
  name: string;
}

export interface Conversation {
  id: string;
  kind: ConversationKind;
  title: string;
  purpose: string;
  member_ids: string[];
  /** A group's standing rules, read into every prompt there; "" when none. */
  rules?: string;
  last_message: string | null;
  created_at: string;
  updated_at: string;
  /** A bot reply arrived after the newest message the user saw in this chat. */
  unread?: boolean;
}

/**
 * What sending does while a reply is already running here: join the running turn, wait for
 * it to end, or stop it and answer this message instead.
 */
export type SendMode = "steer" | "queue" | "interrupt";

/** A completed run the sidebar lists under 已完成 until the user opens it. */
export interface FinishedRun {
  run_id: string;
  conversation_id: string;
  conversation_title: string;
  agent_name: string;
  finished_at: string;
  message_id: string | null;
  preview: string;
}

export interface Run {
  id: string;
  conversation_id: string;
  status: RunStatus;
  current_step: string;
  error: string | null;
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  current_agent_id: string | null;
  pending_agent_ids: string[];
  hops: number;
  /** The schedule that started this run; null for a run the user started. */
  schedule_id?: string | null;
}

/** One remembered 「总是允许」: a tool and the one bot it covers, or every bot. */
export interface ApprovalRule {
  id: string;
  tool_name: string;
  /** The tool's Chinese name, so the settings page never shows a raw identifier. */
  title: string;
  agent_id: string | null;
  agent_name: string | null;
  created_at: string;
}

/** One row of the context breakdown: its share of the request's prompt tokens. */
export interface ContextCategory {
  id: "system" | "memory" | "skills" | "tools" | "user" | "reply" | "calls" | "results" | "summary";
  label: string;
  tokens: number;
  /** Tool results only: one row per tool name, biggest first. */
  items?: { name: string; tokens: number }[];
}

/** How full a bot's context window is for one conversation, plus compaction's thresholds. */
export interface ContextMeter {
  conversation_id: string;
  agent_id: string;
  agent_name: string;
  model: string;
  provider: string;
  tokens: number;
  estimated: number;
  /** The last prompt size the provider reported; 0 when it reported nothing. */
  measured: number;
  window: number;
  usable: number;
  /** The auto-compaction point, in tokens. */
  threshold: number;
  /** The same point as a share of the window. */
  threshold_percent: number;
  /** Room kept for the model's answer. */
  output_reserve: number;
  /** Everything above the threshold: what auto-compaction exists to keep free. */
  buffer: number;
  /** Room left before auto-compaction, 0 once used passes it. */
  free: number;
  percent: number;
  categories: ContextCategory[];
}

/** `POST /conversations/{id}/compact`: the meter after the fold, and how far it moved. */
export interface CompactResult extends ContextMeter {
  compacted: boolean;
  before: number;
  after: number;
}

/** A fold the backend started or finished, so the bar updates without polling. */
export interface ContextEvent {
  conversation_id: string;
  state: "compacting" | "idle";
  auto: boolean;
  before?: number;
  after?: number;
  compacted?: boolean;
}

export interface Decision {
  id: string;
  kind: "action" | "question" | "workflow";
  conversation_id: string;
  message_id: string | null;
  who: string;
  where: string;
  prompt: string;
  if_unanswered: string;
}

export interface Workspace {
  agents: Agent[];
  conversations: Conversation[];
  supervisor_id: string;
  direct_conversation_id: string;
  /** False only until the user has named the supervisor on the first-run screen. */
  onboarded: boolean;
  active_runs: Run[];
  /** Cards and workflow questions waiting on the user. Absent on an older payload. */
  decisions?: Decision[];
  /** Completed runs the user has not opened, newest first (at most ten). */
  finished_runs?: FinishedRun[];
  /** Each group's live task threads, for the sidebar; only open work is listed. */
  group_threads?: GroupThread[];
}

/** One agent's totals for the period `GET /usage` was asked for. */
export interface UsageAgentRow {
  agent_id: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  model_calls: number;
  /** Spend in the agent's own budget period (not the query's); null when it has no budget. */
  budget_used: number | null;
  last_reply_at: string | null;
  last_mood: string | null;
}

/** Everything `GET /usage?period=` answers, one row per agent currently in the workspace. */
export interface UsageReport {
  period: UsagePeriod;
  since: string;
  agents: UsageAgentRow[];
  /** The exclusive end of the period, in the backend's timezone, with its offset. */
  until: string;
  totals: {
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    total_tokens: number;
    model_calls: number;
  };
  /** `totals.total_tokens` of the previous same-length period; 0 when there is none. */
  previous_total: number;
  /** Total tokens per `model_usage.purpose` value; the three known keys are always present. */
  by_purpose: Record<string, number>;
  /** Gap-free buckets across the period; `by_agent` only lists agents with non-zero tokens. */
  series: { start: string; total_tokens: number; by_agent: Record<string, number> }[];
}

export interface LegacyRole {
  name: string;
  instructions: string;
  avatar_seed: string | null;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
  latest_run: Run | null;
  legacy_roles: LegacyRole[];
  /** Older messages exist beyond `messages`; the next page starts at their first `seq`. */
  has_more: boolean;
  /** Messages quoted by rows in this page, although they sit outside it. */
  references: Message[];
}

export interface Snapshot {
  workspace: Workspace;
  conversation: ConversationDetail | null;
  sidebar: SidebarLayout;
  /** The walk the requested conversation is serving right now, if one is live. */
  workflow_run?: WorkflowRun | null;
}

export interface SidebarFolder {
  id: string;
  name: string;
  position: number;
}

/** One conversation's placement. Absent entries are unpinned and ungrouped. */
export interface SidebarItem {
  conversation_id: string;
  pinned: boolean;
  pin_position: number | null;
  folder_id: string | null;
  position: number;
}

export interface SidebarLayout {
  folders: SidebarFolder[];
  items: SidebarItem[];
}

export interface Delta {
  conversation_id: string;
  message_id: string;
  text: string;
}

export interface Health {
  status: "ok";
  configured: boolean;
  model: string;
  base_url: string;
  /** The name of the provider the default model belongs to; empty when nothing is configured. */
  provider_name: string;
  configuration_error: string | null;
  /** True when the backend demands the launch token the desktop shell handed to this window. */
  auth: boolean;
}

/** What a CLI engine says about one of its models: how to word it and which efforts it takes. */
export interface ModelDetail {
  model: string;
  label: string;
  description: string;
  efforts: string[];
  default_effort: string;
  /** Whether the CLI's own configuration currently points at this model. */
  current: boolean;
}

/** Which layer answered one capability: the user, the provider, models.dev, or the default. */
export type CapSource = "user" | "provider" | "catalog" | "default";

/** One model's resolved capabilities, and the layer each field came from. */
export interface ModelCaps {
  images: boolean;
  video: boolean;
  audio: boolean;
  pdf: boolean;
  context_window: number;
  max_output: number;
  reasoning: boolean;
  reasoning_levels: string[];
  sources: Record<string, CapSource>;
}

/** The user's own answer for one model (or `*`); an omitted field stays automatic. */
export interface ModelOverride {
  images?: boolean | null;
  context?: number | null;
  output?: number | null;
}

/** One model service. The key itself never leaves the backend; only `api_key_hint` does. */
export interface Provider {
  id: string;
  name: string;
  protocol: ProviderProtocol;
  base_url: string;
  /** `sk-…abcd`, `已设置` for short keys, null when the provider needs no key. */
  api_key_hint: string | null;
  models: string[];
  /** Resolved capabilities for every listed model, keyed by model name. */
  model_caps: Record<string, ModelCaps>;
  /** The user's own per-model answers (or `*`). */
  model_overrides: Record<string, ModelOverride>;
  /** For a CLI provider: what this machine says about each listed model; empty otherwise. */
  model_details?: ModelDetail[];
  created_at: string;
  updated_at: string;
}

/** One local CLI engine as `GET /providers` reports it: installed, and signed in. */
export interface EngineStatus {
  kind: "claude-code" | "codex";
  label: string;
  installed: boolean;
  ready: boolean;
  /** One Chinese sentence: what state it is in, and what to do when it is not ready. */
  detail: string;
  command: string;
  version: string;
}

/** Everything `GET /providers` answers: the list, and which model every bot follows by default. */
export interface ProvidersState {
  providers: Provider[];
  default: ModelRef | null;
  /** The optional workspace backup model a failed call retries on (absent on an older backend). */
  fallback?: ModelRef | null;
  /** The local CLI engines' availability (absent on an older backend). */
  engines?: EngineStatus[];
}

/** What a form's values send to `POST /providers/test` or `POST /providers/models`. */
export interface ProviderForm {
  /** An existing provider: the stored key is reused when the form leaves the key empty. */
  id?: string | null;
  protocol: ProviderProtocol;
  base_url: string;
  api_key?: string | null;
  model?: string;
}

/** One connection probe: how long it took, and whether the model can call tools. */
export interface ProviderProbe {
  ok: boolean;
  latency_ms: number;
  supports_tools: boolean;
  detail: string;
}

/** How a connector reaches its service: over the network, or as a process on this machine. */
export type ConnectorTransport = "http" | "stdio";

/** One credential the user fills in; secrets never come back, only a masked hint does. */
export interface ConnectorField {
  id: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
}

/** One service the "add connector" grid offers. `custom` ones ask for the address themselves. */
export interface ConnectorPreset {
  id: string;
  name: string;
  description: string;
  transport: ConnectorTransport;
  custom: boolean;
  fields: ConnectorField[];
  token_url: string | null;
  token_label: string | null;
  token_hint: string | null;
  docs_url: string | null;
  /** True when the vendor has a read-only mode worth switching to. */
  writes_toggle: boolean;
  /** e.g. "Node.js" for a preset that starts a local server. */
  requires: string | null;
}

/** One tool a connected service offers. Write tools ask before each call. */
export interface ConnectorTool {
  name: string;
  title: string;
  description: string;
  read_only: boolean;
}

export type ConnectorStatus = "connected" | "connecting" | "error" | "disabled" | "idle";

/** One connected service, shared by every bot. Secrets are never included. */
export interface Connector {
  id: string;
  preset: string;
  name: string;
  /** Prefix of its action tool names: `<slug>__<tool>`. */
  slug: string;
  transport: ConnectorTransport;
  url: string;
  command: string;
  args: string[];
  header_names: string[];
  env_names: string[];
  /** Stored values of the non-secret credential fields, e.g. a GitLab host. */
  values: Record<string, string>;
  /** Field id -> masked hint like `ghp…abcd`, for every stored secret. */
  secret_hints: Record<string, string>;
  allow_writes: boolean;
  enabled: boolean;
  status: ConnectorStatus;
  error: string | null;
  tools: ConnectorTool[];
  created_at: string;
  updated_at: string;
}

/** Everything `GET /connectors` answers: what can be added, and what already is. */
export interface ConnectorsState {
  presets: ConnectorPreset[];
  connectors: Connector[];
}

/** Who is using the sandbox right now. */
export interface ComputerHolder {
  run_id: string;
  conversation_id: string;
  agent_id: string;
  agent_name: string;
}

export type ComputerTarget = "local" | "cloud" | "mixed";

/** One machine the bots can drive. `view` is a live picture when that machine published one. */
export interface ComputerMachine {
  connected: boolean;
  name: string | null;
  os: string | null;
  screen: { width: number; height: number } | null;
  view: string | null;
}

/** The computers the bots may use. `token` only comes with `GET /computer`. */
export interface ComputerState {
  connected: boolean;
  name: string | null;
  os: string | null;
  screen: { width: number; height: number } | null;
  /** The live view of the machine the current target would use. */
  view: string | null;
  /** Which machine a call may use. Absent on an older payload; treat that as local. */
  target?: ComputerTarget;
  machines?: { local: ComputerMachine; cloud: ComputerMachine };
  holder: ComputerHolder | null;
  latest_image_id: string | null;
  token?: string;
}

/** macOS screen recording and accessibility. `needed` is false when this backend is not a Mac. */
export interface ComputerPermissions {
  needed: boolean;
  screen: boolean;
  accessibility: boolean;
}

/** What `POST /connectors` (and `PATCH`, plus `enabled`) sends. */
export interface ConnectorInput {
  preset?: string;
  name?: string;
  secrets?: Record<string, string>;
  allow_writes?: boolean;
  /** Custom remote servers only. */
  url?: string;
  /** Custom remote headers; sent whole, so sending replaces every stored one. */
  headers?: Record<string, string>;
  /** Custom local servers only. */
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  enabled?: boolean;
}

/** How a schedule repeats: a single moment, every N minutes, a 5-field cron rule, or a reminder. */
export type ScheduleKind = "once" | "interval" | "cron" | "reminder";

/** What the last run came to; `missed` means its time passed while the backend was down. */
export type ScheduleLastStatus = "completed" | "error" | "stopped" | "limited" | "missed";

/** One scheduled task: a prompt the backend runs in a conversation at the due time. */
export interface Schedule {
  id: string;
  title: string;
  prompt: string;
  conversation_id: string;
  conversation_title: string;
  kind: ScheduleKind;
  /** `once`: wall time "YYYY-MM-DDTHH:MM" in `timezone`. */
  at: string | null;
  every_minutes: number | null;
  /** A 5-field cron rule: 分 时 日 月 周. */
  cron: string | null;
  /** An IANA zone, e.g. "Asia/Shanghai". */
  timezone: string;
  /** Pre-approves the sandbox computer for the runs this schedule starts. */
  use_computer: boolean;
  enabled: boolean;
  /** ISO 8601 in UTC with an offset; null when it will not run again. */
  next_run_at: string | null;
  /** Due, but the conversation is busy: it starts as soon as that run ends. */
  queued: boolean;
  last_run_at: string | null;
  last_run_id: string | null;
  last_status: ScheduleLastStatus | null;
  last_error: string | null;
  run_count: number;
  /** A bot's own reminder, fired in the chat it was made in. */
  reminder?: boolean;
  /** Working right now in its hidden workspace; null when idle. */
  running: ScheduleLiveRun | null;
  /** The agent's name, or 「你」 when it was made here. */
  created_by: string;
  created_at: string;
  updated_at: string;
  /** The rule in the server's own words, e.g. 「每天 09:00」. */
  describe: string;
}

/** What a schedule's run is doing at this moment. */
export interface ScheduleLiveRun {
  run_id: string;
  /** The run's current step, e.g. 「「主管」正在搜索」. */
  step: string;
  started_at: string;
  model_calls: number;
  tool_calls: number;
}

/** `GET /schedules/{id}/run`: the newest fire, read-only, with every row it wrote. */
export interface ScheduleRunDetail {
  schedule_id: string;
  run: Run | null;
  messages: Message[];
}

/** Everything `GET /schedules` answers: the rules, the default zone and the server's limits. */
export interface SchedulesState {
  schedules: Schedule[];
  /** The backend's own zone, used as the form's default. */
  timezone: string;
  limits: { max_enabled: number; min_interval_minutes: number };
}

/** What `POST /schedules` (and `PATCH`) sends; the unused rule field goes as null. */
export interface ScheduleInput {
  title?: string;
  prompt?: string;
  conversation_id?: string;
  kind?: ScheduleKind;
  at?: string | null;
  every_minutes?: number | null;
  cron?: string | null;
  timezone?: string;
  use_computer?: boolean;
  enabled?: boolean;
}

/** 一条记忆属于谁：用户自己的、某个机器人的、还是整个团队的。 */
/** Work a group took on: one member answering one request, or a part handed on (`parent_id`). */
export interface GroupTask {
  id: string;
  conversation_id: string;
  request_message_id: string | null;
  parent_id: string | null;
  owner_id: string;
  assigned_by: string | null;
  title: string;
  instruction: string;
  /** `queued`: the member already has a task running, or the global cap is full. */
  status: "queued" | "running" | "awaiting" | "done" | "failed" | "stopped";
  /** The task's own run; null while it is still queued. */
  run_id: string | null;
  result_message_id: string | null;
  rounds: number;
  created_at: string;
  updated_at: string;
  /** The group message that carries the card; the card finds its live state through this row. */
  card_message_id: string | null;
  /** When the thread run actually began; null while queued. */
  started_at: string | null;
  /** The hidden conversation this task's turns happen in; null on rows from before threads. */
  thread_id: string | null;
  /** Tool calls so far, for the card's 「第 N 步」. */
  steps: number;
  /** What it is doing right now, already short (「跑测试」). */
  current_step: string;
  /** The newest prose the member wrote in the thread, possibly partial while streaming. */
  latest_text: string;
  /** The newest three action summaries, oldest first. */
  recent_steps: string[];
  /** The consent/question action the thread run waits on, so the card can show its buttons. */
  awaiting_action_id: string | null;
  /** Who holds what the task waits for, e.g. 「等「匠师」用完屏幕」. */
  waiting_for: string | null;
  /** On failed/stopped: why it ended, in words the card can show as-is. */
  end_reason: string | null;
  /** On stopped: who stopped it, e.g. 「你」, 「群里的「匠师」」, 「重启后中断」. */
  stopped_by: string | null;
  /** The user put a stopped/failed task down; it leaves every list but stays in the timeline. */
  dismissed_at: string | null;
  /** The user has seen a finished task's result; done work leaves the sidebar after that. */
  seen_at: string | null;
}

/** One live thread under a group, for the sidebar; only open work is listed. */
export interface GroupThread {
  group_id: string;
  task_id: string;
  thread_id: string;
  owner_id: string;
  title: string;
  status: GroupTask["status"];
  steps: number;
  current_step: string;
  /** Set while the thread waits on a consent or question action; the dot turns amber. */
  awaiting_action_id: string | null;
}

/** `task_progress`: a running task's live line, throttled so the card can animate without the thread. */
export interface TaskProgress {
  conversation_id: string;
  task_id: string;
  latest_text: string;
  recent_steps: string[];
  steps: number;
  current_step: string;
  awaiting_action_id: string | null;
  waiting_for: string | null;
  status: GroupTask["status"];
}

export type MemoryScope = "user" | "agent" | "team" | "group";

/** 你手动加的 / 机器人用 remember 记的 / 对话后自动整理的。 */
export type MemoryOrigin = "user" | "agent" | "extract";

/** One long-term memory row. `status` says whether it still counts, `superseded_by` who replaced it. */
export interface Memory {
  id: string;
  scope: MemoryScope;
  /** Set when `scope` is "agent". */
  agent_id: string | null;
  topic: string;
  content: string;
  pinned: boolean;
  status: "active" | "retired";
  /** Retired and replaced by a newer row; retired and null means it was forgotten. */
  superseded_by: string | null;
  origin: MemoryOrigin;
  /** The bot that wrote it, when `origin` is "agent" or "extract". */
  author_id: string | null;
  source_conversation_id: string | null;
  source_message_ids: string[];
  valid_from: string;
  valid_to: string | null;
  created_at: string;
  updated_at: string;
}

/** How memory behaves for the whole workspace. */
export interface MemorySettings {
  /** Neither read nor written: the bots answer without memories. */
  paused: boolean;
  /** Tidies a conversation into memories after it ends. */
  auto_extract: boolean;
  /** How much of the memory block may enter one prompt, in characters. */
  core_chars: number;
}

/** Everything `GET /memories` answers: every row, active and retired, plus the switches. */
export interface MemoriesState {
  memories: Memory[];
  settings: MemorySettings;
}

// ---- team settings, review, digest, tool permissions

/** The 主管验收 half of the team settings: whether the supervisor checks member work. */
export interface ReviewSettings {
  enabled: boolean;
  /** At most this many send-backs before the work is delivered as-is; 1..3. */
  max_rounds: number;
}

/** The 团队简报 half: a scheduled digest the supervisor writes in its private chat. */
export interface DigestSettings {
  enabled: boolean;
  frequency: "daily" | "weekly";
  /** "HH:MM", 24h, in the backend's timezone. */
  time: string;
  /** 0 = Monday … 6 = Sunday; used only when `frequency` is "weekly". */
  weekday: number;
  /** The system schedule backing the digest; read-only, null when there is none. */
  schedule_id: string | null;
}

/** How many steps one turn and one run may take, picked in 「设置 → 团队」. */
export type StepBudgetPreset = "unlimited" | "standard" | "long" | "marathon";

/** One preset's resolved numbers, read-only: the standard preset follows the backend env. */
export interface StepBudgetNumbers {
  preset: StepBudgetPreset;
  /** No ceiling at all: the other numbers are 0 and the engine skips every step check. */
  unlimited: boolean;
  turn_model_calls: number;
  turn_tool_calls: number;
  run_model_calls: number;
  run_tool_calls: number;
  workflow_model_calls: number;
  workflow_tool_calls: number;
}

/** Workspace-level team behaviour, stored as `meta` keys like the memory switches. */
export interface TeamSettings {
  /** How a send meets a running reply for every bot without its own pick. */
  send_mode: SendMode;
  /** The preset the engine reads on every budget check. */
  step_budget: StepBudgetPreset;
  /** Every preset's resolved numbers; read-only, sent back by both GET and PATCH. */
  step_budgets: Record<StepBudgetPreset, StepBudgetNumbers>;
  /** How many task threads may run at once, 1..8; the rest wait as `queued`. */
  max_parallel_tasks: number;
  review: ReviewSettings;
  digest: DigestSettings;
  /** The group dispatcher's model pick; null means automatic (the fastest configured one). */
  dispatch_model: ModelRef | null;
}

/** Which connector tools a member may call: everything, read-only, or none at all. */
export type ConnectorAccess = "all" | "read" | "none";

/** Per-member tool packs. `null` on the agent means every pack is allowed. */
export interface ToolPermissions {
  /** The code-execution pack (python/shell). */
  code: boolean;
  /** The workspace file pack. */
  files: boolean;
  /** The sandbox computer pack. */
  computer: boolean;
  /** Which connector tools are offered. */
  connectors: ConnectorAccess;
  /** Skill names this member may not use; missing means none are disabled. */
  skills_disabled?: string[];
}

// ---- agent skills

/** Where a skill came from: written here, by a bot, uploaded, from GitHub, or another app on this machine. */
export type SkillSource = "user" | "agent" | "upload" | "github" | "local";

/** One skill in the list `GET /skills` answers. */
export interface Skill {
  id: string;
  name: string;
  description: string;
  source: SkillSource;
  source_url: string | null;
  /** The bot that wrote it (agent source); null for the user. */
  author_id: string | null;
  /** The bot's name, so the card can credit it without a second read. */
  author_name: string | null;
  enabled: boolean;
  has_scripts: boolean;
  file_count: number;
  use_count: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

/** One file inside a skill, every file except SKILL.md, sorted by path. */
export interface SkillFile {
  path: string;
  size: number;
}

/** `GET /skills/{id}`: the list item plus the SKILL.md body and the other files. */
export interface SkillDetail extends Skill {
  /** The SKILL.md body, without its frontmatter. */
  body: string;
  files: SkillFile[];
}

/** `GET /skills/local`: one skill folder another agent (Claude Code, Codex, …) keeps on this machine. */
export interface LocalSkill {
  /** The folder, as the import request sends it back. */
  path: string;
  /** The app whose skills folder holds it, e.g. "Claude Code". */
  app: string;
  name: string;
  description: string;
  /** A skill with this name is already here. */
  installed: boolean;
  /** Why SKILL.md cannot be imported; null when it reads. */
  error: string | null;
}

// ---- workflows

/** What a node asks for: work, a question to the user, or a choice between paths. */
export type WorkflowStepKind = "do" | "ask_user" | "branch";
/** `auto` runs the step; `confirm` shows a card and waits for the user first. */
export type WorkflowStepGate = "auto" | "confirm";
/** What a failed step does to the rest of the run. */
export type WorkflowStepFail = "stop" | "retry" | "skip";

/** One node of a definition: a box on the canvas. */
export interface WorkflowStep {
  id: string;
  title: string;
  kind: WorkflowStepKind;
  instruction: string;
  gate: WorkflowStepGate;
  on_fail: WorkflowStepFail;
  /** Who runs it; null means the workflow's owner. */
  who: string | null;
  /** A `branch` node's options; every outgoing edge of it names one of them. */
  choices?: string[];
  /** Where the canvas put it; null lets the automatic layout decide. */
  x?: number | null;
  y?: number | null;
}

/**
 * One connection between two nodes; `when` is the branch choice it belongs to. `max_loops`
 * marks a loop edge: it goes back to an earlier node, and taking it runs the nodes from there
 * again, at most that many times. Null on every forward edge.
 */
export interface WorkflowEdge {
  from: string;
  to: string;
  when: string | null;
  max_loops?: number | null;
}

/** One node as a create/patch request writes it; the server fills in what is missing. */
export interface WorkflowStepInput {
  id?: string;
  title?: string;
  kind?: WorkflowStepKind;
  instruction: string;
  gate?: WorkflowStepGate;
  on_fail?: WorkflowStepFail;
  choices?: string[];
  x?: number | null;
  y?: number | null;
}

/** One edge as a create/patch request writes it. Omit the list to chain the nodes. */
export interface WorkflowEdgeInput {
  from: string;
  to: string;
  when?: string | null;
  max_loops?: number | null;
}

/** One workflow: the plan, who owns it, and whether it may run. */
export interface Workflow {
  id: string;
  name: string;
  description: string;
  owner_id: string;
  owner_name: string | null;
  /** `user` for the user, else the agent id that wrote it. */
  created_by: string;
  steps: WorkflowStep[];
  /** The connections; a straight list of steps is one chain. */
  edges: WorkflowEdge[];
  version: number;
  enabled: boolean;
  source: string;
  run_count: number;
  last_run_at: string | null;
  created_at: string;
  updated_at: string;
}

/** `GET /workflows/{id}`: the plan plus what the editor needs and the last few walks. */
export interface WorkflowDetail extends Workflow {
  /** The plan rendered as `WORKFLOW.md`, for copying out. */
  markdown: string;
  runs: WorkflowRun[];
}

/** How one step of one run stands. */
export type WorkflowStepStatus =
  | "pending"
  | "running"
  | "waiting"
  | "done"
  | "failed"
  | "skipped";

/** What an earlier round of a looped node produced; its message is still in the chat. */
export interface WorkflowStepRound {
  round: number;
  status: WorkflowStepStatus;
  output: string;
  choice: string | null;
  message_id: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** One step of a walk: its own status, attempts, and what it produced. */
export interface WorkflowStepState {
  /** The node this state belongs to; stable even when the plan is reordered. */
  node_id: string;
  index: number;
  title: string;
  kind: WorkflowStepKind;
  status: WorkflowStepStatus;
  attempts: number;
  output: string;
  /** The branch option this node picked, when it is a branch. */
  choice: string | null;
  /** The message this node produced, so the chat can label it. */
  message_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** 0 on the first pass; each loop that brought the node back adds one. */
  round?: number;
  /** Earlier rounds, oldest first. */
  history?: WorkflowStepRound[];
}

/** Where a walk is: the cursor, the steps, and whether anything is waiting for the user. */
export type WorkflowRunStatus =
  | "queued"
  | "running"
  | "waiting_user"
  | "paused"
  | "done"
  | "failed"
  | "stopped";

/** One walk through one workflow. */
export interface WorkflowRun {
  id: string;
  workflow_id: string;
  workflow_name: string;
  workflow_version: number;
  conversation_id: string;
  /** The engine run serving it right now, or the last one. */
  run_id: string | null;
  status: WorkflowRunStatus;
  cursor: number;
  input: string;
  summary: string;
  /** `user`, a schedule id, or the agent id that started it. */
  started_by: string;
  steps: WorkflowStepState[];
  /** The graph this walk copied when it began. */
  edges: WorkflowEdge[];
  /** Rounds each loop edge has taken so far, keyed `from->to#when` (see `edgeKey`). */
  loops?: Record<string, number>;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

// ---- run trace

export type TraceNodeKind = "user" | "supervisor" | "member";

/** One participant in a run: the user, or an agent that appeared in the exchange. */
export interface RunTraceNode {
  id: string;
  name: string;
  kind: TraceNodeKind;
}

export type TraceEdgeKind = "ask" | "mention" | "consult" | "dm" | "revise";

/** One hand-off between two participants. `revise` is a review send-back (supervisor → member). */
export interface RunTraceEdge {
  from: string;
  to: string;
  kind: TraceEdgeKind;
  at: string;
  conversation_id: string;
  message_id: string | null;
  label: string;
  /** What was handed over, in full: a consult's task, a private message, a review's feedback;
   *  null on the ask and @ edges, whose message is the step itself. */
  brief?: string | null;
}

/** Everything `GET /runs/{id}/trace` answers: the run, its graph, and every message it wrote. */
export interface RunTrace {
  run: Run;
  conversation_id: string;
  nodes: RunTraceNode[];
  edges: RunTraceEdge[];
  messages: Message[];
}

/** `GET /conversations/{id}/runs`: the conversation's runs, newest first. */
export interface ConversationRuns {
  runs: Run[];
}

// ---- member profile

/** The window a member profile's usage covers. */
export type ProfilePeriod = "day" | "week" | "month";

export interface ProfileReview {
  passed: number;
  revised: number;
}

export interface ProfileTopTool {
  tool_name: string;
  count: number;
}

export interface ProfileUsage {
  total_tokens: number;
  model_calls: number;
}

export interface ProfileRecentRun {
  run_id: string;
  conversation_id: string;
  conversation_title: string;
  status: RunStatus;
  goal: string;
  created_at: string;
}

/** Everything `GET /agents/{id}/profile` answers: all-time tallies plus period usage. */
export interface ProfileReport {
  agent_id: string;
  period: ProfilePeriod;
  replies: number;
  runs: number;
  runs_completed: number;
  runs_failed: number;
  tasks_done: number;
  tasks_open: number;
  reviews: ProfileReview;
  top_tools: ProfileTopTool[];
  usage: ProfileUsage;
  memories: number;
  recent_runs: ProfileRecentRun[];
}

// ---- webhook triggers

/** One secret URL that starts a schedule or a workflow now; the secret is never here. */
export interface Webhook {
  id: string;
  target_kind: "schedule" | "workflow";
  target_id: string;
  /** The public path to join to the backend address, e.g. `/hooks/w_ab12`. */
  path: string;
  created_at: string;
  last_called_at: string | null;
}

/** The one answer that carries the secret, when a hook is created or rotated. */
export interface WebhookWithSecret extends Webhook {
  secret: string;
}

// ---- team export / import

/** One planned or applied change in a team import preview. */
export interface TeamImportMemberPlan {
  name: string;
  final_name: string;
  kind: "supervisor" | "member";
  action: "create" | "rename" | "update" | "skip";
}

export interface TeamImportGroupPlan {
  title: string;
  final_title: string;
  action: "create" | "rename" | "skip";
  members: string[];
  purpose: string;
}

export interface TeamImportWorkflowPlan {
  name: string;
  final_name: string;
  action: "create" | "rename" | "skip";
}

/** `POST /team/import`: the preview (and, when applied, the same list after writing). */
export interface TeamImportPlan {
  applied: boolean;
  members: TeamImportMemberPlan[];
  groups: TeamImportGroupPlan[];
  workflows: TeamImportWorkflowPlan[];
  skills: { names: string[]; missing: string[] };
  warnings: string[];
}
