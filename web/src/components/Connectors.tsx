// 「连接器」panel, opened from the rail: the account-wide GitHub / 飞书 / local MCP servers every
// bot can use. Cards carry the service's mark, its name and a one-line status; the switch and the
// disclosure sit on that same head row, and the quieter row of actions closes the card. Adding or
// editing opens its own dialog on top of the panel. The server connects and lists tools before it
// answers, so the form stays open with a busy state until the request comes back, and shows its
// Chinese failure verbatim.

import { useLayoutEffect, useRef, useState, type ChangeEvent } from "react";
import type { Connector, ConnectorInput, ConnectorPreset, ConnectorStatus } from "../api/types";
import { parseArgs, parseEnv, parseHeaders } from "../lib/connectorForm";
import { openExternally } from "../lib/links";
import { store, useTeam } from "../state/store";
import { ConnectorMark } from "./ConnectorMark";
import { Icon } from "./Icon";
import { Modal } from "./Dialog";
import { Dialog } from "./Panels";

type WorkKind = "delete" | "toggle" | "reconnect";
type Tone = "ok" | "error" | "busy" | "idle";

function statusText(status: ConnectorStatus): { text: string; error: boolean } {
  switch (status) {
    case "connected":
      return { text: "已连接", error: false };
    case "connecting":
      return { text: "正在连接…", error: false };
    case "error":
      return { text: "连接失败。", error: true };
    case "disabled":
      return { text: "已停用", error: false };
    default:
      return { text: "还没连接", error: false };
  }
}

/** The line under the name: the enable work in progress wins over the last known status. */
function statusLine(connector: Connector, working: WorkKind | null): { text: string; error: boolean } {
  if (working === "reconnect" || (working === "toggle" && !connector.enabled)) {
    return { text: "正在连接…", error: false };
  }
  if (working === "toggle") return { text: "正在停用…", error: false };
  const known = statusText(connector.status);
  if (connector.status === "connected") return { text: `已连接，${connector.tools.length} 个工具`, error: false };
  if (connector.status === "error") return { text: connector.error ?? known.text, error: true };
  return known;
}

/** The dot before the status line: green once connected, red on failure, a muted pulse while busy. */
function statusTone(connector: Connector, working: WorkKind | null): Tone {
  if (working !== null || connector.status === "connecting") return "busy";
  if (connector.status === "error") return "error";
  if (connector.status === "connected") return "ok";
  return "idle";
}

/** A small switch. A button, so the whole track is the hit area and the state is announced. */
export function Switch({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      className="switch"
      disabled={disabled}
      onClick={onChange}
    >
      <span className="switch-thumb" aria-hidden="true" />
    </button>
  );
}

/** A textarea that starts at two rows and grows with its content up to a cap, then scrolls. */
function GrowingTextarea({
  id,
  value,
  placeholder,
  onChange,
}: {
  id: string;
  value: string;
  placeholder?: string;
  onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
}) {
  const field = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = field.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 176)}px`;
  }, [value]);
  return (
    <textarea
      ref={field}
      id={id}
      className="field connector-textarea"
      rows={2}
      spellCheck={false}
      value={value}
      placeholder={placeholder}
      onChange={onChange}
    />
  );
}

/** Which dialog is open above the panel: the preset grid, or one preset's form. */
type Editor = { step: "presets" } | { step: "form"; preset: ConnectorPreset; connector: Connector | null };

export function ConnectorsPanel({ onClose }: { onClose: () => void }) {
  const connectors = useTeam((s) => s.connectors);
  const connectorsError = useTeam((s) => s.connectorsError);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [confirmingID, setConfirmingID] = useState<string | null>(null);
  const [working, setWorking] = useState<{ id: string; kind: WorkKind } | null>(null);
  const [openID, setOpenID] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const presets = connectors?.presets ?? [];
  const list = connectors?.connectors ?? [];

  return (
    <>
      {/* Adding or editing a connector is a dialog over the panel, even while the panel is a tab. */}
      {editor && (
        <Modal>
          <ConnectorDialog
            editor={editor}
            presets={presets}
            onPreset={(preset) => setEditor({ step: "form", preset, connector: null })}
            onClose={() => setEditor(null)}
          />
        </Modal>
      )}
      <Dialog title="连接器" onClose={onClose} wide>
        <p className="hint">
          所有 bot都能使用这里连接的服务；会改动数据的操作，在每次执行前都会先问你。
        </p>

        {connectorsError && !connectors && (
          <p className="form-error" role="alert">
            {connectorsError}
          </p>
        )}

        {!connectors && !connectorsError && <p className="hint">正在读取连接器…</p>}

        {connectors && list.length === 0 && (
          <p className="hint">还没有连接器。添加一个后，所有 bot都能用它。</p>
        )}

        {list.length > 0 && (
          <ul className="provider-list connector-list">
            {list.map((connector) => {
              const work = working?.id === connector.id ? working.kind : null;
              const line = statusLine(connector, work);
              const tone = statusTone(connector, work);
              const busy = working?.id === connector.id;
              const expanded = openID === connector.id;
              const expandable = connector.tools.length > 0;
              const toolsID = `connector-tools-${connector.id}`;
              const toggle = () => setOpenID(expanded ? null : connector.id);
              // A custom connector points at its preset by transport; a deleted preset has none.
              const preset =
                presets.find((entry) => entry.id === connector.preset) ??
                presets.find((entry) => entry.custom && entry.transport === connector.transport) ??
                null;
              return (
                <li key={connector.id} className="provider-card connector-card">
                  <div className="connector-head">
                    <button
                      type="button"
                      className="connector-head-main"
                      disabled={!expandable}
                      aria-expanded={expandable ? expanded : undefined}
                      aria-controls={expandable ? toolsID : undefined}
                      onClick={toggle}
                    >
                      <span className="connector-mark">
                        <ConnectorMark preset={connector.preset} size={18} />
                      </span>
                      <span className="connector-head-text">
                        <span className="connector-name">{connector.name}</span>
                        <span className={line.error ? "connector-status error" : "connector-status"}>
                          <span className={tone === "idle" ? "connector-dot" : `connector-dot ${tone}`} aria-hidden="true" />
                          {line.text}
                        </span>
                      </span>
                    </button>
                    <div className="connector-tail">
                      <Switch
                        checked={connector.enabled}
                        disabled={busy}
                        label={connector.enabled ? "停用这个连接器" : "启用这个连接器"}
                        onChange={() => {
                          setWorking({ id: connector.id, kind: "toggle" });
                          setError(null);
                          void store.setConnectorEnabled(connector.id, !connector.enabled).then((failure) => {
                            setWorking(null);
                            if (failure) setError(failure);
                          });
                        }}
                      />
                      {expandable && (
                        <button
                          type="button"
                          className={expanded ? "connector-disclose open" : "connector-disclose"}
                          aria-expanded={expanded}
                          aria-controls={toolsID}
                          aria-label={expanded ? "收起工具列表" : "展开工具列表"}
                          title={expanded ? "收起工具列表" : "展开工具列表"}
                          onClick={toggle}
                        >
                          <Icon name="chevron" size={12} />
                        </button>
                      )}
                    </div>
                  </div>

                  {expanded && expandable && (
                    <ul className="connector-tools" id={toolsID}>
                      {connector.tools.map((tool) => (
                        <li key={tool.name} className="connector-tool">
                          <span className="connector-tool-text">
                            <span className="connector-tool-name">{tool.title || tool.name}</span>
                            {tool.description && <span className="connector-tool-desc">{tool.description}</span>}
                          </span>
                          <span className={tool.read_only ? "connector-tool-tag" : "connector-tool-tag write"}>
                            {tool.read_only ? "只读" : "需确认"}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}

                  {confirmingID === connector.id ? (
                    <div className="confirm-step" role="group" aria-label={`确认删除${connector.name}`}>
                      <p className="confirm-prompt">
                        删除「{connector.name}」后，所有 bot都不能再用它。已经保存的密钥也会一起删掉。
                      </p>
                      <div className="confirm-actions">
                        <button
                          type="button"
                          className="danger-button"
                          disabled={busy}
                          onClick={async () => {
                            setWorking({ id: connector.id, kind: "delete" });
                            setError(null);
                            const failure = await store.deleteConnector(connector.id);
                            setWorking(null);
                            setConfirmingID(null);
                            if (failure) setError(failure);
                          }}
                        >
                          <Icon name="trash" size={15} />
                          {busy ? "删除中…" : "删除"}
                        </button>
                        <button
                          type="button"
                          className="ghost-button"
                          disabled={busy}
                          onClick={() => setConfirmingID(null)}
                        >
                          取消
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="connector-actions">
                      {preset && (
                        <button
                          type="button"
                          className="connector-action"
                          disabled={busy}
                          onClick={() => {
                            setEditor({ step: "form", preset, connector });
                            setError(null);
                          }}
                        >
                          <Icon name="rename" size={14} />
                          编辑
                        </button>
                      )}
                      {connector.status === "error" && (
                        <button
                          type="button"
                          className="connector-action"
                          disabled={busy}
                          onClick={() => {
                            setWorking({ id: connector.id, kind: "reconnect" });
                            setError(null);
                            void store.reconnectConnector(connector.id).then((failure) => {
                              setWorking(null);
                              if (failure) setError(failure);
                            });
                          }}
                        >
                          <Icon name="clock" size={14} />
                          {work === "reconnect" ? "正在连接…" : "重新连接"}
                        </button>
                      )}
                      <button
                        type="button"
                        className="connector-action danger"
                        disabled={busy}
                        onClick={() => setConfirmingID(connector.id)}
                      >
                        <Icon name="trash" size={14} />
                        删除
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        <button
          type="button"
          className="connector-add"
          onClick={() => {
            setEditor({ step: "presets" });
            setError(null);
          }}
        >
          <Icon name="plus" size={15} />
          添加连接器
        </button>

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </Dialog>
    </>
  );
}

/** Its own sheet over the panel: the preset grid, then the chosen preset's form. */
function ConnectorDialog({
  editor,
  presets,
  onPreset,
  onClose,
}: {
  editor: Editor;
  presets: ConnectorPreset[];
  onPreset: (preset: ConnectorPreset) => void;
  onClose: () => void;
}) {
  if (editor.step === "presets") {
    const ordered = [...presets.filter((preset) => !preset.custom), ...presets.filter((preset) => preset.custom)];
    return (
      <Dialog title="添加连接器" onClose={onClose} className="connector-preset-dialog">
        <p className="hint">先选一个服务，连好之后所有 bot都能用它。</p>
        <div className="provider-presets connector-presets">
          {ordered.map((preset) => (
            <button key={preset.id} type="button" className="provider-preset connector-preset" onClick={() => onPreset(preset)}>
              <span className="connector-preset-head">
                <ConnectorMark preset={preset.id} size={16} />
                <span className="provider-preset-name">{preset.name}</span>
              </span>
              <span className="connector-preset-desc">{preset.description}</span>
              {preset.requires && <span className="connector-requires">需要 {preset.requires}</span>}
            </button>
          ))}
        </div>
      </Dialog>
    );
  }
  const { preset, connector } = editor;
  return (
    <ConnectorForm
      key={connector?.id ?? `new:${preset.id}`}
      preset={preset}
      connector={connector}
      onClose={onClose}
    />
  );
}

function ConnectorForm({
  preset,
  connector,
  onClose,
}: {
  preset: ConnectorPreset;
  connector: Connector | null;
  onClose: () => void;
}) {
  const editing = connector !== null;
  const [name, setName] = useState(connector?.name ?? preset.name);
  // Every credential field: secrets start empty (the server never returns them), other fields
  // start with the value the server stored, so an edit shows what is in use.
  const [fields, setFields] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const field of preset.fields) {
      const value = field.secret ? "" : connector?.values[field.id] ?? "";
      if (value) initial[field.id] = value;
    }
    return initial;
  });
  const [allowWrites, setAllowWrites] = useState(connector?.allow_writes ?? false);
  const [url, setUrl] = useState(connector?.url ?? "");
  const [headersText, setHeadersText] = useState("");
  const [command, setCommand] = useState(connector?.command ?? "");
  const [argsText, setArgsText] = useState(connector?.args.join("\n") ?? "");
  const [envText, setEnvText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const remote = preset.custom && preset.transport === "http";
  const local = preset.custom && preset.transport === "stdio";

  const save = async () => {
    setError(null);
    const trimmedName = name.trim();
    if (!trimmedName) return setError("请给这个连接器起个名字。");

    const body: ConnectorInput = { name: trimmedName };
    if (!editing) body.preset = preset.id;
    if (preset.writes_toggle && (!editing || allowWrites !== connector.allow_writes)) {
      body.allow_writes = allowWrites;
    }

    const typed: Record<string, string> = {};
    for (const field of preset.fields) {
      const value = (fields[field.id] ?? "").trim();
      const stored = editing && (field.secret ? field.id in connector.secret_hints : field.id in connector.values);
      if (!value) {
        if (!field.secret && stored && connector.values[field.id] !== "") {
          // A stored value that is not a secret is shown, so an empty box means it was erased.
          if (field.required) return setError(`请填写${field.label}。`);
          typed[field.id] = "";
        } else if (field.required && !stored) {
          return setError(`请填写${field.label}。`);
        }
        continue;
      }
      // An unchanged value needs no write; secrets always count as new when typed.
      if (field.secret || !stored || value !== connector.values[field.id]) typed[field.id] = value;
    }
    if (Object.keys(typed).length > 0) body.secrets = typed;

    if (remote) {
      const address = url.trim();
      if (!address) return setError("请填写 MCP服务的地址。");
      if (!editing || address !== connector.url) body.url = address;
      if (headersText.trim()) {
        const parsed = parseHeaders(headersText);
        if (parsed.error) return setError(parsed.error);
        body.headers = parsed.values;
      }
    } else if (local) {
      const program = command.trim();
      if (!program) return setError("请填写启动命令。");
      if (!editing || program !== connector.command) body.command = program;
      const args = parseArgs(argsText);
      if (!editing || args.join("\n") !== connector.args.join("\n")) body.args = args;
      if (envText.trim()) {
        const parsed = parseEnv(envText);
        if (parsed.error) return setError(parsed.error);
        body.env = parsed.values;
      }
    }

    setBusy(true);
    const failure = await store.saveConnector(body, connector?.id);
    setBusy(false);
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <Dialog
      title={connector ? `编辑「${connector.name}」` : `连接「${preset.name}」`}
      mark={<ConnectorMark preset={preset.id} size={18} />}
      onClose={onClose}
      className="connector-form-dialog"
      footer={
        <>
          <button type="button" className="ghost-button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={busy} onClick={() => void save()}>
            {busy ? "正在连接…" : "连接"}
          </button>
        </>
      }
    >
      <div className="provider-form connector-form">
        {preset.requires && <p className="hint">本机需要先装好 {preset.requires}。</p>}

        <div className="connector-field">
          <label className="field-label" htmlFor="connector-name">
            名称
          </label>
          <input
            id="connector-name"
            className="field"
            value={name}
            placeholder={preset.name}
            onChange={(event) => setName(event.target.value)}
          />
        </div>

        {preset.fields.map((field) => {
          const stored = editing && field.secret && field.id in connector.secret_hints;
          return (
            <div key={field.id} className="connector-field">
              <label className="field-label" htmlFor={`connector-field-${field.id}`}>
                {field.label}
              </label>
              <input
                id={`connector-field-${field.id}`}
                className="field"
                type={field.secret ? "password" : "text"}
                autoComplete="off"
                spellCheck={false}
                value={fields[field.id] ?? ""}
                placeholder={stored ? "已设置，留空保持不变" : field.placeholder}
                onChange={(event) => setFields({ ...fields, [field.id]: event.target.value })}
              />
            </div>
          );
        })}

        {(preset.token_hint || (preset.token_url && preset.token_label)) && (
          <div className="connector-help">
            {preset.token_hint && <p className="hint">{preset.token_hint}</p>}
            {preset.token_url && preset.token_label && (
              <a
                className="link-button connector-help-link"
                href={preset.token_url}
                target="_blank"
                rel="noreferrer"
                onClick={openExternally}
              >
                {preset.token_label}
              </a>
            )}
          </div>
        )}

        {preset.writes_toggle && (
          <div className="connector-option">
            <span className="connector-option-text">
              <span className="connector-option-label">允许写操作</span>
              <span className="connector-option-note">写操作在每次调用前仍然会先问你，确认后才执行。</span>
            </span>
            <Switch
              checked={allowWrites}
              disabled={busy}
              label={allowWrites ? "关闭写操作" : "允许写操作"}
              onChange={() => setAllowWrites((value) => !value)}
            />
          </div>
        )}

        {remote && (
          <>
            <div className="connector-field">
              <label className="field-label" htmlFor="connector-url">
                地址
              </label>
              <input
                id="connector-url"
                className="field"
                value={url}
                spellCheck={false}
                placeholder="https://example.com/mcp"
                onChange={(event) => setUrl(event.target.value)}
              />
            </div>

            <div className="connector-field">
              <label className="field-label" htmlFor="connector-headers">
                请求头
              </label>
              <GrowingTextarea
                id="connector-headers"
                value={headersText}
                placeholder={
                  editing && connector.header_names.length > 0
                    ? connector.header_names.map((header) => `${header}: `).join("\n")
                    : "Authorization: Bearer abc123"
                }
                onChange={(event) => setHeadersText(event.target.value)}
              />
              <p className="hint">
                {editing && connector.header_names.length > 0
                  ? "已经保存的值不会显示。留空表示不修改；填写会整体替换已有的请求头。"
                  : "每行一个，写成「名称: 值」。"}
              </p>
            </div>
          </>
        )}

        {local && (
          <>
            <div className="connector-field">
              <label className="field-label" htmlFor="connector-command">
                命令
              </label>
              <input
                id="connector-command"
                className="field"
                value={command}
                spellCheck={false}
                placeholder="npx"
                onChange={(event) => setCommand(event.target.value)}
              />
            </div>

            <div className="connector-field">
              <label className="field-label" htmlFor="connector-args">
                参数
              </label>
              <GrowingTextarea
                id="connector-args"
                value={argsText}
                placeholder={"-y\n@modelcontextprotocol/server-github"}
                onChange={(event) => setArgsText(event.target.value)}
              />
              <p className="hint">每行一个参数。</p>
            </div>

            <div className="connector-field">
              <label className="field-label" htmlFor="connector-env">
                环境变量
              </label>
              <GrowingTextarea
                id="connector-env"
                value={envText}
                placeholder={
                  editing && connector.env_names.length > 0
                    ? connector.env_names.map((variable) => `${variable}=`).join("\n")
                    : "GITHUB_PERSONAL_ACCESS_TOKEN=abc123"
                }
                onChange={(event) => setEnvText(event.target.value)}
              />
              <p className="hint">
                {editing && connector.env_names.length > 0
                  ? "已经保存的值不会显示。留空表示不修改；填写会整体替换已有的变量。"
                  : "每行一个，写成「NAME=value」。"}
              </p>
            </div>
          </>
        )}

        {busy && (
          <p className="hint" role="status">
            正在连接并读取工具，本地服务第一次启动可能要等一分钟。
          </p>
        )}

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}
