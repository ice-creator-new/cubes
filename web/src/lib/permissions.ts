// 「权限」: the three approval modes and the one bit of path formatting the settings page needs.
// The mode names and what each lets through mirror backend/app/permissions.py.

import type { PermissionMode } from "../api/types";

/** The modes from the most careful to the least, in the order the settings cards show them. */
export const PERMISSION_MODES: readonly {
  id: PermissionMode;
  label: string;
  /** What goes ahead without a card. */
  ahead: string;
  /** What still asks, or nothing at all. */
  stillAsks: string;
}[] = [
  {
    id: "ask",
    label: "手动审批",
    ahead: "读取允许文件夹里的文件。",
    stillAsks: "写文件、运行代码、使用电脑、连接器写入、修改定时任务，以及碰文件夹以外的地方，都会先给你一张确认卡。",
  },
  {
    id: "auto",
    label: "自动审批",
    ahead: "在允许的文件夹里读写文件、运行代码，以及使用云端沙盒电脑。",
    stillAsks: "这台 Mac、文件夹以外的位置、连接器写入和定时任务仍会先问你。",
  },
  {
    id: "full",
    label: "完全权限",
    ahead: "除了删除成员和解散群组，其余操作都不再询问。",
    stillAsks: "「总是允许」以外的规则也失去了意义；Cubes 自己的数据文件夹依旧不可读写。",
  },
];

export const MODE_LABELS: Record<PermissionMode, string> = {
  ask: "手动审批",
  auto: "自动审批",
  full: "完全权限",
};

/** 「跟随团队（现在是 手动审批）」 for the member editor's follow option. */
export function followModeLabel(mode: PermissionMode | null | undefined): string {
  return `跟随团队（现在是 ${MODE_LABELS[mode ?? "ask"]}）`;
}

/**
 * A path as the user reads it: under the home folder it starts with `~`, exactly like the
 * backend's `permissions.display`. A sibling whose name only shares the prefix (`/Users/anna2`
 * next to home `/Users/anna`) is left alone, and so is every path when home is unknown.
 */
export function displayPath(path: string, home: string): string {
  const folder = path.trim();
  const root = home.trim().replace(/\/+$/, "");
  if (!root) return folder;
  if (folder === root) return "~";
  if (!folder.startsWith(`${root}/`)) return folder;
  const rest = folder.slice(root.length + 1).replace(/\/+$/, "");
  return rest ? `~/${rest}` : "~";
}
