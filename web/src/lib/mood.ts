import type { MoodTag } from "../bot/catalog";

/** What a member is doing right now, as its face shows it. */
export type Mood = "idle" | "thinking" | "speaking" | "error" | MoodTag;
