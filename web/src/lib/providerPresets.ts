// The presets the "add a model service" form offers. They only prefill fields: nothing here is
// sent to the backend, and a provider is stored like any other once the user saves it.
//
// Every base URL was checked against the vendor's own documentation on 2026-10-21:
//   OpenAI        https://platform.openai.com/docs/api-reference  (base https://api.openai.com/v1)
//   Anthropic     https://docs.anthropic.com/en/api/messages      (base https://api.anthropic.com,
//                 the adapter appends /v1/messages itself)
//   DeepSeek      https://api-docs.deepseek.com/                  (base https://api.deepseek.com,
//                 which serves /chat/completions; /v1 is the documented SDK form and is what we
//                 keep so every OpenAI-compatible entry has a version segment in the path)
//   Moonshot      https://platform.kimi.ai/docs/api/overview      (base https://api.moonshot.ai/v1;
//                 the older mainland host api.moonshot.cn serves the same /v1 surface)
//   DashScope     https://help.aliyun.com/zh/model-studio         (compatible-mode/v1)
//   智谱 GLM      https://docs.bigmodel.cn/cn/guide/develop/openai/introduction
//                 (base https://open.bigmodel.cn/api/paas/v4)
//   OpenRouter    https://openrouter.ai/docs/quickstart           (base https://openrouter.ai/api/v1)
//   Gemini        https://ai.google.dev/gemini-api/docs/openai    (base …/v1beta/openai)
//   Ollama        https://docs.ollama.com/api/openai-compatibility (base http://localhost:11434/v1)
//
// 自定义 is not a preset: it opens the same form with every field left to the user.

import type { ProviderProtocol } from "../api/types";

export interface ProviderPreset {
  /** Stable key for React lists and tests; never stored. */
  id: string;
  label: string;
  protocol: ProviderProtocol;
  /** Empty for 自定义, which only opens the form. */
  baseUrl: string;
  /** Where the user creates a key, shown under the address while filling the form. */
  keyUrl: string | null;
  /** The card's subtitle for a preset that has no address (the two local CLIs). */
  note?: string;
}

export const CUSTOM_PRESET_ID = "custom";

/** The model name that lets a CLI engine pick its own model; the backend stores the same one. */
export const CLI_DEFAULT_MODEL = "默认";

/** Whether a protocol runs a locally installed CLI instead of an HTTP model service. */
export function isCLIProtocol(protocol: ProviderProtocol): boolean {
  return protocol === "claude-code" || protocol === "codex";
}

export const PROVIDER_PRESETS: readonly ProviderPreset[] = [
  {
    id: "openai",
    label: "OpenAI",
    protocol: "openai-chat",
    baseUrl: "https://api.openai.com/v1",
    keyUrl: "https://platform.openai.com/api-keys",
  },
  {
    id: "anthropic",
    label: "Anthropic",
    protocol: "anthropic-messages",
    baseUrl: "https://api.anthropic.com",
    keyUrl: "https://console.anthropic.com/settings/keys",
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    protocol: "openai-chat",
    baseUrl: "https://api.deepseek.com/v1",
    keyUrl: "https://platform.deepseek.com/api_keys",
  },
  {
    id: "moonshot",
    label: "Kimi（Moonshot）",
    protocol: "openai-chat",
    baseUrl: "https://api.moonshot.ai/v1",
    keyUrl: "https://platform.moonshot.cn/console/api-keys",
  },
  {
    id: "dashscope",
    label: "通义千问",
    protocol: "openai-chat",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    keyUrl: "https://bailian.console.aliyun.com/",
  },
  {
    id: "zhipu",
    label: "智谱 GLM",
    protocol: "openai-chat",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    keyUrl: "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    protocol: "openai-chat",
    baseUrl: "https://openrouter.ai/api/v1",
    keyUrl: "https://openrouter.ai/keys",
  },
  {
    id: "gemini",
    label: "Gemini",
    protocol: "openai-chat",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    keyUrl: "https://aistudio.google.com/apikey",
  },
  {
    id: "ollama",
    label: "Ollama（本机）",
    protocol: "openai-chat",
    baseUrl: "http://127.0.0.1:11434/v1",
    keyUrl: null,
  },
  {
    id: "claude-code",
    label: "Claude Code（用你的 Claude 订阅）",
    protocol: "claude-code",
    baseUrl: "",
    keyUrl: null,
    note: "调用本机 claude 命令，不填 key",
  },
  {
    id: "codex",
    label: "Codex（用你的 ChatGPT 订阅）",
    protocol: "codex",
    baseUrl: "",
    keyUrl: null,
    note: "调用本机 codex 命令，不填 key",
  },
  {
    id: CUSTOM_PRESET_ID,
    label: "自定义",
    protocol: "openai-chat",
    baseUrl: "",
    keyUrl: null,
  },
];

/** The protocol label the form's segmented control shows. */
export const PROTOCOL_LABELS: Record<ProviderProtocol, string> = {
  "openai-chat": "OpenAI 兼容",
  "anthropic-messages": "Anthropic",
  "claude-code": "Claude Code",
  codex: "Codex",
};

export function presetByID(id: string): ProviderPreset | undefined {
  return PROVIDER_PRESETS.find((preset) => preset.id === id);
}
