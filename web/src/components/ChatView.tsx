// One conversation, Grok-style: centered reading column, user turns as quiet bubbles on the
// right, agent turns as plain text with a small author line, tool actions as compact rows.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import type { Action, ActionConfirmation, ActionDetail, Agent, ConnectorsState, ConversationDetail, Memory, Message, Origin, Relay, Run } from "../api/types";
import { AskPanel, type AskQuestion } from "./AskPanel";
import { agentByID, drafts, membersOf, moodFor, store, useTeam } from "../state/store";
import { memoriesByMessage } from "../lib/memories";
import { agentModelLabel } from "../lib/modelOptions";
import type { Mood } from "../lib/mood";
import { openExternally, openWebURL } from "../lib/links";
import { prefersReducedMotion, usePresence, useSecondTick } from "../lib/motion";
import {
  buildTimeline,
  commentaryBlocks,
  visibleMessages,
  COMPUTER_TOOLS,
  computerSpans,
  liveNote,
  quoteExcerpt,
  replyAnchor,
  replyText,
  type BurstState,
  type CommentaryBlock,
  type ReplyAnchor,
  type ReplyTarget,
  type TimelineRow,
} from "../lib/turns";
import { groupPlan, isLiveActivity } from "../lib/activities";
import { boardTodos, isTodoBoard, planTodoBoards } from "../lib/todoBoard";
import { taskByID, threadHeading } from "../lib/threads";
import {
  elapsedMs,
  formatElapsed,
  isTaskArchived,
  isTaskDismissed,
  isTicking,
  queuedWaitLine,
  showsStatusWord,
  taskCardActions,
  taskReasonLine,
  taskStatusMeta,
  taskStepLine,
  type TaskStatus,
} from "../lib/taskProgress";
import { pendingReply } from "../lib/pending";
import { shortenPathSummary } from "../lib/paths";
import { commandRow } from "../lib/commandLine";
import { browserRowSummary } from "../lib/actionSummary";
import { queuedMessages, withoutQueued } from "../lib/sendMode";
import { runNotice } from "../lib/runNotice";
import { atBottom } from "../lib/scroll";
import type { RunRef } from "../lib/tabs";

import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Dialog, Modal } from "./Dialog";
import { Avatar, GroupAvatar } from "./Avatar";
import { ActivitySlotView } from "./ActivityCard";
import { TOOL_ICON } from "../lib/toolIcons";
import { AuthorLine } from "./AuthorLine";
import { Composer } from "./Composer";
import { ConnectorMark } from "./ConnectorMark";
import { ImageGrid, MessageAttachments } from "./Images";
import { Icon } from "./Icon";
import { MessageBody } from "./MessageBody";
import { openEmojiSheet, ReactionsRow } from "./Reactions";
import { WorkflowPanel } from "./WorkflowPanel";
import { GroupTasks } from "./GroupTasks";
import { outOfOrderQuote } from "../lib/quotes";
import { RouteHint } from "./RouteHint";
import { StatusCardView } from "./StatusCardView";
import { TodoBoardRow } from "./TodoBoard";
import { RUN_LABELS, isLive, progress, stepLabel, stepOfMessage } from "../lib/workflow";

import { TOOL_LABEL } from "../lib/toolLabels";
export { TOOL_LABEL };

/** The run tab's name: the run, the chat it ran in, and when it started. */
function runRef(run: Run): RunRef {
  return { id: run.id, conversation: run.conversation_id, at: run.created_at, schedule: run.schedule_id };
}

/** A connector tool's name is `<slug>__<tool>`: the double underscore marks it as one. */
function isConnectorTool(toolName: string | undefined): boolean {
  return toolName !== undefined && toolName.includes("__");
}

/** The service behind a connector tool, by the slug in front of its name; a connector the user
 * deleted since leaves the generic MCP mark. */
function connectorPresetOf(toolName: string, connectors: ConnectorsState | null): string {
  const slug = toolName.slice(0, toolName.indexOf("__"));
  return connectors?.connectors.find((connector) => connector.slug === slug)?.preset ?? "mcp";
}

const SUGGESTIONS = [
  "帮我调研一个产品方向，组建合适的团队给出结论",
  "把这个目标拆成几步，并安排成员分别完成",
  "组一个小组，讨论后给我一份对比表",
];

export function ChatView({
  conversationID,
  arrive,
  onOpenMember,
  onOpenConversation,
  onOpenGroupSettings,
  onOpenMemories,
  onClosePrivate,
  onOpenRun,
  onOpenWorkflows,
  onOpenActivity,
  onOpenWorkLog,
}: {
  conversationID: string;
  /** Opened from another conversation, not by the window coming up: fade the column in. */
  arrive?: boolean;
  onOpenMember: (agentID: string) => void;
  onOpenConversation: (id: string) => void;
  onOpenGroupSettings: (id: string) => void;
  /** The reply's memory chip asks for the whole 记忆 panel. */
  onOpenMemories: () => void;
  onClosePrivate: () => void;
  /** Opens the run-trace sheet for one run. */
  onOpenRun: (run: RunRef) => void;
  /** Opens the 工作流 panel from the floating flow. */
  onOpenWorkflows: () => void;
  /** Opens one bot's work detail: a card, a chip, or a row of the work log. */
  onOpenActivity: (activityID: string) => void;
  /** Opens one bot's whole work log, from its right-click menu. */
  onOpenWorkLog: (agentID: string) => void;
}) {
  const state = useTeam((s) => s);
  // Read once: the class belongs to this mounting, not to whatever the parent renders later.
  const arriving = useRef(Boolean(arrive));
  const workspace = state.workspace;
  const detail = state.selectedID === conversationID ? state.detail : null;
  // What the timeline draws: a group hides the consult rows whose answer follows right below.
  const shownDetail = useMemo(() => {
    if (!detail) return detail;
    const messages = detail.conversation.kind === "group" ? visibleMessages(detail.messages, "group") : detail.messages;
    // Queued sends live in the strip above the composer until their turn comes.
    const shown = withoutQueued(messages);
    return shown.length === detail.messages.length ? detail : { ...detail, messages: shown };
  }, [detail]);
  /** Sends still waiting for this conversation's run to end. */
  const queued = useMemo(() => (detail ? queuedMessages(detail.messages) : []), [detail]);
  // The bot's question this run is parked on. The composer answers it, so the newest one wins:
  // only one input line exists, and answering it is what makes the run go on.
  const ask = useMemo<AskQuestion | null>(() => {
    if (!detail) return null;
    let found: AskQuestion | null = null;
    for (const message of detail.messages) {
      const action = message.action;
      if (action?.status !== "awaiting" || action.confirmation?.kind !== "question") continue;
      found = {
        id: action.id,
        who: message.author_name,
        questions: action.confirmation.questions ?? [],
      };
    }
    return found;
  }, [detail]);
  const conversation = workspace?.conversations.find((c) => c.id === conversationID) ?? detail?.conversation;
  const members = conversation ? membersOf(workspace, conversation) : [];
  // A chat can hold several lanes at once: a bot working, another (or the same one) answering.
  const runs = useMemo(
    () => (workspace?.active_runs ?? []).filter((item) => item.conversation_id === conversationID),
    [workspace, conversationID],
  );
  // The step line under the timeline follows the working lane; a reply lane writes its own rows.
  const run = runs.find((item) => item.lane !== "reply") ?? runs[0];
  const isGroup = conversation?.kind === "group";
  /** A direct chat may keep tasks too: a request the bot turned into its own thread. */
  const isDirect = conversation?.kind === "direct";
  /** A task's own thread: hidden from the lists, opened from its group's card. */
  const isThread = conversation?.kind === "thread";
  // The card draws its live state from the same task rows the group chip reads.
  useEffect(() => {
    if (isDirect) void store.loadGroupTasks(conversationID);
  }, [conversationID, isDirect]);
  /** The group this thread belongs to, once the store has found it: its way back. */
  const threadGroup = isThread ? workspace?.conversations.find((c) => c.id === state.threadGroups[conversationID]) : undefined;
  useEffect(() => {
    if (isThread) void store.locateThread(conversationID);
  }, [conversationID, isThread]);
  /** A walk's own page: its flow rides at the top and its thread carries the steps. */
  const isWorkflowPage = conversation?.kind === "workflow";
  /** The walk's float starts open and stays put; closing it gives the thread its width back. */
  const [railOpen, setRailOpen] = useState(true);
  // The walk this chat is serving (its owner's own chat): the stream keeps it current, and a
  // chat opened mid-walk reads it once. A finished walk shows until the user puts it away.
  const workflowRun = state.workflowRuns[conversationID];
  useEffect(() => {
    void store.loadWorkflowRun(conversationID);
  }, [conversationID]);
  const isPrivate = conversation?.kind === "private";
  const isSupervisorThread = conversationID === workspace?.direct_conversation_id;
  /** The message a quote line just jumped to; a short tint marks where it landed. */
  const [flash, setFlash] = useState<string | null>(null);
  const flashTimer = useRef<number | null>(null);

  useEffect(() => {
    void store.open(conversationID);
  }, [conversationID]);

  useEffect(
    () => () => {
      if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    },
    [],
  );

  const scroller = useRef<HTMLDivElement>(null);
  const jumpTo = (messageID: string) => {
    const element = scroller.current;
    const row = element?.querySelector<HTMLElement>(
      `[data-message-id="${CSS.escape(messageID)}"]`,
    );
    if (!element || !row) return;
    const messages = detail?.messages ?? [];
    const newest = messages.length > 0 && messages[messages.length - 1]!.id === messageID;
    if (newest) {
      // Centring the last message would leave empty space under it, so the view would look
      // away from the bottom and offer the jump button with the newest message on screen.
      follow(true);
      element.scrollTo({
        top: element.scrollHeight,
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    } else {
      row.scrollIntoView({
        block: "center",
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
    }
    setFlash(messageID);
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlash(null), 1200);
  };
  const focus = state.focus;
  useEffect(() => {
    if (!focus || focus.conversationID !== conversationID || !focus.messageID) return;
    const row = scroller.current?.querySelector<HTMLElement>(
      `[data-message-id="${CSS.escape(focus.messageID)}"]`,
    );
    if (!row) return;
    jumpTo(focus.messageID);
    store.clearFocus();
  }, [focus, conversationID, detail]);
  // Whether the view follows new output. Any scroll up by the reader lets go at once, however
  // small; reaching the bottom again (or the button below) takes hold again.
  const pinned = useRef(true);
  // True between content growing and the scroll that growth causes, so it does not unpin.
  const growing = useRef(false);
  const lastTop = useRef(0);
  // Shown while the reader is away from the newest message; it stays mounted while it leaves.
  const [away, setAway] = useState(false);
  // The hold matches the exit animation's own length, or the button would unmount mid-blur.
  const latest = usePresence(away ? true : null, 180);
  const follow = (value: boolean) => {
    pinned.current = value;
    setAway(!value);
  };
  const toLatest = () => {
    const element = scroller.current;
    if (!element) return;
    follow(true);
    element.scrollTo({ top: element.scrollHeight, behavior: prefersReducedMotion() ? "auto" : "smooth" });
  };
  // The newest message of your own: a send always brings the view to it, however far up the
  // reader had scrolled, since seeing what was just said is the point of sending.
  const lastOwnID = (() => {
    const messages = detail?.messages ?? [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]!.kind === "user") return messages[index]!.id;
    }
    return "";
  })();
  const seenOwn = useRef("");
  useLayoutEffect(() => {
    const element = scroller.current;
    if (!element || !lastOwnID) return;
    if (seenOwn.current === lastOwnID) return;
    const first = seenOwn.current === "";
    seenOwn.current = lastOwnID;
    if (first) return;
    follow(true);
    element.scrollTop = element.scrollHeight;
  }, [lastOwnID]);
  const lastMessage = detail?.messages[detail.messages.length - 1];
  const scrollKey = `${detail?.messages.length ?? 0}|${lastMessage?.content.length ?? 0}|${lastMessage?.status ?? ""}`;
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);
  // The line's slot is kept once history was read: what appears or goes away above the reader
  // must never push the row they are on.
  const [keptSlot, setKeptSlot] = useState(false);
  // Where the reader's row sat before a page landed, so it sits in the same pixel after.
  const hold = useRef<{
    conversationID: string;
    first: string;
    row: string | null;
    offset: number;
    height: number;
    top: number;
  } | null>(null);

  const loadOlder = async () => {
    if (!detail?.has_more || loadingOlder) return;
    hold.current = null;
    setOlderError(null);
    setKeptSlot(true);
    setLoadingOlder(true);
    const failure = await store.loadOlder(conversationID);
    setLoadingOlder(false);
    if (failure) {
      hold.current = null;
      setOlderError(failure);
    }
  };

  // The height is read while the slot is already in the column, so the line inside it never
  // counts as a change of the content the reader was looking at.
  useLayoutEffect(() => {
    const element = scroller.current;
    if (!loadingOlder || !element || hold.current) return;
    // The first row still on screen is the anchor: whatever lands above it, it must not move.
    const row = [...element.querySelectorAll<HTMLElement>("[data-message-id]")].find(
      (node) => node.getBoundingClientRect().bottom > 0,
    );
    const top = element.getBoundingClientRect().top;
    hold.current = {
      conversationID,
      first: detail?.messages[0]?.id ?? "",
      row: row?.dataset.messageId ?? null,
      offset: row ? row.getBoundingClientRect().top - top + element.scrollTop : 0,
      height: element.scrollHeight,
      top: element.scrollTop,
    };
  }, [loadingOlder, conversationID, detail]);

  useLayoutEffect(() => {
    const element = scroller.current;
    if (!element) return;
    const held = hold.current;
    if (held && held.conversationID !== conversationID) hold.current = null;
    const firstID = detail?.messages[0]?.id ?? "";
    if (held && held.conversationID === conversationID && firstID !== held.first) {
      hold.current = null;
      const row = held.row
        ? element.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(held.row)}"]`)
        : null;
      if (pinned.current) element.scrollTop = element.scrollHeight;
      else if (row) {
        const offset = row.getBoundingClientRect().top - element.getBoundingClientRect().top + element.scrollTop;
        element.scrollTop += offset - held.offset;
      } else element.scrollTop = held.top + (element.scrollHeight - held.height);
    } else if (pinned.current) {
      element.scrollTop = element.scrollHeight;
    }
    updateEdgeFades(element);
  }, [scrollKey, conversationID]);

  // A page shorter than the window leaves the column half empty: keep reading until it fills
  // or history runs out, so a tall screen never shows a conversation that looks finished.
  useEffect(() => {
    const element = scroller.current;
    if (!element || !detail?.has_more || loadingOlder || olderError) return;
    if (element.scrollHeight > element.clientHeight + 8) return;
    void loadOlder();
    // loadOlder is recreated each render; the values it reads are the dependencies here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, loadingOlder, olderError, conversationID]);

  useEffect(() => {
    follow(true);
    seenOwn.current = "";
    lastTop.current = 0;
    hold.current = null;
    setLoadingOlder(false);
    setOlderError(null);
    setKeptSlot(false);
  }, [conversationID]);

  // Content grows without a scroll (streaming text, an image that finished loading, a resized
  // window): a reader who was at the bottom stays there, and the edges follow the new size.
  const hasConversation = Boolean(conversation);
  useEffect(() => {
    const element = scroller.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      if (pinned.current) {
        growing.current = true;
        element.scrollTop = element.scrollHeight;
        requestAnimationFrame(() => {
          growing.current = false;
        });
      }
      updateEdgeFades(element);
    });
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => observer.disconnect();
  }, [conversationID, hasConversation]);

  // The composer floats over the thread. Its height (quotes, images, a question card) becomes
  // the thread's bottom padding, so the last message can always scroll clear of it; the padding
  // resizes the column, and the observer above keeps a pinned reader at the bottom.
  const dock = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = dock.current;
    const thread = scroller.current;
    if (!element || !thread) return;
    const measure = () => thread.style.setProperty("--dock-h", `${element.offsetHeight}px`);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [conversationID, hasConversation]);

  const arrivals = useArrivals(detail);
  const roster = isGroup ? members : [];
  const outside = isGroup && conversation
    ? (workspace?.agents ?? []).filter((agent) => !conversation.member_ids.includes(agent.id))
    : [];
  const memberNames = useMemo(() => (workspace?.agents ?? []).map((a) => a.name), [workspace]);
  const openByName = (name: string) => {
    const agent = workspace?.agents.find((a) => a.name === name);
    if (agent) onOpenMember(agent.id);
  };

  // The message area's own right-click menu: copy what is selected (or the whole message),
  // reply to a bot's reply, copy or open a link. Anywhere without anything to offer keeps the
  // native menu, so text fields and images behave as they always do.
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const shownMenu = usePresence(menu, 140);
  const closeMenu = useCallback(() => setMenu(null), []);
  // Taking the tail back is destructive and confirmed first; the message is kept for the text.
  const [rewind, setRewind] = useState<{
    message: Message;
    /** How many real messages (tool rows aside) the tail holds, for the confirm text. */
    after: number;
    busy: boolean;
    error: string | null;
  } | null>(null);
  const startEditing = (message: Message) => {
    // startEdit remembers the draft it takes over; the message's own text replaces it.
    store.startEdit(conversationID, message.id);
    drafts.set(conversationID, message.content);
  };
  const runRewind = async () => {
    if (!rewind || rewind.busy) return;
    setRewind({ ...rewind, busy: true, error: null });
    const failure = await store.rewind(conversationID, rewind.message.id);
    if (failure) setRewind({ ...rewind, busy: false, error: failure });
    else setRewind(null);
  };
  const onMessageMenu = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!detail) return;
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, [contenteditable='true']")) return;
    const items = messageMenu(target, {
      messages: detail.messages,
      privateChat: isPrivate,
      canReply: !isPrivate,
      // Only a chat you write in can be edited or rewound; a walk page is not one.
      canRestructure: conversation?.kind === "direct" || conversation?.kind === "group",
      onEdit: startEditing,
      onRewind: (message) => {
        const index = detail.messages.findIndex((item) => item.id === message.id);
        const after =
          index < 0
            ? 0
            : detail.messages.slice(index + 1).filter((item) => item.kind !== "action").length;
        setRewind({ message, after, busy: false, error: null });
      },
      onReply: (anchor) =>
        store.startReply(conversationID, {
          messageID: anchor.messageID,
          name: anchor.name,
          excerpt: quoteExcerpt(anchor.text, 120),
        }),
      onOpenRun,
      agentOf: (agentID) => state.workspace?.agents.find((agent) => agent.id === agentID),
      onOpenWorkLog,
      // The sheet lives at the app's root: opening it must not re-render this conversation.
      onReact: (messageID) =>
        openEmojiSheet({
          x: event.clientX,
          y: event.clientY,
          onPick: (emoji) => void store.toggleReaction(messageID, emoji),
        }),
    });
    if (items.length === 0) return;
    event.preventDefault();
    setMenu({ x: event.clientX, y: event.clientY, items });
  };

  if (!conversation) {
    return <div className="chat-empty">{state.workspaceError ?? "正在读取会话…"}</div>;
  }

  const subtitle = isWorkflowPage
    ? workflowRun
      ? `工作流 · 第 ${Math.min(progress(workflowRun).done + 1, progress(workflowRun).total || 1)}/${progress(workflowRun).total || 1} 步`
      : "工作流"
    : isSupervisorThread
      ? "团队负责人"
      : isGroup
        ? `${members.length} 位成员`
        : isThread
          ? `${members[0]?.name ?? "成员"} · 任务过程`
          : "成员 · 私聊";
  const owner = conversation.kind === "direct" || isThread ? members[0] : undefined;
  // The model this bot answers with right now: its own pick, or the default it follows.
  const speakingModel = owner ? agentModelLabel(owner, state.providers, state.health) : null;
  const placeholder = isWorkflowPage
    ? workflowRun?.status === "waiting_user"
      ? "回答这一步的问题，回车发送"
      : "对这个工作流说点什么"
    : isGroup
        ? `给「${conversation.title}」发消息，@ 成员让 TA 直接回复`
        : isThread
          ? `给${owner?.name ?? "成员"}说说这个任务，回车发送`
          : isSupervisorThread
            ? `给${owner?.name ?? "负责人"}发消息`
            : `给${owner?.name ?? "成员"}发消息`;
  // Who answers when the draft names nobody: a direct chat's own bot, a thread's member, else
  // the supervisor.
  const fallbackAnswerer = agentByID(
    workspace,
    conversation.kind === "direct" || isThread
      ? conversation.member_ids[0] ?? workspace?.supervisor_id ?? ""
      : workspace?.supervisor_id ?? "",
  );
  const [left, right] = conversation.kind === "private" ? members : [];
  const pairLabel = `${left?.name ?? "成员"}与${right?.name ?? "成员"}的私聊`;
  /** A thread is titled by the task it carries, not by the member's line in the group. */
  const threadTitle = conversation.purpose || conversation.title;

  return (
    <section
      className={[
        "chat",
        arriving.current ? "arriving" : "",
        isWorkflowPage && workflowRun ? (railOpen ? "rail-open" : "rail-closed") : "",
      ]
        .filter(Boolean)
        .join(" ")}
      aria-label={conversation.title}
    >
      <header className="chat-header" data-drag-region>
        <div className="column chat-header-inner" data-drag-region>
          <div className="chat-title" data-drag-region>
            {conversation.kind === "private" ? (
              <div className="private-pill" role="group" aria-label={pairLabel}>
                {left && (
                  <Avatar
                    seed={left.avatar_seed}
                    appearance={left.appearance}
                    size={24}
                    name={left.name}
                    agentID={left.id}
                    mood={moodFor(state, left.id, conversationID)}
                  />
                )}
                <span className="private-pill-name">{left?.name}</span>
                <span className="private-pill-arrow" aria-hidden="true">
                  ⇄
                </span>
                {right && (
                  <Avatar
                    seed={right.avatar_seed}
                    appearance={right.appearance}
                    size={24}
                    name={right.name}
                    agentID={right.id}
                    mood={moodFor(state, right.id, conversationID)}
                  />
                )}
                <span className="private-pill-name">{right?.name}</span>
              </div>
            ) : owner ? (
              <Avatar
                seed={owner.avatar_seed}
                appearance={owner.appearance}
                size={28}
                name={owner.name}
                mood={moodFor(state, owner.id, conversationID)}
              />
            ) : (
              <GroupAvatar members={members} size={28} />
            )}
            {conversation.kind !== "private" && (
              <div className="chat-title-text">
                <h1>{isThread ? threadHeading(threadTitle) : conversation.title}</h1>
                <p>
                  {subtitle}
                  {speakingModel && (
                    <span className="chat-model" title={speakingModel.title}>
                      {speakingModel.text}
                    </span>
                  )}
                  {isThread && threadGroup && (
                    <button
                      type="button"
                      className="thread-back"
                      onClick={() => onOpenConversation(threadGroup.id)}
                    >
                      <span className="thread-back-arrow" aria-hidden="true">←</span>
                      回到「{threadGroup.title}」
                    </button>
                  )}
                </p>
              </div>
            )}
          </div>
          {isGroup && (
            <div className="chat-header-actions">
              <GroupTasks conversationID={conversationID} onOpenConversation={onOpenConversation} />
              <button type="button" className="icon-button" onClick={() => onOpenGroupSettings(conversationID)} aria-label="群组设置">
                <Icon name="info" />
              </button>
            </div>
          )}
          {isThread && run && (
            <div className="chat-header-actions">
              <button
                type="button"
                className="ghost-button thread-stop"
                onClick={() => void store.stop(conversationID)}
              >
                停止
              </button>
            </div>
          )}
        </div>
      </header>

      <div className="chat-body">
        <div className="chat-main">
      <div
        className="chat-scroll"
        ref={scroller}
        onWheel={(event) => {
          // The wheel says which way the reader meant before any scroll lands. Wheeling down
          // onto the bottom takes hold again even while output keeps pushing it further.
          const element = event.currentTarget;
          // A trackpad's elastic bounce at the bottom still reports upward deltas, and the
          // reader never left the bottom: only a real upward scroll lets go. Without this the
          // button appeared while the newest message was already on screen.
          if (event.deltaY < 0 && pinned.current) {
            if (!atBottom(element.scrollHeight, element.scrollTop, element.clientHeight)) follow(false);
          } else if (event.deltaY > 0 && !pinned.current) {
            const gap = element.scrollHeight - element.scrollTop - element.clientHeight;
            if (gap - event.deltaY < 24) follow(true);
          }
        }}
        onTouchMove={(event) => {
          // The same rule for a finger: a drag that has not left the bottom keeps following.
          const element = event.currentTarget;
          if (pinned.current && !atBottom(element.scrollHeight, element.scrollTop, element.clientHeight)) {
            follow(false);
          }
        }}
        onScroll={(event) => {
          const element = event.currentTarget;
          const bottom = atBottom(element.scrollHeight, element.scrollTop, element.clientHeight);
          const movedUp = element.scrollTop < lastTop.current - 1;
          lastTop.current = element.scrollTop;
          // A jump made by growing content never decides; the reader's own scroll does: up
          // lets go immediately (keys and the scrollbar too), the very bottom takes hold.
          if (!growing.current) {
            if (movedUp && pinned.current && !bottom) follow(false);
            else if (bottom && !pinned.current) follow(true);
          }
          updateEdgeFades(element);
          // Reading into the top of the window asks for the page above it. A failed read waits
          // for the retry button instead of firing again on every scroll event.
          if (element.scrollTop < 240 && detail?.has_more && !loadingOlder && !olderError) void loadOlder();
        }}
        onContextMenu={onMessageMenu}
      >
        <div className="column">
          {(detail?.has_more || keptSlot || loadingOlder) && (
            <div className="history-loading" role="status">
              {loadingOlder ? "正在加载更早的消息…" : ""}
            </div>
          )}
          {olderError && (
            <div className="inline-error" role="alert">
              <span>{olderError}</span>
              <button type="button" className="link-button" onClick={() => void loadOlder()}>
                重试
              </button>
            </div>
          )}
          {isPrivate && <p className="private-notice">两位成员的私聊，你只能旁观。</p>}
          {state.detailError && state.selectedID === conversationID && (
            <div className="inline-error" role="alert">
              <span>{state.detailError}</span>
              <button type="button" className="link-button" onClick={() => void store.reloadSelected()}>
                重新加载
              </button>
            </div>
          )}

          {!detail && !state.detailError && <div className="skeleton" aria-label="正在读取消息" />}

          {detail && detail.messages.length === 0 && (
            <EmptyChat
              title={
                isPrivate
                  ? "还没有私聊消息"
                  : isSupervisorThread
                    ? "今天想让团队做什么？"
                    : isGroup
                      ? `「${conversation.title}」`
                      : `和${owner?.name ?? "成员"}聊聊`
              }
              detail={isGroup ? conversation.purpose : owner?.instructions.split("\n")[0]}
              onPick={(text) => drafts.set(conversationID, text)}
              showSuggestions={isSupervisorThread}
            />
          )}

          {detail && (
            <Timeline
              detail={shownDetail ?? detail}
              workspaceAgents={workspace?.agents ?? []}
              memberNames={memberNames}
              onMention={openByName}
              onOpenMember={onOpenMember}
              onOpenConversation={onOpenConversation}
              onOpenMemories={onOpenMemories}
              onOpenActivity={onOpenActivity}
              onReply={(target) => store.startReply(conversationID, target)}
              onJump={jumpTo}
              flashID={flash}
              arrivals={arrivals}
              canReply={!isPrivate}
              privateChat={isPrivate}
              running={runs.length > 0}
              step={run?.current_step ?? ""}
              stepTitle={(messageID) => {
                const found = workflowRun ? stepOfMessage(workflowRun, messageID) : null;
                return found ? stepLabel(found.step, found.round) : null;
              }}
            />
          )}

          {/* A walk that is working says so until its words land: a step can take a while, and an
              empty page would read as "nothing happened". */}
          {isWorkflowPage &&
            workflowRun &&
            isLive(workflowRun.status) &&
            !(detail?.messages ?? []).some(
              (m) => m.kind === "agent" && m.status === "streaming",
            ) && (
              <div className="wf-loading" role="status" aria-live="polite">
                <span className="wf-loading-dot" aria-hidden="true" />
                <span>
                  {workflowRun.status === "queued"
                    ? "正在排队，马上开始…"
                    : workflowRun.status === "waiting_user"
                      ? "等你回答…"
                      : (() => {
                          const running = workflowRun.steps
                            .filter((step) => step.status === "running")
                            .map((step) => step.title)
                            .join("、");
                          const at = Math.min(
                            progress(workflowRun).done + 1,
                            progress(workflowRun).total || 1,
                          );
                          // Several branches may be running: the line names all of them.
                          return `正在做第 ${at} 步${running ? `「${running}」` : ""}…`;
                        })()}
                </span>
              </div>
            )}

          {(() => {
            if (!detail || !conversation) return null;
            const pending = pendingReply(run, conversation, detail.messages, workspace?.supervisor_id);
            const thinker = pending ? agentByID(workspace, pending.agentID) : undefined;
            if (!thinker) return null;
            // The avatar in its thinking pose is the loading state; nothing else goes under it.
            return (
              <div className="turn agent thinking" role="status" aria-live="polite">
                <AuthorLine agent={thinker} mood="thinking" note="正在思考" />
              </div>
            );
          })()}

          {detail?.latest_run &&
            (detail.latest_run.status === "stopped" ||
              detail.latest_run.status === "error" ||
              detail.latest_run.status === "limited") &&
            !run &&
            // A workflow that parked on a question or a pause stopped its run on purpose:
            // the strip above already says what it waits for, so no "已停止" scare here.
            !(
              workflowRun &&
              (workflowRun.status === "waiting_user" || workflowRun.status === "paused")
            ) &&
            (() => {
              const notice = runNotice(detail.latest_run.status, detail.latest_run.error);
              // A limit already has its reason as a quiet notice in the thread: repeating it in
              // a box right under that line is the duplication the reader notices first. Keep
              // only the way to the trace, on the column's own left edge.
              if (notice === null) {
                return (
                  <div className="run-footer">
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => onOpenRun(runRef(detail.latest_run!))}
                    >
                      查看过程
                    </button>
                  </div>
                );
              }
              return (
                <div className={`run-notice ${detail.latest_run.status}`}>
                  {notice.title}
                  {notice.detail && <span>：{notice.detail}</span>}
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => onOpenRun(runRef(detail.latest_run!))}
                  >
                    查看过程
                  </button>
                </div>
              );
            })()}
        </div>
      </div>

      <div className="chat-dock" ref={dock}>
      {latest.value && (
        <div className="column latest-column">
          <button
            type="button"
            className={latest.open ? "to-latest" : "to-latest leaving"}
            onClick={toLatest}
            tabIndex={latest.open ? undefined : -1}
            aria-hidden={!latest.open}
            aria-label="回到最新"
            title="回到最新"
          >
            <Icon name="arrow-down" size={16} />
          </button>
        </div>
      )}

      {isPrivate ? (
        <div className="column composer-column private-column">
          <button type="button" className="ghost-button private-close" onClick={onClosePrivate}>
            关闭聊天
          </button>
        </div>
      ) : (
        <div className="column composer-column">
          {queued.length > 0 && (
            <ul className="queued-list" aria-label="排队中的消息">
              {queued.map((message) => (
                <li key={message.id} className="queued-row">
                  <span className="queued-tag">排队中</span>
                  <span className="queued-text">{message.content || "（图片）"}</span>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => void store.cancelQueued(message.run_id ?? "", conversationID)}
                  >
                    撤回
                  </button>
                </li>
              ))}
            </ul>
          )}
          <Composer
            conversationID={conversationID}
            roster={roster}
            outside={outside}
            runs={runs}
            placeholder={placeholder}
            autoFocus
            fallbackAnswerer={fallbackAnswerer}
            ask={ask}
          />
        </div>
      )}
      </div>
        </div>

        {/* A walk's own page: the flow stands at the right of the thread it belongs to. */}
        {isWorkflowPage && workflowRun && (
          <aside className={`wf-rail${railOpen ? "" : " is-closed"}`} aria-label="工作流流转">
            <WorkflowPanel
              run={workflowRun}
              open={railOpen}
              onToggle={() => setRailOpen((value) => !value)}
              onPause={() => void store.pauseWorkflow(workflowRun.id)}
              onResume={() => void store.resumeWorkflow(workflowRun.id)}
              onStop={() => void store.stopWorkflow(workflowRun.id)}
              onRetry={(nodeID) => void store.controlWorkflowNode(workflowRun.id, nodeID, "retry")}
              onSkip={(nodeID) => void store.controlWorkflowNode(workflowRun.id, nodeID, "skip")}
              onAnswer={(text) => void store.answerWorkflow(workflowRun.id, text)}
              onOpenWorkflow={onOpenWorkflows}
            />
          </aside>
        )}
      </div>

      {shownMenu.value && (
        <ContextMenu
          x={shownMenu.value.x}
          y={shownMenu.value.y}
          items={shownMenu.value.items}
          open={shownMenu.open}
          onClose={closeMenu}
        />
      )}

      {rewind && (
        <Modal>
          <Dialog
            title="从这里重新开始"
            className="rewind-dialog"
            onClose={() => setRewind(null)}
            footer={
              <>
                <button
                  type="button"
                  className="ghost-button"
                  disabled={rewind.busy}
                  onClick={() => setRewind(null)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="danger-button"
                  disabled={rewind.busy}
                  onClick={() => void runRewind()}
                >
                  <Icon name="rewind" size={15} />
                  {rewind.busy ? "正在删除…" : "删掉后面的内容"}
                </button>
              </>
            }
          >
            <p className="rewind-lead">
              保留「{quoteExcerpt(rewind.message.content, 60) || "这条消息"}」，它之后的内容全部删除。
            </p>
            <p className="hint">
              {rewind.after > 0
                ? `后面还有 ${rewind.after} 条消息，删掉后找不回来。`
                : "它已经是最后一条消息了。"}
            </p>
            {rewind.error && (
              <p className="form-error" role="alert">
                {rewind.error}
              </p>
            )}
          </Dialog>
        </Modal>
      )}
    </section>
  );
}

function copyText(text: string) {
  void navigator.clipboard.writeText(text).catch(() => undefined);
}

/** The selection, when it lies inside `within`; empty otherwise. */
function selectionIn(within: Element): string {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return "";
  const range = selection.getRangeAt(0);
  if (!within.contains(range.commonAncestorContainer)) return "";
  return selection.toString().trim();
}

/**
 * What a right-click inside the message area offers, from the most specific thing under the
 * pointer outward: the selection, a link, then the message it landed on. Empty means "nothing
 * of ours here" and the native menu stays.
 */
function messageMenu(
  target: HTMLElement,
  context: {
    messages: Message[];
    privateChat: boolean;
    canReply: boolean;
    /** A chat the user writes in: editing and rewinding are its operations. */
    canRestructure: boolean;
    onEdit: (message: Message) => void;
    onRewind: (message: Message) => void;
    onReply: (anchor: ReplyAnchor) => void;
    onOpenRun: (run: RunRef) => void;
    /** Open the emoji panel for this message's id, at the pointer. */
    onReact: (messageID: string) => void;
    /** The bot behind an author id, for the 工作记录 item. */
    agentOf: (agentID: string) => Agent | undefined;
    /** Open one bot's whole work log. */
    onOpenWorkLog: (agentID: string) => void;
  },
): MenuItem[] {
  const items: MenuItem[] = [];
  const scroller = target.closest(".chat-scroll");
  const selected = scroller ? selectionIn(scroller) : "";
  if (selected) items.push({ label: "复制", icon: "copy", onSelect: () => copyText(selected) });

  const link = target.closest<HTMLAnchorElement>("a[href]");
  if (link && /^https?:\/\//i.test(link.href)) {
    const href = link.href;
    items.push(
      { label: "打开链接", icon: "link", onSelect: () => openWebURL(href) },
      { label: "复制链接", icon: "copy", onSelect: () => copyText(href) },
    );
  }

  const row = target.closest<HTMLElement>("[data-message-id]");
  const message = row ? context.messages.find((m) => m.id === row.dataset.messageId) : undefined;
  if (message && message.kind !== "action" && message.kind !== "notice" && message.kind !== "task") {
    const runID = message.run_id;
    if (runID) {
      // The tab names this chat and when the reply was written; the trace corrects the start time.
      const run = { id: runID, conversation: message.conversation_id, at: message.created_at };
      items.push({ label: "查看这次的过程", icon: "history", onSelect: () => context.onOpenRun(run) });
    }
    if (message.kind === "user") {
      if (!selected && message.content.trim()) {
        const text = message.content.trim();
        items.push({ label: "复制消息", icon: "copy", onSelect: () => copyText(text) });
      }
    } else {
      const anchor = replyAnchor(context.messages, message.id, { privateChat: context.privateChat });
      if (anchor) {
        if (!selected) items.push({ label: "复制整条回复", icon: "copy", onSelect: () => copyText(anchor.text) });
        if (context.canReply && anchor.settled) {
          items.push({ label: "回复", icon: "reply", onSelect: () => context.onReply(anchor) });
        }
      }
    }
    // Changing the thread itself: only in a chat the user writes in, and only on a settled
    // row — a streaming reply has no tail worth keeping or cutting.
    if (context.canRestructure && message.status === "complete") {
      if (message.kind === "user") {
        items.push({ label: "编辑", icon: "compose", onSelect: () => context.onEdit(message) });
      }
      items.push({ label: "从这里重新开始", icon: "rewind", onSelect: () => context.onRewind(message) });
    }
    // The same rule the backend applies: a finished message in a user-facing conversation.
    if (message.status === "complete") {
      items.push({ label: "表情回应", icon: "smiley", onSelect: () => context.onReact(message.id) });
    }
  }
  // A bot's row (its face, its answer, its tool row) also offers its whole work log.
  const author = message?.author_id ? context.agentOf(message.author_id) : undefined;
  if (author) {
    items.push({ label: "工作记录", icon: "history", onSelect: () => context.onOpenWorkLog(author.id) });
  }
  return items;
}

/**
 * Which message ids showed up while this conversation was already open. The first snapshot is
 * the baseline — the load and every switch are still — and only rows newer than the newest one
 * seen then are marked, once, however often the list re-renders. A page read further back
 * prepends older rows, which are history, not arrivals.
 */
function useArrivals(detail: ConversationDetail | null): Set<string> {
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  const seen = useRef<{ conversationID: string; newest: number } | null>(null);
  useEffect(() => {
    const newest = detail?.messages[detail.messages.length - 1];
    if (!detail || !newest) return;
    const previous = seen.current;
    if (previous === null || previous.conversationID !== detail.conversation.id) {
      seen.current = { conversationID: detail.conversation.id, newest: newest.seq };
      return;
    }
    if (newest.seq <= previous.newest) return;
    const added = detail.messages.filter((message) => message.seq > previous.newest).map((message) => message.id);
    seen.current = { ...previous, newest: newest.seq };
    setFresh((current) => {
      const next = new Set(current);
      for (const id of added) next.add(id);
      return next;
    });
  }, [detail]);
  return fresh;
}

export function Timeline({
  detail,
  workspaceAgents,
  memberNames,
  onMention,
  onOpenMember,
  onOpenConversation,
  onOpenMemories,
  onOpenActivity,
  onHandoff,
  onReply,
  onJump,
  flashID,
  arrivals,
  canReply,
  privateChat,
  running,
  step,
  highlightIDs,
  stepTitle,
}: {
  detail: ConversationDetail;
  workspaceAgents: Agent[];
  memberNames: string[];
  onMention: (name: string) => void;
  onOpenMember: (id: string) => void;
  onOpenConversation: (id: string) => void;
  /** Opens the 记忆 panel from a reply's chip. */
  onOpenMemories: () => void;
  /** Opens one activity's detail sheet, from its card or its chip. A read-only record (a
   *  scheduled run, a task card's peek) leaves it out and keeps the full steps instead. */
  onOpenActivity?: (id: string) => void;
  /** A hand-off row (ask_agent, start_task, group_assign, direct_message) in a record:
   *  return true when the record opened the section it led to, else the row keeps its default. */
  onHandoff?: (message: Message) => boolean;
  onReply: (target: ReplyTarget) => void;
  /** Scroll to one message and mark it briefly (the quote line above a user bubble). */
  onJump: (messageID: string) => void;
  flashID: string | null;
  /** Ids that arrived while this conversation was open; they get one arrival, never a replay. */
  arrivals: Set<string>;
  /** Private chats are watched, not joined: no reply affordance there. */
  canReply: boolean;
  /** Two bots talking: each same-author run is its own reply. */
  privateChat: boolean;
  /** This conversation's run is still going, even if every row so far is complete. */
  running: boolean;
  /** The run's current step, e.g. 「主管」正在思考（25 秒）: a long model call shows it. */
  step: string;
  /** Messages to tint, e.g. while their node or edge is highlighted in the run graph. */
  highlightIDs?: Set<string>;
  /** A workflow step's label for the reply it produced, or null for an ordinary reply. */
  stepTitle?: (messageID: string) => string | null;
}) {
  const state = useTeam((s) => s);
  // Parents quoted from outside the page travel with it, so their quote lines still read.
  const byID: Record<string, Message> = Object.fromEntries(
    [...detail.references, ...detail.messages].map((m) => [m.id, m]),
  );
  // A group shows one bot's work as a card while it runs and a chip above its answer: every row
  // written inside an activity folds away, except the consent cards the user acts on.
  const openActivity = onOpenActivity ?? (() => undefined);
  const groupChat = detail.conversation.kind === "group" && onOpenActivity !== undefined;
  const plan = useMemo(
    () => (groupChat ? groupPlan(detail.messages, state.activities, detail.conversation.id) : null),
    [groupChat, detail.messages, detail.conversation.id, state.activities],
  );
  const messages = plan?.messages ?? detail.messages;
  const activities = state.activities;
  // One turn's board writes fold into one row: the newest board, drawn where the turn's
  // first plan was written, so it never jumps as later writes arrive.
  const boards = useMemo(() => planTodoBoards(messages), [messages]);
  const liveRuns = new Set(
    (state.workspace?.active_runs ?? [])
      .filter((run) => run.status === "running")
      .map((run) => run.id),
  );
  /** The board one row draws, from the lane's live board or the newest write's items. */
  const boardOf = (message: Message) => {
    const newest = boards.shown.get(message.id);
    if (newest === undefined) return null;
    const activity = newest.activity_id ? activities[newest.activity_id] : undefined;
    const live = activity
      ? isLiveActivity(activity.status)
      : newest.run_id !== null && liveRuns.has(newest.run_id);
    const todos = live && activity?.todos?.length ? activity.todos : boardTodos(newest);
    // A row from before boards carried their items keeps its plain sentence.
    if (todos.length === 0) return null;
    return { id: newest.id, todos, live };
  };
  // A reload reads the rows before any event, so an activity the chat names is read on demand.
  useEffect(() => {
    for (const slot of plan?.slots ?? []) {
      if (!activities[slot.activityID]) void store.loadActivity(slot.activityID);
    }
  }, [plan, activities]);
  // A finished chip rides inside the answer's own article, right under the author line; a live
  // card and a finished one with no answer stand where the activity's rows were.
  const chipsAt = new Map<string, string[]>();
  const standalone = new Map<number, string[]>();
  for (const slot of plan?.slots ?? []) {
    const activity = activities[slot.activityID];
    const answer = activity?.answer_message_id ?? null;
    if (activity && !isLiveActivity(activity.status) && answer !== null && messages.some((m) => m.id === answer)) {
      chipsAt.set(answer, [...(chipsAt.get(answer) ?? []), slot.activityID]);
      continue;
    }
    standalone.set(slot.at, [...(standalone.get(slot.at) ?? []), slot.activityID]);
  }
  // Spacing, author lines and the single tools row of each reply.
  const rows = buildTimeline(messages, { privateChat, running });
  // Where each row sits, for the one rule that needs to look back at the question answered.
  const messageIndex = new Map(messages.map((message, index) => [message.id, index]));
  // Which finished replies left a memory behind, by the id of the message that wrote it.
  const memoryIndex = memoriesByMessage(state.memories?.memories ?? []);
  const spans = new Map(computerSpans(messages, { privateChat }).map((span) => [span.start, span]));
  // Sandbox sessions keep their own shape: a note inside one is not running commentary.
  const folded = new Set<number>();
  for (const span of spans.values()) {
    for (let index = span.start; index <= span.end; index += 1) folded.add(index);
  }
  const commentary = commentaryBlocks(rows, { exclude: folded });
  const blockAt = new Map(commentary.map((block) => [block.head, block]));
  const swallowed = new Set(commentary.flatMap((block) => block.members.slice(1)));
  const rowClass = (base: string, messageID: string) =>
    `${base}${flashID === messageID ? " flash" : ""}${highlightIDs?.has(messageID) ? " highlight" : ""}`;
  const authorLine = (message: Message, live: BurstState | null) => {
    const agent = workspaceAgents.find((a) => a.id === message.author_id);
    return (
      <AuthorLine
        agent={agent}
        fallbackName={message.author_name}
        fallbackSeed={message.avatar_seed}
        mood={liveMood(live) ?? (agent ? moodFor(state, agent.id, message.conversation_id) : "idle")}
        note={liveNote(live, step, message.author_name)}
        onOpen={agent ? () => onOpenMember(agent.id) : undefined}
      />
    );
  };
  /** ``head`` marks the first bubble of one speaker's run: it carries the bubble's tail. */
  const renderRow = ({ message, continues, showAuthor, tools, live, run }: TimelineRow, head = false) => {
    if (message.kind === "status") {
      // The dispatcher's progress card: no author, no bubble, no reply tools.
      return (
        <div key={message.id} className={rowClass("turn status-turn", message.id)} data-message-id={message.id}>
          {message.status_card ? (
            <StatusCardView card={message.status_card} agents={workspaceAgents} />
          ) : (
            <p className="notice-line">{message.content}</p>
          )}
        </div>
      );
    }
    if (message.kind === "notice") {
      // A system note (a model switch): quiet, centered, no bubble and no author line.
      return (
        <div key={message.id} className={rowClass("turn notice", message.id)} data-message-id={message.id}>
          <p className="notice-line">
            {message.content}
            <InviteNoticeButton conversationID={detail.conversation.id} message={message} />
          </p>
        </div>
      );
    }
    if (message.kind === "user") {
      if (message.reminder) {
        // A reminder's trigger is a system line: quiet, centered, no bubble and no author.
        return (
          <div key={message.id} className={rowClass("turn notice reminder", message.id)} data-message-id={message.id}>
            <p className="notice-line">
              <Icon name="alarm" size={12} /> {message.content}
            </p>
          </div>
        );
      }
      const parent = message.reply_to_id ? byID[message.reply_to_id] : undefined;
      return (
        <div
          key={message.id}
          className={`${rowClass("turn user", message.id)}${run ? ` run-${run}` : ""}${
            arrivals.has(message.id) ? " enter-user" : ""
          }`}
          data-message-id={message.id}
        >
          <div className="user-row">
            <div className="bubble-stack">
              <div className="bubble">
                {parent && (
                  <button
                    type="button"
                    className="bubble-quote"
                    onClick={() => onJump(parent.id)}
                    aria-label={`查看被回复的消息，来自${parent.author_name}`}
                  >
                    回复 {parent.author_name}：{quoteExcerpt(parent.content, 60)}
                  </button>
                )}
                {message.attachments && message.attachments.length > 0 && (
                  <MessageAttachments attachments={message.attachments} from="你" />
                )}
                {message.content && (
                  <MessageBody text={message.content} markdown={false} memberNames={memberNames} onMention={onMention} />
                )}
              </div>
              {/* The pills ride the bubble's bottom edge, like a badge on it. */}
              <ReactionsRow
                reactions={message.reactions ?? []}
                onToggle={(emoji) => void store.toggleReaction(message.id, emoji)}
              />
            </div>
          </div>
          {detail.conversation.kind === "group" && message.route && (
            <RouteHint
              message={message}
              conversationID={detail.conversation.id}
              members={workspaceAgents.filter((agent) => detail.conversation.member_ids.includes(agent.id))}
              agents={workspaceAgents}
              taskTitleOf={(taskID) =>
                state.groupTasks[detail.conversation.id]?.find((task) => task.id === taskID)?.title
              }
            />
          )}
        </div>
      );
    }
    if (message.kind === "task") {
      // The group's one card for this task: its live state comes from the store, not the row.
      return (
        <article
          key={message.id}
          className={rowClass("turn agent task-turn", message.id)}
          data-message-id={message.id}
        >
          <TaskCard
            message={message}
            conversationID={detail.conversation.id}
            direct={detail.conversation.kind === "direct"}
            onOpenConversation={onOpenConversation}
            onOpenMember={onOpenMember}
            onMention={onMention}
            onOpenMemories={onOpenMemories}
            memberNames={memberNames}
          />
        </article>
      );
    }
    if (message.kind === "action") {
      const board = boardOf(message);
      const row = board ? (
        <TodoBoardRow
          key={message.id}
          id={board.id}
          todos={board.todos}
          live={board.live}
          continues={continues}
        />
      ) : (
        <ActionRow
          key={message.id}
          message={message}
          continues={continues}
          onOpenMember={onOpenMember}
          onOpenConversation={onOpenConversation}
          onHandoff={onHandoff}
        />
      );
      if (!showAuthor) return row;
      // A reply that starts with a tool call: the author line comes first, so the search
      // reads as part of the answer rather than a notice above it.
      return (
        <article
          key={message.id}
          className={`${rowClass("turn agent opens-with-action", message.id)}${arrivals.has(message.id) ? " enter-agent" : ""}`}
          data-message-id={message.id}
        >
          {authorLine(message, live)}
          {row}
        </article>
      );
    }
    // The watched exchange itself stays private: only the marker reaches the user.
    if (message.relay && message.relay.direction !== "summary") {
      return (
        <div
          key={message.id}
          className={rowClass("turn relay-only", message.id)}
          data-message-id={message.id}
        >
          <RelayMarker relay={message.relay} onOpen={() => onOpenConversation(message.relay!.conversation_id)} />
        </div>
      );
    }
    const agent = workspaceAgents.find((a) => a.id === message.author_id);
    // An answer that reached an older question is named here; the activity's request is the
    // fallback for a row that carries no ``reply_to_id`` of its own.
    const chipIDs = chipsAt.get(message.id) ?? [];
    const questionID =
      message.reply_to_id ??
      (chipIDs.length > 0 ? activities[chipIDs[0]!]?.request_message_id ?? null : null);
    const quote = outOfOrderQuote(
      messages,
      messageIndex.get(message.id) ?? -1,
      questionID,
    );
    const mood = agent ? moodFor(state, agent.id, message.conversation_id) : "idle";
    // Live state belongs to the burst, not the row: it shows on the one author line above.
    // The question an answer belongs to is the bubble's quote (out of order only), never a
    // short 「回复 你」 note here.
    const note = liveNote(live, step, message.author_name);
    const text = tools ? replyText(detail.messages, message.id, { privateChat }) : "";
    const memoryWritten = memoryIndex.get(message.id);
    return (
      <article
        key={message.id}
        className={`${rowClass(continues ? "turn agent continues" : "turn agent", message.id)}${
          arrivals.has(message.id) && showAuthor ? " enter-agent" : ""
        }`}
        data-message-id={message.id}
      >
        {message.origin && (
          <OriginLine origin={message.origin} onOpen={() => onOpenConversation(message.origin!.conversation_id)} />
        )}
        {message.relay && (
          <RelaySummaryLabel relay={message.relay} onOpen={() => onOpenConversation(message.relay!.conversation_id)} />
        )}
        {message.schedule && (
          <p className="schedule-note">
            <Icon name="alarm" size={12} />
            <span>定时任务「{message.schedule.title}」</span>
          </p>
        )}
        {stepTitle?.(message.id) && (
          <p className="workflow-step-note">
            <Icon name="workflows" size={12} />
            <span>{stepTitle(message.id)}</span>
          </p>
        )}
        {showAuthor && (
          <AuthorLine
            agent={agent}
            fallbackName={message.author_name}
            fallbackSeed={message.avatar_seed}
            mood={liveMood(live) ?? mood}
            note={note}
            onOpen={agent ? () => onOpenMember(agent.id) : undefined}
          />
        )}
        {/* How the bot got to this answer, collapsed to one line: the steps are one click away. */}
        {(chipsAt.get(message.id) ?? []).map((id) => (
          <ActivitySlotView
            key={`activity-${id}`}
            activity={activities[id]}
            activityID={id}
            agent={undefined}
            showAuthor={false}
            onOpen={openActivity}
          />
        ))}
        {/* The reply is a bubble of its own, so a long answer reads as one block; its pills ride
            the bottom edge. Nothing to show yet (a call that opened only tools) keeps no bubble. */}
        <div className="bubble-stack">
          {(message.content !== "" || message.status !== "streaming" || (message.attachments?.length ?? 0) > 0) && (
            <div className={head ? "bubble reply head" : "bubble reply"}>
              {quote && (
                <button
                  type="button"
                  className="bubble-quote"
                  onClick={() => onJump(quote.id)}
                  aria-label={`查看被回复的消息，来自${quote.label}`}
                >
                  回复 {quote.label}：{quote.excerpt}
                </button>
              )}
              {/* An empty reply that has just opened: its avatar above is already thinking. */}
              {(message.content !== "" || message.status !== "streaming") && (
                <MessageBody
                  text={message.content}
                  markdown
                  streaming={message.status === "streaming"}
                  memberNames={memberNames}
                  onMention={onMention}
                />
              )}
              {message.attachments && message.attachments.length > 0 && (
                <MessageAttachments attachments={message.attachments} from={message.author_name} />
              )}
            </div>
          )}
          {/* The pills belong to the bubble, not to the tools row: a reply that is not the
              last of its run still shows them. */}
          {message.status === "complete" && (
            <ReactionsRow
              reactions={message.reactions ?? []}
              onToggle={(emoji) => void store.toggleReaction(message.id, emoji)}
            />
          )}
        </div>
        {tools && (
          <TurnTools
            text={text}
            canReply={canReply}
            onReply={() =>
              onReply({
                messageID: message.id,
                name: message.author_name,
                excerpt: quoteExcerpt(text, 120),
              })
            }
          />
        )}
        {/* What it remembered sits right under the tools row.
            The read-only run sheet stays a record: no way out to memory from there. */}
        {tools && canReply && message.status === "complete" && memoryWritten && (
          <MemoryChip memories={memoryWritten} onOpenMemories={onOpenMemories} />
        )}
      </article>
    );
  };
  /** Several short notes of one burst as one bubble; the newest row carries the menu and pills. */
  const renderCommentary = (block: CommentaryBlock, head: boolean) => {
    const row = rows[block.head]!;
    const latest = rows[block.latest]!.message;
    return (
      <article
        key={row.message.id}
        className={`${rowClass(head ? "turn agent" : "turn agent continues", latest.id)}${
          arrivals.has(latest.id) ? " enter-agent" : ""
        }`}
        data-message-id={latest.id}
      >
        {row.showAuthor && authorLine(row.message, row.live)}
        <div className="bubble-stack">
          <div className={head ? "bubble reply head commentary" : "bubble reply commentary"}>
            <ol className="commentary-lines">
              {block.members.map((index) => (
                <li key={rows[index]!.message.id}>{quoteExcerpt(rows[index]!.message.content, 200)}</li>
              ))}
            </ol>
          </div>
          <ReactionsRow
            reactions={latest.reactions ?? []}
            onToggle={(emoji) => void store.toggleReaction(latest.id, emoji)}
          />
        </div>
      </article>
    );
  };
  const out: React.ReactNode[] = [];
  // True from a row that shows the author line until the run's first bubble has taken the tail.
  let tailWaiting = false;
  const bubbleShown = (row: TimelineRow) =>
    row.message.kind === "agent" &&
    (row.message.content !== "" ||
      row.message.status !== "streaming" ||
      (row.message.attachments?.length ?? 0) > 0);
  // Rows that can share one list. A sandbox session, a confirmation and a way into another
  // conversation each own their shape, so only plain completed calls fold together.
  const plainTool = (entry: TimelineRow) => {
    const action = entry.message.action;
    if (entry.message.kind !== "action" || action === null) return false;
    if (action.status === "awaiting" || action.tool_name === "direct_message") return false;
    if (COMPUTER_TOOLS.has(action.tool_name)) return false;
    // The board keeps its own row: its expanded list is not a line inside a shared frame.
    if (isTodoBoard(entry.message)) return false;
    return !(action.tool_name === "workflow_run" && action.target_conversation_id !== null);
  };
  /** A live card, or a finished one that left no answer, stands where its rows would have been. */
  const pushActivitySlots = (at: number) => {
    for (const id of standalone.get(at) ?? []) {
      const activity = activities[id];
      // Before the payload lands, the turn's own rows still name the speaker: the card must
      // never show a blank face (a grey placeholder) for a bot that is working.
      const owner = detail.messages.find((message) => message.activity_id === id);
      const authorID = activity?.agent_id ?? owner?.author_id ?? null;
      const agent = authorID ? workspaceAgents.find((item) => item.id === authorID) : undefined;
      const before = messages[at - 1];
      const continues = Boolean(before && authorID && before.author_id === authorID);
      out.push(
        <ActivitySlotView
          key={`activity-${id}`}
          activity={activity}
          activityID={id}
          agent={agent}
          fallbackName={activity?.agent_name ?? owner?.author_name}
          fallbackSeed={owner?.avatar_seed}
          showAuthor={!continues}
          onOpen={openActivity}
          quote={outOfOrderQuote(messages, at, activity?.request_message_id)}
          onJumpQuote={onJump}
        />,
      );
    }
  };
  for (let index = 0; index < rows.length; index += 1) {
    const span = spans.get(index);
    const row = rows[index]!;
    pushActivitySlots(index);
    // An older board of this turn: the newest write is drawn on the turn's first board row.
    if (boards.hidden.has(row.message.id)) continue;
    if (swallowed.has(index)) continue;
    if (row.showAuthor) tailWaiting = true;
    const head = tailWaiting && bubbleShown(row);
    if (head) tailWaiting = false;
    const block = blockAt.get(index);
    if (block) {
      out.push(renderCommentary(block, head));
      continue;
    }
    if (!span) {
      // Consecutive tool rows are one block with dividers, not a pile of separate pills. They
      // keep their own row and their own fold; only the frame around them is shared.
      if (plainTool(row)) {
        let end = index;
        while (
          end + 1 < rows.length &&
          plainTool(rows[end + 1]!) &&
          rows[end + 1]!.continues &&
          !spans.has(end + 1)
        ) {
          end += 1;
        }
        if (end > index) {
          const group = rows.slice(index, end + 1);
          const inner = (
            <div
              key={`tools-${row.message.id}`}
              className={`action-group${row.continues ? " continues" : ""}`}
            >
              {/* The author line is drawn once, above the frame: a row inside it must not
                  open a second one of its own. */}
              {group.map((entry) => renderRow({ ...entry, showAuthor: false }, false))}
            </div>
          );
          out.push(
            row.showAuthor ? (
              <article
                key={`tools-turn-${row.message.id}`}
                className={`${rowClass("turn agent opens-with-action", row.message.id)}${
                  arrivals.has(row.message.id) ? " enter-agent" : ""
                }`}
                data-message-id={row.message.id}
              >
                {authorLine(row.message, row.live)}
                {inner}
              </article>
            ) : (
              inner
            ),
          );
          index = end;
          continue;
        }
      }
      out.push(renderRow(row, head));
      continue;
    }
    // The sandbox work folds into one row; who did it stays above, the answer after stays out.
    const inside = rows.slice(span.start, span.end + 1);
    // Text whose response has already committed to tools, or is still streaming with no
    // verdict: it belongs to the fold, in the note slot under its header, so it never appears
    // outside as an answer and then jumps in when the next step arrives.
    const notes =
      span.noteStart === undefined ? [] : rows.slice(span.noteStart, (span.noteEnd ?? span.noteStart) + 1);
    const session = (
      <ComputerSession
        key={`computer-${row.message.id}`}
        rows={inside.map((entry, at) => (at === 0 ? { ...entry, showAuthor: false } : entry))}
        notes={notes}
        continues={row.continues}
        renderRow={renderRow}
        renderNote={(entry) => (
          <MessageBody
            text={entry.message.content}
            markdown
            streaming={entry.message.status === "streaming"}
            memberNames={memberNames}
            onMention={onMention}
          />
        )}
      />
    );
    out.push(
      row.showAuthor ? (
        <article
          key={`computer-turn-${row.message.id}`}
          className={`${rowClass("turn agent opens-with-action", row.message.id)}${arrivals.has(row.message.id) ? " enter-agent" : ""}`}
          data-message-id={row.message.id}
        >
          {authorLine(row.message, row.live)}
          {session}
        </article>
      ) : (
        session
      ),
    );
    index = span.end;
  }
  // Work that has not written a row yet: its card waits at the end, where its rows will land.
  pushActivitySlots(rows.length);
  return <>{out}</>;
}

/** Elapsed work time in the card's header; terminal cards freeze at their last update. */
function TaskElapsed({
  startedAt,
  endedAt,
  ticking,
}: {
  startedAt: string | null;
  endedAt: string | null;
  ticking: boolean;
}) {
  const now = useSecondTick(ticking);
  if (!startedAt) return null;
  return <span className="task-card-elapsed">{formatElapsed(elapsedMs(startedAt, now, ticking ? null : endedAt))}</span>;
}

const NO_IDS = new Set<string>();

/**
 * The group's one card for a task, and its live window: who took it, what it is, how far it got,
 * the member's latest line streaming in, and any action it waits on — all inside the reply column.
 * Its state comes from the store, so a running card moves without the group's message list growing.
 */
function TaskCard({
  message,
  conversationID,
  direct,
  onOpenConversation,
  onOpenMember,
  onMention,
  onOpenMemories,
  memberNames,
}: {
  message: Message;
  conversationID: string;
  /** The user's own chat with one bot: a queued card may start right away. */
  direct: boolean;
  onOpenConversation: (id: string) => void;
  onOpenMember: (id: string) => void;
  onMention: (name: string) => void;
  onOpenMemories: () => void;
  memberNames: string[];
}) {
  const tasks = useTeam((s) => s.groupTasks[conversationID]);
  const task = taskByID(tasks, message.task_id);
  const workspace = useTeam((s) => s.workspace);
  const threadID = task?.thread_id ?? null;
  const thread = useTeam((s) => (threadID ? s.threadDetails[threadID] : undefined));
  const [expanded, setExpanded] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const owner = agentByID(workspace, task?.owner_id ?? message.author_id);
  const status: TaskStatus = task?.status ?? "running";
  const awaitingID = task?.awaiting_action_id ?? null;
  const awaiting = awaitingID !== null;
  const archived = task ? isTaskArchived(task) : false;
  const dismissed = task ? isTaskDismissed(task) : false;
  const closed = archived || dismissed;
  const actions = closed ? [] : taskCardActions(status, direct);
  const reason = task ? taskReasonLine(task) : "";
  const waitingFor = (task?.waiting_for ?? "").trim();
  const meta = taskStatusMeta(status, awaiting, waitingFor !== "");
  const title = task?.title || message.content.trim() || "任务";
  const live = (task?.latest_text ?? "").trim();
  const recent = (task?.recent_steps ?? []).filter((step) => step.trim() !== "").slice(-3);
  const queuedLine =
    task && status === "queued" ? queuedWaitLine(task, tasks, agentByID(workspace, task.owner_id)?.name) : "";
  // A caret means words are arriving. With the thread loaded, its newest agent row decides;
  // without it, only a plain running task can be writing (not one that waits or asks).
  const newestAgent = thread?.messages.findLast((row) => row.kind === "agent");
  const streaming = newestAgent
    ? newestAgent.status === "streaming"
    : status === "running" && !waitingFor && !awaiting;
  const reserveTwoLines = status === "running" && !waitingFor && !awaiting;
  // Only live work has words before its first line; a stopped or done card stays quiet.
  const liveLine = live || (awaiting ? "在等你确认…" : status === "running" ? "正在开始…" : "");
  // The inline thread is read when opened, and earlier when the card has an action to draw.
  useEffect(() => {
    if (threadID && (expanded || awaiting)) void store.loadThreadDetail(threadID);
  }, [threadID, expanded, awaiting]);
  const awaitingMessage = awaitingID
    ? thread?.messages.find((row) => row.action?.id === awaitingID)
    : undefined;
  const awaitingAction = awaitingMessage?.action;
  const run = useTeam((s) =>
    status === "running" || awaiting
      ? s.workspace?.active_runs.some((row) => row.conversation_id === threadID)
      : false,
  );
  const control = async (action: () => Promise<string | null>) => {
    setPending(true);
    setError(null);
    const failure = await action();
    setPending(false);
    if (failure) setError(failure);
  };
  // A finished task leaves the sidebar once its result (this card is its handle) has been seen.
  const cardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!task || status !== "done" || task.seen_at) return;
    const element = cardRef.current;
    if (!element) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer.disconnect();
          void store.markTaskSeen(conversationID, task.id);
        }
      },
      { threshold: 0.5 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [task, status, conversationID]);
  return (
    <div
      ref={cardRef}
      className={[
        "task-card",
        meta.tone,
        dismissed ? "dismissed" : "",
        archived ? "archived" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {closed && <div className="task-card-flag">{archived ? "已归档" : "已放下"}</div>}
      <div className="task-card-head">
        {owner ? (
          <Avatar seed={owner.avatar_seed} appearance={owner.appearance} size={20} name={owner.name} agentID={owner.id} />
        ) : (
          <span className="work-gone" aria-hidden="true" />
        )}
        <span className="task-card-owner">{owner?.name ?? message.author_name}</span>
        {threadID ? (
          <button type="button" className="task-card-title" title={title} onClick={() => onOpenConversation(threadID)}>
            {title}
          </button>
        ) : (
          <span className="task-card-title" title={title}>
            {title}
          </span>
        )}
        <TaskElapsed
          startedAt={task?.started_at ?? null}
          endedAt={task?.updated_at ?? null}
          ticking={isTicking(status) || awaiting}
        />
        {showsStatusWord(status, queuedLine) && (
          <span className={`task-card-status ${meta.tone}`}>
            {(meta.tone === "running" || meta.tone === "awaiting") && (
              <span className="task-card-dot" aria-hidden="true" />
            )}
            {meta.label}
          </span>
        )}
      </div>

      {/* A queued card says once why it waits; the fixed two-line box belongs to live work. */}
      {status === "queued" ? (
        <p className="task-card-live queued">{queuedLine}</p>
      ) : (
        !expanded &&
        liveLine !== "" && (
          <p
            className={[
              "task-card-live",
              live ? "" : "empty",
              reserveTwoLines ? "two-line" : "",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            {liveLine}
            {streaming && <span className="task-card-caret" aria-hidden="true" />}
          </p>
        )
      )}

      {!expanded && recent.length > 0 && (
        <ul className="task-card-steps">
          {recent.map((step, index) => (
            <li key={`${index}-${step}`}>
              <span className="task-card-step-dot" aria-hidden="true" />
              <span>{step}</span>
            </li>
          ))}
        </ul>
      )}

      {status !== "queued" &&
        taskStepLine({ status, steps: task?.steps ?? 0, current_step: task?.current_step ?? "" }) && (
          <div className="task-card-foot">
            <span className="task-card-step">
              {taskStepLine({ status, steps: task?.steps ?? 0, current_step: task?.current_step ?? "" })}
            </span>
            {waitingFor && <span className="task-card-waiting">{waitingFor}</span>}
          </div>
        )}

      {awaiting && (
        <div className="task-card-await">
          {awaitingAction?.confirmation && awaitingAction.status === "awaiting" ? (
            awaitingAction.confirmation.kind === "question" ? (
              <AskPanel
                ask={{
                  id: awaitingAction.id,
                  who: owner?.name ?? message.author_name,
                  questions: awaitingAction.confirmation.questions ?? [],
                }}
                onAnswer={(answers, skip) => store.answerAction(awaitingAction.id, { answers, skip })}
              />
            ) : (
              <AwaitingAction action={awaitingAction} />
            )
          ) : (
            <p className="hint">正在读取要确认的事…</p>
          )}
        </div>
      )}

      {reason && <p className={`task-card-reason ${meta.tone}`}>{reason}</p>}

      {actions.length > 0 && (
        <div className="task-card-actions">
          {actions.includes("expand") && threadID && (
            <button type="button" className="ghost-button task-card-action" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
              {expanded ? "收起" : "展开"}
            </button>
          )}
          {actions.includes("thread") && threadID && (
            <button type="button" className="ghost-button task-card-action" onClick={() => onOpenConversation(threadID)}>
              打开线程
            </button>
          )}
          {actions.includes("stop") && task && (
            <button
              type="button"
              className="ghost-button task-card-action"
              disabled={pending}
              onClick={() => void control(() => store.stopTask(conversationID, task.id))}
            >
              停止
            </button>
          )}
          {actions.includes("start") && task && (
            <button
              type="button"
              className="ghost-button task-card-action task-card-now"
              disabled={pending}
              title="不用等这个成员手上的活，另一路同时开始"
              onClick={() => void control(() => store.startTaskNow(conversationID, task.id))}
            >
              现在就做
            </button>
          )}
          {actions.includes("cancel") && task && (
            <button
              type="button"
              className="ghost-button task-card-action task-card-quiet"
              disabled={pending}
              onClick={() => void control(() => store.stopTask(conversationID, task.id))}
            >
              取消排队
            </button>
          )}
          {actions.includes("merge") && task && (
            <button
              type="button"
              className="ghost-button task-card-action"
              disabled={pending}
              title="把这件事并进这个成员正在做的那件里"
              onClick={() => void control(() => store.mergeTask(conversationID, task.id))}
            >
              并入正在做的那件
            </button>
          )}
          {actions.includes("resume") && task && (
            <button
              type="button"
              className="ghost-button task-card-action task-card-resume"
              disabled={pending}
              onClick={() => void control(() => store.resumeTask(conversationID, task.id))}
            >
              接着做
            </button>
          )}
          {actions.includes("dismiss") && task && (
            <button
              type="button"
              className="ghost-button task-card-action"
              disabled={pending}
              title="不做了：收起来，不再出现在侧边栏"
              onClick={() => void control(() => store.dismissTask(conversationID, task.id))}
            >
              不做了
            </button>
          )}
        </div>
      )}

      {expanded && status !== "queued" && thread && (
        <div className="task-card-peek">
          <Timeline
            detail={
              thread.messages.length > 12
                ? { ...thread, messages: thread.messages.slice(-12) }
                : thread
            }
            workspaceAgents={workspace?.agents ?? []}
            memberNames={memberNames}
            onMention={onMention}
            onOpenMember={onOpenMember}
            onOpenConversation={onOpenConversation}
            onOpenMemories={onOpenMemories}
            onReply={() => undefined}
            onJump={() => undefined}
            flashID={null}
            arrivals={NO_IDS}
            canReply={false}
            privateChat={false}
            running={status === "running" || (awaiting && Boolean(run))}
            step=""
          />
        </div>
      )}
      {expanded && status !== "queued" && !thread && <p className="hint task-card-peek-empty">正在读取过程…</p>}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

const TOOL_KIND = {
  computer_shell: "终端",
  computer_browser: "浏览器",
  computer: "屏幕",
  view_image: "图片",
} as const;

/**
 * One bot's stretch of work in the sandbox, folded to a single line: how many steps, what it is
 * doing now. Opened, the steps lie side by side as cards (screenshot, what it did, what it said
 * before it), scrolling sideways. A consent card stays outside the fold, because the run waits
 * on it.
 */
function ComputerSession({
  rows,
  notes,
  continues,
  renderRow,
  renderNote,
}: {
  rows: TimelineRow[];
  /** Rows after the last step that already belong here: narration, or a live undecided row. */
  notes: TimelineRow[];
  continues: boolean;
  renderRow: (row: TimelineRow) => React.ReactNode;
  renderNote: (row: TimelineRow) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const steps = rows.filter((row) => COMPUTER_TOOLS.has(row.message.action?.tool_name ?? "")).map((row) => row.message);
  const asking = rows.filter((row) => row.message.action?.status === "awaiting" && row.message.action.confirmation);
  // Notes render in their own slot under the header, never in the step log.
  const noteIDs = new Set(notes.map((row) => row.message.id));
  const folded = rows.filter((row) => !asking.includes(row) && !noteIDs.has(row.message.id));
  const segments = stepSegments(folded);
  const busy = steps.some((step) => step.action?.status === "running" || step.action?.status === "awaiting");
  const failed = steps.filter((step) => step.action?.status === "error").length;
  const latest = steps.findLast((step) => step.action?.status !== "awaiting")?.action?.summary ?? "";
  const count = steps.length - asking.length;
  // Which way each step went, so a glance shows whether commands or pixels did the work.
  const kinds = (["computer_shell", "computer_browser", "computer", "view_image"] as const)
    .map((tool) => [TOOL_KIND[tool], steps.filter((step) => step.action?.tool_name === tool).length] as const)
    .filter(([, n]) => n > 0);
  const breakdown = kinds.length > 1 ? `：${kinds.map(([name, n]) => `${name} ${n}`).join("、")}` : "";
  const text = busy
    ? `正在使用沙盒电脑${latest ? `：${latest}` : ""}`
    : `使用沙盒电脑 ${count} 步${breakdown}${failed > 0 ? `，${failed} 步没成功` : ""}`;
  const panelID = `computer-session-${rows[0]?.message.id ?? "none"}`;
  return (
    <>
      {count > 0 && (
        <div
          className={["action-block", continues ? "continues" : "", open ? "session-open" : ""]
            .filter(Boolean)
            .join(" ")}
        >
          <button
            type="button"
            className={busy ? "action-row running expandable" : "action-row completed expandable"}
            aria-expanded={open}
            aria-controls={panelID}
            title={text}
            onClick={() => setOpen((value) => !value)}
          >
            <span className="action-icon" aria-hidden="true">
              <Icon name="computer" size={14} />
            </span>
            <span className="action-summary">{text}</span>
            <span className={open ? "action-caret open" : "action-caret"} aria-hidden="true">
              <Icon name="chevron" size={12} />
            </span>
          </button>
          <div className={open ? "computer-steps collapse open" : "computer-steps collapse"} aria-hidden={!open} inert={!open}>
            <div className="collapse-inner" id={panelID}>
              {/* The padding lives on this inner box: on the fold itself it would survive the
                  0fr row and leave a band of empty bubble under a closed session. */}
              <div className="computer-steps-body">
                {segments.map((segment, index) =>
                  segment.kind === "list" ? (
                    <div key={index} className="computer-list">
                      {segment.rows.map(renderRow)}
                    </div>
                  ) : (
                    <StepStrip key={index} cards={segment.cards} />
                  ),
                )}
              </div>
            </div>
          </div>
          {/* The reply's latest words sit at the box's bottom edge, folded or open: that is
              where the step log grows, so narration that leads into the next step stays put
              when that step lands above it, instead of streaming in under the header first. */}
          {notes.length > 0 && (
            <div className="computer-notes">
              {notes.map((entry) => (
                <div
                  key={entry.message.id}
                  className={entry.message.status === "streaming" ? "computer-note live" : "computer-note"}
                  data-message-id={entry.message.id}
                >
                  {renderNote(entry)}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {asking.map(renderRow)}
    </>
  );
}

/**
 * One row of picture cards. WebKit hands a shift+wheel to the chat as vertical scroll, so the
 * strip takes it itself and moves sideways; a plain wheel still scrolls the chat, and a
 * trackpad's own sideways swipe scrolls natively. Non-passive, so it may cancel the page's.
 */
function StepStrip({ cards }: { cards: StripCard[] }) {
  const strip = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const element = strip.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.shiftKey || event.deltaX !== 0 || event.deltaY === 0) return;
      const room = element.scrollWidth - element.clientWidth;
      const next = Math.min(room, Math.max(0, element.scrollLeft + event.deltaY));
      if (next === element.scrollLeft) return;
      event.preventDefault();
      element.scrollLeft = next;
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, []);
  return (
    <ol ref={strip} className="computer-strip" aria-label="沙盒操作步骤">
      {cards.map(({ step, said, number }) => (
        <StepCard key={step.id} step={step} number={number} said={said} />
      ))}
    </ol>
  );
}

/** Sandbox tools that work on what is on screen: shown as picture cards, not log lines. */
const VISUAL_TOOLS: ReadonlySet<string> = new Set(["computer", "computer_browser"]);

/** A picture card; `number` is the step's place in the whole stretch, terminal steps included. */
type StripCard = { step: Message; said: string; number: number };

type StepSegment = { kind: "list"; rows: TimelineRow[] } | { kind: "strip"; cards: StripCard[] };

/**
 * A stretch in reading order: terminal commands (and the words around them) stay a vertical
 * log; screen and browser steps in a row become one strip of cards, each card carrying the
 * narration written just before it.
 */
function stepSegments(rows: TimelineRow[]): StepSegment[] {
  const segments: StepSegment[] = [];
  let pending: TimelineRow[] = [];
  let number = 0;
  const list = (more: TimelineRow[]) => {
    const last = segments.at(-1);
    if (last?.kind === "list") last.rows.push(...more);
    else if (more.length > 0) segments.push({ kind: "list", rows: [...more] });
  };
  for (const row of rows) {
    const tool = row.message.action?.tool_name ?? "";
    if (row.message.kind !== "action") {
      pending.push(row);
      continue;
    }
    number += 1;
    if (VISUAL_TOOLS.has(tool)) {
      const said = pending.map((item) => item.message.content.trim()).filter(Boolean).join(" ");
      const card = { step: row.message, said, number };
      const last = segments.at(-1);
      if (last?.kind === "strip") last.cards.push(card);
      else segments.push({ kind: "strip", cards: [card] });
      pending = [];
    } else {
      list([...pending, row]);
      pending = [];
    }
  }
  list(pending);
  return segments;
}

/** One sandbox step as a card in the strip: its screenshot if any, then what it did. */
function StepCard({ step, number, said }: { step: Message; number: number; said: string }) {
  const action = step.action;
  const tool = action?.tool_name ?? "";
  const failed = action?.status === "error";
  const shot = step.attachments?.[0];
  const text = action?.summary || step.content || TOOL_LABEL[tool] || "工具调用";
  const error = action?.error && !(action.summary ?? "").includes(action.error) ? action.error : null;
  // A browser card names the page it was on, the same way its action row does; the card's two
  // lines would otherwise show only the start of the sentence. The tooltip keeps it all.
  const page = action?.detail?.kind === "page" ? action.detail : null;
  const pageText = page ? browserRowSummary(text, page) : null;
  const what = `${pageText ?? text}${error ? `：${error}` : ""}`;
  const tip = page ? browserRowSummary(text, page, Number.POSITIVE_INFINITY) : null;
  return (
    <li className={failed ? "computer-card-step failed" : "computer-card-step"}>
      {shot ? (
        <div className="computer-card-shot">
          <ImageGrid images={[shot]} from={step.author_name} />
        </div>
      ) : (
        <div className="computer-card-icon" aria-hidden="true">
          <Icon name={TOOL_ICON[tool] ?? "info"} size={18} />
        </div>
      )}
      <p className="computer-card-what" title={tip ? `${tip}${error ? `：${error}` : ""}` : what}>
        <span className="computer-card-number">{number}</span>
        <span className="computer-card-text">{what}</span>
      </p>
      {said && <p className="computer-card-said">{said}</p>}
    </li>
  );
}

function liveMood(live: BurstState | null): Mood | null {
  return live === "interrupted" ? "error" : live;
}

/** Fade the top edge only while something is hidden past it. The bottom needs no fade: the
    thread runs on under the glass composer. */
function updateEdgeFades(element: HTMLElement) {
  element.classList.toggle("fade-top", element.scrollTop > 1);
}

/** The host a link points at, for the quiet second line under a title. */
function domainOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** One tool call: a compact row, and — when the tool has something to show — a panel under it. */
function ActionRow({
  message,
  continues,
  onOpenMember,
  onOpenConversation,
  onHandoff,
}: {
  message: Message;
  continues: boolean;
  onOpenMember: (id: string) => void;
  onOpenConversation: (id: string) => void;
  /** A record's hand-off row: true when it opened the section it led to. */
  onHandoff?: (message: Message) => boolean;
}) {
  const conversations = useTeam((s) => s.workspace?.conversations);
  const agents = useTeam((s) => s.workspace?.agents);
  const connectors = useTeam((s) => s.connectors);
  // A walk's row says where it is right now, so the entry never needs opening to be useful.
  const walkRuns = useTeam((s) => s.workflowRuns);
  const [expanded, setExpanded] = useState(false);
  const action = message.action;
  // A tool that stops to ask, then resolves: one beat of motion for each state, and none for a
  // card that was already pending when the conversation opened.
  const status = action?.status ?? null;
  const [settling, setSettling] = useState<"entering" | "resolved" | null>(null);
  const previousStatus = useRef(status);
  useEffect(() => {
    const was = previousStatus.current;
    previousStatus.current = status;
    if (was === status) return;
    if (status === "awaiting") setSettling("entering");
    else if (was === "awaiting") setSettling("resolved");
    else return;
    const timer = window.setTimeout(() => setSettling(null), 260);
    return () => window.clearTimeout(timer);
  }, [status]);
  /** Every variant of the row shares one block wrapper. */
  const blockClass = (...extra: Array<string | false>) =>
    ["action-block", continues ? "continues" : "", ...extra].filter(Boolean).join(" ");
  if (action?.status === "awaiting" && action.confirmation) {
    if (action.confirmation.kind === "question") {
      return <AskCard confirmation={action.confirmation} entering={settling === "entering"} />;
    }
    return <AwaitingAction action={action} entering={settling === "entering"} />;
  }
  // A private message reads like the marker the receiver gets: who it went to, and a way in.
  if (action?.tool_name === "direct_message" && action.status !== "error") {
    const peerID = action.target_agent_id;
    const peer = peerID ? agents?.find((agent) => agent.id === peerID) : undefined;
    const peerName = peer?.name ?? (action.summary.replace(/^(私信|正在私信成员)\s*/, "") || "成员");
    const privateID = action.target_conversation_id;
    if (!privateID || !peerID) {
      // The private chat is opened a moment after the call starts; until then there is no way in.
      return (
        <div className={blockClass(settling === "resolved" && "resolved")}>
          <span className="relay relay-out pending" role="status">
            <span className="relay-label">正在私信成员</span>
          </span>
        </div>
      );
    }
    return (
      <div className={blockClass(settling === "resolved" && "resolved")}>
        <RelayMarker
          relay={{ conversation_id: privateID, direction: "out", peer_id: peerID, peer_name: peerName }}
          label={action.status === "running" ? "正在私信" : undefined}
          onOpen={() => {
            if (onHandoff?.(message) !== true) onOpenConversation(privateID);
          }}
        />
      </div>
    );
  }
  // A walk's page is reached from the owner's chat through this one row, like a private chat.
  const walkPage =
    action?.tool_name === "workflow_run" && action.target_conversation_id
      ? action.target_conversation_id
      : null;
  if (walkPage !== null && (conversations?.some((c) => c.id === walkPage) ?? false)) {
    const walk = walkRuns[walkPage];
    const running = walk !== undefined && isLive(walk.status);
    return (
      <div className={blockClass(settling === "resolved" && "resolved")}>
        <button
          type="button"
          className="wf-entry"
          onClick={() => {
            if (onHandoff?.(message) !== true) onOpenConversation(walkPage);
          }}
        >
          <span className={`wf-entry-dot${running ? " is-live" : ""}`} aria-hidden="true" />
          <span className="wf-entry-text">{action?.summary || message.content}</span>
          {walk && running && (
            <span className={`wf-entry-status is-${walk.status}`}>{RUN_LABELS[walk.status]}</span>
          )}
          <span className="wf-entry-caret" aria-hidden="true">
            <Icon name="chevron" size={13} />
          </span>
        </button>
      </div>
    );
  }
  const label = TOOL_LABEL[action?.tool_name ?? ""] ?? "工具调用";
  // A supervisor review row reads its verdict from the summary: 打回 = warn, 验收通过 = ok.
  const reviewTone =
    action?.tool_name === "review"
      ? (action.summary ?? "").startsWith("打回")
        ? "review-revise"
        : "review-pass"
      : "";
  const target = action?.target_conversation_id ?? null;
  const member = action?.target_agent_id ?? null;
  // A dissolved group takes its conversation with it: the row stays as history only.
  const openTarget =
    target !== null && (conversations?.some((c) => c.id === target) ?? false) ? target : null;
  const detail = action?.detail ?? null;
  // A running row has nothing final to show yet, so it gets no caret.
  const expandable = detail !== null && action?.status !== "running";
  const navigable = openTarget !== null || member !== null;
  // The backend writes the sentence; a search row counts its own sources for the user.
  const found = detail?.kind === "search" ? (detail.items?.length ?? 0) : 0;
  // A write gets its own line: the file it touched, then the lines it added and removed as
  // coloured counts. The backend's sentence repeats those numbers, so the row does not quote it.
  const diff = detail?.kind === "diff" ? detail : null;
  const diffVerb = diff?.mode === "create" ? "新建" : diff?.mode === "append" ? "追加" : "写入";
  const diffName = diff ? diff.display_path || (diff.path ?? "").split("/").pop() || "文件" : "";
  const base =
    diff !== null
      ? `${diffVerb} ${diffName}`
      : found > 0
        ? `搜索「${detail?.query ?? ""}」，找到 ${found}条`
        : action?.summary || message.content || label;
  const error = action?.error && !(action.summary ?? "").includes(action.error) ? action.error : null;
  // The whole sentence stays in the tooltip; the row shows a path's end, not its root.
  const full = error ? `${base}：${error}` : base;
  const text = shortenPathSummary(full);
  // A browser step names the page it was on: 「点击 #12」 alone cannot tell two pages apart.
  // The tooltip keeps the page's whole title, which the folded row may cut short.
  const page = detail?.kind === "page" ? detail : null;
  const pageTip = page ? browserRowSummary(text, page, Number.POSITIVE_INFINITY) : null;
  const rowText = (page ? browserRowSummary(text, page) : null) ?? text;
  const tip = pageTip ?? full;
  // A terminal step keeps both ends of its command on the folded row: the start shrinks with
  // an ellipsis, the end and the status always show. Rows from before the detail carried the
  // command fall back to the plain sentence.
  const command = detail?.command ? commandRow(full, detail.command) : null;
  const panelID = action ? `action-detail-${action.id}` : undefined;
  const toggle = () => setExpanded((value) => !value);
  return (
    <div className={blockClass(settling === "resolved" && "resolved", diff !== null && expanded && "diff-open")}>
      <button
        type="button"
        className={[
          "action-row",
          action?.status ?? "completed",
          reviewTone,
          expandable && navigable ? "has-caret" : "",
          expandable && !navigable ? "expandable" : "",
        ]
          .filter(Boolean)
          .join(" ")}
        title={command ? `${label}：${command.head}${command.tail}` : tip === label ? label : `${label}：${tip}`}
        disabled={!navigable && !expandable}
        aria-expanded={expandable && !navigable ? expanded : undefined}
        aria-controls={expandable && !navigable ? panelID : undefined}
        onClick={() => {
          // A record's hand-off opens the section it led to, ahead of any target of its own.
          if (onHandoff?.(message) === true) return;
          if (navigable) {
            if (openTarget) onOpenConversation(openTarget);
            else if (member) onOpenMember(member);
            return;
          }
          if (expandable) toggle();
        }}
      >
        <span className="action-icon" aria-hidden="true">
          {action && isConnectorTool(action.tool_name) ? (
            <ConnectorMark preset={connectorPresetOf(action.tool_name, connectors)} size={14} />
          ) : (
            <Icon name={TOOL_ICON[action?.tool_name ?? ""] ?? "info"} size={14} />
          )}
        </span>
        {command ? (
          <span className="action-summary command-summary">
            <span className="command-head">{command.head}</span>
            <span className="command-tail">{command.tail}</span>
          </span>
        ) : (
          <span className="action-summary">{rowText}</span>
        )}
        {diff && (
          <span
            className="action-diff-stat"
            aria-label={`新增 ${diff.added ?? 0} 行，删除 ${diff.removed ?? 0} 行`}
          >
            <span className="diff-added">+{diff.added ?? 0}</span>
            <span className="diff-removed">-{diff.removed ?? 0}</span>
          </span>
        )}
        {expandable && !navigable && (
          <span className={expanded ? "action-caret open" : "action-caret"} aria-hidden="true">
            <Icon name="chevron" size={12} />
          </span>
        )}
      </button>
      {expandable && navigable && (
        <button
          type="button"
          className="action-toggle"
          aria-expanded={expanded}
          aria-controls={panelID}
          aria-label={expanded ? "收起详情" : "展开详情"}
          title={expanded ? "收起详情" : "展开详情"}
          onClick={toggle}
        >
          <Icon name="chevron" size={12} />
        </button>
      )}
      {expandable && detail && (
        <div
          className={
            expanded ? "action-detail-collapse collapse open" : "action-detail-collapse collapse"
          }
          aria-hidden={!expanded}
          inert={!expanded}
        >
          <div className="collapse-inner">
            <ActionDetailPanel id={panelID} detail={detail} />
          </div>
        </div>
      )}
      {message.attachments && message.attachments.length > 0 && (
        <div className="action-shot">
          <MessageAttachments attachments={message.attachments} from={message.author_name} />
        </div>
      )}
    </div>
  );
}

/** What the tool actually did: the query and its sources, a page, or a short text. */
export function ActionDetailPanel({ id, detail }: { id?: string; detail: ActionDetail }) {
  if (detail.kind === "search") {
    return (
      <div className="action-detail" id={id}>
        <p className="action-detail-query">搜索「{detail.query ?? ""}」</p>
        {detail.provider && <p className="action-detail-meta">来源：{detail.provider}</p>}
        <ul className="action-detail-list">
          {(detail.items ?? []).map((item, index) => (
            <li key={`${item.url}-${index}`}>
              <a className="action-link" href={item.url} target="_blank" rel="noreferrer" onClick={openExternally}>
                {item.title || domainOf(item.url)}
              </a>
              <span className="action-domain">{domainOf(item.url)}</span>
              {item.snippet && <p className="action-snippet">{item.snippet}</p>}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  if (detail.kind === "page") {
    return (
      <div className="action-detail" id={id}>
        <div className="action-detail-head">
          {detail.url ? (
            <a className="action-link" href={detail.url} target="_blank" rel="noreferrer" onClick={openExternally}>
              {detail.title || domainOf(detail.url)}
            </a>
          ) : (
            <p className="action-detail-query">{detail.title}</p>
          )}
          {detail.url && <span className="action-domain">{domainOf(detail.url)}</span>}
        </div>
        {detail.text && <p className="action-excerpt">{detail.text}</p>}
        {detail.snapshot && (
          <div className="action-snapshot">
            <p className="action-snapshot-title">页面元素</p>
            <pre className="action-text full action-snapshot-body">{detail.snapshot}</pre>
          </div>
        )}
      </div>
    );
  }
  if (detail.kind === "subtasks") {
    return (
      <div className="action-detail" id={id}>
        <ul className="subtask-list">
          {(detail.steps ?? []).map((step) => (
            <li key={step.title} className={`subtask-step is-${step.status}`}>
              <span className="subtask-mark" aria-hidden="true">
                <Icon name={step.status === "done" ? "done" : step.status === "failed" ? "blocked" : "doing"} size={14} />
              </span>
              <span className="subtask-body">
                <span className="subtask-title">{step.title}</span>
                {step.output && <span className="subtask-output">{step.output}</span>}
              </span>
              <span className="subtask-status">
                {step.status === "done" ? "完成" : step.status === "failed" ? "没成功" : "进行中"}
              </span>
            </li>
          ))}
        </ul>
      </div>
    );
  }
  if (detail.kind === "diff") {
    const lines = detail.lines ?? [];
    const path = detail.display_path || detail.path;
    return (
      <div className="action-detail diff-detail" id={id}>
        {/* The whole path on hover: the line itself stays short enough to read. */}
        {path && (
          <p className="diff-path" title={detail.path ?? undefined}>
            {path}
          </p>
        )}
        {lines.length > 0 ? (
          <div className="diff-body">
            {lines.map((line, index) => (
              <div
                key={index}
                className={`diff-line${line.op === "+" ? " add" : line.op === "-" ? " del" : ""}`}
              >
                <span className="diff-sign" aria-hidden="true">
                  {line.op === " " ? " " : line.op}
                </span>
                <span className="diff-text">{line.text || " "}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="diff-note">内容没有变化。</p>
        )}
        {detail.note && <p className="diff-note">{detail.note}</p>}
        {detail.truncated && <p className="diff-note">只显示了前 {lines.length} 行。</p>}
      </div>
    );
  }
  if (detail.command) {
    // A terminal step opened: the whole command as it ran, then everything it printed. Both
    // are shown in full; a long output scrolls inside its own box instead of being cut.
    return (
      <div className="action-detail command-detail" id={id}>
        <pre className="action-command">
          <span className="action-command-prompt" aria-hidden="true">$ </span>
          {detail.command}
        </pre>
        {detail.text && <pre className="action-text full">{detail.text}</pre>}
      </div>
    );
  }
  if (!detail.text) return null;
  return (
    <div className="action-detail" id={id}>
      <pre className="action-text">{detail.text}</pre>
    </div>
  );
}

/** Copy, reply and react, once per reply: copying takes every line the bot wrote in it. */
function TurnTools({
  text,
  canReply,
  onReply,
}: {
  text: string;
  canReply: boolean;
  onReply: () => void;
  onReact?: (emoji: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  // The note fades out instead of vanishing on the same tick the text clears.
  const note = usePresence(copied ? "已复制" : null, 160);
  const timer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );
  return (
    <div className="turn-tools">
      <button
        type="button"
        className="tool-button"
        aria-label="复制这条回复"
        title="复制这条回复"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
          } catch {
            return;
          }
          setCopied(true);
          if (timer.current !== null) window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => setCopied(false), 1200);
        }}
      >
        <Icon name="copy" size={15} />
      </button>
      {canReply && (
        <button
          type="button"
          className="tool-button"
          onClick={onReply}
          aria-label="回复这条消息"
          title="回复这条消息"
        >
          <Icon name="reply" size={15} />
        </button>
      )}
      <span className={note.open ? "copy-note on" : "copy-note"} role="status" aria-live="polite">
        {note.value}
      </span>
    </div>
  );
}

/**
 * Under a finished reply: what it chose to remember. The chip opens a small list — each row can
 * be deleted right there — and a way into the whole 记忆 panel.
 */
function MemoryChip({ memories, onOpenMemories }: { memories: Memory[]; onOpenMemories: () => void }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The list grows downward unless the chip sits near the foot of the window.
  const [up, setUp] = useState(false);
  const shell = useRef<HTMLDivElement>(null);
  // The list leaves on a short fade before it unmounts.
  const popover = usePresence(open ? true : null, 140);

  useLayoutEffect(() => {
    if (!open) return;
    const rect = shell.current?.getBoundingClientRect();
    if (rect) setUp(window.innerHeight - rect.bottom < 280);
  }, [open]);

  useEffect(() => {
    if (!open) {
      setError(null);
      return;
    }
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

  const remove = async (memory: Memory) => {
    const failure = await store.deleteMemory(memory.id);
    setError(failure);
  };

  return (
    <div className="memory-chip-shell" ref={shell}>
      <button
        type="button"
        className="memory-chip"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="memory" size={13} />
        记住了 {memories.length}条
      </button>
      {popover.value && (
        <div
          className={["memory-pop", up ? "up" : "", popover.open ? "" : "closing"].filter(Boolean).join(" ")}
          role="dialog"
          aria-label="这条回复记下的记忆"
        >
          <ul className="memory-pop-list">
            {memories.map((memory) => (
              <li key={memory.id} className="memory-pop-row">
                <p className="memory-pop-content">{memory.content}</p>
                <button type="button" className="memory-action danger" onClick={() => void remove(memory)}>
                  删除
                </button>
              </li>
            ))}
          </ul>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <button
            type="button"
            className="memory-pop-manage"
            onClick={() => {
              setOpen(false);
              onOpenMemories();
            }}
          >
            管理记忆
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * A bot's pending questions, as one quiet line in the thread. The questions and their options
 * live only in the composer's picker, as in Claude Code: drawing them here too showed every
 * option twice, one copy of it unclickable.
 */
function AskCard({ confirmation, entering }: { confirmation: ActionConfirmation; entering?: boolean }) {
  const questions = confirmation.questions ?? [];
  const what =
    questions.length > 1 ? questions.map((question) => question.header).join("、") : (questions[0]?.question ?? "");
  return (
    <section className={entering ? "ask-card entering" : "ask-card"} aria-label={confirmation.prompt}>
      <span className="ask-card-icon" aria-hidden="true">
        <Icon name="chat" size={14} />
      </span>
      <span className="ask-card-label">在等你回答</span>
      <span className="ask-card-what">{what}</span>
    </section>
  );
}

/** A tool that stopped to ask the user: the card is the decision, the row comes back after. */
function AwaitingAction({ action, entering }: { action: Action; entering?: boolean }) {
  const connectors = useTeam((s) => s.connectors);
  const confirmation = action.confirmation;
  const [pending, setPending] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!confirmation) return null;
  return (
    <section className={entering ? "confirm-card entering" : "confirm-card"} aria-label={confirmation.prompt}>
      <div className="confirm-head">
        <span className="confirm-icon" aria-hidden="true">
          {isConnectorTool(action.tool_name) ? (
            <ConnectorMark preset={connectorPresetOf(action.tool_name, connectors)} size={16} />
          ) : (
            <Icon name={TOOL_ICON[action.tool_name] ?? "shield"} size={16} />
          )}
        </span>
        <span className="confirm-label">等待你确认</span>
      </div>
      <p className="confirm-prompt">{confirmation.prompt}</p>
      <div className="confirm-actions">
        <button
          type="button"
          className="danger-button"
          disabled={pending !== null}
          onClick={async () => {
            setPending(true);
            setError(null);
            const failure = await store.confirmAction(action.id, true);
            if (failure) {
              setError(failure);
              setPending(null);
            }
          }}
        >
          <Icon
            name={isConnectorTool(action.tool_name) ? "check" : TOOL_ICON[action.tool_name] ?? "trash"}
            size={15}
          />
          {confirmation.confirm_label}
        </button>
        <button
          type="button"
          className="ghost-button"
          disabled={pending !== null}
          onClick={async () => {
            setPending(true);
            setError(null);
            // 「总是允许」: this yes, plus a rule that covers the next identical call.
            const failure = await store.confirmAction(action.id, true, true);
            if (failure) {
              setError(failure);
              setPending(null);
            } else {
              store.notify("以后这个成员的这个操作会直接执行，可以随时在「设置 → 审批」里删掉。");
            }
          }}
          title="这次同意，并且以后同样的操作不再询问"
        >
          总是允许
        </button>
        <button
          type="button"
          className="ghost-button"
          disabled={pending !== null}
          onClick={async () => {
            setPending(false);
            setError(null);
            const failure = await store.confirmAction(action.id, false);
            if (failure) {
              setError(failure);
              setPending(null);
            }
          }}
        >
          {confirmation.deny_label}
        </button>
      </div>
      {error && (
        <p className="form-error confirm-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

/** The watched-bot marker: who spoke across the private link, and the way back into it. */
function RelayMarker({ relay, onOpen, label }: { relay: Relay; onOpen: () => void; label?: string }) {
  const workspace = useTeam((s) => s.workspace);
  const peer = agentByID(workspace, relay.peer_id);
  const incoming = relay.direction === "in";
  return (
    <button
      type="button"
      className={[incoming ? "relay relay-in" : "relay relay-out", label ? "pending" : ""].filter(Boolean).join(" ")}
      onClick={onOpen}
      // A deleted member takes its private chats with it; the marker stays as history only.
      disabled={!workspace?.conversations.some((conversation) => conversation.id === relay.conversation_id)}
      aria-label={`查看与${relay.peer_name}的私聊`}
    >
      <span className="relay-label">{label ?? (incoming ? "消息来自" : "已发消息给")}</span>
      <Avatar seed={peer?.avatar_seed ?? null} appearance={peer?.appearance} size={16} name={relay.peer_name} frozen />
      <span className="relay-name">{relay.peer_name}</span>
    </button>
  );
}

/** Where a bot's own report came from: still a way into the private chat, not a transcript. */
function RelaySummaryLabel({ relay, onOpen }: { relay: Relay; onOpen: () => void }) {
  const workspace = useTeam((s) => s.workspace);
  const peer = agentByID(workspace, relay.peer_id);
  return (
    <button
      type="button"
      className="relay-summary"
      onClick={onOpen}
      disabled={!workspace?.conversations.some((conversation) => conversation.id === relay.conversation_id)}
      aria-label={`查看与${relay.peer_name}的私聊`}
    >
      <span>来自与</span>
      <Avatar seed={peer?.avatar_seed ?? null} appearance={peer?.appearance} size={16} name={relay.peer_name} frozen />
      <span className="relay-name">{relay.peer_name}</span>
      <span>的私聊</span>
    </button>
  );
}

/** In a private chat: who asked the question this exchange serves, and where, with a way back. */
function OriginLine({ origin, onOpen }: { origin: Origin; onOpen: () => void }) {
  const conversations = useTeam((s) => s.workspace?.conversations);
  const source = conversations?.find((conversation) => conversation.id === origin.conversation_id);
  const place = source ? `在「${source.title}」` : "";
  return (
    <button
      type="button"
      className="origin-line"
      onClick={onOpen}
      // A deleted conversation keeps the question as text only.
      disabled={!source}
      aria-label={`查看${origin.author_name}${place}的提问`}
    >
      {origin.author_name}
      {place}问：{quoteExcerpt(origin.content, 60)}
    </button>
  );
}

function EmptyChat({
  title,
  detail,
  onPick,
  showSuggestions,
}: {
  title: string;
  detail?: string;
  onPick: (text: string) => void;
  showSuggestions: boolean;
}) {
  return (
    <div className="empty-chat">
      <h2>{title}</h2>
      {detail && <p>{detail}</p>}
      {showSuggestions && (
        <div className="suggestions">
          {SUGGESTIONS.map((text) => (
            <button key={text} type="button" className="suggestion" onClick={() => onPick(text)}>
              {text}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** A notice's 「拉进群」: the no-supervisor case, where the user confirms the pull-in. */
function InviteNoticeButton({ conversationID, message }: { conversationID: string; message: Message }) {
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const invite = message.invite;
  if (!invite) return null;
  const pull = async () => {
    if (sending) return;
    setSending(true);
    setError(null);
    const failure = await store.inviteMember(
      conversationID,
      invite.agent_id,
      invite.message_id,
      message.id,
    );
    setSending(false);
    if (failure) setError(failure);
  };
  return (
    <>
      <button
        type="button"
        className="link-button notice-invite"
        disabled={sending}
        onClick={() => void pull()}
      >
        {sending ? "拉进群中…" : "拉进群"}
      </button>
      {error && <span className="form-error">{error}</span>}
    </>
  );
}
