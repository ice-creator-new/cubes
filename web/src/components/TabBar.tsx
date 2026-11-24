// The message area's tab strip, browser-style. Every tab can close: a conversation with a bot or
// a group, a tool, a bot's editor, a group's settings, a run's trace. The shown tab is the
// same surface as the page under it and opens into it, with Chrome's curved feet at its base.
// The strip is the window's title row: on macOS empty space in it drags the window.
//
// Right-click (or Shift+F10 / the ContextMenu key) on a tab opens its menu: close, close others,
// close to the right, pin, reopen the last closed tab. Pinned tabs sit at the left as just their
// face and have no close button.

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
import { prefersReducedMotion, usePresence } from "../lib/motion";
import type { Agent, Schedule, Workspace } from "../api/types";
import * as tabsModel from "../lib/tabs";
import { tabKey, type Tab, type TabsState } from "../lib/tabs";
import { momentStamp } from "../lib/time";
import { agentByID, membersOf, useTeam } from "../state/store";
import { findThreadOrigin, threadTabTitle } from "../lib/threads";
import { Avatar, GroupAvatar } from "./Avatar";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Icon, type IconName } from "./Icon";

/** Stable empty list for the selector below: a fresh `[]` would churn the store subscription. */
const NO_SCHEDULES: Schedule[] = [];

const TOOL_TITLES: Record<string, [string, IconName]> = {
  team: ["团队", "team"],
  connectors: ["连接器", "connectors"],
  computer: ["电脑", "computer"],
  workbench: ["工作台", "notebook"],
  memory: ["记忆", "memory"],
  schedules: ["定时任务", "alarm"],
  usage: ["用量", "usage"],
  settings: ["设置", "settings"],
  skills: ["技能", "skills"],
  workflows: ["工作流", "workflows"],
};

interface Described {
  title: string;
  /** A second, quieter line under the title: when a run started, so runs of one chat differ. */
  detail?: string;
  agent?: Agent;
  members?: Agent[];
  icon?: IconName;
}

/** A conversation's name and face: a group's members, a direct chat's bot (the supervisor for
 *  its own thread, which has no member). */
function conversationLook(workspace: Workspace | null, id: string): Described | null {
  const conversation = workspace?.conversations.find((c) => c.id === id);
  if (!conversation) return null;
  if (conversation.kind === "group") return { title: conversation.title, members: membersOf(workspace, conversation) };
  if (conversation.kind === "thread") {
    // A thread names its member and the task under it: several tasks of one group can be open.
    const agent = agentByID(workspace, conversation.member_ids[0]);
    return {
      title: threadTabTitle(agent?.name, conversation.purpose || conversation.title),
      agent,
    };
  }
  const ownerID = conversation.member_ids.find((member) => member !== workspace?.supervisor_id) ?? workspace?.supervisor_id;
  return { title: conversation.title, agent: agentByID(workspace, ownerID) };
}

/** A tab's label and the face or glyph in front of it. `thread` names a chat the directory does
 *  not list: a task's thread, known only through the tasks loaded from its group. */
function describe(
  tab: Tab,
  workspace: Workspace | null,
  schedules: readonly Schedule[],
  thread: Described | null = null,
): Described {
  if (tab.kind === "chat") return conversationLook(workspace, tab.id) ?? thread ?? { title: "会话", icon: "chat" };
  if (tab.kind === "member") {
    const agent = agentByID(workspace, tab.id);
    return { title: agent ? `${agent.name} · 编辑` : "成员", agent };
  }
  if (tab.kind === "group") {
    const group = workspace?.conversations.find((c) => c.id === tab.id);
    return { title: group ? `${group.title} · 设置` : "群组设置", members: group ? membersOf(workspace, group) : [] };
  }
  if (tab.kind === "run") {
    // Several runs can be open at once: each names where it ran on top (the chat and its face,
    // or the scheduled task, whose hidden workspace is in no list) and when it started
    // underneath, so two runs of the same chat still tell apart at a glance.
    const detail = tab.at ? `${momentStamp(tab.at)} 的过程` : "运行过程";
    const look = tab.conversation ? conversationLook(workspace, tab.conversation) : null;
    if (look) return { ...look, detail };
    const task = tab.schedule ? schedules.find((schedule) => schedule.id === tab.schedule) : undefined;
    if (task) return { title: `定时 · ${task.title}`, detail, icon: "alarm" };
    return { title: "运行过程", detail: tab.at ? momentStamp(tab.at) : undefined, icon: "history" };
  }
  const [title, icon] = TOOL_TITLES[tab.kind] ?? [tab.kind, "info"];
  return { title, icon };
}

function Face({ agent, members, icon }: Omit<Described, "title">) {
  if (agent) return <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={18} name={agent.name} frozen />;
  if (members) return <GroupAvatar members={members} size={18} awake={false} track="off" />;
  return icon ? <Icon name={icon} size={15} /> : null;
}

/** How far the pointer travels before a press on a tab becomes a drag instead of a click. */
const DRAG_SLOP = 5;

interface Drag {
  key: string;
  pointer: number;
  startX: number;
  /** Every tab's left edge and width at the start, in strip coordinates. */
  slots: { key: string; left: number; width: number }[];
  from: number;
  /** Where the dragged tab would land, as it is now. */
  to: number;
  dx: number;
  moved: boolean;
}

/**
 * The slot a dragged tab lands in. Crossing a neighbour's middle is the cue, measured by the
 * dragged tab's leading edge (its right edge going right, its left edge going left), so a tab
 * pulled to the end of the strip lands last even though it can travel no further than that.
 */
function landing(drag: Drag, dx: number): number {
  const dragged = drag.slots[drag.from]!;
  const edge = dx >= 0 ? dragged.left + dragged.width + dx : dragged.left + dx;
  return drag.slots.filter((slot, index) => index !== drag.from && slot.left + slot.width / 2 < edge).length;
}

/** Where a tab's menu opens: at the pointer, or under the tab when the keyboard asked. */
function menuPoint(event: MouseEvent<HTMLElement> | KeyboardEvent<HTMLElement>) {
  if ("clientX" in event) return { x: event.clientX, y: event.clientY };
  const rect = event.currentTarget.getBoundingClientRect();
  return { x: rect.left + 8, y: rect.bottom + 4 };
}

export function TabBar({
  workspace,
  state,
  onActivate,
  onClose,
  onMove,
  onChange,
}: {
  workspace: Workspace | null;
  state: TabsState;
  onActivate: (key: string) => void;
  onClose: (key: string) => void;
  /** Drop a dragged tab into slot `to`. */
  onMove: (key: string, to: number) => void;
  /** Apply a change from the tab menu (close others, pin, reopen). */
  onChange: (change: (state: TabsState) => TabsState) => void;
}) {
  const { tabs, active } = state;
  const strip = useRef<HTMLDivElement>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; key: string; trigger: HTMLElement | null } | null>(null);
  const shownMenu = usePresence(menu, 140);
  const closeMenu = () => {
    menu?.trigger?.focus();
    setMenu(null);
  };
  // A scheduled run's tab is named by its task.
  const schedules = useTeam((s) => s.schedules)?.schedules ?? NO_SCHEDULES;
  // Task threads are not in the directory: their name comes from the task that owns them.
  const tasksByConversation = useTeam((s) => s.groupTasks);
  const threadLook = (id: string): Described | null => {
    const origin = findThreadOrigin(tasksByConversation, id);
    if (!origin) return null;
    const agent = agentByID(workspace, origin.task.owner_id);
    return { title: threadTabTitle(agent?.name, origin.task.title), agent };
  };
  // Dragging a tab, Chrome-style: the tab lifts and follows the pointer along the strip, the tabs
  // it passes glide aside, and on release it settles into its slot from wherever it was. A short
  // press is a click.
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragRef = useRef<Drag | null>(null);
  const suppressClick = useRef(false);
  // Where each tab stood on screen just before a drop, so the reordered tabs can glide from there.
  const settling = useRef<Map<string, number> | null>(null);
  const frame = useRef(0);
  const startDrag = (event: PointerEvent<HTMLDivElement>, key: string) => {
    if (event.button !== 0 || menu || (event.target as HTMLElement).closest(".tab-close")) return;
    const element = strip.current;
    if (!element) return;
    const base = element.getBoundingClientRect().left - element.scrollLeft;
    const slots = [...element.querySelectorAll<HTMLElement>(".tab")].map((node) => {
      const box = node.getBoundingClientRect();
      return { key: node.dataset.key ?? "", left: box.left - base, width: box.width };
    });
    const from = slots.findIndex((slot) => slot.key === key);
    if (from < 0) return;
    dragRef.current = { key, pointer: event.pointerId, startX: event.clientX, slots, from, to: from, dx: 0, moved: false };
  };
  const moveDrag = (event: PointerEvent<HTMLDivElement>) => {
    const current = dragRef.current;
    if (!current || current.pointer !== event.pointerId) return;
    const first = current.slots[0]!;
    const last = current.slots[current.slots.length - 1]!;
    const dragged = current.slots[current.from]!;
    // The tab stays inside the strip: it cannot be pulled past the first or last slot.
    const dx = Math.max(first.left - dragged.left, Math.min(last.left + last.width - dragged.left - dragged.width, event.clientX - current.startX));
    if (!current.moved && Math.abs(event.clientX - current.startX) < DRAG_SLOP) return;
    if (!current.moved) event.currentTarget.setPointerCapture(event.pointerId);
    const next = { ...current, dx, to: landing(current, dx), moved: true };
    dragRef.current = next;
    // One render per frame, however fast the pointer reports: the motion stays even.
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => setDrag(dragRef.current));
  };
  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    const current = dragRef.current;
    if (!current || current.pointer !== event.pointerId) return;
    cancelAnimationFrame(frame.current);
    dragRef.current = null;
    if (!current.moved) {
      setDrag(null);
      return;
    }
    // Remember where every tab is drawn right now (the dragged one under the pointer, the others
    // shifted aside), then commit the order; the layout effect below plays them home from there.
    const element = strip.current;
    if (element) {
      settling.current = new Map(
        [...element.querySelectorAll<HTMLElement>(".tab")].map((node) => [node.dataset.key ?? "", node.getBoundingClientRect().left]),
      );
    }
    setDrag(null);
    // The press became a drag: the click it ends with must not also switch tabs.
    suppressClick.current = true;
    window.setTimeout(() => (suppressClick.current = false), 0);
    if (current.to !== current.from) onMove(current.key, current.to);
    onActivate(current.key);
  };
  // The drop's FLIP: each tab starts where it was drawn at release and glides into its new slot,
  // so the dragged tab sinks into place instead of snapping.
  useLayoutEffect(() => {
    const before = settling.current;
    const element = strip.current;
    if (!before || !element || drag) return;
    settling.current = null;
    if (prefersReducedMotion()) return;
    for (const node of element.querySelectorAll<HTMLElement>(".tab")) {
      const from = before.get(node.dataset.key ?? "");
      if (from === undefined) continue;
      const delta = from - node.getBoundingClientRect().left;
      if (Math.abs(delta) < 0.5) continue;
      node.animate([{ transform: `translateX(${delta}px)` }, { transform: "translateX(0)" }], {
        duration: 240,
        easing: "cubic-bezier(0.2, 0.9, 0.25, 1)",
      });
    }
  });
  useEffect(() => () => cancelAnimationFrame(frame.current), []);
  // How far each tab is pushed while another one is dragged over it.
  const offsetOf = (key: string): number => {
    if (!drag) return 0;
    if (key === drag.key) return drag.dx;
    const index = drag.slots.findIndex((slot) => slot.key === key);
    const width = drag.slots[drag.from]!.width;
    if (drag.to > drag.from && index > drag.from && index <= drag.to) return -width;
    if (drag.to < drag.from && index < drag.from && index >= drag.to) return width;
    return 0;
  };
  // The shown tab stays in view when the strip is narrower than its tabs: on a switch, a new
  // tab, and when the window (and with it the strip) changes width.
  useEffect(() => {
    const element = strip.current;
    if (!element || active === null) return;
    // Scrolls only the strip (scrollIntoView could also scroll the page), on the frame after a
    // resize, when the tabs have their new widths.
    let frame = 0;
    const reveal = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const tab = element.querySelector<HTMLElement>(`[data-tab="${CSS.escape(active)}"]`)?.closest<HTMLElement>(".tab");
        if (!tab) return;
        const left = tab.offsetLeft - element.offsetLeft;
        const right = left + tab.offsetWidth;
        if (left < element.scrollLeft) element.scrollLeft = left - 12;
        else if (right > element.scrollLeft + element.clientWidth) element.scrollLeft = right - element.clientWidth + 12;
      });
    };
    reveal();
    // The strip's own box can stay put while the window narrows (it is flexed inside the bar),
    // so the window's resize drives it as well.
    const observer = new ResizeObserver(reveal);
    observer.observe(element);
    window.addEventListener("resize", reveal);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("resize", reveal);
    };
  }, [active, tabs.length]);

  const keys = tabs.map(tabKey);

  /** The menu for one tab. Bulk closes never touch pinned tabs, so they count only the others. */
  const menuFor = (key: string): MenuItem[] => {
    const at = keys.indexOf(key);
    const pinned = tabsModel.isPinned(state, key);
    const closable = (index: number) => !tabsModel.isPinned(state, keys[index]!);
    const others = keys.some((other, index) => other !== key && closable(index));
    const right = keys.some((_, index) => index > at && closable(index));
    const tab = tabs[at];
    const { title, detail } = tab
      ? describe(tab, workspace, schedules, tab.kind === "chat" ? threadLook(tab.id) : null)
      : { title: "", detail: undefined };
    return [
      { label: "关闭", icon: "close", onSelect: () => onClose(key) },
      { label: "关闭其他标签", icon: "collapse", disabled: !others, onSelect: () => onChange((current) => tabsModel.closeOthers(current, key)) },
      { label: "关闭右侧标签", icon: "chevron", disabled: !right, onSelect: () => onChange((current) => tabsModel.closeRight(current, key)) },
      {
        label: pinned ? "取消置顶" : "置顶",
        icon: pinned ? "unpin" : "pin",
        onSelect: () => onChange((current) => tabsModel.setPinned(current, key, !pinned)),
      },
      {
        label: "复制标题",
        icon: "copy",
        onSelect: () => void navigator.clipboard.writeText(detail ? `${title} · ${detail}` : title).catch(() => undefined),
      },
      {
        label: "重新打开关闭的标签",
        icon: "history",
        disabled: state.closed.every((entry) => keys.includes(tabKey(entry.tab))),
        onSelect: () => onChange(tabsModel.reopen),
      },
    ];
  };
  const openMenu = (event: MouseEvent<HTMLElement> | KeyboardEvent<HTMLElement>, key: string) => {
    event.preventDefault();
    const point = menuPoint(event);
    // The keyboard opened it from the tab button; a right-click lands on the tab's wrapper.
    const trigger = strip.current?.querySelector<HTMLElement>(`[data-tab="${CSS.escape(key)}"]`) ?? null;
    setMenu({ ...point, key, trigger });
  };
  // Arrow keys move along the strip like any tablist; with Shift they carry the focused tab along
  // (the keyboard's drag). Delete / Backspace closes the focused tab.
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, key: string) => {
    const at = keys.indexOf(key);
    const go = (to: number) => {
      event.preventDefault();
      const next = keys[(to + keys.length) % keys.length]!;
      onActivate(next);
      strip.current?.querySelector<HTMLElement>(`[data-tab="${CSS.escape(next)}"]`)?.focus();
    };
    const carry = (to: number) => {
      event.preventDefault();
      if (to < 0 || to >= keys.length) return;
      onMove(key, to);
      requestAnimationFrame(() => strip.current?.querySelector<HTMLElement>(`[data-tab="${CSS.escape(key)}"]`)?.focus());
    };
    if (event.shiftKey && event.key === "ArrowRight") carry(at + 1);
    else if (event.shiftKey && event.key === "ArrowLeft") carry(at - 1);
    else if (event.key === "ArrowRight") go(at + 1);
    else if (event.key === "ArrowLeft") go(at - 1);
    else if (event.key === "Home") go(0);
    else if (event.key === "End") go(keys.length - 1);
    else if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) openMenu(event, key);
    else if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      onClose(key);
    }
  };
  // Middle click closes, as in a browser.
  const onAuxClick = (event: MouseEvent, key: string) => {
    if (event.button === 1) {
      event.preventDefault();
      onClose(key);
    }
  };

  return (
    <div className="tab-bar" data-drag-region>
      <div className="tab-strip" role="tablist" aria-label="打开的页面" ref={strip} data-drag-region>
        {tabs.map((tab, index) => {
          const key = tabKey(tab);
          const on = key === active;
          // A hairline divides two quiet tabs; it hides next to the shown tab, as in Chrome.
          const nextOn = keys[index + 1] === active;
          const { title, detail, ...face } = describe(
            tab,
            workspace,
            schedules,
            tab.kind === "chat" ? threadLook(tab.id) : null,
          );
          const label = detail ? `${title} · ${detail}` : title;
          const pinned = tabsModel.isPinned(state, key);
          return (
            <div
              key={key}
              data-key={key}
              className={[
                "tab",
                on ? "on" : "",
                pinned ? "pinned" : "",
                nextOn ? "before-on" : "",
                drag?.key === key ? "dragging" : "",
                drag ? "sorting" : "",
                shownMenu.value?.key === key ? "menu-open" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={drag ? { transform: `translateX(${offsetOf(key)}px)` } : undefined}
              role="presentation"
              onAuxClick={(event) => onAuxClick(event, key)}
              onContextMenu={(event) => openMenu(event, key)}
              onPointerDown={(event) => startDrag(event, key)}
              onPointerMove={moveDrag}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
            >
              {/* The shown tab's silhouette (rounded top, curved feet), painted in CSS. */}
              <span className="tab-shape" aria-hidden="true" />
              <button
                type="button"
                role="tab"
                data-tab={key}
                className="tab-main"
                aria-selected={on}
                aria-haspopup="menu"
                // A pinned tab shows only its face, so its name is spoken from here.
                aria-label={pinned ? `${label}（已置顶）` : undefined}
                tabIndex={on || (active === null && index === 0) ? 0 : -1}
                title={pinned ? `${label}（已置顶）` : label}
                onClick={() => {
                  if (!suppressClick.current) onActivate(key);
                }}
                onKeyDown={(event) => onKeyDown(event, key)}
              >
                <span className="tab-face">
                  <Face {...face} />
                </span>
                {pinned ? null : detail ? (
                  <span className="tab-text">
                    <span className="tab-title">{title}</span>
                    <span className="tab-detail">{detail}</span>
                  </span>
                ) : (
                  <span className="tab-title">{title}</span>
                )}
              </button>
              {!pinned && (
                <button
                  type="button"
                  className="tab-close"
                  aria-label={`关闭「${label}」`}
                  title="关闭（⌘W）"
                  tabIndex={-1}
                  onClick={() => onClose(key)}
                >
                  <Icon name="close" size={12} />
                </button>
              )}
            </div>
          );
        })}
      </div>
      {shownMenu.value && (
        <ContextMenu
          x={shownMenu.value.x}
          y={shownMenu.value.y}
          items={menuFor(shownMenu.value.key)}
          open={shownMenu.open}
          onClose={closeMenu}
        />
      )}
    </div>
  );
}
