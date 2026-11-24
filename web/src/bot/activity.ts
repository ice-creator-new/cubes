// One app-wide clock of "something happened". The avatar loop reads it to decide when a bot
// has been idle long enough to fall asleep; the store and the pointer feed it. Kept out of
// React on purpose: an avatar must not re-render to notice the room went quiet.

/** Seconds of silence before a visible bot nods off. */
export const SLEEP_AFTER = 120;

let lastAt = Date.now();

export function noteActivity(): void {
  lastAt = Date.now();
}

export function idleSeconds(): number {
  return (Date.now() - lastAt) / 1000;
}
