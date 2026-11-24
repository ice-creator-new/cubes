// Enter in the composer sends and picks a mention, but not while an input method (拼音, 注音,
// 仓颉, Japanese, Korean…) is using the key to confirm a candidate. Browsers disagree about
// the order of events:
//
// - Chrome, Firefox: the confirming Enter arrives with `isComposing === true`, before
//   `compositionend`.
// - WebKit (Safari, and the WKWebView our desktop window runs in): `compositionend` fires
//   first, then the Enter arrives with `isComposing === false` and `keyCode === 229`; with
//   some input methods as a plain 13 right after.
//
// So a key belongs to the input method when any of these says so.

/** How long after `compositionend` a key still belongs to the input method. */
export const COMPOSITION_GRACE_MS = 60;

export interface KeyContext {
  /** `KeyboardEvent.isComposing`. */
  isComposing: boolean;
  /** `KeyboardEvent.keyCode`: 229 means "an input method is handling this key". */
  keyCode: number;
  /** Whether a composition is open, tracked from `compositionstart`/`compositionend`. */
  composing: boolean;
  /** `performance.now()` at the last `compositionend`, or null. */
  composedAt: number | null;
  now: number;
}

/** Whether this keydown is the input method's, so the composer must leave it alone. */
export function composingKey(key: KeyContext): boolean {
  if (key.isComposing || key.composing || key.keyCode === 229) return true;
  return key.composedAt !== null && key.now - key.composedAt < COMPOSITION_GRACE_MS;
}
