// Commands from the native menu bar (web/electron/menu.cjs). The shell sends each click as one
// `menu` event carrying the item's id; this module names every id it may send and hands them to
// one handler. menu.test.ts reads the shell's menu, so an id renamed on either side fails a test
// instead of turning into a menu item that silently does nothing.

import { desktop } from "./platform";

export const MENU_COMMANDS = [
  "settings",
  "home",
  "new-member",
  "toggle-sidebar",
  "search",
  "zoom-reset",
  "zoom-in",
  "zoom-out",
  "focus-composer",
  "stop",
  "team",
  "connectors",
  "computer",
  "workbench",
  "memory",
  "schedules",
  "skills",
  "workflows",
] as const;

export type MenuCommand = (typeof MENU_COMMANDS)[number];

const known = new Set<string>(MENU_COMMANDS);

/** Calls `handler` for every menu command; returns the unsubscribe. A no-op in a browser. */
export function onMenuCommand(handler: (command: MenuCommand) => void): () => void {
  if (!desktop) return () => undefined;
  return desktop.on("menu", (id) => {
    if (typeof id === "string" && known.has(id)) handler(id as MenuCommand);
  });
}

/** Page zoom steps, like a browser's: ⌘= / ⌘- walk them, ⌘0 returns to 1. */
export const ZOOM_STEPS = [0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6] as const;

export function nextZoom(current: number, direction: 1 | -1): number {
  const index = ZOOM_STEPS.findIndex((step) => Math.abs(step - current) < 0.001);
  const at = index >= 0 ? index : ZOOM_STEPS.indexOf(1);
  const next = Math.min(ZOOM_STEPS.length - 1, Math.max(0, at + direction));
  return ZOOM_STEPS[next]!;
}
