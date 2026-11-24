// Self-drawn context menu. Rendered into a portal so the rail's scroll mask cannot clip it, and
// kept inside the window bounds. Keys: up/down move, Enter or space picks, right opens a submenu,
// left closes it, Esc closes everything. The owner restores focus when it clears the menu.

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Icon, type IconName } from "./Icon";

export interface MenuItem {
  label: string;
  icon?: IconName;
  onSelect?: () => void;
  submenu?: MenuItem[];
  danger?: boolean;
  disabled?: boolean;
}

export function ContextMenu({
  x,
  y,
  items,
  open = true,
  above = false,
  onClose,
}: {
  x: number;
  y: number;
  items: MenuItem[];
  /** False while the owner keeps the panel mounted for its closing fade. */
  open?: boolean;
  /** Grow upward from (x, y): the panel's bottom edge sits at y, e.g. a menu over a button. */
  above?: boolean;
  onClose: () => void;
}) {
  return createPortal(<MenuPanel items={items} x={x} y={y} above={above} open={open} onClose={onClose} />, document.body);
}

const EDGE = 8;
const SUBMENU_GAP = 4;

/** Whether the page has a non-empty text selection (what a copy item acts on). */
function hasSelection(): boolean {
  const selection = window.getSelection();
  return !!selection && !selection.isCollapsed && selection.toString().trim() !== "";
}

function MenuPanel({
  items,
  x,
  y,
  above = false,
  open = true,
  onClose,
  onBack,
}: {
  items: MenuItem[];
  x: number;
  y: number;
  above?: boolean;
  /** False while the owner keeps the panel mounted for its closing fade. */
  open?: boolean;
  /** Closes the whole chain. */
  onClose: () => void;
  /** Closes this submenu only and returns to its parent item. */
  onBack?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const [left, setLeft] = useState(x);
  const [top, setTop] = useState(y);
  // Which corner the panel grew out of: the one nearest the point it was opened at.
  const [origin, setOrigin] = useState({ x: 0, y: 0 });
  const [submenu, setSubmenu] = useState<{ index: number; x: number; y: number } | null>(null);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const wantTop = above ? y - rect.height : y;
    const nextLeft = Math.max(EDGE, Math.min(x, window.innerWidth - rect.width - EDGE));
    const nextTop = Math.max(EDGE, Math.min(wantTop, window.innerHeight - rect.height - EDGE));
    setLeft(nextLeft);
    setTop(nextTop);
    setOrigin({ x: nextLeft < x ? 100 : 0, y: above || nextTop < y ? 100 : 0 });
  }, [x, y, above, items]);

  // A menu opens focused, so the arrow keys work without a click first. A replacement menu
  // (same panel, new items) starts collapsed and focused again. Opened over selected text
  // (copy the selection), it leaves focus where it is: WebKit — the macOS desktop window —
  // clears the page's selection as soon as a button takes focus, so the highlight would vanish
  // under the menu. The first arrow key then moves focus in; the selection was already read.
  const [holdsSelection] = useState(() => !onBack && hasSelection());
  useEffect(() => {
    setSubmenu(null);
    if (holdsSelection) return;
    buttons.current.find((button) => button && !button.disabled)?.focus();
  }, [items, holdsSelection]);

  useEffect(() => {
    if (!holdsSelection || !open) return;
    const onKey = (event: globalThis.KeyboardEvent) => {
      // Once a menu item has focus the panel's own handler owns the keys.
      if (ref.current?.contains(document.activeElement)) return;
      if (event.key === "Escape" || event.key === "Tab") {
        event.preventDefault();
        onClose();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
        event.preventDefault();
        const list = enabled();
        (event.key === "ArrowUp" || event.key === "End" ? list.at(-1) : list[0])?.focus();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [holdsSelection, open, onClose]);

  useEffect(() => {
    if (onBack) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose();
    };
    const close = () => onClose();
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [onBack, onClose]);

  const enabled = () => buttons.current.filter((button): button is HTMLButtonElement => !!button && !button.disabled);

  const step = (direction: number) => {
    const list = enabled();
    const at = list.indexOf(document.activeElement as HTMLButtonElement);
    list[(at + direction + list.length) % list.length]?.focus();
  };

  const openSubmenu = (index: number) => {
    const button = buttons.current[index];
    const entries = items[index]?.submenu;
    if (!button || !entries?.length) return;
    const rect = button.getBoundingClientRect();
    setSubmenu({ index, x: rect.right + SUBMENU_GAP, y: rect.top });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!open) return;
    // A submenu is nested in this panel's DOM, so its keys must not reach the parent twice.
    event.stopPropagation();
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        step(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        step(-1);
        return;
      case "Home":
        event.preventDefault();
        enabled()[0]?.focus();
        return;
      case "End":
        event.preventDefault();
        enabled().at(-1)?.focus();
        return;
      case "ArrowRight": {
        const index = buttons.current.findIndex((button) => button === document.activeElement);
        if (index >= 0) {
          event.preventDefault();
          openSubmenu(index);
        }
        return;
      }
      case "ArrowLeft":
        if (onBack) {
          event.preventDefault();
          onBack();
        }
        return;
      case "Escape":
      case "Tab":
        event.preventDefault();
        onClose();
        return;
      default:
    }
  };

  return (
    <div
      ref={ref}
      className={open ? "menu" : "menu closing"}
      role="menu"
      style={{ left, top, transformOrigin: `${origin.x}% ${origin.y}%` }}
      onKeyDown={onKeyDown}
      // Pressing an item must not take focus or start a new selection: either would clear the
      // text the menu was opened over before its item reads it.
      onMouseDown={(event) => event.preventDefault()}
    >
      {items.map((item, index) => (
        <button
          key={`${index}-${item.label}`}
          ref={(element) => {
            buttons.current[index] = element;
          }}
          type="button"
          role="menuitem"
          className={item.danger ? "menu-item danger" : "menu-item"}
          disabled={item.disabled}
          aria-haspopup={item.submenu ? "menu" : undefined}
          aria-expanded={item.submenu ? submenu?.index === index : undefined}
          tabIndex={-1}
          onPointerEnter={() => (item.submenu ? openSubmenu(index) : setSubmenu(null))}
          onClick={() => {
            if (item.submenu) openSubmenu(index);
            else if (item.onSelect) {
              item.onSelect();
              onClose();
            } else onClose();
          }}
        >
          {item.icon && <Icon name={item.icon} size={16} />}
          <span>{item.label}</span>
          {item.submenu && <Icon name="chevron" size={14} />}
        </button>
      ))}
      {submenu && (
        <MenuPanel
          key={submenu.index}
          items={items[submenu.index]?.submenu ?? []}
          x={submenu.x}
          y={submenu.y}
          onClose={onClose}
          onBack={() => {
            setSubmenu(null);
            buttons.current[submenu.index]?.focus();
          }}
        />
      )}
    </div>
  );
}
