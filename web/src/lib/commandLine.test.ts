import { describe, expect, it } from "vitest";
import { commandRow } from "./commandLine";

describe("commandRow", () => {
  it("keeps the end of a long command beside its status", () => {
    const command = "cd ~/projects/doc2sheet && flutter build web --release 2>&1 | tail -6";
    const row = commandRow("运行「cd ~/projects/doc…2>&1 | tail -6」，完成", command, 12)!;
    // The last 12 characters of the command, then the status: the part that never shrinks.
    expect(row.tail).toBe("&1 | tail -6」，完成");
    expect(row.head).toBe("运行「cd ~/projects/doc2sheet && flutter build web --release 2>");
  });

  it("joins the two halves back into the whole sentence", () => {
    const command = "python3 -m http.server 8791 --directory build/web";
    const row = commandRow("运行「python3 -m http.server 8791 --directory build/web」，退出码 1", command, 10)!;
    expect(row.head + row.tail).toBe("运行「python3 -m http.server 8791 --directory build/web」，退出码 1");
    expect(row.tail).toBe(" build/web」，退出码 1");
  });

  it("collapses a multi-line command to one line", () => {
    const row = commandRow("运行「cat <<'EOF' …」，完成", "cat <<'EOF'\nhello\nEOF", 40)!;
    expect(row.head + row.tail).toBe("运行「cat <<'EOF' hello EOF」，完成");
  });

  it("puts a short command entirely in the tail", () => {
    expect(commandRow("运行「ls」，完成", "ls")).toEqual({ head: "运行「", tail: "ls」，完成" });
  });

  it("falls back when the summary is not a command sentence", () => {
    expect(commandRow("看了一眼屏幕", "ls")).toBeNull();
    expect(commandRow("运行「ls」，完成", "   ")).toBeNull();
  });
});
