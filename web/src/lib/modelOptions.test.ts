import { describe, expect, it } from "vitest";
import type { Agent, Health, ModelRef, Provider, ProvidersState } from "../api/types";
import {
  CLAUDE_EFFORTS,
  CODEX_EFFORTS,
  FALLBACK_HINT,
  FOLLOW_DEFAULT,
  agentModelLabel,
  agentModelValue,
  agentsUsingProvider,
  currentModelLabel,
  defaultEffortLabel,
  deleteProviderPrompt,
  followDefaultLabel,
  modelEfforts,
  modelGroups,
  modelLabel,
  modelOptionText,
  modelValue,
  parseModelValue,
} from "./modelOptions";

function provider(patch: Partial<Provider>): Provider {
  return {
    id: "p_1",
    name: "mify",
    protocol: "openai-chat",
    base_url: "http://model.mify.ai.srv/v1",
    api_key_hint: "sk-…abcd",
    models: ["deepseek-v3", "gpt-5"],
    model_caps: {},
    model_overrides: {},
    created_at: "2026-10-20T10:00:00Z",
    updated_at: "2026-10-20T10:00:00Z",
    ...patch,
  };
}

function agent(patch: Partial<Agent>): Agent {
  return {
    id: "a_1",
    name: "小北",
    instructions: "",
    kind: "member",
    avatar_seed: "north",
    appearance: null,
    model_ref: null,
    model_status: "default",
    token_budget: null,
    tool_permissions: null,
    send_mode: null,
    permission_mode: null,
    created_at: "2026-10-20T10:00:00Z",
    updated_at: "2026-10-20T10:00:00Z",
    ...patch,
  };
}

function state(patch: Partial<ProvidersState> = {}): ProvidersState {
  return {
    providers: [provider({})],
    default: { provider_id: "p_1", model: "deepseek-v3" },
    ...patch,
  };
}

describe("model select values", () => {
  it("round-trips a pick even when the model name holds a slash", () => {
    const ref = { provider_id: "p_open", model: "anthropic/claude-sonnet-4.5" };
    expect(parseModelValue(modelValue(ref))).toEqual(ref);
  });

  it("treats the empty value as follow-the-default and rejects anything else", () => {
    expect(parseModelValue(FOLLOW_DEFAULT)).toBeNull();
    expect(parseModelValue("not-json")).toBeNull();
    expect(parseModelValue('["p_1"]')).toBeNull();
    expect(parseModelValue('["p_1",""]')).toBeNull();
  });

  it("shows a bot's own pick, else the follow-default option", () => {
    expect(agentModelValue(agent({}))).toBe(FOLLOW_DEFAULT);
    expect(agentModelValue(agent({ model_ref: { provider_id: "p_1", model: "gpt-5" } }))).toBe(
      modelValue({ provider_id: "p_1", model: "gpt-5" }),
    );
    // A fallback bot still shows what it picked, so the user can see what vanished.
    const falling = agent({
      model_ref: { provider_id: "p_gone", model: "old" },
      model_status: "fallback",
    });
    expect(agentModelValue(falling)).toBe(modelValue({ provider_id: "p_gone", model: "old" }));
  });
});

describe("followDefaultLabel", () => {
  it("names the model the bot follows right now", () => {
    expect(followDefaultLabel(state())).toBe("跟随默认（当前：mify / deepseek-v3）");
  });

  it("says so when nothing is configured yet", () => {
    expect(followDefaultLabel(null)).toBe("跟随默认（还没有默认模型）");
    expect(followDefaultLabel(state({ default: null }))).toBe("跟随默认（还没有默认模型）");
  });

  it("falls back to the bare model name when the provider vanished", () => {
    expect(followDefaultLabel(state({ providers: [], default: { provider_id: "p_gone", model: "x" } }))).toBe(
      "跟随默认（当前：x）",
    );
  });
});

describe("modelGroups", () => {
  it("groups models by provider and drops providers with nothing to pick", () => {
    const groups = modelGroups(
      state({
        providers: [provider({}), provider({ id: "p_2", name: "空服务", models: [] })],
      }),
    );
    expect(groups.map((group) => group.provider.name)).toEqual(["mify"]);
    expect(groups[0]?.models).toEqual(["deepseek-v3", "gpt-5"]);
  });

  it("is empty without a provider list", () => {
    expect(modelGroups(null)).toEqual([]);
  });
});

describe("agentModelLabel", () => {
  const health: Health = {
    status: "ok",
    configured: true,
    model: "deepseek-v3",
    base_url: "http://model.mify.ai.srv/v1",
    provider_name: "mify",
    configuration_error: null,
    auth: false,
  };

  it("shows the bot's own model with its provider in the tooltip", () => {
    const label = agentModelLabel(
      agent({ model_ref: { provider_id: "p_1", model: "gpt-5" }, model_status: "custom" }),
      state(),
      health,
    );
    expect(label).toEqual({ text: "gpt-5", title: "mify / gpt-5" });
  });

  it("shows the default for a bot that follows it", () => {
    expect(agentModelLabel(agent({}), state(), health)).toEqual({
      text: "deepseek-v3",
      title: "mify / deepseek-v3",
    });
  });

  it("shows the default for a bot whose pick fell away", () => {
    const falling = agent({ model_ref: { provider_id: "p_gone", model: "old" }, model_status: "fallback" });
    expect(agentModelLabel(falling, state(), health)?.text).toBe("deepseek-v3");
  });

  it("falls back to the health line when the provider list could not be read", () => {
    expect(agentModelLabel(agent({}), null, health)).toEqual({
      text: "deepseek-v3",
      title: "mify / deepseek-v3",
    });
    expect(agentModelLabel(agent({}), null, null)).toBeNull();
  });
});

describe("provider deletion", () => {
  it("counts the bots whose own pick points at the provider", () => {
    const agents = [
      agent({ id: "a1", model_ref: { provider_id: "p_1", model: "gpt-5" } }),
      agent({ id: "a2", model_ref: null }),
      agent({ id: "a3", model_ref: { provider_id: "p_2", model: "x" } }),
    ];
    expect(agentsUsingProvider(agents, "p_1").map((item) => item.id)).toEqual(["a1"]);
  });

  it("says how many bots go back to the default", () => {
    expect(deleteProviderPrompt("mify", 2)).toContain("有 2 个 bot 正在用它");
    expect(deleteProviderPrompt("mify", 2)).toContain("回到默认模型");
    expect(deleteProviderPrompt("mify", 0)).toContain("没有 bot 正在用它");
  });

  it("keeps the fallback sentence the member editor shows", () => {
    expect(FALLBACK_HINT).toBe("原来的模型已被删除，正在使用默认模型。");
  });
});

describe("CLI model wording and effort", () => {
  const cli = provider({
    id: "p_cli",
    name: "Claude 订阅",
    protocol: "claude-code",
    base_url: "",
    api_key_hint: null,
    models: ["默认", "sonnet", "kiro/claude-sonnet-5-5[1m]"],
    model_details: [
      {
        model: "sonnet",
        label: "sonnet（最新 Sonnet）",
        description: "本机设置里指向 mify/deepseek-flash[1m]",
        efforts: [...CLAUDE_EFFORTS],
        default_effort: "",
        current: true,
      },
      {
        model: "kiro/claude-sonnet-5-5[1m]",
        label: "claude-sonnet-5.5 · Kiro",
        description: "",
        efforts: ["low", "high"],
        default_effort: "high",
        current: false,
      },
    ],
  });

  it("shows the CLI's own label and marks the model it is configured with", () => {
    expect(modelLabel(cli, "kiro/claude-sonnet-5-5[1m]")).toBe("claude-sonnet-5.5 · Kiro");
    expect(modelLabel(cli, "默认")).toBe("默认");
    expect(modelOptionText(cli, "sonnet")).toBe("sonnet（最新 Sonnet）（CLI 当前使用）");
    expect(currentModelLabel(cli)).toBe("sonnet（最新 Sonnet）");
    expect(currentModelLabel(provider({}))).toBe("");
  });

  it("marks image-capable models in the pickers", () => {
    const seeing = provider({
      model_caps: {
        "gpt-5": {
          images: true,
          video: false,
          audio: false,
          pdf: false,
          context_window: 200000,
          max_output: 64000,
          reasoning: false,
          reasoning_levels: [],
          sources: {},
        },
      },
    });
    expect(modelOptionText(seeing, "gpt-5")).toBe("gpt-5 🖼️");
    expect(modelOptionText(seeing, "deepseek-v3")).toBe("deepseek-v3");
  });

  it("offers exactly the efforts the picked model accepts", () => {
    const ref: ModelRef = { provider_id: "p_cli", model: "kiro/claude-sonnet-5-5[1m]" };
    const providers = state({ providers: [cli], default: { provider_id: "p_cli", model: "sonnet" } });
    expect(modelEfforts(providers, ref)).toEqual(["low", "high"]);
    expect(defaultEffortLabel(providers, ref)).toBe("默认（high）");
    // A name the payload does not describe still offers what the CLI knows.
    expect(modelEfforts(providers, { provider_id: "p_cli", model: "默认" })).toEqual(CLAUDE_EFFORTS);
    expect(
      modelEfforts(providers, { provider_id: "p_cli", model: "sonnet" }),
    ).toEqual(CLAUDE_EFFORTS);
  });

  it("offers nothing for an HTTP model", () => {
    const providers = state({ providers: [provider({})] });
    expect(modelEfforts(providers, { provider_id: "p_1", model: "gpt-5" })).toEqual([]);
    expect(modelEfforts(providers, null)).toEqual([]);
  });

  it("falls back to the protocol's known efforts for a Codex model", () => {
    const codex = provider({ id: "p_cx", protocol: "codex", models: ["gpt-5-codex"] });
    const providers = state({ providers: [codex] });
    expect(modelEfforts(providers, { provider_id: "p_cx", model: "gpt-5-codex" })).toEqual(
      CODEX_EFFORTS,
    );
  });
});
