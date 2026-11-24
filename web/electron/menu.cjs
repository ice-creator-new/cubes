// The macOS menu bar. Every Cubes item sends its id to the page as one `menu` event; the page
// names the ids it handles in web/src/lib/menu.ts, and menu.test.ts checks the two lists agree.

const { Menu } = require("electron");

/** [id, label, accelerator] per group, in menu order. */
const COMMANDS = {
  app: [["settings", "设置…", "CmdOrCtrl+,"]],
  file: [
    ["home", "回到主管", "CmdOrCtrl+N"],
    ["new-member", "新建成员…", "CmdOrCtrl+Shift+N"],
  ],
  view: [
    ["toggle-sidebar", "显示/隐藏侧栏", "CmdOrCtrl+\\"],
    ["search", "搜索机器人", "CmdOrCtrl+K"],
  ],
  zoom: [
    ["zoom-reset", "实际大小", "CmdOrCtrl+0"],
    ["zoom-in", "放大", "CmdOrCtrl+="],
    ["zoom-out", "缩小", "CmdOrCtrl+-"],
  ],
  conversation: [
    ["focus-composer", "输入消息", "CmdOrCtrl+L"],
    ["stop", "停止回复", "CmdOrCtrl+."],
  ],
  tools: [
    ["team", "团队", "CmdOrCtrl+1"],
    ["connectors", "连接器", "CmdOrCtrl+2"],
    ["computer", "电脑", "CmdOrCtrl+3"],
    ["workbench", "工作台", "CmdOrCtrl+4"],
    ["memory", "记忆", "CmdOrCtrl+5"],
    ["schedules", "定时任务", "CmdOrCtrl+6"],
    ["skills", "技能", "CmdOrCtrl+7"],
    ["workflows", "工作流", "CmdOrCtrl+8"],
  ],
};

/**
 * @param {{ send: (id: string) => void, showMain: () => void, showLogs: () => void, dev: boolean }} shell
 */
function buildMenu(shell) {
  const name = "Cubes";
  const items = (group) =>
    COMMANDS[group].map(([id, label, accelerator]) => ({ label, accelerator, click: () => shell.send(id) }));
  const separator = { type: "separator" };
  return Menu.buildFromTemplate([
    {
      label: name,
      submenu: [
        { role: "about", label: `关于 ${name}` },
        separator,
        ...items("app"),
        separator,
        { role: "services", label: "服务" },
        separator,
        { role: "hide", label: `隐藏 ${name}` },
        { role: "hideOthers", label: "隐藏其他" },
        { role: "unhide", label: "全部显示" },
        separator,
        { role: "quit", label: `退出 ${name}` },
      ],
    },
    { label: "文件", submenu: [...items("file"), separator, { role: "close", label: "关闭窗口" }] },
    {
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        separator,
        { role: "cut", label: "剪切" },
        { role: "copy", label: "拷贝" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" },
      ],
    },
    {
      label: "显示",
      submenu: [
        ...items("view"),
        separator,
        ...items("zoom"),
        separator,
        { role: "togglefullscreen", label: "进入全屏幕" },
        ...(shell.dev ? [separator, { role: "toggleDevTools", label: "开发者工具" }] : []),
      ],
    },
    { label: "会话", submenu: items("conversation") },
    { label: "工具", submenu: items("tools") },
    {
      role: "window",
      label: "窗口",
      submenu: [
        { role: "minimize", label: "最小化" },
        { role: "zoom", label: "缩放" },
        separator,
        { label: name, click: shell.showMain },
        separator,
        { role: "front", label: "前置全部窗口" },
      ],
    },
    { role: "help", label: "帮助", submenu: [{ label: "打开后端日志", click: shell.showLogs }] },
  ]);
}

module.exports = { COMMANDS, buildMenu };
