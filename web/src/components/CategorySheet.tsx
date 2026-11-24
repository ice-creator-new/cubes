// The category sheet 设置 and a member's editor share: the categories down the left (or in a
// strip on top), the chosen one's panel beside them. A long sheet stays findable without
// scrolling past everything else, and both sheets remember where the user left off.

import { useEffect, useRef, useState } from "react";
import { Icon, type IconName } from "./Icon";

export type Category<Id extends string = string> = {
  id: Id;
  label: string;
  icon: IconName;
  /** The one grey line under the category's title. */
  description: string;
  /** Unsaved edits in this category: its nav item carries a dot. */
  unsaved?: boolean;
};

/** True while the categories run down the left; narrow windows put them in a strip on top. */
function useSideBySide() {
  const [sideBySide, setSideBySide] = useState(
    () => !window.matchMedia("(max-width: 720px)").matches,
  );
  useEffect(() => {
    // The same breakpoint as the `.settings` media query in styles.css.
    const media = window.matchMedia("(max-width: 720px)");
    const listen = () => setSideBySide(!media.matches);
    media.addEventListener("change", listen);
    return () => media.removeEventListener("change", listen);
  }, []);
  return sideBySide;
}

/**
 * The categories of one sheet: the chosen category, its nav list and the strip/column switch.
 * `initial` forces the first category (a member editor opened on its delete step) and is not
 * written back, so the sheet still reopens where the user left it.
 */
function useCategoryNav<Id extends string>({
  ids,
  storageKey,
  initial,
}: {
  ids: readonly Id[];
  /** Per sheet, so 设置 and each member keep a category of their own. */
  storageKey: string;
  initial?: Id;
}) {
  const forced = useRef(initial && ids.includes(initial) ? initial : undefined).current;
  const [id, setId] = useState<Id>(() => {
    if (forced) return forced;
    // A stored category the sheet no longer has (a removed category) starts from the first.
    const stored = localStorage.getItem(storageKey) as Id | null;
    return stored && ids.includes(stored) ? stored : ids[0]!;
  });
  const nav = useRef<HTMLDivElement>(null);
  const sideBySide = useSideBySide();

  useEffect(() => {
    if (id !== forced) localStorage.setItem(storageKey, id);
    // A new category starts at its first section, not wherever the last one was scrolled to.
    nav.current?.closest(".dialog-body")?.scrollTo(0, 0);
    // Narrow windows scroll the strip sideways: the chosen tab must stay in sight.
    nav.current
      ?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [id, forced, storageKey]);

  // Arrow keys walk the list like a tab strip: the focused tab follows the selection.
  const onKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step =
      event.key === "ArrowDown" || event.key === "ArrowRight"
        ? 1
        : event.key === "ArrowUp" || event.key === "ArrowLeft"
          ? -1
          : 0;
    if (step === 0 && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const at = ids.indexOf(id);
    const next =
      event.key === "Home" ? 0 : event.key === "End" ? ids.length - 1 : (at + step + ids.length) % ids.length;
    setId(ids[next]!);
    nav.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
  };

  return { id, setId, nav, sideBySide, onKey };
}

export function CategorySheet<Id extends string>({
  navLabel,
  idPrefix,
  categories,
  storageKey,
  initial,
  children,
}: {
  /** What the tab list is: 「设置类别」, 「成员面板」. */
  navLabel: string;
  /** Tab and panel ids: `${idPrefix}-tab-${id}`. */
  idPrefix: string;
  categories: readonly Category<Id>[];
  storageKey: string;
  initial?: Id;
  children: (active: Category<Id>) => React.ReactNode;
}) {
  const { id, setId, nav, sideBySide, onKey } = useCategoryNav({
    ids: categories.map((choice) => choice.id),
    storageKey,
    initial,
  });
  // The first category is always there, so the fallback is never used in practice.
  const active = categories.find((choice) => choice.id === id) ?? categories[0]!;

  return (
    <div className="settings settings-categories">
      <div
        className="settings-nav"
        role="tablist"
        aria-label={navLabel}
        aria-orientation={sideBySide ? "vertical" : "horizontal"}
        ref={nav}
        onKeyDown={onKey}
      >
        {categories.map((choice) => (
          <button
            key={choice.id}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${choice.id}`}
            aria-selected={choice.id === active.id}
            aria-controls={`${idPrefix}-panel-${choice.id}`}
            // The dot is only a shape: the name says what it means.
            aria-label={choice.unsaved ? `${choice.label}，有未保存的改动` : undefined}
            tabIndex={choice.id === active.id ? 0 : -1}
            className={choice.id === active.id ? "settings-nav-item on" : "settings-nav-item"}
            onClick={() => setId(choice.id)}
          >
            <Icon name={choice.icon} size={16} />
            {choice.label}
            {choice.unsaved && <span className="settings-nav-dot" aria-hidden="true" />}
          </button>
        ))}
      </div>

      <div
        className="settings-panel"
        role="tabpanel"
        id={`${idPrefix}-panel-${active.id}`}
        aria-labelledby={`${idPrefix}-tab-${active.id}`}
      >
        <header className="settings-category-head">
          <h3>{active.label}</h3>
          <p>{active.description}</p>
        </header>
        {children(active)}
      </div>
    </div>
  );
}
