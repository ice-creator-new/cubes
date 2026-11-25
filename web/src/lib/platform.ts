// The same bundle runs in a browser and inside the Electron window. The desktop shell needs room
// for the macOS traffic lights and lets the title row drag the window.

/** What web/electron/preload.cjs exposes. Absent in a browser. */
export interface DesktopBridge {
  /** Where the backend is (`{baseURL, token, remote}`), why it is not (`{error, logPath}`), or null. */
  backendInfo(): Promise<string | null>;
  restartBackend(): Promise<string | null>;
  /** Resolves with the new description; rejects with a Chinese sentence. */
  setRemote(baseURL: string, token: string): Promise<string>;
  clearRemote(): Promise<string | null>;
  setTheme(theme: "light" | "dark" | null): void;
  setZoom(factor: number): void;
  /** The window material behind the sidebar and tab band (macOS). */
  setGlass(glass: { kind: "liquid" | "frosted"; transparency: number; blur: number }): void;
  /** Which materials this Mac can draw: Liquid Glass needs macOS 26. */
  glassSupport(): Promise<{ liquid: boolean; frosted: boolean }>;
  /** The dock/taskbar picture: follow the system appearance, or hold one variant, or a picked image. */
  setAppIcon?(mode: AppIconMode): Promise<{ mode: AppIconMode } | null>;
  /** Opens a picture picker and keeps the chosen image as the app icon. */
  pickAppIcon?(): Promise<{ mode: AppIconMode } | null>;
  /** The icon setting in effect. */
  appIconInfo?(): Promise<{ mode: AppIconMode } | null>;
  openExternal(url: string): void;
  isFullscreen(): Promise<boolean>;
  /** Asks the shell for a folder; optional, so an older shell falls back to a text field. */
  pickFolder?(): Promise<string | null>;
  /** Subscribes to window or menu events; returns the unsubscribe. */
  on(event: "menu" | "fullscreen", handler: (payload: string | boolean) => void): () => void;
}

declare global {
  interface Window {
    cubesDesktop?: DesktopBridge;
  }
}

export const desktop: DesktopBridge | null = (typeof window !== "undefined" && window.cubesDesktop) || null;
export const isDesktop = desktop !== null;
export const isMac = typeof navigator !== "undefined" && /Mac/i.test(navigator.platform || navigator.userAgent);

/** The app icon in the dock/taskbar: light and dark variants can follow the appearance, or hold
 *  one of themselves, or be a picture the user picked. */
export type AppIconMode = "auto" | "light" | "dark" | "custom";

if (typeof document !== "undefined") {
  document.documentElement.dataset.shell = isDesktop ? (isMac ? "mac" : "desktop") : "web";
}
