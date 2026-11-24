import { describe, expect, it } from "vitest";
import type { ModelCaps } from "../api/types";
import { capSourceLabel, capabilityView, cycleImages, formatTokens, withOverrideField } from "./modelCaps";

const caps: ModelCaps = {
  images: true,
  video: true,
  audio: false,
  pdf: false,
  context_window: 1048576,
  max_output: 131072,
  reasoning: true,
  reasoning_levels: ["low", "high"],
  sources: { images: "provider", context_window: "provider", max_output: "provider", reasoning: "provider" },
};

describe("formatTokens", () => {
  it("reads windows and limits the way the chips show them", () => {
    expect(formatTokens(1048576)).toBe("1M");
    expect(formatTokens(1000000)).toBe("1M");
    expect(formatTokens(1500000)).toBe("1.5M");
    expect(formatTokens(200000)).toBe("200K");
    expect(formatTokens(4096)).toBe("4K");
    expect(formatTokens(0)).toBe("—");
  });
});

describe("capabilityView", () => {
  it("shows the detected values and their sources", () => {
    const view = capabilityView(caps, undefined);
    expect(view.images).toBe(true);
    expect(view.context_window).toBe(1048576);
    expect(capSourceLabel(view.sources.images)).toBe("服务商提供");
    expect(capSourceLabel(view.sources.pdf)).toBe("默认值");
  });

  it("lets the unsaved override win and marks it as the user's", () => {
    const view = capabilityView(caps, { images: false, context: 64000 });
    expect(view.images).toBe(false);
    expect(view.context_window).toBe(64000);
    expect(view.max_output).toBe(131072);
    expect(view.sources.images).toBe("user");
    expect(view.sources.context_window).toBe("user");
    expect(view.sources.max_output).toBe("provider");
  });

  it("has sensible defaults for a model nothing described", () => {
    const view = capabilityView(undefined, undefined);
    expect(view.images).toBe(false);
    expect(view.context_window).toBe(0);
    expect(view.sources.images).toBeUndefined();
  });
});

describe("cycleImages", () => {
  it("cycles auto → on → off → auto", () => {
    expect(cycleImages(undefined)).toEqual({ images: true });
    expect(cycleImages({ images: true })).toEqual({ images: false });
    expect(cycleImages({ images: false })).toEqual({});
  });

  it("keeps the other overrides while cycling", () => {
    expect(cycleImages({ images: true, context: 64000 })).toEqual({ images: false, context: 64000 });
    expect(cycleImages({ images: false, context: 64000 })).toEqual({ images: null, context: 64000 });
  });
});

describe("withOverrideField", () => {
  it("merges a patch and drops an entry that is fully automatic again", () => {
    const start = { m1: { images: false as const, context: 32000 } };
    expect(withOverrideField(start, "m1", { context: null })).toEqual({ m1: { images: false } });
    expect(withOverrideField(start, "m1", { images: null, context: null })).toEqual({});
    expect(withOverrideField(start, "m2", { output: 4096 })).toEqual({
      m1: { images: false, context: 32000 },
      m2: { output: 4096 },
    });
  });
});
