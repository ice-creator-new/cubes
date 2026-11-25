// The message area's tabs, browser-style. Every page is a tab: a conversation with a bot or a
// group, a rail tool, a bot's editor, a group's settings, a run's trace. Every tab can close;
// with none left the area shows an empty state. Pure state only; the tab bar renders it and App
// keeps it. Keys are stable, so opening something already open just switches to it, and a
// reload restores the same tabs.

import type { SidebarTool } from "../components/Sidebar";

export type Tab =
  | { kind: "chat"; id: string }
  | { kind: SidebarTool }
  | { kind: "member"; id: string; confirmDelete?: boolean }
  | { kind: "group"; id: string; confirmDissolve?: boolean }
  /** `conversation` and `at` (the run's start) name the tab, and `schedule` when a scheduled
   *  task started it (its hidden workspace is in no list); tabs stored before these existed
   *  have none of them and fall back to a plain label. */
  | { kind: "run"; id: string; conversation?: string; at?: string; schedule?: string }
  /** One bot's work detail and one bot's whole work log. A detail tab learns its title from the
   *  payload; a restored one falls back to a plain label. */
  | { kind: "activity"; id: string; title?: string }
  | { kind: "activities"; id: string };

/** What opening a run's trace needs: the run, the conversation it ran in, when it started, and
 *  the scheduled task behind it, if any. */
export interface RunRef {
  id: string;
  conversation: string;
  at: string;
  schedule?: string | null;
}

export function runTab(run: RunRef): Tab {
  return {
    kind: "run",
    id: run.id,
    conversation: run.conversation,
    at: run.at,
    ...(run.schedule ? { schedule: run.schedule } : {}),
  };
}

/** Fill in an open run tab's conversation and start once the trace has read them (a tab restored
 *  from before tabs carried them); every other tab, and which one shows, stay as they are. */
export function describeRun(state: TabsState, run: RunRef): TabsState {
  const key = `run:${run.id}`;
  const index = state.tabs.findIndex((tab) => tabKey(tab) === key);
  const current = state.tabs[index];
  if (
    !current ||
    current.kind !== "run" ||
    (current.conversation === run.conversation && current.at === run.at && current.schedule === (run.schedule || undefined))
  ) {
    return state;
  }
  const tabs = [...state.tabs];
  tabs[index] = runTab(run);
  return { ...state, tabs };
}

/** Fill in an open activity tab's title once its payload has been read (a restored tab has none). */
export function describeActivity(state: TabsState, id: string, title: string): TabsState {
  const key = `activity:${id}`;
  const index = state.tabs.findIndex((tab) => tabKey(tab) === key);
  const current = state.tabs[index];
  if (!current || current.kind !== "activity" || current.title === title) return state;
  const tabs = [...state.tabs];
  tabs[index] = { kind: "activity", id, title };
  return { ...state, tabs };
}

export interface TabsState {
  /** Open tabs, left to right; the pinned ones always come first. */
  tabs: Tab[];
  /** The shown tab's key, or null when every tab is closed. */
  active: string | null;
  /** For each tab, the tab that was showing when it opened: closing it goes back there. */
  openers: Record<string, string>;
  /** Keys of the pinned tabs, in strip order. */
  pinned: string[];
  /** Recently closed tabs, newest first, with the slot each had: 重新打开 brings them back. */
  closed: { tab: Tab; index: number }[];
}

export const EMPTY: TabsState = { tabs: [], active: null, openers: {}, pinned: [], closed: [] };

/** How many closed tabs 重新打开 remembers. */
const CLOSED_LIMIT = 20;

export function tabKey(tab: Tab): string {
  return "id" in tab ? `${tab.kind}:${tab.id}` : tab.kind;
}

export function activeTab(state: TabsState): Tab | null {
  return state.tabs.find((tab) => tabKey(tab) === state.active) ?? null;
}

/**
 * Show `tab`: switch to it when it is already open (taking its new flags), else add it. A new tab
 * goes right after the one showing, as a browser opens a link next to the page it came from.
 */
export function openTab(state: TabsState, tab: Tab): TabsState {
  const key = tabKey(tab);
  const index = state.tabs.findIndex((item) => tabKey(item) === key);
  if (index >= 0) {
    const tabs = [...state.tabs];
    const current = tabs[index]!;
    // Opening an activity again (a card, a chip, the work log) must not forget the title the
    // sheet already learned: the new request only names the id.
    tabs[index] =
      tab.kind === "activity" && current.kind === "activity" && !tab.title && current.title
        ? { kind: "activity", id: tab.id, title: current.title }
        : tab;
    return { ...state, tabs, active: key };
  }
  const from = state.active === null ? -1 : state.tabs.findIndex((item) => tabKey(item) === state.active);
  // Never inside the pinned group: a tab opened from a pinned one lands just after the pins.
  const at = Math.max(state.pinned.length, from >= 0 ? from + 1 : state.tabs.length);
  const tabs = [...state.tabs.slice(0, at), tab, ...state.tabs.slice(at)];
  const openers = state.active === null ? state.openers : { ...state.openers, [key]: state.active };
  return { ...state, tabs, active: key, openers };
}

export function activate(state: TabsState, key: string): TabsState {
  if (!state.tabs.some((tab) => tabKey(tab) === key)) return state;
  return state.active === key ? state : { ...state, active: key };
}

/**
 * Close one tab. When it was the shown one, show the tab it was opened from if that is still
 * open (a member's editor goes back to 团队), else its right neighbour, else its left, else
 * nothing (the empty state).
 */
export function closeTab(state: TabsState, key: string): TabsState {
  const index = state.tabs.findIndex((tab) => tabKey(tab) === key);
  if (index < 0) return state;
  const tabs = state.tabs.filter((_, i) => i !== index);
  const { [key]: opener, ...openers } = state.openers;
  const pinned = state.pinned.filter((item) => item !== key);
  // A pending delete confirmation is not worth bringing back with the tab.
  const closed = [{ tab: plain(state.tabs[index]!), index }, ...state.closed.filter((entry) => tabKey(entry.tab) !== key)].slice(
    0,
    CLOSED_LIMIT,
  );
  if (state.active !== key) return { ...state, tabs, openers, pinned, closed };
  const right = tabs[index];
  const left = tabs[index - 1];
  const active =
    opener !== undefined && tabs.some((tab) => tabKey(tab) === opener)
      ? opener
      : right
        ? tabKey(right)
        : left
          ? tabKey(left)
          : null;
  return { tabs, active, openers, pinned, closed };
}

/** Close every tab matching `drop` (a deleted bot's chat and editor, a dissolved group). These
 *  are gone for good, so they are not offered back by 重新打开. */
export function closeWhere(state: TabsState, drop: (tab: Tab) => boolean): TabsState {
  const next = state.tabs.filter(drop).reduce((acc, tab) => closeTab(acc, tabKey(tab)), state);
  return next === state ? state : { ...next, closed: state.closed };
}

/** Close tabs the user picked from the menu (关闭其他, 关闭右侧, 关闭全部). A pinned tab is never
 *  closed by one of these, only one at a time, as in a browser. */
export function closeMany(state: TabsState, drop: (tab: Tab, index: number) => boolean): TabsState {
  const picked = state.tabs.filter((tab, index) => !isPinned(state, tabKey(tab)) && drop(tab, index));
  return picked.reduce((next, tab) => closeTab(next, tabKey(tab)), state);
}

/** Every unpinned tab but `key` closes; `key` shows. */
export function closeOthers(state: TabsState, key: string): TabsState {
  return closeMany(activate(state, key), (tab) => tabKey(tab) !== key);
}

/** Every unpinned tab to the right of `key` closes. */
export function closeRight(state: TabsState, key: string): TabsState {
  const at = state.tabs.findIndex((tab) => tabKey(tab) === key);
  if (at < 0) return state;
  return closeMany(state, (_, index) => index > at);
}

export function isPinned(state: TabsState, key: string): boolean {
  return state.pinned.includes(key);
}

/**
 * Pin or unpin `key`. Pinned tabs sit together at the left in the order they were pinned and show
 * only their face; pinning moves a tab to the end of that group, unpinning to just after it.
 */
export function setPinned(state: TabsState, key: string, on: boolean): TabsState {
  const tab = state.tabs.find((item) => tabKey(item) === key);
  if (!tab || isPinned(state, key) === on) return state;
  const pinned = on ? [...state.pinned, key] : state.pinned.filter((item) => item !== key);
  const rest = state.tabs.filter((item) => tabKey(item) !== key);
  // The tab lands at the boundary either way: last of the pins, or first after them.
  const lead = rest.filter((item) => pinned.includes(tabKey(item)));
  const tail = rest.filter((item) => !pinned.includes(tabKey(item)));
  const tabs = [...lead, tab, ...tail];
  return { ...state, tabs, pinned };
}

/** Bring back the last closed tab that is not open again, where it was and shown (⇧⌘T). With
 *  nothing to bring back the state is returned as is. */
export function reopen(state: TabsState): TabsState {
  const open = new Set(state.tabs.map(tabKey));
  const skip = state.closed.findIndex((entry) => !open.has(tabKey(entry.tab)));
  if (skip < 0) return state;
  const last = state.closed[skip]!;
  const closed = state.closed.slice(skip + 1);
  const key = tabKey(last.tab);
  const at = Math.max(state.pinned.length, Math.min(last.index, state.tabs.length));
  const tabs = [...state.tabs.slice(0, at), last.tab, ...state.tabs.slice(at)];
  return { ...state, tabs, active: key, closed };
}

/**
 * Move the tab `key` to slot `to` (clamped), keeping which tab is shown (dragging a tab). A pinned
 * tab stays among the pinned ones and an unpinned one stays after them.
 */
export function moveTab(state: TabsState, key: string, to: number): TabsState {
  const from = state.tabs.findIndex((tab) => tabKey(tab) === key);
  if (from < 0) return state;
  const pins = state.tabs.filter((tab) => isPinned(state, tabKey(tab))).length;
  const [low, high] = isPinned(state, key) ? [0, pins - 1] : [pins, state.tabs.length - 1];
  const target = Math.max(low, Math.min(high, to));
  if (target === from) return state;
  const tabs = [...state.tabs];
  const [moved] = tabs.splice(from, 1);
  tabs.splice(target, 0, moved!);
  return { ...state, tabs };
}

/** The next or previous tab, wrapping (⌃Tab / ⌃⇧Tab). */
export function cycle(state: TabsState, direction: 1 | -1): TabsState {
  if (state.tabs.length === 0) return state;
  const keys = state.tabs.map(tabKey);
  const at = state.active === null ? -1 : keys.indexOf(state.active);
  const next = at < 0 ? (direction === 1 ? 0 : keys.length - 1) : (at + direction + keys.length) % keys.length;
  return { ...state, active: keys[next]! };
}

/** A tab without its one-off flags (a pending delete or dissolve confirmation). */
function plain(tab: Tab): Tab {
  if (tab.kind === "run") return runTab({ id: tab.id, conversation: tab.conversation ?? "", at: tab.at ?? "", schedule: tab.schedule });
  // The title is what the payload said; keeping it names a restored tab before any read.
  if (tab.kind === "activity") return tab.title ? { kind: "activity", id: tab.id, title: tab.title } : { kind: "activity", id: tab.id };
  return "id" in tab ? ({ kind: tab.kind, id: tab.id } as Tab) : tab;
}

/** What a reload brings back: the tabs, which are pinned, and the shown one, without one-off
 *  flags. The closed-tab history is this session's only. */
export function serialize(state: TabsState): string {
  return JSON.stringify({ tabs: state.tabs.map(plain), active: state.active, pinned: state.pinned });
}

const TOOLS: readonly string[] = [
  "team",
  "workbench",
  "memory",
  "schedules",
  "usage",
  "connectors",
  "computer",
  "settings",
  "skills",
  "workflows",
];
const WITH_ID: readonly string[] = ["chat", "member", "group", "run", "activity", "activities"];

/** The stored tabs, keeping only well-formed ones; `null` (never stored) is told apart from empty. */
export function restore(raw: string | null): TabsState | null {
  if (raw === null) return null;
  try {
    const data = JSON.parse(raw) as { tabs?: unknown; active?: unknown; pinned?: unknown };
    const tabs: Tab[] = [];
    for (const item of Array.isArray(data.tabs) ? data.tabs : []) {
      if (!item || typeof item !== "object") continue;
      const { kind, id, conversation, at, schedule, title } = item as Record<string, unknown>;
      if (typeof kind !== "string") continue;
      if (TOOLS.includes(kind)) tabs.push({ kind: kind as SidebarTool });
      else if (kind === "run" && typeof id === "string" && id) {
        tabs.push({
          kind,
          id,
          ...(typeof conversation === "string" && conversation ? { conversation } : {}),
          ...(typeof at === "string" && at ? { at } : {}),
          ...(typeof schedule === "string" && schedule ? { schedule } : {}),
        });
      } else if (kind === "activity" && typeof id === "string" && id) {
        tabs.push({ kind, id, ...(typeof title === "string" && title ? { title } : {}) });
      } else if (WITH_ID.includes(kind) && typeof id === "string" && id) tabs.push({ kind, id } as Tab);
    }
    const unique = tabs.filter((tab, i) => tabs.findIndex((other) => tabKey(other) === tabKey(tab)) === i);
    const open = new Set(unique.map(tabKey));
    const stored = Array.isArray(data.pinned) ? data.pinned : [];
    const pinned = stored.filter((key, i): key is string => typeof key === "string" && open.has(key) && stored.indexOf(key) === i);
    // Pinned tabs lead the strip whatever order was stored.
    const ordered = [...pinned.map((key) => unique.find((tab) => tabKey(tab) === key)!), ...unique.filter((tab) => !pinned.includes(tabKey(tab)))];
    const active =
      typeof data.active === "string" && open.has(data.active) ? data.active : ordered[0] ? tabKey(ordered[0]) : null;
    return { tabs: ordered, active, openers: {}, pinned, closed: [] };
  } catch {
    return null;
  }
}
