// The one panel shell every sheet uses: a head with the title and a close button, a scrolling
// body and an optional sticky action row. Rendered inside a `PaneHost` (the message area) it is a
// pane that takes the conversation's place; anywhere else it is a modal dialog over a scrim. A
// form opened from inside a pane is a dialog again, on top of it.

import { createContext, useContext, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useExitTransition } from "../lib/motion";
import { Icon } from "./Icon";

/** True below the message area's pane slot: the next Dialog renders in place, not as a modal. */
const InPane = createContext(false);

/** Wraps the message area's pane slot: the panel rendered directly inside it becomes a pane. */
export function PaneHost({ children }: { children: React.ReactNode }) {
  return <InPane.Provider value={true}>{children}</InPane.Provider>;
}

/**
 * A form or picker a panel opens beside its own Dialog (adding a connector, editing a
 * schedule): it stays a modal over the panel even though both sit in the pane slot.
 */
export function Modal({ children }: { children: React.ReactNode }) {
  return <InPane.Provider value={false}>{children}</InPane.Provider>;
}

/** Open dialogs, oldest first: Escape belongs to the one on top, never to the sheet under it. */
const DIALOG_STACK: symbol[] = [];

export function Dialog({
  title,
  mark,
  onClose,
  children,
  footer,
  wide,
  className,
  closeOnly = false,
}: {
  title: string;
  /** A small mark before the title: the service a form is about. */
  mark?: React.ReactNode;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  wide?: boolean;
  className?: string;
  /** The footer is only a close button: shown in a modal, left out in a tab (its ✕ closes it). */
  closeOnly?: boolean;
}) {
  const pane = useContext(InPane);
  // Escape, the scrim and the close button share one exit: it fades and drops before it goes.
  const { closing, requestClose } = useExitTransition(onClose, 120);
  const [id] = useState(() => Symbol("dialog"));
  // Only modals join the Escape stack: a tab is left with Escape by the shell, never closed, and
  // several mounted tabs must not fight over the key.
  useEffect(() => {
    if (pane) return;
    DIALOG_STACK.push(id);
    return () => {
      const index = DIALOG_STACK.lastIndexOf(id);
      if (index >= 0) DIALOG_STACK.splice(index, 1);
    };
  }, [id, pane]);
  useEffect(() => {
    if (pane) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || DIALOG_STACK[DIALOG_STACK.length - 1] !== id) return;
      event.preventDefault();
      requestClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [id, pane, requestClose]);
  const classes = ["dialog", pane ? "pane" : "", wide ? "wide" : "", className ?? "", closing ? "closing" : ""]
    .filter(Boolean)
    .join(" ");
  const head = (
    <header className="dialog-head">
      <h2>
        {mark && (
          <span className="dialog-mark" aria-hidden="true">
            {mark}
          </span>
        )}
        {title}
      </h2>
      <button type="button" className="icon-button" onClick={requestClose} aria-label="关闭">
        <Icon name="close" />
      </button>
    </header>
  );
  // In a tab, a footer that only offers to close is dropped: the tab's ✕ closes the page.
  const showFooter = Boolean(footer) && !(pane && closeOnly);
  const body = (
    <div className="dialog-body">
      {children}
      {showFooter && <div className="dialog-actions">{footer}</div>}
    </div>
  );
  if (pane) {
    // Everything opened from inside the pane (a form, a picker) is a modal over it again.
    return (
      <InPane.Provider value={false}>
        <section className={classes} aria-label={title}>
          {head}
          {body}
        </section>
      </InPane.Provider>
    );
  }
  // Rendered on <body>: a dialog opened from inside another must cover the whole window, not
  // the transformed sheet it was opened from.
  return createPortal(
    <div
      className={closing ? "scrim closing" : "scrim"}
      onMouseDown={(event) => event.target === event.currentTarget && requestClose()}
    >
      <div className={classes} role="dialog" aria-modal="true" aria-label={title}>
        {head}
        {body}
      </div>
    </div>,
    document.body,
  );
}
