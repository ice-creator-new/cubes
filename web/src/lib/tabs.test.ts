import { describe, expect, it } from "vitest";
import {
  EMPTY,
  activate,
  activeTab,
  closeOthers,
  closeRight,
  closeTab,
  closeWhere,
  cycle,
  describeRun,
  moveTab,
  openTab,
  reopen,
  restore,
  runTab,
  serialize,
  setPinned,
  tabKey,
} from "./tabs";

const keys = (state: { tabs: { kind: string; id?: string }[] }) => state.tabs.map((tab) => tabKey(tab as never));

describe("opening tabs", () => {
  it("opens a new tab right after the shown one, and only switches to one already open", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "sup" });
    state = openTab(state, { kind: "chat", id: "dev" });
    state = activate(state, "chat:sup");
    state = openTab(state, { kind: "team" }); // lands next to 主管, not at the end
    expect(keys(state)).toEqual(["chat:sup", "team", "chat:dev"]);
    expect(state.active).toBe("team");
    state = openTab(state, { kind: "chat", id: "dev" });
    expect(keys(state)).toEqual(["chat:sup", "team", "chat:dev"]);
    expect(state.active).toBe("chat:dev");
  });

  it("keys pages by what they show, so two bots get two chat tabs and two editor tabs", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "a" });
    state = openTab(state, { kind: "member", id: "a" });
    state = openTab(state, { kind: "member", id: "b" });
    state = openTab(state, { kind: "member", id: "a", confirmDelete: true });
    expect(keys(state)).toEqual(["chat:a", "member:a", "member:b"]);
    expect(activeTab(state)).toEqual({ kind: "member", id: "a", confirmDelete: true });
  });
});

describe("closing tabs", () => {
  it("goes back to the tab it was opened from when that is still open", () => {
    let state = openTab(EMPTY, { kind: "team" });
    state = openTab(state, { kind: "usage" });
    state = activate(state, "team");
    state = openTab(state, { kind: "member", id: "a" }); // opened from 团队
    state = closeTab(state, "member:a");
    expect(state.active).toBe("team");
  });

  it("falls back to the right neighbour, then the left, then nothing", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "a" });
    state = openTab(state, { kind: "chat", id: "b" });
    state = openTab(state, { kind: "chat", id: "c" });
    state = closeTab(state, "chat:a");
    state = activate(state, "chat:b");
    state = closeTab(state, "chat:b"); // its opener is gone → right neighbour
    expect(state.active).toBe("chat:c");
    state = closeTab(state, "chat:c");
    expect(state).toMatchObject({ tabs: [], active: null, pinned: [] });
  });

  it("closing a background tab keeps the shown one", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "a" });
    state = openTab(state, { kind: "usage" });
    state = closeTab(state, "chat:a");
    expect(state.active).toBe("usage");
    expect(keys(state)).toEqual(["usage"]);
  });

  it("drops a deleted bot's chat and editor together", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "a" });
    state = openTab(state, { kind: "team" });
    state = openTab(state, { kind: "chat", id: "b" });
    state = openTab(state, { kind: "member", id: "b" });
    state = closeWhere(state, (tab) => "id" in tab && tab.id === "b");
    expect(keys(state)).toEqual(["chat:a", "team"]);
    expect(state.active).toBe("team");
  });
});

describe("switching and restoring", () => {
  it("cycles through the tabs, wrapping both ways", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "a" });
    state = openTab(state, { kind: "usage" });
    expect(cycle(state, 1).active).toBe("chat:a");
    expect(cycle(state, -1).active).toBe("chat:a");
    expect(cycle(EMPTY, 1)).toBe(EMPTY);
  });

  it("drags a tab to another slot, clamped, keeping the shown tab", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "a" });
    state = openTab(state, { kind: "chat", id: "b" });
    state = openTab(state, { kind: "chat", id: "c" });
    expect(keys(moveTab(state, "chat:c", 0))).toEqual(["chat:c", "chat:a", "chat:b"]);
    expect(keys(moveTab(state, "chat:a", 99))).toEqual(["chat:b", "chat:c", "chat:a"]);
    expect(moveTab(state, "chat:a", 0)).toBe(state);
    expect(moveTab(state, "nope", 1)).toBe(state);
    expect(moveTab(state, "chat:a", 2).active).toBe("chat:c");
  });

  it("restores the same tabs without one-off flags; never-stored differs from all-closed", () => {
    let state = openTab(EMPTY, { kind: "chat", id: "a" });
    state = openTab(state, { kind: "member", id: "a", confirmDelete: true });
    const back = restore(serialize(state))!;
    expect(back.tabs).toEqual([{ kind: "chat", id: "a" }, { kind: "member", id: "a" }]);
    expect(back.active).toBe("member:a");
    expect(restore(null)).toBeNull();
    expect(restore(serialize(EMPTY))).toEqual(EMPTY);
    expect(restore("{nope")).toBeNull();
    expect(
      restore(JSON.stringify({ tabs: [{ kind: "rm -rf" }, { kind: "chat" }, { kind: "team" }, { kind: "team" }], active: "x" })),
    ).toEqual({ tabs: [{ kind: "team" }], active: "team", openers: {}, pinned: [], closed: [] });
  });

  it("a run tab keeps the chat it ran in and its start across a reload", () => {
    const state = openTab(EMPTY, runTab({ id: "r1", conversation: "c1", at: "2026-10-29T06:00:00Z" }));
    expect(restore(serialize(state))!.tabs).toEqual([
      { kind: "run", id: "r1", conversation: "c1", at: "2026-10-29T06:00:00Z" },
    ]);
  });

  it("a run tab stored without its chat is filled in once the trace reads it, and only that tab", () => {
    let state = restore(JSON.stringify({ tabs: [{ kind: "chat", id: "c1" }, { kind: "run", id: "r1" }], active: "chat:c1" }))!;
    expect(state.tabs[1]).toEqual({ kind: "run", id: "r1" });
    const run = { id: "r1", conversation: "c1", at: "2026-10-29T06:00:00Z" };
    state = describeRun(state, run);
    expect(state.tabs).toEqual([{ kind: "chat", id: "c1" }, runTab(run)]);
    expect(state.active).toBe("chat:c1");
    // Same facts again (the trace polls while running): nothing changes, so nothing re-renders.
    expect(describeRun(state, run)).toBe(state);
    // A run that is not open: untouched.
    expect(describeRun(state, { ...run, id: "r2" })).toBe(state);
  });
});

describe("pinning", () => {
  const four = () => ["a", "b", "c", "d"].reduce((state, id) => openTab(state, { kind: "chat", id }), EMPTY);

  it("pins to the left in pin order, and unpins to just after the pins", () => {
    let state = setPinned(four(), "chat:c", true);
    state = setPinned(state, "chat:a", true);
    expect(keys(state)).toEqual(["chat:c", "chat:a", "chat:b", "chat:d"]);
    state = setPinned(state, "chat:c", false);
    expect(keys(state)).toEqual(["chat:a", "chat:c", "chat:b", "chat:d"]);
    expect(state.pinned).toEqual(["chat:a"]);
  });

  it("never opens or drags a tab into the pinned group, nor a pin out of it", () => {
    let state = setPinned(four(), "chat:a", true);
    state = setPinned(state, "chat:b", true);
    state = activate(state, "chat:a");
    state = openTab(state, { kind: "team" });
    expect(keys(state)).toEqual(["chat:a", "chat:b", "team", "chat:c", "chat:d"]);
    expect(keys(moveTab(state, "chat:d", 0))).toEqual(["chat:a", "chat:b", "chat:d", "team", "chat:c"]);
    expect(keys(moveTab(state, "chat:a", 9))).toEqual(["chat:b", "chat:a", "team", "chat:c", "chat:d"]);
  });

  it("close others and close right spare pinned tabs, and close others shows the kept tab", () => {
    let state = setPinned(four(), "chat:a", true);
    state = activate(state, "chat:b");
    const others = closeOthers(state, "chat:c");
    expect(keys(others)).toEqual(["chat:a", "chat:c"]);
    expect(others.active).toBe("chat:c");
    const right = closeRight(state, "chat:b");
    expect(keys(right)).toEqual(["chat:a", "chat:b"]);
    expect(right.active).toBe("chat:b");
  });

  it("keeps pins across a reload and leads the strip with them", () => {
    const state = setPinned(four(), "chat:c", true);
    const back = restore(serialize(state))!;
    expect(keys(back)).toEqual(["chat:c", "chat:a", "chat:b", "chat:d"]);
    expect(back.pinned).toEqual(["chat:c"]);
    // A stored pin whose tab is gone is dropped, and an older store without pins still loads.
    expect(restore(JSON.stringify({ tabs: [{ kind: "team" }], pinned: ["chat:x"] }))!.pinned).toEqual([]);
    expect(restore(JSON.stringify({ tabs: [{ kind: "team" }] }))!.pinned).toEqual([]);
  });
});

describe("reopening closed tabs", () => {
  it("brings tabs back newest first, in their old slot, without one-off flags", () => {
    let state = ["a", "b", "c"].reduce((acc, id) => openTab(acc, { kind: "chat", id }), EMPTY);
    state = openTab(state, { kind: "member", id: "m", confirmDelete: true });
    state = closeTab(state, "member:m");
    state = closeTab(state, "chat:b");
    state = reopen(state);
    expect(keys(state)).toEqual(["chat:a", "chat:b", "chat:c"]);
    expect(state.active).toBe("chat:b");
    state = reopen(state);
    expect(activeTab(state)).toEqual({ kind: "member", id: "m" });
    expect(reopen(state)).toBe(state);
  });

  it("does not offer back what was deleted, and skips a closed tab that is open again", () => {
    let state = ["a", "b"].reduce((acc, id) => openTab(acc, { kind: "chat", id }), EMPTY);
    state = closeWhere(state, (tab) => tabKey(tab) === "chat:a");
    expect(state.closed).toEqual([]);
    state = closeTab(state, "chat:b");
    state = openTab(state, { kind: "chat", id: "b" });
    expect(reopen(state)).toBe(state);
  });
});
