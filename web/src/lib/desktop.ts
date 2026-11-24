// The desktop shell starts its own backend and hands this window the address and the launch
// token; a browser has neither. Every reader here answers null outside the Electron shell.

import { desktop, isMac } from "./platform";

export interface EmbeddedBackend {
  baseURL: string;
  token: string;
  /** True when the address is a server picked in settings, not a child the shell started. */
  remote: boolean;
}

export interface BackendFailure {
  error: string;
  logPath: string;
}

/** null means "no backend from the shell": the browser, where the configured address is used. */
export type BackendInfo = EmbeddedBackend | BackendFailure | null;

function parse(raw: string | null): BackendInfo {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<EmbeddedBackend & BackendFailure>;
    if (typeof value.baseURL === "string" && typeof value.token === "string") {
      return { baseURL: value.baseURL, token: value.token, remote: value.remote === true };
    }
    if (typeof value.error === "string") {
      return { error: value.error, logPath: typeof value.logPath === "string" ? value.logPath : "" };
    }
  } catch {
    // A shell that answers nonsense is no bundled backend at all.
  }
  return null;
}

/** The shell answers JSON text; a failed call is the same thing as having no bundled backend. */
export function backendInfo(): Promise<BackendInfo> {
  if (!desktop) return Promise.resolve(null);
  return desktop.backendInfo().then(parse, () => null);
}

export function restartBackend(): Promise<BackendInfo> {
  if (!desktop) return Promise.resolve(null);
  return desktop.restartBackend().then(parse, () => null);
}

/**
 * Points the shell at a backend on a server: it writes the address and token for the next start
 * and stops the embedded one. Rejects with a Chinese sentence when the file cannot be written.
 */
export function setRemote(baseURL: string, token: string): Promise<string> {
  if (!desktop) return Promise.reject(new Error("浏览器里没有可切换的后端。"));
  return desktop.setRemote(baseURL, token);
}

/** Forgets the server and starts the embedded backend again; null in a browser. */
export function clearRemote(): Promise<string | null> {
  if (!desktop) return Promise.resolve(null);
  return desktop.clearRemote();
}

/**
 * The native window follows the theme picked in 设置, not only the system's: the frosted sidebar
 * material, the traffic lights and the macOS-drawn menus would otherwise stay light under a dark
 * page (or the reverse). `null` hands the choice back to the system. A no-op in a browser.
 */
export function setWindowTheme(theme: "light" | "dark" | null): void {
  desktop?.setTheme(theme);
}

/**
 * Marks the root with `data-fullscreen` while the window is in macOS fullscreen. There the traffic
 * lights are gone, so the title row stops reserving room for them and the content moves up.
 */
export function watchFullscreen(): () => void {
  if (!desktop || !isMac) return () => undefined;
  const root = document.documentElement;
  const apply = (fullscreen: boolean) => {
    if (fullscreen) root.dataset.fullscreen = "";
    else delete root.dataset.fullscreen;
  };
  let disposed = false;
  void desktop.isFullscreen().then((fullscreen) => !disposed && apply(fullscreen), () => undefined);
  const stop = desktop.on("fullscreen", (fullscreen) => apply(fullscreen === true));
  return () => {
    disposed = true;
    stop();
    delete root.dataset.fullscreen;
  };
}
