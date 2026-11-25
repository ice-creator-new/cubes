// `window.cubesDesktop`: the few things the page asks of the shell (DesktopBridge in
// web/src/lib/platform.ts), carried over IPC to main.cjs. Its presence is what tells the page it
// runs in the desktop window.

const { contextBridge, ipcRenderer } = require("electron");

/** Electron wraps a rejected handler as "Error invoking remote method …: <message>". */
function call(name, ...args) {
  return ipcRenderer.invoke(`cubes:${name}`, ...args).catch((error) => {
    throw String(error?.message ?? error).replace(/^Error invoking remote method '[^']+': (Error: )?/, "");
  });
}

contextBridge.exposeInMainWorld("cubesDesktop", {
  backendInfo: () => call("backend-info"),
  restartBackend: () => call("restart-backend"),
  setRemote: (baseURL, token) => call("set-remote", String(baseURL), String(token)),
  clearRemote: () => call("clear-remote"),
  setTheme: (theme) => void call("set-theme", theme),
  setZoom: (factor) => void call("set-zoom", Number(factor)),
  setGlass: (glass) =>
    void call("set-glass", {
      kind: String(glass?.kind),
      transparency: Number(glass?.transparency),
      blur: Number(glass?.blur),
    }),
  glassSupport: () => call("glass-support"),
  appIconInfo: () => call("app-icon-info"),
  setAppIcon: (mode) => call("set-app-icon", String(mode)),
  pickAppIcon: () => call("pick-app-icon"),
  openExternal: (url) => void call("open-external", String(url)),
  isFullscreen: () => call("is-fullscreen"),
  pickFolder: () => call("pick-folder"),
  on: (event, handler) => {
    if (event !== "menu" && event !== "fullscreen") return () => undefined;
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on(`cubes:${event}`, listener);
    return () => ipcRenderer.removeListener(`cubes:${event}`, listener);
  },
});
