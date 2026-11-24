// The Cubes desktop app: the web client in an Electron window, with its own backend started as a
// child on a random loopback port and a random launch token. Only the page learns the token (over
// IPC, see preload.cjs), so no other local process can use the API.
//
// - Packaged (`npm run build:desktop`): the page is served from Resources/web over app://cubes,
//   the frozen backend in Resources/cubes-backend runs against the app's own data directory
//   (~/Library/Application Support/com.ice.cubes.desktop): an empty team, nothing from .env.
// - Development (`npm run desktop`): the page comes from the Vite dev server on 1420 (started
//   here unless one already listens), and backend/desktop_main.py runs from source with the
//   backend's virtualenv, reading backend/.env, so the window shows the dev team.
//
// CUBES_GLASS=0 drops the sidebar material (vibrancy, frosted or Liquid Glass) and transparency,
// to measure that on its own.

const { app, BrowserWindow, Menu, dialog, ipcMain, nativeTheme, net, protocol, shell } = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const netSocket = require("node:net");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { buildMenu } = require("./menu.cjs");

const DEV = !app.isPackaged;
const GLASS = process.env.CUBES_GLASS !== "0";
const DEV_PORT = 1420;
const DEV_ORIGIN = `http://127.0.0.1:${DEV_PORT}`;
const APP_ORIGIN = "app://cubes";
const HEALTH_TIMEOUT_MS = 60_000;
/** Long enough for uvicorn to close its database, short enough to not hold the app open. */
const SHUTDOWN_GRACE_MS = 2_000;
/** A freshly picked port can still be taken by the time the child binds it: try this many. */
const PORT_ATTEMPTS = 3;

const WEB = path.resolve(__dirname, "..");
const BACKEND_SRC = path.resolve(WEB, "..", "backend");
// The app's bundle id since its first release, so an update keeps its data, logs and remote.json.
app.setPath("userData", path.join(app.getPath("appData"), "com.ice.cubes.desktop"));
const DATA_DIR = app.getPath("userData");
const LOG_PATH = DEV
  ? path.join(BACKEND_SRC, "data", "logs", "desktop-dev.log")
  : path.join(DATA_DIR, "logs", "backend.log");
const REMOTE_PATH = path.join(DATA_DIR, "remote.json");
// The `cubes` CLI and MCP server read this: the backend picks a new port and token each launch.
const CLI_PATH = path.join(DATA_DIR, "cli.json");
const ICON = path.join(__dirname, "build", "icon.png");
/** The page, the backend on loopback, and https for a remote server and images. */
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: http://127.0.0.1:* http://localhost:* https:",
  "connect-src 'self' http://127.0.0.1:* http://localhost:* https:",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "frame-ancestors 'none'",
].join("; ");

// Before ready: app:// is a real origin, so fetches to the backend carry `Origin: app://cubes`.
protocol.registerSchemesAsPrivileged([
  { scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } },
]);

/** @type {BrowserWindow | null} */
let window = null;
/** @type {import("node:child_process").ChildProcess | null} */
let backend = null;
/** @type {import("node:child_process").ChildProcess | null} */
let vite = null;
/** What the page is told: `{baseURL, token, remote}`, `{error, logPath}`, or null. */
let session = null;
/** Settles once the first backend is up (or has failed); every call waits for it. */
let started = Promise.resolve();
let quitting = false;

// ------------------------------------------------------------------------------- backend

function freePort() {
  return new Promise((resolve, reject) => {
    const server = netSocket.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function listening(port) {
  return new Promise((resolve) => {
    const socket = netSocket.connect(port, "127.0.0.1");
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
  });
}

async function healthy(port, token) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/health`, { headers: { Authorization: `Bearer ${token}` } });
    return response.status === 200;
  } catch {
    return false;
  }
}

/** SIGTERM, then SIGKILL when the child overstays the grace period. */
function terminate(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(() => child.kill("SIGKILL"), SHUTDOWN_GRACE_MS);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    child.kill("SIGTERM");
  });
}

/** The program and the arguments that say where the data lives, or a failure for the page. */
function launcher() {
  if (!DEV) {
    const program = path.join(process.resourcesPath, "cubes-backend", process.platform === "win32" ? "cubes-backend.exe" : "cubes-backend");
    if (!fs.existsSync(program)) return { error: "安装包里没有后端，请重新安装 Cubes。" };
    return { program, leading: [], trailing: ["--data-dir", DATA_DIR], cwd: undefined };
  }
  const program = path.join(BACKEND_SRC, ".venv", "bin", "python");
  if (!fs.existsSync(program)) {
    return { error: `开发模式需要后端的虚拟环境：在 ${BACKEND_SRC} 里执行 python3 -m venv .venv 并安装 requirements.txt。` };
  }
  return {
    program,
    leading: [path.join(BACKEND_SRC, "desktop_main.py")],
    trailing: ["--env-file", path.join(BACKEND_SRC, ".env"), "--allow-origin", DEV_ORIGIN],
    cwd: BACKEND_SRC,
  };
}

async function launchOnce(plan) {
  const port = await freePort();
  const token = crypto.randomBytes(32).toString("hex");
  const log = fs.openSync(LOG_PATH, "w");
  const child = spawn(
    plan.program,
    [...plan.leading, "--port", String(port), "--token", token, ...plan.trailing, "--parent-pid", String(process.pid)],
    { cwd: plan.cwd, stdio: ["ignore", log, log] },
  );
  fs.closeSync(log);
  backend = child;
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) return { exited: child.exitCode };
    if (await healthy(port, token)) return { ready: { baseURL: `http://127.0.0.1:${port}`, token, remote: false } };
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  await terminate(child);
  return { timeout: true };
}

async function startBackend() {
  await terminate(backend);
  backend = null;
  fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true, mode: 0o700 });
  const plan = launcher();
  if (plan.error) return { error: plan.error, logPath: LOG_PATH };
  let last = "后端没有启动。";
  for (let attempt = 0; attempt < PORT_ATTEMPTS; attempt++) {
    const outcome = await launchOnce(plan);
    if (outcome.ready) return outcome.ready;
    if (outcome.timeout) return { error: `后端在 ${HEALTH_TIMEOUT_MS / 1000} 秒内没有就绪。`, logPath: LOG_PATH };
    // A taken port exits early; anything else exits the same way on every port, so say why.
    last = `后端启动后退出了（退出码 ${outcome.exited}）。`;
  }
  return { error: last, logPath: LOG_PATH };
}

// --------------------------------------------------------------------------- remote file

/** https off this machine, http only for loopback. */
function checkRemoteURL(raw) {
  const url = String(raw ?? "").trim().replace(/\/+$/, "");
  if (!url || /\s/.test(url)) throw new Error("远端地址不能为空，也不能有空格。");
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error("远端地址要以 https:// 开头。");
  }
  if (parsed.protocol === "https:" && parsed.hostname) return url;
  if (parsed.protocol === "http:" && ["127.0.0.1", "localhost"].includes(parsed.hostname)) return url;
  if (parsed.protocol === "http:") throw new Error("只有本机地址可以用 http://，远端服务器要用 https://。");
  throw new Error("远端地址要以 https:// 开头。");
}

/** The saved server, or null; a broken file must not leave the window with nothing. */
function readRemote() {
  try {
    const value = JSON.parse(fs.readFileSync(REMOTE_PATH, "utf8"));
    if (typeof value.token !== "string" || !value.token) return null;
    return { baseURL: checkRemoteURL(value.baseURL), token: value.token, remote: true };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------- cli file

/** Write the session the `cubes` CLI should use; never let this break the window. */
function writeCliConfig(value) {
  if (!value || typeof value.baseURL !== "string" || typeof value.token !== "string") return;
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });
    fs.writeFileSync(CLI_PATH, `${JSON.stringify({ url: value.baseURL, token: value.token })}\n`, {
      mode: 0o600,
    });
    fs.chmodSync(CLI_PATH, 0o600);
  } catch {
    // A convenience file for scripts: losing it leaves the app working as before.
  }
}

/** Quitting removes the token file; a stale one would only fail to connect anyway. */
function removeCliConfig() {
  try {
    fs.rmSync(CLI_PATH, { force: true });
  } catch {
    // Nothing to remove, or a read-only data directory.
  }
}

// --------------------------------------------------------------------------------- IPC

const describe = () => (session ? JSON.stringify(session) : null);

function handle(name, fn) {
  ipcMain.handle(`cubes:${name}`, async (event, ...args) => {
    // Only the app's own page may ask: a link that somehow navigated elsewhere gets nothing.
    if (!window || event.sender !== window.webContents) throw new Error("不是 Cubes 窗口。");
    try {
      return (await fn(...args)) ?? null;
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : String(error));
    }
  });
}

handle("backend-info", async () => {
  await started;
  return describe();
});
handle("restart-backend", async () => {
  await started;
  session = await startBackend();
  writeCliConfig(session);
  return describe();
});
handle("set-remote", async (baseURL, token) => {
  const url = checkRemoteURL(baseURL);
  const secret = String(token ?? "").trim();
  if (!secret) throw new Error("访问令牌不能为空。");
  fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });
  fs.writeFileSync(REMOTE_PATH, `${JSON.stringify({ baseURL: url, token: secret })}\n`, { mode: 0o600 });
  fs.chmodSync(REMOTE_PATH, 0o600);
  await started;
  await terminate(backend);
  backend = null;
  session = { baseURL: url, token: secret, remote: true };
  writeCliConfig(session);
  return describe();
});
handle("clear-remote", async () => {
  fs.rmSync(REMOTE_PATH, { force: true });
  await started;
  session = await startBackend();
  writeCliConfig(session);
  return describe();
});
handle("set-theme", (theme) => {
  nativeTheme.themeSource = theme === "light" || theme === "dark" ? theme : "system";
});
handle("set-zoom", (factor) => window?.webContents.setZoomFactor(Math.min(3, Math.max(0.5, Number(factor) || 1))));
handle("open-external", (url) => {
  if (/^https?:\/\//i.test(String(url))) return shell.openExternal(String(url));
});
handle("is-fullscreen", () => window?.isFullScreen() ?? false);
// 「设置 → 权限 → 文件夹」: the shell owns the dialog; the path goes back to the page, which
// still lets the backend decide whether the folder is acceptable.
handle("pick-folder", async () => {
  if (!window) return null;
  const result = await dialog.showOpenDialog(window, {
    properties: ["openDirectory", "createDirectory"],
    message: "选择成员可以使用的文件夹",
    buttonLabel: "使用这个文件夹",
  });
  return result.canceled ? null : (result.filePaths[0] ?? null);
});
handle("set-glass", async (value) => {
  glass = cleanGlass(value);
  try {
    fs.writeFileSync(GLASS_PATH, `${JSON.stringify(glass)}\n`);
  } catch {
    // A read-only data directory: the choice still holds for this launch.
  }
  await applyGlass();
});
handle("glass-support", async () => {
  const native = await glassModule();
  return { liquid: Boolean(native) && liquidSupported(), frosted: true };
});

// ------------------------------------------------------------------------- glass

// The material behind the sidebar and the tab band. 「液态玻璃」 is the system's NSGlassEffectView
// and follows the system appearance on its own; 「毛玻璃」 is a Core Animation backdrop whose blur
// the user sets (the see-through amount is the page's own tint over it). Both come from
// electron-window-glass. When it cannot load, the window keeps the plain sidebar vibrancy.
const GLASS_PATH = path.join(DATA_DIR, "glass.json");
const MAX_BLUR = 80;
let glass = readGlass();
let effect = null;
let glassLoad = null;

function cleanGlass(value) {
  const kind = value?.kind === "liquid" ? "liquid" : "frosted";
  const blur = Number(value?.blur);
  return { kind, blur: Number.isFinite(blur) ? Math.min(MAX_BLUR, Math.max(0, Math.round(blur))) : 30 };
}

function readGlass() {
  try {
    return cleanGlass(JSON.parse(fs.readFileSync(GLASS_PATH, "utf8")));
  } catch {
    return cleanGlass(null);
  }
}

/** Liquid Glass is public API from macOS 26 (Darwin 25) on. */
function liquidSupported() {
  return process.platform === "darwin" && Number(process.getSystemVersion().split(".")[0]) >= 26;
}

/** The ESM package, loaded once; null off macOS or when its native part will not load. */
function glassModule() {
  if (process.platform !== "darwin" || !GLASS) return Promise.resolve(null);
  glassLoad ??= import("electron-window-glass").catch((error) => {
    console.error(`[glass] ${error?.message ?? error}`);
    return null;
  });
  return glassLoad;
}

async function applyGlass() {
  if (!window || !GLASS) return;
  const native = await glassModule();
  if (!window) return;
  // Same frosted view, new blur: change it in place, so a slider drag never flickers.
  if (effect && !effect.isDisposed && effect.kind === "backdrop" && glass.kind === "frosted") {
    effect.setBlurRadius(glass.blur);
    return;
  }
  effect?.dispose();
  effect = null;
  if (!native) {
    window.setVibrancy("sidebar");
    return;
  }
  try {
    // Vibrancy would sit on top of the effect and blur it a second time.
    window.setVibrancy(null);
    effect =
      glass.kind === "liquid" && liquidSupported()
        ? native.createWindowGlass(window, { kind: "glass", style: "regular" })
        : native.createWindowGlass(window, {
            kind: "backdrop",
            blurRadius: glass.blur,
            saturation: 1.8,
            gradientMaskEndX: 0,
          });
  } catch (error) {
    console.error(`[glass] ${error?.message ?? error}`);
    effect = null;
    window.setVibrancy("sidebar");
  }
}

// -------------------------------------------------------------------------------- window

function showMain() {
  if (!window) return;
  window.show();
  window.focus();
}

function send(channel, payload) {
  window?.webContents.send(`cubes:${channel}`, payload);
}

/** The page's own address: links elsewhere go to the system browser, never into this window. */
const OWN_ORIGIN = DEV ? DEV_ORIGIN : APP_ORIGIN;

function createWindow() {
  window = new BrowserWindow({
    width: 1180,
    height: 800,
    minWidth: 720,
    minHeight: 520,
    title: "Cubes",
    icon: ICON,
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 24 },
    show: false,
    ...(GLASS
      ? { vibrancy: "sidebar", visualEffectState: "followWindow", backgroundColor: "#00000000" }
      : { backgroundColor: nativeTheme.shouldUseDarkColors ? "#17181c" : "#f3f2f0" }),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  // The saved material goes on before the first frame, so a launch never flashes the default.
  window.once("ready-to-show", () => {
    void applyGlass().finally(() => window?.show());
  });
  window.on("closed", () => {
    effect = null;
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (new URL(url).origin === OWN_ORIGIN) return;
    event.preventDefault();
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
  });
  window.on("enter-full-screen", () => send("fullscreen", true));
  window.on("leave-full-screen", () => send("fullscreen", false));
  // macOS: closing the window only hides it, so the backend and scheduled tasks keep running;
  // the Dock icon or 窗口 → Cubes brings it back, ⌘Q quits.
  window.on("close", (event) => {
    if (process.platform === "darwin" && !quitting) {
      event.preventDefault();
      window?.hide();
    }
  });
  void window.loadURL(`${OWN_ORIGIN}/`);
}

/** Resources/web over app://cubes, with the page's CSP. Unknown paths fall back to index.html. */
function serveBundle() {
  const root = path.join(process.resourcesPath, "web");
  protocol.handle("app", async (request) => {
    const { host, pathname } = new URL(request.url);
    if (host !== "cubes") return new Response("not found", { status: 404 });
    let file = path.normalize(path.join(root, decodeURIComponent(pathname)));
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      file = path.join(root, "index.html");
    }
    const response = await net.fetch(pathToFileURL(file).toString());
    const headers = new Headers(response.headers);
    if (file.endsWith(".html")) headers.set("Content-Security-Policy", CSP);
    return new Response(response.body, { status: response.status, headers });
  });
}

async function ensureVite() {
  if (await listening(DEV_PORT)) return;
  vite = spawn(path.join(WEB, "node_modules", ".bin", "vite"), [], { cwd: WEB, stdio: "inherit" });
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline && !(await listening(DEV_PORT))) await new Promise((resolve) => setTimeout(resolve, 100));
}

app.setName("Cubes");
app.setAboutPanelOptions({ applicationName: "Cubes", applicationVersion: app.getVersion(), iconPath: ICON });

app.whenReady().then(async () => {
  // In development this process is Electron.app: without this the Dock shows Electron's atom.
  if (DEV && process.platform === "darwin") app.dock?.setIcon(ICON);
  Menu.setApplicationMenu(
    buildMenu({
      dev: DEV,
      send: (id) => {
        // A command for a hidden window would land nowhere visible: show it first.
        showMain();
        send("menu", id);
      },
      showMain,
      showLogs: () => void shell.openPath(path.dirname(LOG_PATH)),
    }),
  );
  // A crash last time may have left a token this launch cannot honour; this launch rewrites it.
  removeCliConfig();
  // A server wins: the user picked it in 「设置 →后端」, and starting the embedded backend anyway
  // would only cost a port, a process and a second copy of the data.
  started = (async () => {
    session = readRemote() ?? (await startBackend());
    writeCliConfig(session);
  })();
  if (DEV) await ensureVite();
  else serveBundle();
  createWindow();
});

app.on("activate", showMain);

app.on("before-quit", () => {
  quitting = true;
  // The token in this file is only good while this window is up.
  removeCliConfig();
});

// Quitting must take the backend with it: nothing else owns that process.
app.on("will-quit", (event) => {
  if (!backend && !vite) return;
  event.preventDefault();
  const children = [backend, vite];
  backend = null;
  vite = null;
  void Promise.all(children.map(terminate)).then(() => app.quit());
});
