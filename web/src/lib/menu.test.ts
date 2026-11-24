import { describe, expect, it } from "vitest";
import shell from "../../electron/menu.cjs?raw";
import { MENU_COMMANDS } from "./menu";

// The shell's menu sends ids the page must know; a renamed id on either side would leave a menu
// item that silently does nothing.
describe("the native menu", () => {
  it("sends exactly the commands the page handles", () => {
    const sent = [...shell.matchAll(/\["([a-z-]+)", "/g)].map((match) => match[1]);
    expect([...sent].sort()).toEqual([...MENU_COMMANDS].sort());
  });
});
