import { describe, expect, it } from "vitest";
import { COMPOSITION_GRACE_MS, composingKey, type KeyContext } from "./ime";

const key = (patch: Partial<KeyContext> = {}): KeyContext => ({
  isComposing: false,
  keyCode: 13,
  composing: false,
  composedAt: null,
  now: 1000,
  ...patch,
});

describe("composingKey", () => {
  it("leaves an ordinary Enter to the composer", () => {
    expect(composingKey(key())).toBe(false);
  });

  it("claims the Enter Chrome and Firefox send before compositionend", () => {
    expect(composingKey(key({ isComposing: true, keyCode: 229, composing: true }))).toBe(true);
  });

  it("claims the Enter WebKit sends after compositionend with keyCode 229", () => {
    expect(composingKey(key({ keyCode: 229, composedAt: 995 }))).toBe(true);
  });

  it("claims a plain 13 that lands within the grace window after compositionend", () => {
    expect(composingKey(key({ composedAt: 1000 - COMPOSITION_GRACE_MS + 1 }))).toBe(true);
  });

  it("releases the next deliberate Enter after the candidate is confirmed", () => {
    expect(composingKey(key({ composedAt: 1000 - COMPOSITION_GRACE_MS }))).toBe(false);
  });
});
