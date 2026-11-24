// Model picks, as the UI talks about them: one select option per listed model, plus the
// "follow the default" option every bot starts on. Everything here is pure so the wording and
// the grouping can be tested without a dialog.

import type { Agent, Health, ModelDetail, ModelRef, Provider, ProvidersState } from "../api/types";

/** The option value meaning "follow the workspace default". */
export const FOLLOW_DEFAULT = "";

export const FALLBACK_HINT = "原来的模型已被删除，正在使用默认模型。";

/** The effort levels each CLI accepts when the payload has no per-model detail yet. */
export const CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"];
export const CODEX_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"];

/** How an effort level is written in the pickers; an unknown level is shown as itself. */
export const EFFORT_LABELS: Record<string, string> = {
  minimal: "最低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "很高",
  max: "最高",
  ultra: "极高",
};

/** What the CLI says about one of its models, or undefined for anything else. */
export function modelDetail(provider: Provider, model: string): ModelDetail | undefined {
  return (provider.model_details ?? []).find((detail) => detail.model === model);
}

/** The wording a model gets in the pickers: the CLI's own label, else the name itself. */
export function modelLabel(provider: Provider, model: string): string {
  return modelDetail(provider, model)?.label || model;
}

/** The model the CLI itself is configured with right now, if the catalog names one. */
export function currentModelLabel(provider: Provider): string {
  const current = (provider.model_details ?? []).find((detail) => detail.current);
  return current ? current.label || current.model : "";
}

/** A model's option text: its label, a note when the CLI already uses it, and an image mark. */
export function modelOptionText(provider: Provider, model: string): string {
  const label = modelLabel(provider, model);
  const note = modelDetail(provider, model)?.current ? "（CLI 当前使用）" : "";
  const sees = provider.model_caps?.[model]?.images ? " 🖼️" : "";
  return `${label}${note}${sees}`;
}

/** The model a pick points at, with its provider; null for follow-the-default. */
export function pickedModel(
  providers: ProvidersState | null,
  ref: ModelRef | null,
): { provider: Provider; detail: ModelDetail | undefined } | null {
  if (!ref) return null;
  const provider = providers?.providers.find((item) => item.id === ref.provider_id);
  if (!provider) return null;
  return { provider, detail: modelDetail(provider, ref.model) };
}

/**
 * The reasoning efforts a picked model accepts; empty when the pick is not a CLI model.
 * A model the payload does not describe still offers what its CLI knows.
 */
export function modelEfforts(providers: ProvidersState | null, ref: ModelRef | null): string[] {
  const picked = pickedModel(providers, ref);
  if (!picked) return [];
  if (picked.detail?.efforts?.length) return picked.detail.efforts;
  if (picked.provider.protocol === "claude-code") return CLAUDE_EFFORTS;
  if (picked.provider.protocol === "codex") return CODEX_EFFORTS;
  return [];
}

/** The effort the CLI would use by itself, for the picker's "no pick" option. */
export function defaultEffortLabel(
  providers: ProvidersState | null,
  ref: ModelRef | null,
): string {
  const defaultEffort = pickedModel(providers, ref)?.detail?.default_effort ?? "";
  return defaultEffort ? `默认（${defaultEffort}）` : "默认";
}

/** A model option's value: JSON, so a model name may hold any character a provider allows. */
export function modelValue(ref: ModelRef): string {
  return JSON.stringify([ref.provider_id, ref.model]);
}

/** The pick behind a select value, or null for "follow the default" (and any stale value). */
export function parseModelValue(value: string): ModelRef | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      Array.isArray(parsed) &&
      parsed.length === 2 &&
      typeof parsed[0] === "string" &&
      typeof parsed[1] === "string" &&
      parsed[0] &&
      parsed[1]
    ) {
      return { provider_id: parsed[0], model: parsed[1] };
    }
  } catch {
    // Not one of ours: treat it as "no pick".
  }
  return null;
}

export interface ModelGroup {
  provider: Provider;
  models: string[];
}

/** Providers that list at least one model, in the order the backend returned them. */
export function modelGroups(providers: ProvidersState | null): ModelGroup[] {
  return (providers?.providers ?? [])
    .filter((provider) => provider.models.length > 0)
    .map((provider) => ({ provider, models: provider.models }));
}

/** The default model's own label, e.g. `mify / deepseek-v3`; empty when nothing is configured. */
export function defaultModelLabel(providers: ProvidersState | null): string {
  const chosen = providers?.default;
  if (!chosen) return "";
  const provider = providers?.providers.find((item) => item.id === chosen.provider_id);
  return provider ? `${provider.name} / ${chosen.model}` : chosen.model;
}

/** The first option of a bot's model select: what it follows, and what that currently is. */
export function followDefaultLabel(providers: ProvidersState | null): string {
  const current = defaultModelLabel(providers);
  return current ? `跟随默认（当前：${current}）` : "跟随默认（还没有默认模型）";
}

/** The value a bot's model select shows: its own pick, or the follow-default option. */
export function agentModelValue(agent: Agent): string {
  return agent.model_ref ? modelValue(agent.model_ref) : FOLLOW_DEFAULT;
}

/**
 * The model a bot actually answers with right now, for the chat header. `fallback` bots answer
 * with the default, so they show the default rather than a pick that no longer exists.
 */
export function agentModelLabel(
  agent: Agent,
  providers: ProvidersState | null,
  health: Health | null,
): { text: string; title: string } | null {
  const chosen = agent.model_status === "custom" ? agent.model_ref : null;
  if (chosen) {
    const provider = providers?.providers.find((item) => item.id === chosen.provider_id);
    const text = chosen.model;
    return { text, title: provider ? `${provider.name} / ${chosen.model}` : chosen.model };
  }
  const fallback = defaultModelLabel(providers);
  if (fallback) return { text: providers?.default?.model ?? fallback, title: fallback };
  // No provider list (an older backend): the health line still knows the default.
  if (health?.configured && health.model) {
    const title = health.provider_name ? `${health.provider_name} / ${health.model}` : health.model;
    return { text: health.model, title };
  }
  return null;
}

/**
 * The protocol that answers for a bot: its own pick, else the workspace default. A pick whose
 * provider is gone falls back to the default, exactly like the backend resolves it.
 */
export function agentProtocol(agent: Agent | undefined, providers: ProvidersState | null): string | null {
  if (!providers) return null;
  const ref = agent && agent.model_status !== "fallback" ? agent.model_ref ?? providers.default : providers.default;
  if (!ref) return null;
  return providers.providers.find((provider) => provider.id === ref.provider_id)?.protocol ?? null;
}

/** Whether a bot runs on a local CLI, which cannot take a message mid-turn. */
export function isCliBot(agent: Agent | undefined, providers: ProvidersState | null): boolean {
  const protocol = agentProtocol(agent, providers);
  return protocol === "claude-code" || protocol === "codex";
}

/** Bots whose own pick points at this provider: they fall back once it is deleted. */
export function agentsUsingProvider(agents: Agent[], providerID: string): Agent[] {
  return agents.filter((agent) => agent.model_ref?.provider_id === providerID);
}

/** The sentence the delete confirmation shows: who is affected, and what happens to them. */
export function deleteProviderPrompt(name: string, affected: number): string {
  if (affected === 0) return `删除「${name}」后不能恢复。没有 bot 正在用它。`;
  return `删除「${name}」后不能恢复。有 ${affected} 个 bot 正在用它，删除后会回到默认模型。`;
}
