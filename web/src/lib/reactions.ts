// Pure reaction-list logic: the optimistic toggle the store applies before it asks the server,
// kept separate so its edge cases (new pill, own-increment, own-removal, dropping at zero) are
// tested without touching the store.

import type { Reaction } from "../api/types";

/** The user's actor identity, matching the backend's storage convention. */
export const USER_ACTOR_ID = "user";
export const USER_ACTOR_NAME = "你";

/** Whether the user has already reacted with `emoji`. */
export function hasOwnReaction(reactions: Reaction[] | undefined, emoji: string): boolean {
  return reactions?.some((reaction) => reaction.emoji === emoji && reaction.mine) ?? false;
}

/**
 * Toggle the user's own reaction for `emoji`. Returns a new list, never mutating the input:
 * adding a fresh pill, incrementing and marking an existing one as mine, removing the user's
 * own reaction, or dropping the pill entirely once the user was its last actor.
 */
export function toggleOwnReaction(reactions: Reaction[], emoji: string): Reaction[] {
  const index = reactions.findIndex((reaction) => reaction.emoji === emoji);
  if (index < 0) {
    return [
      ...reactions,
      { emoji, count: 1, mine: true, actors: [{ id: USER_ACTOR_ID, name: USER_ACTOR_NAME }] },
    ];
  }
  const pill = reactions[index]!;
  const own = pill.actors.some((actor) => actor.id === USER_ACTOR_ID);
  if (own) {
    const actors = pill.actors.filter((actor) => actor.id !== USER_ACTOR_ID);
    if (actors.length === 0) return reactions.filter((_, i) => i !== index);
    const next = [...reactions];
    next[index] = { ...pill, count: actors.length, mine: false, actors };
    return next;
  }
  const actors = [...pill.actors, { id: USER_ACTOR_ID, name: USER_ACTOR_NAME }];
  const next = [...reactions];
  next[index] = { ...pill, count: actors.length, mine: true, actors };
  return next;
}
