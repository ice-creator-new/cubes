// Grok-style shell: collapsible history rail on the left, the message area on the right. The
// message area has browser-style tabs: each conversation, rail tool, bot editor, group settings
// or run trace is a tab, and every tab closes. With none open the area invites a first chat.
// Short forms (a new member) stay dialogs.

import { useCallback, useEffect, useRef, useState } from "react";
import type { Agent } from "./api/types";
import { ChatView } from "./components/ChatView";
import { EmojiSheet } from "./components/Reactions";
import { ComputerPanel } from "./components/Computer";
import { ConnectorsPanel } from "./components/Connectors";
import { MemoryPanel } from "./components/Memory";
import { Onboarding } from "./components/Onboarding";
import {
  CreateMember,
  GroupSettings,
  NewGroupDialog,
  MemberEditor,
  Settings,
  TeamPanel,
  Workbench,
  type ThemeChoice,
} from "./components/Panels";
import { SchedulesPanel } from "./components/Schedules";
import { WorkflowsPanel } from "./components/Workflows";
import { SkillsPanel } from "./components/Skills";
import { UsagePanel } from "./components/Usage";
import { RunTraceSheet } from "./components/RunTrace";
import { PaneHost } from "./components/Dialog";
import { ACCENT_TOKENS, accentStyle, parseAccent, type Accent } from "./lib/accent";
import { applyGlass, GLASS_KEY, parseGlass, type Glass } from "./lib/glass";
import { asHexColor } from "./bot/catalog";
import { TabBar } from "./components/TabBar";
import { Sidebar, type SidebarTool } from "./components/Sidebar";
import { setWindowTheme, watchFullscreen } from "./lib/desktop";
import { nextZoom, onMenuCommand, type MenuCommand } from "./lib/menu";
import { desktop, isDesktop } from "./lib/platform";
import { usePresence } from "./lib/motion";
import * as tabsModel from "./lib/tabs";
import { tabKey, type Tab } from "./lib/tabs";
import { supervisorName } from "./lib/team";
import { Icon } from "./components/Icon";
import { store, useTeam } from "./state/store";

const APPEARANCE_KEY = "cubes.appearance";
const ACCENT_KEY = "cubes.accent";
const TABS_KEY = "cubes.tabs";

/** What opens over everything as a dialog: short forms only. */
type Overlay = { kind: "create" } | null;

/** Whether the system is in dark mode, followed live: 跟随系统 repaints without a reload. */
function useSystemDark(): boolean {
  const query = "(prefers-color-scheme: dark)";
  const [dark, setDark] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const onChange = () => setDark(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return dark;
}

export function App() {
  const workspace = useTeam((s) => s.workspace);
  const streamError = useTeam((s) => s.streamError);
  const notice = useTeam((s) => s.notice);
  const embeddedFailure = useTeam((s) => s.embeddedFailure);
  const backendSettled = useTeam((s) => s.backendSettled);
  // The message area's tabs. A tool or editor tab stays mounted when hidden (scroll and forms
  // survive a switch); a conversation renders while shown (the store holds one open thread).
  // Restored across reloads; a first run starts on the supervisor's chat.
  const [tabs, setTabs] = useState(() => tabsModel.restore(localStorage.getItem(TABS_KEY)) ?? null);
  useEffect(() => {
    if (tabs) localStorage.setItem(TABS_KEY, tabsModel.serialize(tabs));
  }, [tabs]);
  const view = tabs ?? tabsModel.EMPTY;
  const shown = tabsModel.activeTab(view);
  /** The conversation on screen, if the shown tab is one. */
  const openID = shown?.kind === "chat" ? shown.id : null;
  // The chat column crossfades only when another conversation replaces one already on screen.
  const previousOpen = useRef<string | null>(null);
  const switching = previousOpen.current !== null && openID !== null && previousOpen.current !== openID;
  // The notice leaves on a short fade before it unmounts.
  const noticeBanner = usePresence(notice, 120);
  // Where the user stood before stepping into a private bot-to-bot chat.
  const [originID, setOriginID] = useState<string | null>(null);
  const [creatingGroup, setCreatingGroup] = useState(false);
  const [overlay, setOverlay] = useState<Overlay>(null);
  const update = useCallback((change: (state: tabsModel.TabsState) => tabsModel.TabsState) => {
    setTabs((state) => change(state ?? tabsModel.EMPTY));
  }, []);
  const openTab = useCallback((tab: Tab) => update((state) => tabsModel.openTab(state, tab)), [update]);
  const openTool = useCallback(
    (kind: SidebarTool) => {
      openTab({ kind });
      // A narrow window's rail floats over the page; picking a tool folds it out of the way.
      if (window.innerWidth <= 760) setCollapsed(true);
    },
    [openTab],
  );
  const closeTabKey = useCallback((key: string) => update((state) => tabsModel.closeTab(state, key)), [update]);
  const [collapsed, setCollapsed] = useState(() => window.innerWidth < 760);
  const [appearance, setAppearance] = useState<ThemeChoice>(
    () => (localStorage.getItem(APPEARANCE_KEY) as ThemeChoice | null) ?? "system",
  );
  const [accent, setAccent] = useState<Accent>(() => parseAccent(localStorage.getItem(ACCENT_KEY)));
  const [glass, setGlass] = useState<Glass>(() => parseGlass(localStorage.getItem(GLASS_KEY)));
  const systemDark = useSystemDark();
  const darkMode = appearance === "dark" || (appearance === "system" && systemDark);

  useEffect(() => {
    void store.boot();
  }, []);

  // macOS fullscreen hides the traffic lights: the title rows give their room back to content.
  useEffect(() => watchFullscreen(), []);

  useEffect(() => {
    if (appearance === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = appearance;
    setWindowTheme(appearance === "system" ? null : appearance);
    localStorage.setItem(APPEARANCE_KEY, appearance);
  }, [appearance]);

  useEffect(() => {
    applyGlass(glass);
    localStorage.setItem(GLASS_KEY, JSON.stringify(glass));
  }, [glass]);

  // A preset accent is one `data-accent` rule in the stylesheet. A free colour is computed for
  // the theme on screen and written to the same four tokens, so a pick repaints the whole app
  // (bubbles included) without a re-render.
  useEffect(() => {
    const root = document.documentElement;
    const custom = asHexColor(accent);
    if (custom) delete root.dataset.accent;
    else root.dataset.accent = accent;
    const style = custom ? accentStyle(custom, darkMode ? "dark" : "light") : null;
    for (const token of ACCENT_TOKENS) {
      if (style) root.style.setProperty(token, style[token]);
      else root.style.removeProperty(token);
    }
    localStorage.setItem(ACCENT_KEY, accent);
  }, [accent, darkMode]);

  useEffect(() => {
    previousOpen.current = openID;
  }, [openID]);

  // A first run (nothing stored) lands in the supervisor's thread once the directory arrives.
  useEffect(() => {
    if (tabs === null && workspace?.direct_conversation_id) {
      setTabs(tabsModel.openTab(tabsModel.EMPTY, { kind: "chat", id: workspace.direct_conversation_id }));
    }
  }, [tabs, workspace?.direct_conversation_id]);

  /**
   * Show a conversation in its own tab: every bot and group gets one, opened next to the shown
   * tab, and picking a conversation that already has a tab switches to it. A private bot-to-bot
   * chat remembers where the user came from.
   */
  const openConversation = useCallback(
    (id: string) => {
      const target = workspace?.conversations.find((conversation) => conversation.id === id);
      if (target?.kind === "private") {
        const current = workspace?.conversations.find((conversation) => conversation.id === openID);
        if (current && current.kind !== "private") setOriginID(current.id);
      } else {
        setOriginID(null);
      }
      update((state) => tabsModel.openTab(state, { kind: "chat", id }));
      if (window.innerWidth < 760) setCollapsed(true);
    },
    [openID, workspace, update],
  );
  /** Open a conversation in a new tab of its own, whatever is showing (middle click, ⌘ click). */
  const openConversationTab = useCallback(
    (id: string) => openTab({ kind: "chat", id }),
    [openTab],
  );

  const closePrivate = useCallback(() => {
    const home = originID && workspace?.conversations.some((conversation) => conversation.id === originID) ? originID : workspace?.direct_conversation_id;
    setOriginID(null);
    if (home) openConversation(home);
  }, [originID, workspace, openConversation]);

  useEffect(() => {
    // Inside the desktop shell ⌘\ is the 显示 menu's accelerator; handling it here as well
    // would toggle twice. The browser has no menu bar, so the page keeps the shortcut.
    if (isDesktop) return;
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "\\") {
        event.preventDefault();
        setCollapsed((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // The native menu bar: every item arrives here as one command. The handler reads live state
  // through a ref, so the listener is registered once rather than on every render.
  const zoom = useRef(1);
  const onMenu = useRef<(command: MenuCommand) => void>(() => undefined);
  onMenu.current = (command) => {
    const tools: Partial<Record<MenuCommand, SidebarTool>> = {
      settings: "settings",
      team: "team",
      connectors: "connectors",
      computer: "computer",
      workbench: "workbench",
      memory: "memory",
      schedules: "schedules",
      skills: "skills",
      workflows: "workflows",
    };
    const tool = tools[command];
    if (tool) {
      setOverlay(null);
      openTool(tool);
      return;
    }
    if (command === "new-member") {
      openTool("team");
      setOverlay({ kind: "create" });
      return;
    }
    switch (command) {
      case "home":
      case "focus-composer":
        setOverlay(null);
        if (command === "home") {
          setOriginID(null);
          if (workspace?.direct_conversation_id) openConversation(workspace.direct_conversation_id);
        }
        requestAnimationFrame(() => document.querySelector<HTMLElement>(".composer-field")?.focus());
        return;
      case "toggle-sidebar":
        setCollapsed((value) => !value);
        return;
      case "search":
        setOverlay(null);
        setCollapsed(false);
        // Two copies exist (title row in fullscreen, below it otherwise); focus the one shown.
        requestAnimationFrame(() => {
          const fields = document.querySelectorAll<HTMLInputElement>('.sidebar input[aria-label="搜索机器人"]');
          [...fields].find((field) => field.offsetParent !== null)?.focus();
        });
        return;
      case "stop":
        if (openID) void store.stop(openID);
        return;
      case "zoom-reset":
      case "zoom-in":
      case "zoom-out": {
        const next = command === "zoom-reset" ? 1 : nextZoom(zoom.current, command === "zoom-in" ? 1 : -1);
        zoom.current = next;
        desktop?.setZoom(next);
        return;
      }
    }
  };
  useEffect(() => onMenuCommand((command) => onMenu.current(command)), []);

  /** A conversation picked from inside a tab: it opens in its own tab, the tool stays open. */
  const openFromTab = openConversationTab;
  // The rail lights the tool whose tab is shown.
  const activeTool: SidebarTool | null = shown && !("id" in shown) ? shown.kind : null;

  // ⌘W / Ctrl+W closes the shown tab, ⇧⌘T brings back the last closed one, ⌃Tab / ⌃⇧Tab walks
  // the tabs, as in a browser.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || document.querySelector(".scrim")) return;
      const command = event.metaKey || event.ctrlKey;
      if (command && !event.shiftKey && event.key.toLowerCase() === "w") {
        if (view.active === null) return;
        event.preventDefault();
        closeTabKey(view.active);
      } else if (command && event.shiftKey && event.key.toLowerCase() === "t") {
        event.preventDefault();
        update(tabsModel.reopen);
      } else if (event.ctrlKey && event.key === "Tab") {
        event.preventDefault();
        update((state) => tabsModel.cycle(state, event.shiftKey ? -1 : 1));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view.active, closeTabKey, update]);

  // Tabs about something that no longer exists (a deleted bot and its chats, a dissolved group)
  // close; nothing else moves. A thread may not be listed in the directory at all — it is reached
  // through its group's task — so a chat tab the store already knows as a thread stays.
  useEffect(() => {
    if (!workspace) return;
    const conversations = new Set(workspace.conversations.map((conversation) => conversation.id));
    const agents = new Set(workspace.agents.map((agent) => agent.id));
    const threads = store.getState().threadGroups;
    setTabs((state) =>
      state === null
        ? state
        : tabsModel.closeWhere(
            state,
            (tab) =>
              ((tab.kind === "chat" && !conversations.has(tab.id) && threads[tab.id] === undefined) ||
                (tab.kind === "group" && !conversations.has(tab.id))) ||
              (tab.kind === "member" && !agents.has(tab.id)),
          ),
    );
  }, [workspace]);

  // A restored thread tab's chat is not in the directory: ask the groups which one owns it, so
  // the tab gets its label and the header its way back without the user reopening the group.
  useEffect(() => {
    if (!workspace || tabs === null) return;
    const known = new Set(workspace.conversations.map((conversation) => conversation.id));
    for (const tab of tabs.tabs) {
      if (tab.kind === "chat" && !known.has(tab.id)) void store.locateThread(tab.id);
    }
    // Only a new directory or the first restored strip needs this probe.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace, tabs === null]);

  const message = async (agent: Agent) => {
    if (agent.kind === "supervisor" && workspace) {
      setOverlay(null);
      openConversationTab(workspace.direct_conversation_id);
      return;
    }
    const result = await store.openDirect(agent.id);
    if ("id" in result) {
      setOverlay(null);
      openConversationTab(result.id);
    } else {
      window.alert(result.error);
    }
  };

  // The shell starts the backend before this window can work: until it says where, every view
  // that loads on mount (a restored chat tab, the computer panel) would ask the default address
  // and show 连不上后端服务 for a backend that is merely still starting.
  if (!backendSettled) {
    return (
      <div className="standstill" role="status">
        <p>正在启动后端…</p>
      </div>
    );
  }

  // Without a backend there is no app at all.
  if (embeddedFailure) {
    return (
      <div className="standstill">
        <h1>后端没能启动</h1>
        <p>{embeddedFailure.error}</p>
        {embeddedFailure.logPath && <p className="standstill-path">日志：{embeddedFailure.logPath}</p>}
        <button type="button" className="primary-button" onClick={() => void store.restartBackend()}>
          重试
        </button>
      </div>
    );
  }

  // A workspace that has not named its supervisor yet shows only the first-run screen; the
  // workspace event that follows completion mounts the shell with the chosen name and look.
  if (workspace && workspace.onboarded === false) {
    return <Onboarding />;
  }

  return (
    <div className="app">
      <Sidebar
        collapsed={collapsed}
        onToggle={() => setCollapsed((value) => !value)}
        onOpen={openConversation}
        onTeam={() => openTool("team")}
        onWorkbench={() => openTool("workbench")}
        onMemory={() => openTool("memory")}
        onSchedules={() => openTool("schedules")}
        onUsage={() => openTool("usage")}
        onConnectors={() => openTool("connectors")}
        onComputer={() => openTool("computer")}
        onSettings={() => openTool("settings")}
        onSkills={() => openTool("skills")}
        onWorkflows={() => openTool("workflows")}
        active={activeTool}
        selectedID={openID}
        onEditBot={(id) => openTab({ kind: "member", id })}
        onDeleteMember={(id) => openTab({ kind: "member", id, confirmDelete: true })}
        onDissolveGroup={(id) => openTab({ kind: "group", id, confirmDissolve: true })}
        onNewGroup={() => setCreatingGroup(true)}
        onNewMember={() => setOverlay({ kind: "create" })}
      />
      {creatingGroup && (
        <NewGroupDialog
          onClose={() => setCreatingGroup(false)}
          onCreated={(id) => {
            setCreatingGroup(false);
            openConversation(id);
          }}
        />
      )}
      {/* Narrow windows float the opened rail over the page: a scrim beside it dims the page and
          folds the rail on a click, so the page under it never reads as part of the rail. */}
      {!collapsed && <div className="rail-scrim" aria-hidden="true" onClick={() => setCollapsed(true)} />}

      <main className="main tabbed">
        {view.tabs.length > 0 && (
          <TabBar
            workspace={workspace}
            state={view}
            onActivate={(key) => update((state) => tabsModel.activate(state, key))}
            onClose={closeTabKey}
            onMove={(key, to) => update((state) => tabsModel.moveTab(state, key, to))}
            onChange={update}
          />
        )}
        {streamError && (
          <div className="banner" role="alert">
            <span>{streamError}</span>
            <button type="button" className="link-button" onClick={() => void store.retryStream()}>
              重新连接
            </button>
          </div>
        )}
        {noticeBanner.value && (
          <div className={noticeBanner.open ? "banner" : "banner closing"} role="alert">
            <span>{noticeBanner.value}</span>
            <button type="button" className="link-button" onClick={() => store.dismissNotice()}>
              知道了
            </button>
          </div>
        )}
        {/* The shown conversation; tool tabs below stay mounted and hidden when not shown. */}
        {openID && (
          <div className="main-chat" role="tabpanel">
            <ChatView
              key={openID}
              conversationID={openID}
              arrive={switching}
              onOpenMember={(id) => openTab({ kind: "member", id })}
              onOpenConversation={openConversation}
              onOpenGroupSettings={(id) => openTab({ kind: "group", id })}
              onOpenMemories={() => openTool("memory")}
              onClosePrivate={closePrivate}
              onOpenRun={(run) => openTab(tabsModel.runTab(run))}
              onOpenWorkflows={() => openTool("workflows")}
            />
          </div>
        )}
        {view.active === null && (
          <div className="tabs-empty" data-drag-region>
            <span className="tabs-empty-mark" aria-hidden="true">
              <Icon name="chat" size={30} />
            </span>
            <h2>选择一个机器人来聊天吧</h2>
            <p>在左侧点一个机器人或群组，或者从团队里找一位成员。</p>
            <div className="tabs-empty-actions">
              {workspace?.direct_conversation_id && (
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => openConversation(workspace.direct_conversation_id)}
                >
                  和{supervisorName(workspace)}聊聊
                </button>
              )}
              <button type="button" className="ghost-button" onClick={() => openTool("team")}>
                <Icon name="team" size={15} />
                查看团队
              </button>
            </div>
          </div>
        )}
        {view.tabs.map((tab) => {
          if (tab.kind === "chat") return null;
          const key = tabKey(tab);
          const close = () => closeTabKey(key);
          const shownTab = key === view.active;
          return (
            <div key={key} className="main-pane" role="tabpanel" hidden={!shownTab} inert={!shownTab}>
              <PaneHost>
                {tab.kind === "team" && (
                  <TeamPanel
                    onClose={close}
                    onOpenMember={(id) => openTab({ kind: "member", id })}
                    onCreate={() => setOverlay({ kind: "create" })}
                    onMessage={message}
                    onOpenConversation={openFromTab}
                    onOpenUsage={() => openTool("usage")}
                  />
                )}
                {tab.kind === "workbench" && <Workbench onClose={close} />}
                {tab.kind === "memory" && <MemoryPanel onClose={close} onOpenConversation={openFromTab} />}
                {tab.kind === "schedules" && <SchedulesPanel onClose={close} onOpenConversation={openFromTab} />}
                {tab.kind === "connectors" && <ConnectorsPanel onClose={close} />}
                {tab.kind === "computer" && <ComputerPanel onClose={close} />}
                {tab.kind === "usage" && <UsagePanel onClose={close} />}
                {tab.kind === "skills" && <SkillsPanel onClose={close} />}
                {tab.kind === "workflows" && (
                  <WorkflowsPanel onClose={close} onOpenConversation={openFromTab} />
                )}
                {tab.kind === "settings" && (
                  <Settings
                    onClose={close}
                    appearance={appearance}
                    onAppearance={setAppearance}
                    accent={accent}
                    onAccent={setAccent}
                    glass={glass}
                    onGlass={setGlass}
                  />
                )}
                {tab.kind === "member" && (
                  <MemberEditor
                    agentID={tab.id}
                    startConfirming={tab.confirmDelete}
                    onClose={close}
                    onMessage={message}
                    onOpenRun={(run) => openTab(tabsModel.runTab(run))}
                    onDeleted={close}
                  />
                )}
                {tab.kind === "group" && (
                  <GroupSettings
                    conversationID={tab.id}
                    startConfirming={tab.confirmDissolve}
                    onClose={close}
                    onDissolved={close}
                  />
                )}
                {tab.kind === "run" && (
                  <RunTraceSheet
                    runID={tab.id}
                    onClose={close}
                    onOpenConversation={openFromTab}
                    onLoaded={(run) => update((state) => tabsModel.describeRun(state, run))}
                  />
                )}
              </PaneHost>
            </div>
          );
        })}
      </main>

      {overlay?.kind === "create" && <CreateMember onClose={() => setOverlay(null)} />}
      {/* One emoji sheet for the whole app: opening it never re-renders a conversation. */}
      <EmojiSheet />
    </div>
  );
}
