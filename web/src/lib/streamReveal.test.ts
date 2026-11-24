import { describe, expect, it } from "vitest";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkRehype from "remark-rehype";
import remarkMath from "remark-math";
import rehypeKatex, { type Options as KatexOptions } from "rehype-katex";
import {
  FADE_MS,
  batchesWithin,
  liveBatches,
  nextVisible,
  rehypeFadeIn,
  rehypeMentions,
  splitBlocks,
  type Batch,
  type HElement,
  type HRoot,
} from "./streamReveal";

const KATEX: KatexOptions = { strict: "ignore" };

/** The hast tree the reply renders from, after both plugins, as plain data. */
function render(source: string, batches: Batch[], names: string[] = [], now = 1000): HRoot {
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkMath)
    .use(remarkRehype)
    .use(rehypeKatex, KATEX)
    .use(rehypeMentions, { names: new Set(names) })
    .use(rehypeFadeIn, { batches, now: () => now });
  return processor.runSync(processor.parse(source)) as unknown as HRoot;
}

function fades(node: HRoot | HElement): { text: string; delay: string }[] {
  const out: { text: string; delay: string }[] = [];
  for (const child of node.children) {
    if (child.type !== "element") continue;
    const element = child as HElement;
    const classes = element.properties.className;
    if (Array.isArray(classes) && classes.includes("fade-in")) {
      const text = element.children.map((piece) => ("value" in piece ? piece.value : "")).join("");
      out.push({ text, delay: String(element.properties.style) });
    } else {
      out.push(...fades(element));
    }
  }
  return out;
}

function visibleText(node: HRoot | HElement): string {
  return node.children
    .map((child) => (child.type === "text" ? child.value : child.type === "element" ? visibleText(child as HElement) : ""))
    .join("");
}

describe("pacing", () => {
  it("spreads a burst over frames and still catches up with a long backlog", () => {
    const burst = "一".repeat(400);
    let shown = 0;
    let frames = 0;
    while (shown < burst.length) {
      const next = nextVisible(burst, shown, 1 / 60);
      expect(next).toBeGreaterThan(shown);
      // Never the whole burst at once: the first frames show a handful of characters.
      if (frames === 0) expect(next).toBeLessThan(40);
      shown = next;
      frames += 1;
    }
    // 400 characters land within about a second at 60 fps, not over many seconds.
    expect(frames).toBeGreaterThan(20);
    expect(frames).toBeLessThan(75);
  });

  it("caps a long frame gap, so a window coming back does not dump the backlog", () => {
    expect(nextVisible("x".repeat(1000), 0, 5)).toBe(nextVisible("x".repeat(1000), 0, 0.1));
    expect(nextVisible("abc", 3, 0.016)).toBe(3);
  });

  it("never splits a surrogate pair", () => {
    const text = "a😀b";
    for (let shown = 0; shown < text.length; shown += 1) {
      const next = nextVisible(text, shown, 0.001);
      const code = text.charCodeAt(next - 1);
      expect(code >= 0xd800 && code <= 0xdbff, `cut at ${next}`).toBe(false);
    }
  });
});

describe("blocks", () => {
  it("concatenates back to the source and keeps earlier boundaries as text is appended", () => {
    const full = "第一段，**加粗**。\n\n## 标题\n\n```js\nlet a = 1\n\nlet b = 2\n```\n\n- 一\n\n- 二\n\n最后一段";
    let previous: number[] = [];
    for (let length = 1; length <= full.length; length += 1) {
      const blocks = splitBlocks(full.slice(0, length));
      expect(blocks.map((block) => block.source).join("")).toBe(full.slice(0, length));
      const starts = blocks.map((block) => block.start);
      // A boundary, once made, stays: earlier blocks keep their keys while the reply grows.
      for (const start of previous) expect(starts).toContain(start);
      previous = starts;
    }
  });

  it("does not split inside a fence, a loose list or around a reference definition", () => {
    expect(splitBlocks("```\na\n\nb\n```\n").length).toBe(1);
    expect(splitBlocks("- 一\n\n- 二\n").length).toBe(1);
    expect(splitBlocks("见 [文档][d]。\n\n[d]: https://example.com\n").length).toBe(1);
    expect(splitBlocks("一段\n\n二段\n").map((block) => block.source)).toEqual(["一段\n\n", "二段\n"]);
  });

  it("keeps display math whole even with blank lines inside, and splits around it", () => {
    const source = "公式：\n\n$$\na = 1\n\nb = 2\n$$\n\n之后\n";
    expect(splitBlocks(source).map((block) => block.source)).toEqual(["公式：\n\n", "$$\na = 1\n\nb = 2\n$$\n\n", "之后\n"]);
    // One-line display math closes on its own line.
    expect(splitBlocks("$$ x $$\n\n之后\n").map((block) => block.source)).toEqual(["$$ x $$\n\n", "之后\n"]);
  });

  it("maps batches into a block, carrying a straddling batch in at offset 0", () => {
    // Only whole lines split, so the second block needs its newline to exist yet.
    const [first, second] = splitBlocks("一段\n\n二段\n");
    const batches = [
      { at: 1, when: 10 },
      { at: 3, when: 20 },
      { at: 5, when: 30 },
    ];
    expect(batchesWithin(batches, first!)).toEqual([
      { at: 1, when: 10 },
      { at: 3, when: 20 },
    ]);
    // The second block starts at 4, inside the batch that began at 3.
    expect(batchesWithin(batches, second!)).toEqual([
      { at: 0, when: 20 },
      { at: 1, when: 30 },
    ]);
  });
});

describe("fade", () => {
  it("leaves settled text plain and fades each batch on its own clock", () => {
    const tree = render("你好，世界", [
      { at: 2, when: 900 },
      { at: 4, when: 990 },
    ]);
    expect(visibleText(tree)).toBe("你好，世界");
    expect(fades(tree)).toEqual([
      { text: "，世", delay: "animation-delay:-100ms" },
      { text: "界", delay: "animation-delay:-10ms" },
    ]);
  });

  it("keeps a batch's clock when Markdown restructures the line around it", () => {
    // Before the closing `**` arrives the stars are text; after, they form <strong>. The batch
    // that revealed 加粗 keeps its timestamp either way, so its fade resumes rather than restarts.
    const batches = [{ at: 2, when: 950 }];
    const open = fades(render("前文**加粗", batches));
    const closed = fades(render("前文**加粗**", batches));
    expect(open.every((piece) => piece.delay === "animation-delay:-50ms")).toBe(true);
    expect(closed).toContainEqual({ text: "加粗", delay: "animation-delay:-50ms" });
  });

  it("does not fade code, so a closing backtick cannot make shown text flash", () => {
    const tree = render("看 `x = 1` 这里", [{ at: 0, when: 990 }]);
    expect(visibleText(tree)).toBe("看 x = 1 这里");
    expect(fades(tree).map((piece) => piece.text)).toEqual(["看 ", " 这里"]);
  });

  it("does not fade or chip inside typeset math, so a formula is never split apart", () => {
    const tree = render("面积 $S = \\pi r^2$ 结束，@甲", [{ at: 0, when: 990 }], ["甲"]);
    expect(fades(tree).map((piece) => piece.text)).toEqual(["面积 ", " 结束，", "@", "甲"]);
    const paragraph = tree.children[0] as HElement;
    const math = paragraph.children.find(
      (child) => child.type === "element" && ((child as HElement).properties.className as string[] | undefined)?.includes("katex"),
    );
    expect(math).toBeDefined();
  });

  it("turns a member's name into a chip that fades with its batch", () => {
    const tree = render("请 @甲 看看，@路人 不算", [{ at: 0, when: 990 }], ["甲"]);
    const paragraph = tree.children[0] as HElement;
    const chip = paragraph.children.find((child) => child.type === "element" && (child as HElement).tagName === "button") as HElement;
    expect(chip.properties.dataMention).toBe("甲");
    expect(visibleText(chip)).toBe("@甲");
    expect(fades(chip).map((piece) => piece.text).join("")).toBe("@甲");
    expect(visibleText(tree)).toBe("请 @甲 看看，@路人 不算");
  });

  it("drops a batch once its fade is over", () => {
    const batches = [
      { at: 0, when: 0 },
      { at: 5, when: FADE_MS - 1 },
    ];
    expect(liveBatches(batches, FADE_MS)).toEqual([{ at: 5, when: FADE_MS - 1 }]);
    expect(liveBatches(batches, FADE_MS * 3)).toEqual([]);
  });
});
