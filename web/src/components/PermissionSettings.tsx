// 「设置 → 权限」: how much bots may do without asking, which folders they may touch, and the
// remembered 「总是允许」 rules. The mode is a workspace default each member may override.

import { useEffect, useState } from "react";
import type { PermissionMode } from "../api/types";
import { PERMISSION_MODES, displayPath } from "../lib/permissions";
import { desktop } from "../lib/platform";
import { store, useTeam } from "../state/store";
import { ApprovalRulesSection } from "./ApprovalRules";
import { Icon } from "./Icon";

/** The desktop shell's picker; a browser (or an older shell) types the path instead. */
const canPick = Boolean(desktop?.pickFolder);

export function PermissionSettingsSection() {
  const settings = useTeam((s) => s.permissionSettings);
  const loadError = useTeam((s) => s.permissionSettingsError);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  // 完全权限 is the one choice that takes a second click, like every other point of no return.
  const [confirmingFull, setConfirmingFull] = useState(false);
  const [typed, setTyped] = useState("");

  useEffect(() => {
    if (!settings) void store.loadPermissionSettings();
  }, [settings]);

  const choose = async (mode: PermissionMode) => {
    if (!settings || mode === settings.mode) {
      setConfirmingFull(false);
      return;
    }
    if (mode === "full") {
      setError(null);
      setConfirmingFull(true);
      return;
    }
    setConfirmingFull(false);
    setBusy(true);
    setError(null);
    const failure = await store.patchPermissionSettings({ mode });
    setBusy(false);
    if (failure) setError(failure);
  };

  const confirmFull = async () => {
    setBusy(true);
    setError(null);
    const failure = await store.patchPermissionSettings({ mode: "full" });
    setBusy(false);
    if (failure) setError(failure);
    else setConfirmingFull(false);
  };

  /** A folder the server refused answers 422 with its Chinese reason; show it right here. */
  const addFolder = async (raw: string) => {
    const folder = raw.trim();
    if (!settings || !folder) return false;
    setError(null);
    const failure = await store.patchPermissionSettings({ folders: [...settings.folders, folder] });
    if (failure) {
      setError(failure);
      return false;
    }
    setTyped("");
    return true;
  };

  const browse = async () => {
    if (!canPick) return;
    setPicking(true);
    setError(null);
    try {
      const folder = await desktop?.pickFolder?.();
      if (folder) await addFolder(folder);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setPicking(false);
    }
  };

  const remove = async (folder: string) => {
    if (!settings) return;
    setBusy(true);
    setError(null);
    const failure = await store.patchPermissionSettings({
      folders: settings.folders.filter((item) => item !== folder),
    });
    setBusy(false);
    if (failure) setError(failure);
  };

  if (!settings) {
    return (
      <section className="settings-section">
        {loadError ? (
          <>
            <p className="hint">{loadError}</p>
            <button type="button" className="ghost-button" onClick={() => void store.loadPermissionSettings()}>
              重新读取
            </button>
          </>
        ) : (
          <p className="hint">正在读取权限设置…</p>
        )}
      </section>
    );
  }

  return (
    <>
      <section className="settings-section">
        <div className="settings-heading">
          <h4>审批方式</h4>
          <p>成员默认按这种方式工作，也可以在某位成员的「权限」里单独改；「总是允许」的规则在任何方式下都继续生效。</p>
        </div>
        <div className="permission-modes" role="radiogroup" aria-label="审批方式">
          {PERMISSION_MODES.map((option) => {
            const on = option.id === settings.mode;
            return (
              <button
                key={option.id}
                type="button"
                role="radio"
                aria-checked={on}
                className={on ? "permission-mode on" : "permission-mode"}
                disabled={busy}
                onClick={() => void choose(option.id)}
              >
                <span className="permission-mode-head">
                  <span className="permission-mode-title">{option.label}</span>
                  {on && <Icon name="check" size={14} />}
                </span>
                <span className="permission-mode-ahead">直接可以做：{option.ahead}</span>
                <span className="permission-mode-asks">{option.stillAsks}</span>
              </button>
            );
          })}
        </div>
        {confirmingFull && (
          <div className="confirm-step" role="group" aria-label="确认开启完全权限">
            <p className="confirm-prompt">
              完全权限之后，成员不用再问就能写任何位置的文件、运行代码、使用这台 Mac、写连接器、改定时任务。确定要打开吗？
            </p>
            <div className="confirm-actions">
              <button type="button" className="primary-button" disabled={busy} onClick={() => void confirmFull()}>
                {busy ? "开启中…" : "开启完全权限"}
              </button>
              <button
                type="button"
                className="ghost-button"
                disabled={busy}
                autoFocus
                onClick={() => setConfirmingFull(false)}
              >
                取消
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="settings-section">
        <div className="settings-heading">
          <h4>文件夹</h4>
          <p>
            成员的文件工具只能在这些文件夹里读写，相对路径都落在工作文件夹；Cubes
            自己的数据文件夹（里面存着模型密钥）不会被放开。
          </p>
        </div>
        <ul className="folder-list" aria-label="成员可以使用的文件夹">
          <li className="folder-row">
            <Icon name="folder" size={15} />
            <span className="folder-path" title={settings.working_folder}>
              {displayPath(settings.working_folder, settings.home)}
            </span>
            <span className="folder-tag">工作文件夹</span>
          </li>
          {settings.folders.map((folder) => (
            <li key={folder} className="folder-row">
              <Icon name="folder" size={15} />
              <span className="folder-path" title={folder}>
                {displayPath(folder, settings.home)}
              </span>
              <button
                type="button"
                className="ghost-button"
                disabled={busy}
                aria-label={`不再允许成员使用 ${displayPath(folder, settings.home)}`}
                onClick={() => void remove(folder)}
              >
                <Icon name="trash" size={14} />
                移除
              </button>
            </li>
          ))}
        </ul>
        {settings.folders.length === 0 && <p className="hint">还没有额外的文件夹：成员只能读写上面那个工作文件夹。</p>}
        {canPick ? (
          <button type="button" className="ghost-button" disabled={picking || busy} onClick={() => void browse()}>
            <Icon name="plus" size={14} />
            {picking ? "正在选择…" : "选择文件夹…"}
          </button>
        ) : (
          <form
            className="folder-add"
            onSubmit={(event) => {
              event.preventDefault();
              void addFolder(typed);
            }}
          >
            <input
              className="field"
              aria-label="要加入的文件夹路径"
              placeholder="/Users/你/Downloads 或 ~/Downloads"
              value={typed}
              spellCheck={false}
              onChange={(event) => setTyped(event.target.value)}
            />
            <button type="submit" className="primary-button" disabled={busy || !typed.trim()}>
              添加
            </button>
          </form>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </section>

      <ApprovalRulesSection />
    </>
  );
}
