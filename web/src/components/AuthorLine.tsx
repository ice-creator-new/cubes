// The one line that names who is speaking: a face, a name, and a quiet note about what it is
// doing. Its own file so the activity card can draw the same line over a bot's work.

import type { Agent } from "../api/types";
import type { Mood } from "../lib/mood";
import { Avatar } from "./Avatar";

export function AuthorLine({
  agent,
  fallbackName,
  fallbackSeed,
  mood,
  note,
  onOpen,
}: {
  agent: Agent | undefined;
  fallbackName?: string;
  fallbackSeed?: string | null;
  mood: Mood;
  note?: string;
  onOpen?: () => void;
}) {
  const name = agent?.name ?? fallbackName ?? "成员";
  const content = (
    <>
      <Avatar
        seed={agent?.avatar_seed ?? fallbackSeed}
        appearance={agent?.appearance}
        size={24}
        name={name}
        mood={mood}
      />
      <span className="author-name">{name}</span>
      {note && <span className="author-note">{note}</span>}
    </>
  );
  return onOpen ? (
    <button type="button" className="author-line" onClick={onOpen}>
      {content}
    </button>
  ) : (
    <div className="author-line">{content}</div>
  );
}
