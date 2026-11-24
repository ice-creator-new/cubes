// Web links in replies and tool details. A browser follows target="_blank" on its own; in the
// desktop window the link goes to the system browser through the shell instead, never into a
// second app window. Only http(s) is ever handed over.

import type { MouseEvent } from "react";
import { desktop } from "./platform";

export function isWebURL(href: string | undefined): href is string {
  return typeof href === "string" && /^https?:\/\//i.test(href);
}

/** onClick for an external <a>: hands the URL to the system browser inside the desktop shell. */
export function openExternally(event: MouseEvent<HTMLAnchorElement>): void {
  const href = event.currentTarget.href;
  if (!desktop || !isWebURL(href)) return;
  event.preventDefault();
  desktop.openExternal(href);
}

/** Open a web URL from code (a menu item): the system browser on desktop, a new tab otherwise. */
export function openWebURL(href: string): void {
  if (!isWebURL(href)) return;
  if (desktop) desktop.openExternal(href);
  else window.open(href, "_blank", "noopener,noreferrer");
}
