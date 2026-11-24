// Whether a scroller is at its end. The chat follows new output only while the reader is there,
// and shows the jump-to-latest button only when they are not, so this single decision drives
// both. It is a small tolerance rather than an exact zero on purpose: a scrolling element's
// height is a fractional number while scrollTop is rounded, so a view that is genuinely at the
// end reports a sub-pixel or few-pixel gap — reading that as "the reader left" is what made the
// button appear over the newest message.

/** How close to the end still counts as being at it, in CSS pixels. */
export const BOTTOM_EPS = 8;

/** Whether a scroller showing `scrollTop` of `scrollHeight` in `clientHeight` is at its end. */
export function atBottom(scrollHeight: number, scrollTop: number, clientHeight: number): boolean {
  return scrollHeight - scrollTop - clientHeight <= BOTTOM_EPS;
}
