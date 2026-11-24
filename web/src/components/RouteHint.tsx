// The faint line under a routed user message in a group, and the small popover that re-answers
// it with other members. The line alone is the normal state; the popover is where the user
// overrides what the invisible dispatcher decided.

import { useEffect, useRef, useState } from "react";
import type { Agent, Message } from "../api/types";
import { usePresence } from "../lib/motion";
import { routeHint } from "../lib/routeHint";
import { store } from "../state/store";
import { Avatar } from "./Avatar";

export function RouteHint({
  message,
  conversationID,
  members,
  taskTitleOf,
}: {
  message: Message;
  conversationID: string;
  /** The group's members, in roster order, for the popover's checkboxes. */
  members: Agent[];
  taskTitleOf: (taskID: string) => string | undefined;
}) {
  const route = message.route;
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState<Set<string>>(() => new Set());
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const shell = useRef<HTMLSpanElement>(null);
  const popover = usePresence(open ? true : null, 140);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!shell.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (!route) return null;
  const hint = routeHint(
    route,
    (id) => members.find((agent) => agent.id === id)?.name,
    taskTitleOf,
  );
  const openPopover = () => {
    setChosen(new Set(route.speakers));
    setError(null);
    setOpen(true);
  };
  const toggle = (id: string) => {
    setChosen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  // The chosen order follows the roster, not the clicks: the dispatcher's speakers read the same.
  const speakers = members.filter((agent) => chosen.has(agent.id)).map((agent) => agent.id);
  const reroute = async () => {
    if (speakers.length === 0 || sending) return;
    setSending(true);
    setError(null);
    const failure = await store.rerouteMessage(conversationID, message.id, speakers);
    setSending(false);
    if (failure) setError(failure);
    else setOpen(false);
  };

  return (
    <span className="route-hint-shell" ref={shell}>
      <button
        type="button"
        className={`route-hint ${route.kind}`}
        title={hint.title}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`${hint.text}。${hint.title}。点击改派`}
        onClick={() => (open ? setOpen(false) : openPopover())}
      >
        {hint.text}
      </button>
      {popover.value && (
        <div className={popover.open ? "route-pop" : "route-pop closing"} role="dialog" aria-label="改派这条消息">
          <div className="route-pop-head">谁来回答</div>
          <ul className="route-pop-list">
            {members.map((agent) => {
              const on = chosen.has(agent.id);
              return (
                <li key={agent.id}>
                  <label className={on ? "route-person on" : "route-person"}>
                    <input type="checkbox" checked={on} onChange={() => toggle(agent.id)} />
                    <Avatar
                      seed={agent.avatar_seed}
                      appearance={agent.appearance}
                      size={20}
                      name={agent.name}
                      agentID={agent.id}
                    />
                    <span>{agent.name}</span>
                  </label>
                </li>
              );
            })}
          </ul>
          <div className="route-pop-foot">
            <span className="hint">
              {route.kind === "answer" ? "换几个人重新回答。" : "选人后改成让别人来接这条。"}
            </span>
            <button
              type="button"
              className="primary-button route-pop-go"
              disabled={sending || speakers.length === 0}
              onClick={() => void reroute()}
            >
              {sending ? "派发中…" : "重新派发"}
            </button>
          </div>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </span>
  );
}
