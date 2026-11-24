import { describe, expect, it } from "vitest";
import { CUSTOM_PRESET_ID, PROVIDER_PRESETS, PROTOCOL_LABELS, presetByID } from "./providerPresets";

describe("provider presets", () => {
  it("only offers protocols the backend accepts", () => {
    for (const preset of PROVIDER_PRESETS) {
      expect(Object.keys(PROTOCOL_LABELS)).toContain(preset.protocol);
    }
  });

  it("points every remote preset at https, and only local ones at http", () => {
    for (const preset of PROVIDER_PRESETS) {
      if (!preset.baseUrl) continue;
      if (preset.id === "ollama") {
        expect(preset.baseUrl).toBe("http://127.0.0.1:11434/v1");
        continue;
      }
      expect(preset.baseUrl.startsWith("https://")).toBe(true);
      // The backend strips trailing slashes itself; the presets come without one.
      expect(preset.baseUrl.endsWith("/")).toBe(false);
    }
  });

  it("keeps every preset a filled form except 自定义 and the local CLI engines", () => {
    const custom = presetByID(CUSTOM_PRESET_ID);
    const withoutAddress = PROVIDER_PRESETS.filter((preset) => !preset.baseUrl);
    expect(withoutAddress).toEqual([presetByID("claude-code"), presetByID("codex"), custom]);
    // The CLI presets fill the form from the installed command; 自定义 fills nothing.
    expect(withoutAddress.every((preset) => preset.id === CUSTOM_PRESET_ID || preset.note)).toBe(true);
  });

  it("has unique ids so React lists and key lookups stay stable", () => {
    const ids = PROVIDER_PRESETS.map((preset) => preset.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("prefills Anthropic with the base the adapter appends /v1/messages to", () => {
    expect(presetByID("anthropic")?.baseUrl).toBe("https://api.anthropic.com");
    expect(presetByID("anthropic")?.protocol).toBe("anthropic-messages");
  });
});
