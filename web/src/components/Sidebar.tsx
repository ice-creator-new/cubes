// The bot rail: pinned avatars on top, the ungrouped chats by recency, then collapsible folders.
// Every row is right-clickable (or Shift+F10 / the ContextMenu key) for pin, folder and
// appearance actions; a whole-layout PUT keeps the server in step.

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
import type { Conversation, Decision, SidebarFolder } from "../api/types";
import { isDesktop, isMac } from "../lib/platform";
import { useFlip, usePresence } from "../lib/motion";
import { RAIL_DEFAULT, RAIL_FOLDED, RAIL_MAX, RAIL_MIN, RAIL_WIDTH_KEY, clampRailWidth, readRailWidth } from "../lib/railWidth";
import { shortTime } from "../lib/time";
import { inSidebar, isThread } from "../lib/threads";
import { taskStatusMeta, threadsOfGroup } from "../lib/taskProgress";
import type { GroupThread } from "../api/types";
import { agentByID, membersOf, moodFor, store, useTeam } from "../state/store";
import { Avatar, GroupAvatar } from "./Avatar";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Icon, type IconName } from "./Icon";

const isMacShell = isDesktop && isMac;
const COLLAPSED_KEY = "cubes.folders.collapsed";

/** The panels the dock at the rail's foot opens. */
export type SidebarTool =
  | "team"
  | "workbench"
  | "memory"
  | "schedules"
  | "usage"
  | "connectors"
  | "computer"
  | "settings"
  | "skills"
  | "workflows";

type Draft =
  | { mode: "create"; conversationID: string; value: string }
  | { mode: "rename"; folderID: string; value: string };

type MenuState = { x: number; y: number; items: MenuItem[]; trigger: HTMLElement | null; above?: boolean };

function readCollapsed(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "[]");
    return Array.isArray(raw) ? raw.filter((id): id is string => typeof id === "string") : [];
  } catch {
    return [];
  }
}

/** A menu opens on right-click, Shift+F10 or the ContextMenu key, anchored to the row. */
function menuPoint(event: MouseEvent<HTMLElement> | KeyboardEvent<HTMLElement>) {
  if ("clientX" in event) return { x: event.clientX, y: event.clientY };
  const rect = event.currentTarget.getBoundingClientRect();
  return { x: rect.left + 24, y: rect.bottom - 8 };
}

function opensMenu(event: KeyboardEvent<HTMLElement>): boolean {
  return event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey);
}

export function Sidebar({
  collapsed,
  onToggle,
  onOpen,
  onTeam,
  onWorkbench,
  onMemory,
  onSchedules,
  onUsage,
  onConnectors,
  onComputer,
  onSettings,
  onSkills,
  onWorkflows,
  active,
  selectedID,
  onEditBot,
  onDeleteMember,
  onDissolveGroup,
  onNewGroup,
  onNewMember,
  onOpenWorkLog,
}: {
  collapsed: boolean;
  onToggle: () => void;
  onOpen: (id: string) => void;
  onTeam: () => void;
  onWorkbench: () => void;
  onMemory: () => void;
  onSchedules: () => void;
  onUsage: () => void;
  onConnectors: () => void;
  onComputer: () => void;
  onSettings: () => void;
  onSkills: () => void;
  onWorkflows: () => void;
  /** The tool whose panel is open, lit in the dock. */
  active?: SidebarTool | null;
  selectedID: string | null;
  /** Opens the bot's editor: name, duties, model and appearance. */
  onEditBot: (agentId: string) => void;
  /** Opens the member editor on its confirmation step; the server still guards the delete. */
  onDeleteMember: (agentId: string) => void;
  /** Opens the group dialog on its confirmation step; the server still guards the dissolve. */
  onDissolveGroup: (conversationID: string) => void;
  /** Opens the dialog that creates a group. */
  onNewGroup: () => void;
  /** Opens the dialog that creates a member bot. */
  onNewMember: () => void;
  /** Opens a bot's whole work log from its row's menu. */
  onOpenWorkLog: (agentID: string) => void;
}) {
  const state = useTeam((s) => s);
  const workspace = state.workspace;
  const layout = state.sidebar;
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [closedFolders, setClosedFolders] = useState<string[]>(readCollapsed);
  // One row at a time is under the pointer; that row's face is awake and its own row is the
  // region its eyes may follow the pointer in. Keyboard focus wakes a face but never tracks.
  const [hovered, setHovered] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  // Only after the rail has been folded once do its labels fade back in: the window opening
  // is still.
  const [toggled, setToggled] = useState(false);
  const rail = useRef<HTMLDivElement>(null);
  const aside = useRef<HTMLElement>(null);
  // The open width the user dragged to; the folded rail ignores it.
  const [width, setWidth] = useState(() => clampRailWidth(readRailWidth(), window.innerWidth));
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ startX: number; startWidth: number } | null>(null);
  const moreButton = useRef<HTMLButtonElement>(null);
  // Which trigger the last menu belonged to and when it closed: a click on that same trigger
  // right after (the outside-pointerdown that closed it) must not open it again.
  const lastClosed = useRef<{ trigger: HTMLElement | null; at: number }>({ trigger: null, at: 0 });

  // A window that shrinks takes the rail down with it, so the chat column never goes too narrow.
  useEffect(() => {
    const onResize = () => setWidth(clampRailWidth(readRailWidth(), window.innerWidth));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const commitWidth = (next: number) => {
    setWidth(next);
    localStorage.setItem(RAIL_WIDTH_KEY, String(next));
  };

  const resizeHandlers = {
    onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = { startX: event.clientX, startWidth: width };
      setResizing(true);
    },
    onPointerMove: (event: PointerEvent<HTMLDivElement>) => {
      const start = drag.current;
      if (!start) return;
      const next = clampRailWidth(start.startWidth + event.clientX - start.startX, window.innerWidth);
      // Written straight to the element while dragging: no React render per pointer move.
      aside.current?.style.setProperty("--rail-open", `${next}px`);
    },
    onPointerUp: (event: PointerEvent<HTMLDivElement>) => {
      const start = drag.current;
      if (!start) return;
      drag.current = null;
      setResizing(false);
      commitWidth(clampRailWidth(start.startWidth + event.clientX - start.startX, window.innerWidth));
    },
    onPointerCancel: () => {
      drag.current = null;
      setResizing(false);
      aside.current?.style.setProperty("--rail-open", `${width}px`);
    },
    onDoubleClick: () => commitWidth(clampRailWidth(RAIL_DEFAULT, window.innerWidth)),
    onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
      const delta = event.key === "ArrowLeft" ? -16 : event.key === "ArrowRight" ? 16 : 0;
      if (!delta) return;
      event.preventDefault();
      commitWidth(clampRailWidth(width + delta, window.innerWidth));
    },
  };
  // Pinning and filing change where rows sit; that is the move worth gliding, not every
  // redraw as messages arrive.
  useFlip(rail, [layout], { enabled: layout !== null });
  const shownMenu = usePresence(menu, 140);

  const needle = query.trim().toLowerCase();

  // The dock shows 团队, 连接器 and 电脑; the rest live behind 更多, so the bot list keeps its
  // height. A tool behind 更多 whose panel is open lights the 更多 entry instead.
  const tools: { id: SidebarTool; label: string; icon: IconName; onSelect: () => void }[] = [
    { id: "team", label: "团队", icon: "team", onSelect: onTeam },
    { id: "connectors", label: "连接器", icon: "connectors", onSelect: onConnectors },
    { id: "computer", label: "电脑", icon: "computer", onSelect: onComputer },
  ];
  const moreTools: { id: SidebarTool; label: string; icon: IconName; onSelect: () => void }[] = [
    { id: "workbench", label: "工作台", icon: "notebook", onSelect: onWorkbench },
    { id: "memory", label: "记忆", icon: "memory", onSelect: onMemory },
    { id: "schedules", label: "定时任务", icon: "alarm", onSelect: onSchedules },
    { id: "usage", label: "用量", icon: "usage", onSelect: onUsage },
    { id: "workflows", label: "工作流", icon: "workflows", onSelect: onWorkflows },
    { id: "skills", label: "技能", icon: "skills", onSelect: onSkills },
    { id: "settings", label: "设置", icon: "settings", onSelect: onSettings },
  ];
  const moreActive = moreTools.some((tool) => tool.id === active);
  const plan = useMemo(() => {
    // Private bot-to-bot chats, a walk's own page and a task's thread are reached from the chat
    // row that leads there, never listed here: the sidebar stays the list of chats you talk in.
    const conversations = (workspace?.conversations ?? []).filter(inSidebar);
    const byID = new Map(conversations.map((conversation) => [conversation.id, conversation]));
    const items = layout?.items ?? [];
    const pinnedItems = items
      .filter((item) => item.pinned)
      .sort((a, b) => (a.pin_position ?? 0) - (b.pin_position ?? 0));
    const pinnedIDs = new Set(pinnedItems.map((item) => item.conversation_id));
    const folderOf = new Map<string, string>();
    for (const item of items) if (item.folder_id) folderOf.set(item.conversation_id, item.folder_id);
    const conversationsOf = (ids: string[]) =>
      ids
        .map((id) => byID.get(id))
        .filter((conversation): conversation is Conversation => conversation !== undefined);
    return {
      pinned: conversationsOf(pinnedItems.map((item) => item.conversation_id)),
      pinnedIDs,
      folderOf,
      ungrouped: conversations
        .filter((conversation) => !pinnedIDs.has(conversation.id) && !folderOf.has(conversation.id))
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
      folders: [...(layout?.folders ?? [])]
        .sort((a, b) => a.position - b.position)
        .map((folder) => ({
          folder,
          chats: conversationsOf(
            items
              .filter((item) => item.folder_id === folder.id && !pinnedIDs.has(item.conversation_id))
              .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
              .map((item) => item.conversation_id),
          ),
        })),
    };
  }, [workspace, layout]);

  /** Whether a conversation matches the rail's search box. */
  const matches = (conversation: Conversation) =>
    !needle ||
    conversation.title.toLowerCase().includes(needle) ||
    (conversation.last_message ?? "").toLowerCase().includes(needle);

  const visiblePinned = plan.pinned.filter(matches);
  const visibleUngrouped = plan.ungrouped.filter(matches);
  const visibleFolders = plan.folders
    .map((entry) => ({ ...entry, chats: entry.chats.filter(matches) }))
    .filter((entry) => entry.chats.length > 0 || !needle);
  const empty = Boolean(workspace) && Boolean(needle) && visiblePinned.length === 0 && visibleUngrouped.length === 0 && visibleFolders.length === 0;
  const knownThreads = useTeam((s) => s.threadGroups);
  // Finished runs the user has not opened: newest first, at most ten from the server. A thread's
  // run is part of its task, not a chat of its own, so the group's result is what gets the notice.
  const threadIDs = new Set([
    ...Object.keys(knownThreads),
    ...(workspace?.conversations ?? []).filter(isThread).map((conversation) => conversation.id),
  ]);
  const finished = (workspace?.finished_runs ?? []).filter(
    (run) =>
      !threadIDs.has(run.conversation_id) &&
      (!needle ||
        run.conversation_title.toLowerCase().includes(needle) ||
        run.preview.toLowerCase().includes(needle) ||
        run.agent_name.toLowerCase().includes(needle)),
  );

  // A row that the filter hid and then brought back fades in; the rows that stayed put are
  // left alone. The first render is the baseline, so nothing fades when the window opens.
  const seenRows = useRef<Set<string> | null>(null);
  const returningRows = useRef<Set<string>>(new Set());
  const visibleIDs = [
    ...visiblePinned,
    ...visibleUngrouped,
    ...visibleFolders.flatMap((entry) => entry.chats),
  ].map((conversation) => conversation.id);
  if (seenRows.current === null) {
    seenRows.current = new Set(visibleIDs);
  } else {
    for (const id of visibleIDs) {
      if (seenRows.current.has(id)) continue;
      seenRows.current.add(id);
      returningRows.current.add(id);
    }
  }
  const rowClass = (conversation: Conversation, base: string) =>
    returningRows.current.has(conversation.id) ? `${base} row-returning` : base;

  const openMenu = useCallback(
    (x: number, y: number, items: MenuItem[], trigger: HTMLElement | null, above = false) => {
      setMenu({ x, y, items, trigger, above });
    },
    [],
  );

  const closeMenu = useCallback(() => {
    const trigger = menu?.trigger ?? null;
    lastClosed.current = { trigger, at: Date.now() };
    setMenu(null);
    trigger?.focus();
  }, [menu]);

  const toggleFolder = (id: string) => {
    const next = closedFolders.includes(id) ? closedFolders.filter((item) => item !== id) : [...closedFolders, id];
    setClosedFolders(next);
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
  };

  const commitDraft = async () => {
    if (!draft) return;
    const name = draft.value.trim();
    const current = draft;
    setDraft(null);
    if (!name) return;
    if (current.mode === "rename") {
      await store.renameFolder(current.folderID, name);
      return;
    }
    const folder = await store.createFolder(name);
    if (folder) store.moveToFolder(current.conversationID, folder.id);
  };

  const folderDraftInput = (placeholder: string) => (
    <div className="folder-draft">
      <input
        autoFocus
        value={draft?.value ?? ""}
        placeholder={placeholder}
        aria-label={placeholder}
        onChange={(event) => setDraft(draft ? { ...draft, value: event.target.value } : draft)}
        onClick={(event) => event.stopPropagation()}
        onBlur={() => setDraft(null)}
        onKeyDown={(event) => {
          event.stopPropagation();
          if (event.key === "Enter") {
            event.preventDefault();
            void commitDraft();
          }
          if (event.key === "Escape") {
            event.preventDefault();
            setDraft(null);
          }
        }}
      />
    </div>
  );

  const conversationMenu = (conversation: Conversation): MenuItem[] => {
    const pinned = plan.pinnedIDs.has(conversation.id);
    const folderID = plan.folderOf.get(conversation.id) ?? null;
    const owner = conversation.kind === "direct" ? membersOf(workspace, conversation)[0] : undefined;
    const items: MenuItem[] = [
      {
        label: pinned ? "取消置顶" : "置顶",
        icon: pinned ? "unpin" : "pin",
        onSelect: () => (pinned ? store.unpin(conversation.id) : store.pin(conversation.id)),
      },
    ];
    if (!collapsed) {
      const targets: MenuItem[] = plan.folders.map(({ folder }) => ({
        label: folder.name,
        icon: "folder",
        disabled: folder.id === folderID,
        onSelect: () => store.moveToFolder(conversation.id, folder.id),
      }));
      targets.push({
        label: "不分组",
        icon: "ungroup",
        disabled: !pinned && folderID === null,
        onSelect: () => store.moveToFolder(conversation.id, null),
      });
      targets.push({
        label: "新建分组…",
        icon: "folder-add",
        onSelect: () => setDraft({ mode: "create", conversationID: conversation.id, value: "" }),
      });
      items.push({ label: "移到分组", icon: "folder", submenu: targets });
    }
    if (owner) {
      items.push({ label: "工作记录", icon: "history", onSelect: () => onOpenWorkLog(owner.id) });
      items.push({ label: "编辑", icon: "rename", onSelect: () => onEditBot(owner.id) });
    }
    if (owner?.kind === "member") items.push({ label: "删除成员…", icon: "trash", danger: true, onSelect: () => onDeleteMember(owner.id) });
    if (conversation.kind === "group") {
      items.push({ label: "解散群组…", icon: "trash", danger: true, onSelect: () => onDissolveGroup(conversation.id) });
    }
    return items;
  };

  const folderMenu = (folder: SidebarFolder): MenuItem[] => [
    { label: "重命名", icon: "rename", onSelect: () => setDraft({ mode: "rename", folderID: folder.id, value: folder.name }) },
    { label: "删除分组", icon: "trash", danger: true, onSelect: () => void store.deleteFolder(folder.id) },
  ];

  /** Right-click and keyboard hooks shared by rows, pin cards and folder headers. */
  const menuHandlers = (items: () => MenuItem[]) => ({
    onContextMenu: (event: MouseEvent<HTMLElement>) => {
      event.preventDefault();
      const point = menuPoint(event);
      openMenu(point.x, point.y, items(), event.currentTarget);
    },
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      if (!opensMenu(event)) return;
      event.preventDefault();
      const point = menuPoint(event);
      openMenu(point.x, point.y, items(), event.currentTarget);
    },
  });

  /** A bot at work shows it without being touched: working faces stay awake. */
  const busy = (conversation: Conversation) =>
    membersOf(workspace, conversation).some((member) => {
      const kind = moodFor(state, member.id, conversation.id);
      return kind === "thinking" || kind === "speaking";
    });

  /**
   * A sidebar face rests still unless it is under the pointer, focused, open, or working. The
   * row element is also the tracking region: a face only follows the pointer while the
   * pointer is inside its own row.
   */
  const face = (conversation: Conversation, size: number) => {
    const members = membersOf(workspace, conversation);
    const owner = conversation.kind === "direct" ? members[0] : undefined;
    const awake =
      hovered === conversation.id || focused === conversation.id || conversation.id === selectedID || busy(conversation);
    if (!owner) return <GroupAvatar members={members} size={size} awake={awake} track="region" />;
    return (
      <Avatar
        seed={owner.avatar_seed}
        appearance={owner.appearance}
        size={size}
        name={owner.name}
        agentID={owner.id}
        mood={moodFor(state, owner.id, conversation.id)}
        awake={awake}
        track="region"
      />
    );
  };

  /** Hover, focus and the tracking region, for rows, pin cards and rail faces. */
  const trackHandlers = (conversation: Conversation) => ({
    "data-track-region": "",
    onPointerEnter: () => setHovered(conversation.id),
    onPointerLeave: () => setHovered((current) => (current === conversation.id ? null : current)),
    onFocus: () => setFocused(conversation.id),
    onBlur: () => setFocused((current) => (current === conversation.id ? null : current)),
  });

  /** One live task thread under its group: status dot, who works it, and how far it got. */
  const threadRow = (thread: GroupThread) => {
    const owner = agentByID(workspace, thread.owner_id);
    const meta = taskStatusMeta(thread.status, thread.awaiting_action_id !== null);
    const at =
      thread.status === "running" || thread.awaiting_action_id
        ? `第 ${thread.steps + 1} 步`
        : thread.status === "queued"
          ? "排队中"
          : meta.label;
    return (
      <button
        key={thread.thread_id}
        type="button"
        className={thread.thread_id === selectedID ? "thread-row active" : "thread-row"}
        onClick={() => onOpen(thread.thread_id)}
        aria-current={thread.thread_id === selectedID ? "page" : undefined}
        title={`${owner?.name ?? "成员"} · ${thread.title} · ${at}`}
      >
        <span className={`thread-dot ${meta.tone}`} aria-hidden="true" />
        <span className="thread-row-text">
          ↳ {owner?.name ?? "成员"} · {thread.title} · {at}
        </span>
      </button>
    );
  };

  const row = (conversation: Conversation) => {
    const running = workspace?.active_runs.some((run) => run.conversation_id === conversation.id);
    const preview = (conversation.last_message ?? "").replace(/\s+/g, " ").trim();
    // A group's live threads sit under its row; done work is already gone from the payload.
    const threads =
      conversation.kind === "group" ? threadsOfGroup(workspace?.group_threads, conversation.id) : [];
    return (
      <div key={conversation.id} className="row-slot">
        <div className="row-line">
          <button
            type="button"
            data-flip-key={conversation.id}
            className={rowClass(conversation, conversation.id === selectedID ? "bot-row active" : "bot-row")}
            onClick={() => onOpen(conversation.id)}
            aria-current={conversation.id === selectedID ? "page" : undefined}
            title={conversation.title}
            {...trackHandlers(conversation)}
            {...menuHandlers(() => conversationMenu(conversation))}
          >
            <span className="row-face">{face(conversation, 30)}</span>
            {/* A chat that never started is just its name: no placeholder line, no date. */}
            <span className="row-text">
              <span className="row-title">{conversation.title}</span>
              {preview && <span className="row-preview">{preview}</span>}
            </span>
            <span className="row-meta">
              {conversation.unread && <span className="unread-dot" aria-label="有未读回复" />}
              {running ? <span className="dot-running" aria-label="正在回复" /> : preview ? shortTime(conversation.updated_at) : null}
            </span>
          </button>
        </div>
        {threads.length > 0 && <div className="thread-rows">{threads.map(threadRow)}</div>}
      </div>
    );
  };

  const pinCard = (conversation: Conversation) => (
    <button
      key={conversation.id}
      type="button"
      data-flip-key={conversation.id}
      className={rowClass(conversation, conversation.id === selectedID ? "pin-card active" : "pin-card")}
      onClick={() => onOpen(conversation.id)}
      aria-current={conversation.id === selectedID ? "page" : undefined}
      title={conversation.title}
      {...trackHandlers(conversation)}
      {...menuHandlers(() => conversationMenu(conversation))}
    >
      <span className="pin-face">{face(conversation, 56)}</span>
      {conversation.unread && <span className="unread-dot pin-unread" aria-label="有未读回复" />}
      <span className="pin-name">{conversation.title}</span>
    </button>
  );

  // 「新建」: a group or a member bot, from one menu under the button.
  const openCreateMenu = (event: MouseEvent<HTMLButtonElement>) => {
    const closed = lastClosed.current;
    if (closed.trigger === event.currentTarget && Date.now() - closed.at < 300) return;
    const rect = event.currentTarget.getBoundingClientRect();
    openMenu(
      collapsed ? rect.right + 6 : rect.left,
      collapsed ? rect.top : rect.bottom + 4,
      [
        { label: "新建群组…", icon: "team", onSelect: onNewGroup },
        { label: "新建成员机器人…", icon: "user", onSelect: onNewMember },
      ],
      event.currentTarget,
    );
  };

  const newButton = (className: string, size = 16) => (
    <button
      type="button"
      className={className}
      onClick={openCreateMenu}
      title="新建"
      aria-label="新建"
      aria-haspopup="menu"
    >
      <Icon name="plus" size={size} />
    </button>
  );

  const railFace = (conversation: Conversation) => {
    const live = conversation.kind === "group" ? threadsOfGroup(workspace?.group_threads, conversation.id).length : 0;
    return (
      <button
        key={conversation.id}
        type="button"
        className={conversation.id === selectedID ? "rail-face active" : "rail-face"}
        onClick={() => onOpen(conversation.id)}
        aria-current={conversation.id === selectedID ? "page" : undefined}
        title={conversation.title}
        aria-label={live > 0 ? `${conversation.title}，${live} 个进行中的任务` : conversation.title}
        {...trackHandlers(conversation)}
        {...menuHandlers(() => conversationMenu(conversation))}
      >
        {face(conversation, 32)}
        {conversation.unread && <span className="unread-dot rail-unread" aria-label="有未读回复" />}
        {live > 0 && <span className="rail-thread-count">{live}</span>}
      </button>
    );
  };

  const folderHead = (folder: SidebarFolder, count: number) => {
    if (draft?.mode === "rename" && draft.folderID === folder.id) return folderDraftInput("分组名称");
    const openFolder = !closedFolders.includes(folder.id);
    return (
      <div className="folder-head">
        <button
          type="button"
          className="folder-toggle"
          aria-expanded={openFolder}
          onClick={() => toggleFolder(folder.id)}
          {...menuHandlers(() => folderMenu(folder))}
        >
          <Icon name="chevron" size={13} />
          <span className="folder-name">{folder.name}</span>
          <span className="folder-count">{count || ""}</span>
        </button>
      </div>
    );
  };

  return (
    <aside
      ref={aside}
      className={["sidebar", collapsed ? "collapsed" : "", toggled ? "animated" : "", resizing ? "resizing" : ""]
        .filter(Boolean)
        .join(" ")}
      style={{ "--rail-open": `${width}px`, "--rail-folded": `${RAIL_FOLDED}px` } as React.CSSProperties}
      aria-label="机器人与会话"
    >
      {/* The title row: macOS traffic lights live on its left (native), the collapse control on
          its right. The whole row drags the window. In fullscreen there are no lights, so the
          search moves up into this row (CSS swaps which copy shows) and the list rises with it. */}
      <div className="sidebar-head" data-drag-region>
        {!collapsed && !isMacShell && <span className="brand" data-drag-region>Cubes</span>}
        {!collapsed && isMacShell && (
          <label className="search search-in-head">
            <Icon name="search" />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索机器人" aria-label="搜索机器人" />
          </label>
        )}
        {/* Fullscreen moves the search up here, so the new-group button comes with it. */}
        {!collapsed && isMacShell && newButton("new-group new-group-in-head")}
        <button
          type="button"
          className="icon-button"
          onClick={() => {
            setToggled(true);
            onToggle();
          }}
          aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
        >
          <Icon name="sidebar" />
        </button>
      </div>

      {!collapsed && (
        <div className="search-row search-below-head">
          <label className="search">
            <Icon name="search" />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索机器人" aria-label="搜索机器人" />
          </label>
          {newButton("new-group")}
        </div>
      )}

      <div className={collapsed ? "rail-list slim" : "rail-list"} ref={rail}>
        {(workspace?.decisions?.length ?? 0) > 0 && (
          <WaitingList
            decisions={workspace?.decisions ?? []}
            collapsed={collapsed}
            onOpen={(decision) => {
              if (decision.message_id) store.focusMessage(decision.conversation_id, decision.message_id);
              onOpen(decision.conversation_id);
            }}
            onExpand={onToggle}
          />
        )}
        {collapsed ? (
          <div className="rail-stack">
            {newButton("rail-face rail-new-group", 18)}
            {visiblePinned.map(railFace)}
            {visibleUngrouped.length > 0 && visiblePinned.length > 0 && <span className="rail-divider" aria-hidden="true" />}
            {visibleUngrouped.map(railFace)}
            {/* Groups and other filed bots stay reachable folded: one quiet divider per folder,
                named on hover, then its faces. A closed folder stays closed here too. */}
            {visibleFolders.map(({ folder, chats }) =>
              chats.length === 0 ? null : (
                <section key={folder.id} className="rail-folder" aria-label={folder.name}>
                  <button
                    type="button"
                    className="rail-divider rail-folder-toggle"
                    title={folder.name}
                    aria-label={`${folder.name}，${closedFolders.includes(folder.id) ? "展开" : "收起"}`}
                    aria-expanded={!closedFolders.includes(folder.id)}
                    onClick={() => toggleFolder(folder.id)}
                  />
                  {!closedFolders.includes(folder.id) && chats.map(railFace)}
                </section>
              ),
            )}
            {!workspace && <span className="history-empty">正在读取…</span>}
            {finished.length > 0 && (
              <button
                type="button"
                className="wait-folded finished-folded"
                title={`${finished.length} 个已完成的对话`}
                aria-label={`${finished.length} 个已完成的对话，展开侧栏`}
                onClick={onToggle}
              >
                {finished.length}
              </button>
            )}
          </div>
        ) : (
          <>
            {visiblePinned.length > 0 && <div className="pin-grid">{visiblePinned.map(pinCard)}</div>}

            {visibleUngrouped.length > 0 && <div className="bot-group">{visibleUngrouped.map(row)}</div>}

            {visibleFolders.map(({ folder, chats }) => (
              <section key={folder.id} className="folder">
                {folderHead(folder, chats.length)}
                <div
                  className={closedFolders.includes(folder.id) ? "collapse" : "collapse open"}
                  inert={closedFolders.includes(folder.id)}
                >
                  <div className="collapse-inner">
                    {chats.length > 0 ? (
                      <div className="bot-group">{chats.map(row)}</div>
                    ) : (
                      <p className="folder-empty">右键机器人可移入</p>
                    )}
                  </div>
                </div>
              </section>
            ))}

            {draft?.mode === "create" && <section className="folder">{folderDraftInput("新建分组")}</section>}

            {empty && <p className="history-empty">没有找到「{query.trim()}」</p>}

            {finished.length > 0 && (
              <section className="finished-list" aria-label="已完成">
                <p className="finished-label">已完成</p>
                {finished.map((run) => (
                  <button
                    key={run.run_id}
                    type="button"
                    className="finished-row"
                    title={run.conversation_title}
                    onClick={() => {
                      if (run.message_id) store.focusMessage(run.conversation_id, run.message_id);
                      onOpen(run.conversation_id);
                    }}
                  >
                    <span className="finished-check" aria-hidden="true">
                      <Icon name="check" size={11} />
                    </span>
                    <span className="finished-text">
                      <span className="finished-title">{run.conversation_title}</span>
                      <span className="finished-preview">
                        {run.agent_name ? `${run.agent_name}：` : ""}
                        {run.preview || "已完成"}
                      </span>
                    </span>
                    <span className="finished-time">{shortTime(run.finished_at)}</span>
                  </button>
                ))}
              </section>
            )}

            {state.workspaceError && (
              <div className="rail-error">
                <p>{state.workspaceError}</p>
                <button type="button" className="link-button" onClick={() => void store.loadWorkspace()}>
                  重新读取
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* The tools dock: one column of the daily tools and 更多, which opens the rest (and
          settings) as a menu above it. Folded, the same entries are icons, named on hover. */}
      <nav className="sidebar-foot" aria-label="工具">
        {tools.map((tool) => (
          <button
            key={tool.id}
            type="button"
            className={active === tool.id ? "foot-button active" : "foot-button"}
            onClick={tool.onSelect}
            title={collapsed ? tool.label : undefined}
            aria-label={tool.label}
            aria-pressed={active === tool.id}
          >
            <Icon name={tool.icon} />
            {!collapsed && <span className="foot-label">{tool.label}</span>}
          </button>
        ))}
        <button
          type="button"
          className={moreActive ? "foot-button active" : "foot-button"}
          title={collapsed ? "更多" : undefined}
          aria-label="更多"
          aria-haspopup="menu"
          aria-expanded={menu !== null && menu.trigger === moreButton.current}
          ref={moreButton}
          onClick={(event) => {
            const closed = lastClosed.current;
            if (closed.trigger === event.currentTarget && Date.now() - closed.at < 300) return;
            const rect = event.currentTarget.getBoundingClientRect();
            const items = moreTools.map((tool) => ({ label: tool.label, icon: tool.icon, onSelect: tool.onSelect }));
            openMenu(rect.left, rect.top - 4, items, event.currentTarget, true);
          }}
        >
          <Icon name="more" />
          {!collapsed && <span className="foot-label">更多</span>}
        </button>
      </nav>

      {shownMenu.value && (
        <ContextMenu
          x={shownMenu.value.x}
          y={shownMenu.value.y}
          items={shownMenu.value.items}
          above={shownMenu.value.above}
          open={shownMenu.open}
          onClose={closeMenu}
        />
      )}
      {!collapsed && (
        <div
          className="rail-resize"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整侧栏宽度"
          aria-valuemin={RAIL_MIN}
          aria-valuemax={RAIL_MAX}
          aria-valuenow={width}
          tabIndex={0}
          title="拖动调整宽度，双击恢复默认"
          {...resizeHandlers}
        />
      )}
    </aside>
  );
}

function WaitingList({
  decisions,
  collapsed,
  onOpen,
  onExpand,
}: {
  decisions: Decision[];
  collapsed: boolean;
  onOpen: (decision: Decision) => void;
  onExpand: () => void;
}) {
  if (collapsed) {
    return (
      <button
        type="button"
        className="wait-folded"
        onClick={onExpand}
        title={`${decisions.length} 件待确认`}
        aria-label={`${decisions.length} 件待确认，展开侧栏`}
      >
        {decisions.length}
      </button>
    );
  }
  return (
    <section className="wait-list" aria-label="待确认">
      <p className="wait-list-label">待确认</p>
      {decisions.map((decision) => (
        <button key={decision.id} type="button" className="wait-row" onClick={() => onOpen(decision)}>
          <span className="wait-row-who">{decision.who || decision.where}</span>
          <span className="wait-row-prompt">{decision.prompt}</span>
          <span className="wait-row-if">{decision.if_unanswered}</span>
        </button>
      ))}
    </section>
  );
}
