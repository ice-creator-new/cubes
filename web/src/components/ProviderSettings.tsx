// 「设置 → 模型」: the cards and the workspace default, inside the settings sheet. Adding or
// editing a provider opens its own dialog on top (presets first when adding), so the form is
// never squeezed into the settings sheet. Each model shows what it can do — images, window,
// output, thinking — and where each answer came from; the user can override any of them.
// Everything edits through the store; a failed save shows the server's own Chinese sentence.

import { useState } from "react";
import type {
  Agent,
  EngineStatus,
  ModelCaps,
  ModelOverride,
  Provider,
  ProviderProbe,
  ProviderProtocol,
} from "../api/types";
import { errorMessage } from "../api/client";
import {
  agentsUsingProvider,
  currentModelLabel,
  deleteProviderPrompt,
  modelGroups,
  modelOptionText,
  modelValue,
  parseModelValue,
  defaultModelLabel,
} from "../lib/modelOptions";
import {
  capSourceLabel,
  capabilityView,
  cycleImages,
  formatTokens,
  withOverrideField,
} from "../lib/modelCaps";
import {
  CLI_DEFAULT_MODEL,
  CUSTOM_PRESET_ID,
  isCLIProtocol,
  PROTOCOL_LABELS,
  PROVIDER_PRESETS,
  type ProviderPreset,
} from "../lib/providerPresets";
import { supervisorName } from "../lib/team";
import { store, useTeam } from "../state/store";
import { Icon } from "./Icon";
import { Modal } from "./Dialog";
import { Dialog } from "./Panels";

/** The form's values, kept apart from what the store already holds. */
interface FormState {
  id: string | null;
  name: string;
  protocol: ProviderProtocol;
  baseUrl: string;
  /** Empty while editing means "keep the stored key". */
  apiKey: string;
  keyStored: boolean;
  models: string[];
  /** Capabilities as the backend last resolved them, keyed by model name. */
  caps: Record<string, ModelCaps>;
  /** The user's own per-model answers, edited live in the form. */
  overrides: Record<string, ModelOverride>;
}

/** Stable empty list: a fresh `[]` on every render would churn the store subscription. */
const NO_AGENTS: Agent[] = [];

function formFrom(provider: Provider | null, preset: ProviderPreset | null): FormState {
  if (provider) {
    return {
      id: provider.id,
      name: provider.name,
      protocol: provider.protocol,
      baseUrl: provider.base_url,
      apiKey: "",
      keyStored: provider.api_key_hint !== null,
      models: [...provider.models],
      caps: { ...(provider.model_caps ?? {}) },
      overrides: { ...(provider.model_overrides ?? {}) },
    };
  }
  return {
    id: null,
    name: preset && preset.id !== CUSTOM_PRESET_ID ? preset.label : "",
    protocol: preset?.protocol ?? "openai-chat",
    baseUrl: preset?.baseUrl ?? "",
    apiKey: "",
    keyStored: false,
    // A CLI picks its own model until the user names one.
    models: preset && isCLIProtocol(preset.protocol) ? [CLI_DEFAULT_MODEL] : [],
    caps: {},
    overrides: {},
  };
}

/** The status of the CLI behind a protocol, when the backend reported one. */
function engineOf(
  engines: EngineStatus[] | undefined,
  protocol: ProviderProtocol,
): EngineStatus | undefined {
  if (!isCLIProtocol(protocol)) return undefined;
  return engines?.find((engine) => engine.kind === protocol);
}

/** A typed token count, or `null` for "clear this override". */
function tokenInput(value: string): number | null {
  const amount = Number(value);
  return Number.isFinite(amount) && amount > 0 ? Math.round(amount) : null;
}

/** What the provider dialog shows: the preset grid for a new one, then (or directly) the form. */
type Editor = { step: "presets" } | { step: "form"; form: FormState };

export function ProviderSettings() {
  const providers = useTeam((s) => s.providers);
  const providersError = useTeam((s) => s.providersError);
  const workspace = useTeam((s) => s.workspace);
  const agents = workspace?.agents ?? NO_AGENTS;
  const [editor, setEditor] = useState<Editor | null>(null);
  const [confirmingID, setConfirmingID] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [defaultBusy, setDefaultBusy] = useState(false);
  const [fallbackBusy, setFallbackBusy] = useState(false);

  const list = providers?.providers ?? [];
  const groups = modelGroups(providers);
  const current = providers?.default ? modelValue(providers.default) : "";
  const currentFallback = providers?.fallback ? modelValue(providers.fallback) : "";

  return (
    <>
      {/* Adding or editing a model service is a dialog over 设置, even while 设置 is a tab. */}
      {editor && (
        <Modal>
          <ProviderDialog
            editor={editor}
            engines={providers?.engines}
            onPreset={(preset) => setEditor({ step: "form", form: formFrom(null, preset) })}
            onClose={() => setEditor(null)}
          />
        </Modal>
      )}

      <section className="settings-section">
        <div className="settings-heading">
          <h4>模型服务</h4>
          <p>可以存多家 OpenAI 兼容或 Anthropic 服务，也可以直接用本机已登录的命令行。</p>
        </div>

        {providersError && !providers && (
          <p className="form-error" role="alert">
            {providersError}
          </p>
        )}

        {list.length === 0 ? (
          <p className="hint">还没有模型服务。添加一个后，{supervisorName(workspace)}和成员才能回复。</p>
        ) : (
          <ul className="provider-list">
            {list.map((provider) => {
              const isDefault = providers?.default?.provider_id === provider.id;
              const affected = agentsUsingProvider(agents, provider.id).length;
              const engine = engineOf(providers?.engines, provider.protocol);
              const seeing = provider.models.filter((model) => provider.model_caps?.[model]?.images).length;
              return (
                <li key={provider.id} className={isDefault ? "provider-card on" : "provider-card"}>
                  <div className="provider-card-head">
                    <h3>{provider.name}</h3>
                    {isDefault && <span className="provider-badge">默认</span>}
                  </div>
                  <p className="provider-card-protocol">{PROTOCOL_LABELS[provider.protocol]}</p>
                  <p className="provider-card-url">
                    {provider.base_url ||
                      (isCLIProtocol(provider.protocol)
                        ? "用本机已登录的命令行，不需要 API key"
                        : "还没有地址")}
                  </p>
                  {engine && (
                    <p className="provider-card-meta">
                      <span>{engine.detail}</span>
                    </p>
                  )}
                  <p className="provider-card-meta">
                    <span>{provider.api_key_hint ?? "不需要 API key"}</span>
                    <span>{provider.models.length} 个模型</span>
                    {!isCLIProtocol(provider.protocol) && provider.models.length > 0 && (
                      <span>{seeing > 0 ? `${seeing} 个能看图片` : "都不能看图片"}</span>
                    )}
                    {currentModelLabel(provider) && (
                      <span title="CLI 自己配置里的模型">CLI 当前：{currentModelLabel(provider)}</span>
                    )}
                  </p>
                  {confirmingID === provider.id ? (
                    <div className="confirm-step" role="group" aria-label={`确认删除${provider.name}`}>
                      <p className="confirm-prompt">{deleteProviderPrompt(provider.name, affected)}</p>
                      <div className="confirm-actions">
                        <button
                          type="button"
                          className="danger-button"
                          disabled={busy}
                          onClick={async () => {
                            setBusy(true);
                            setError(null);
                            const failure = await store.deleteProvider(provider.id);
                            setBusy(false);
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
                    <div className="provider-card-actions">
                      <button
                        type="button"
                        className="ghost-button"
                        onClick={() => {
                          setEditor({ step: "form", form: formFrom(provider, null) });
                          setError(null);
                        }}
                      >
                        <Icon name="rename" size={15} />
                        编辑
                      </button>
                      <button
                        type="button"
                        className="ghost-button danger-ghost"
                        onClick={() => setConfirmingID(provider.id)}
                      >
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
          className="ghost-button"
          onClick={() => {
            setEditor({ step: "presets" });
            setError(null);
          }}
        >
          <Icon name="plus" size={15} />
          添加模型服务
        </button>
      </section>

      {list.length > 0 && (
        <section className="settings-section">
          <div className="settings-heading">
            <h4>默认与备用模型</h4>
            <p>没有单独选过模型的成员都用默认模型；它连不上时，这次回复改用备用模型。</p>
          </div>

          <div className="provider-default">
            <label className="field-label" htmlFor="default-model">
              默认模型
            </label>
            <select
              id="default-model"
              className="field select"
              value={current}
              disabled={defaultBusy || groups.length === 0}
              onChange={async (event) => {
                const ref = parseModelValue(event.target.value);
                if (!ref) return;
                setDefaultBusy(true);
                setError(null);
                const failure = await store.setDefaultModel(ref);
                setDefaultBusy(false);
                if (failure) setError(failure);
              }}
            >
              {current === "" && <option value="">{defaultModelLabel(providers) || "还没有选择"}</option>}
              {groups.map((group) => (
                <optgroup key={group.provider.id} label={group.provider.name}>
                  {group.models.map((model) => (
                    <option key={model} value={modelValue({ provider_id: group.provider.id, model })}>
                      {modelOptionText(group.provider, model)}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <p className="hint">没有单独选过模型的 bot 都用这个。改动从下一次回复开始生效。</p>
          </div>

          <div className="provider-default">
            <label className="field-label" htmlFor="fallback-model">
              备用模型
            </label>
            <select
              id="fallback-model"
              className="field select"
              value={currentFallback}
              disabled={fallbackBusy || groups.length === 0}
              onChange={async (event) => {
                const ref = parseModelValue(event.target.value);
                setFallbackBusy(true);
                setError(null);
                const failure = await store.setFallbackModel(ref);
                setFallbackBusy(false);
                if (failure) setError(failure);
              }}
            >
              <option value="">不设置备用模型</option>
              {groups.map((group) => (
                <optgroup key={group.provider.id} label={group.provider.name}>
                  {group.models.map((model) => (
                    <option key={model} value={modelValue({ provider_id: group.provider.id, model })}>
                      {modelOptionText(group.provider, model)}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <p className="hint">当前模型连不上、被限流或额度用完时，这次回复自动换它再试一次，聊天里会留一条提示。</p>
          </div>
        </section>
      )}

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

/** Its own sheet over the settings sheet: the preset grid when adding, then the form. */
function ProviderDialog({
  editor,
  engines,
  onPreset,
  onClose,
}: {
  editor: Editor;
  engines: EngineStatus[] | undefined;
  onPreset: (preset: ProviderPreset) => void;
  onClose: () => void;
}) {
  if (editor.step === "presets") {
    return (
      <Dialog title="添加模型服务" onClose={onClose} wide>
        <p className="hint provider-presets-hint">先选一个预设，协议和地址会自动填好；找不到就选「自定义」。</p>
        <div className="provider-presets">
          {PROVIDER_PRESETS.map((preset) => {
            const engine = engineOf(engines, preset.protocol);
            return (
              <button key={preset.id} type="button" className="provider-preset" onClick={() => onPreset(preset)}>
                <span className="provider-preset-name">{preset.label}</span>
                <span className="provider-preset-url" title={preset.baseUrl || undefined}>
                  {preset.baseUrl || preset.note || "自己填写地址和协议"}
                </span>
                {engine && (
                  <span className="provider-preset-url">
                    {engine.installed ? (engine.ready ? "已安装，已登录" : "已安装，未登录") : "未安装"}：
                    {engine.detail}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </Dialog>
    );
  }
  const { form } = editor;
  // A new key per form: picking another preset, or editing another card, starts a fresh form.
  return (
    <ProviderForm
      key={form.id ?? `new:${form.protocol}:${form.baseUrl}:${form.name}`}
      initial={form}
      engines={engines}
      onClose={onClose}
    />
  );
}

function ProviderForm({
  initial,
  engines,
  onClose,
}: {
  initial: FormState;
  engines: EngineStatus[] | undefined;
  onClose: () => void;
}) {
  const [name, setName] = useState(initial.name);
  const [protocol, setProtocol] = useState<ProviderProtocol>(initial.protocol);
  const [baseUrl, setBaseUrl] = useState(initial.baseUrl);
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<string[]>(initial.models);
  const [caps, setCaps] = useState<Record<string, ModelCaps>>(initial.caps);
  const [overrides, setOverrides] = useState<Record<string, ModelOverride>>(initial.overrides);
  const [editing, setEditing] = useState<string | null>(null);
  const [draftModel, setDraftModel] = useState("");
  const [fetched, setFetched] = useState<string[] | null>(null);
  const [probe, setProbe] = useState<ProviderProbe | null>(null);
  const [busy, setBusy] = useState<"save" | "models" | "test" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const supervisor = supervisorName(useTeam((s) => s.workspace));

  const editingProvider = initial.id;
  const cli = isCLIProtocol(protocol);
  const engine = engineOf(engines, protocol);
  const form = () => ({
    ...(editingProvider ? { id: editingProvider } : {}),
    protocol,
    base_url: baseUrl.trim(),
    ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
  });

  const addModel = (model: string) => {
    const clean = model.trim();
    if (!clean || models.includes(clean)) return;
    setModels([...models, clean]);
  };

  const patchOverride = (model: string, patch: ModelOverride) =>
    setOverrides((current) => withOverrideField(current, model, patch));

  const save = async () => {
    setError(null);
    if (!name.trim()) return setError("请给这个模型服务起个名字。");
    // A CLI has no address and no key: its subscription is the credential.
    if (!cli && !baseUrl.trim()) return setError("请填写服务地址。");
    if (!cli && models.length === 0) return setError("至少添加一个模型。");
    setBusy("save");
    const failure = await store.saveProvider(
      {
        name,
        protocol,
        baseUrl,
        models: cli && models.length === 0 ? [CLI_DEFAULT_MODEL] : models,
        modelOverrides: overrides,
        // An empty field on an existing provider means "keep the stored key".
        ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
      },
      editingProvider ?? undefined,
    );
    setBusy(null);
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <Dialog
      title={editingProvider ? `编辑「${initial.name}」` : "添加模型服务"}
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="ghost-button" disabled={busy !== null} onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={busy !== null} onClick={() => void save()}>
            {busy === "save" ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
    <div className="provider-form">
      <label className="field-label" htmlFor="provider-name">
        名称
      </label>
      <input
        id="provider-name"
        className="field"
        value={name}
        placeholder="例如 mify"
        onChange={(event) => setName(event.target.value)}
      />

      <span className="field-label" id="provider-protocol-label">
        协议
      </span>
      <div className="segmented" role="radiogroup" aria-labelledby="provider-protocol-label">
        {(["openai-chat", "anthropic-messages", "claude-code", "codex"] as const).map((value) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={protocol === value}
            className={protocol === value ? "on" : ""}
            onClick={() => {
              setProtocol(value);
              // Keep the model list honest when switching between a service and a CLI.
              if (isCLIProtocol(value) && models.length === 0) setModels([CLI_DEFAULT_MODEL]);
              if (!isCLIProtocol(value) && models.length === 1 && models[0] === CLI_DEFAULT_MODEL) {
                setModels([]);
              }
            }}
          >
            {PROTOCOL_LABELS[value]}
          </button>
        ))}
      </div>

      {cli ? (
        <div className="provider-engine">
          <span className="field-label">本机命令行</span>
          <p className="hint">
            {engine
              ? `${engine.label}${engine.version ? ` ${engine.version}` : ""}：${engine.installed ? (engine.ready ? "已安装，已登录" : "已安装，未登录") : "未安装"}。${engine.detail}`
              : engines
                ? `还没有检测到 ${protocol === "codex" ? "codex" : "claude"}，请确认它已安装。`
                : "正在检测本机命令行…"}
          </p>
          <p className="hint">
            {engine?.ready
              ? "这个服务直接用你已经登录的 CLI，不需要 API key。点「读取本机模型」能列出它在这台电脑上可用的模型；「默认」用 CLI 自己配置的那个。"
              : `请在终端运行 ${protocol === "codex" ? "codex login" : "claude"} 完成登录，然后重新打开这里。`}
          </p>
        </div>
      ) : (
        <>
          <label className="field-label" htmlFor="provider-url">
            服务地址
          </label>
          <input
            id="provider-url"
            className="field"
            value={baseUrl}
            spellCheck={false}
            placeholder="https://api.example.com/v1"
            onChange={(event) => setBaseUrl(event.target.value)}
          />
          <p className="hint">
            {protocol === "anthropic-messages" ? "填到域名即可，程序会自己接上 /v1/messages。" : "填到 /v1 为止，程序会自己接上 /chat/completions。"}
          </p>

          <label className="field-label" htmlFor="provider-key">
            API key
          </label>
          <input
            id="provider-key"
            className="field"
            type="password"
            autoComplete="off"
            value={apiKey}
            placeholder={initial.keyStored ? "已设置，留空保持不变" : "本机服务可以留空"}
            onChange={(event) => setApiKey(event.target.value)}
          />
        </>
      )}

      <div className="provider-models-head">
        <span className="field-label" id="provider-models-label">
          模型
        </span>
        <button
          type="button"
          className="link-button"
          disabled={busy === "models" || (!cli && !baseUrl.trim())}
          onClick={async () => {
            setBusy("models");
            setError(null);
            setNotice(null);
            try {
              const found = await store.providerModels(form());
              setFetched(found.models);
              // The answer's capabilities drive the chips right away, before saving.
              setCaps((current) => ({ ...current, ...found.caps }));
              setNotice(
                found.models.length
                  ? `读到 ${found.models.length} 个模型，勾选要用的。`
                  : "这个服务没有返回任何模型。",
              );
            } catch (failure) {
              setError(errorMessage(failure));
              setFetched(null);
            } finally {
              setBusy(null);
            }
          }}
        >
          {busy === "models"
            ? cli
              ? "读取中…"
              : "获取中…"
            : cli
              ? "读取本机模型"
              : "获取模型列表"}
        </button>
      </div>

      {models.length > 0 ? (
        <ul className="model-rows" aria-labelledby="provider-models-label">
          {models.map((model) => (
            <ModelRow
              key={model}
              model={model}
              caps={caps[model]}
              override={overrides[model]}
              open={editing === model}
              onCycleImages={() => patchOverride(model, cycleImages(overrides[model]))}
              onToggleEditor={() => setEditing(editing === model ? null : model)}
              onPatch={(patch) => patchOverride(model, patch)}
              onRemove={() => setModels(models.filter((item) => item !== model))}
            />
          ))}
        </ul>
      ) : (
        <p className="hint">
          {cli
            ? "还没有模型名：点「读取本机模型」看看这台电脑上有哪些；保存时会自动填上它报告的全部模型。"
            : "还没有模型。可以获取列表勾选，也可以在下面手动填一个。"}
        </p>
      )}
      {models.length > 0 && (
        <p className="hint">
          {cli
            ? "「默认」不传模型参数，CLI 用自己的配置；其他名字会原样传给它，bot 设置里还能挑思考强度。"
            : "能不能看图、窗口多大都是自动查的：先看服务商自己的说明，再查 models.dev。查错了就点「看图 / 不看图」改掉（标「手动」），「调整」可以手填窗口和输出上限。"}
        </p>
      )}

      <div className="inline-field">
        <input
          className="field"
          value={draftModel}
          aria-label="手动添加模型名"
          placeholder="手动填一个模型名，例如 deepseek-v3"
          onChange={(event) => setDraftModel(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            addModel(draftModel);
            setDraftModel("");
          }}
        />
        <button
          type="button"
          className="ghost-button"
          disabled={!draftModel.trim()}
          onClick={() => {
            addModel(draftModel);
            setDraftModel("");
          }}
        >
          添加
        </button>
      </div>

      {notice && <p className="hint">{notice}</p>}

      {fetched && fetched.length > 0 && (
        <div className="model-picker" role="group" aria-label="从服务读到的模型">
          {fetched.map((model) => (
            <label key={model} className={models.includes(model) ? "model-pick on" : "model-pick"}>
              <input
                type="checkbox"
                checked={models.includes(model)}
                onChange={(event) =>
                  setModels(event.target.checked ? [...models, model] : models.filter((item) => item !== model))
                }
              />
              <span>{model}</span>
              {caps[model]?.images && (
                <span className="model-pick-caps" title="能看图片">
                  <Icon name="image" size={12} />
                </span>
              )}
            </label>
          ))}
        </div>
      )}

      <div className="provider-test">
        <button
          type="button"
          className="ghost-button"
          disabled={busy === "test" || (!cli && !baseUrl.trim()) || models.length === 0}
          onClick={async () => {
            setBusy("test");
            setProbe(null);
            setError(null);
            try {
              const result = await store.probeProvider({ ...form(), model: models[0]! });
              setProbe(result);
            } catch (failure) {
              setError(errorMessage(failure));
            } finally {
              setBusy(null);
            }
          }}
        >
          {busy === "test" ? "正在测试…" : cli ? "检测命令行" : "测试连接"}
        </button>
        {probe && (
          <p className={probe.ok ? (probe.supports_tools ? "probe ok" : "probe warn") : "probe bad"} role="status">
            {cli
              ? probe.detail
              : probe.ok
              ? probe.supports_tools
                ? `连接正常，耗时 ${probe.latency_ms} 毫秒，模型可以调用工具。`
                : `连接正常，耗时 ${probe.latency_ms} 毫秒，但这个模型没有调用工具。${supervisor}和成员都依赖工具，它只适合不需要工具的场景。`
              : probe.detail}
          </p>
        )}
      </div>

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

    </div>
    </Dialog>
  );
}

/** One model's row: what it can do, where that came from, and the inline override editor. */
function ModelRow({
  model,
  caps,
  override,
  open,
  onCycleImages,
  onToggleEditor,
  onPatch,
  onRemove,
}: {
  model: string;
  caps: ModelCaps | undefined;
  override: ModelOverride | undefined;
  open: boolean;
  onCycleImages: () => void;
  onToggleEditor: () => void;
  onPatch: (patch: ModelOverride) => void;
  onRemove: () => void;
}) {
  const view = capabilityView(caps, override);
  const source = (field: string) => capSourceLabel(view.sources[field]);
  // The chip says what the model does, lit when it reads images and struck through when not;
  // the user's own choice is the only state that also says who decided.
  const pinned = typeof override?.images === "boolean";
  const imageLabel = view.images ? "看图" : "不看图";
  const levels = view.reasoning_levels.length > 0 ? `，可选 ${view.reasoning_levels.join("、")}` : "";
  const userSet = (field: string) => view.sources[field] === "user";
  return (
    <li className="model-row">
      <div className="model-row-head">
        <span className="model-row-name" title={model}>
          {model}
        </span>
        <div className="cap-chips">
          <button
            type="button"
            className={`cap-chip image ${view.images ? "on" : "off"}${pinned ? " pinned" : ""}`}
            title={`${view.images ? "能看图片" : "不能看图片"}（${source("images")}）。点一下切换：自动 → 能看 → 不能看`}
            aria-label={`${model}：${view.images ? "能看图片" : "不能看图片"}，${pinned ? "你设置的" : "自动探测"}`}
            onClick={onCycleImages}
          >
            <Icon name="image" size={12} />
            {imageLabel}
            {pinned && <span className="cap-chip-mark">手动</span>}
          </button>
          {view.video && (
            <span className="cap-chip media" title={`视频输入：${source("video")}`}>
              视频
            </span>
          )}
          {view.audio && (
            <span className="cap-chip media" title={`音频输入：${source("audio")}`}>
              音频
            </span>
          )}
          {view.pdf && (
            <span className="cap-chip media" title={`PDF 输入：${source("pdf")}`}>
              PDF
            </span>
          )}
          {view.reasoning && (
            <span className="cap-chip media" title={`支持思考：${source("reasoning")}${levels}`}>
              思考
            </span>
          )}
          <span
            className={userSet("context_window") || userSet("max_output") ? "cap-limits pinned" : "cap-limits"}
            title={`上下文窗口：${source("context_window")}；单次输出上限：${source("max_output")}`}
          >
            {formatTokens(view.context_window)} 上下文 · 输出 {formatTokens(view.max_output)}
          </span>
        </div>
        <button
          type="button"
          className="model-row-action"
          aria-expanded={open}
          aria-label={`调整 ${model} 的窗口和输出上限`}
          onClick={onToggleEditor}
        >
          {open ? "收起" : "调整"}
        </button>
        <button type="button" className="model-row-remove" aria-label={`移除 ${model}`} onClick={onRemove}>
          <Icon name="close" size={12} />
        </button>
      </div>
      {open && (
        <div className="model-row-editor">
          <label className="model-row-field">
            <span>上下文窗口</span>
            <input
              className="field"
              type="number"
              min={1}
              inputMode="numeric"
              value={override?.context ?? ""}
              placeholder={`自动（${formatTokens(caps?.context_window ?? 0)}）`}
              onChange={(event) => onPatch({ context: tokenInput(event.target.value) })}
            />
          </label>
          <label className="model-row-field">
            <span>输出上限</span>
            <input
              className="field"
              type="number"
              min={1}
              inputMode="numeric"
              value={override?.output ?? ""}
              placeholder={`自动（${formatTokens(caps?.max_output ?? 0)}）`}
              onChange={(event) => onPatch({ output: tokenInput(event.target.value) })}
            />
          </label>
          <p className="hint model-row-note">
            留空就是自动探测；填了就以你为准，工具条上标成「你设置的」。
          </p>
          <button
            type="button"
            className="link-button"
            disabled={override?.context == null && override?.output == null}
            onClick={() => onPatch({ context: null, output: null })}
          >
            两项都恢复自动
          </button>
        </div>
      )}
    </li>
  );
}
