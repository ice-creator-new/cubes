import { describe, expect, it } from "vitest";
import type { Element, Root } from "hast";
import { rehypeHighlightCode } from "./highlight";

function block(language: string | null, text: string): Root {
  const code: Element = {
    type: "element",
    tagName: "code",
    properties: language ? { className: [`language-${language}`] } : {},
    children: [{ type: "text", value: text }],
  };
  return { type: "root", children: [{ type: "element", tagName: "pre", properties: {}, children: [code] }] };
}

function code(tree: Root): Element {
  const pre = tree.children[0] as Element;
  return pre.children[0] as Element;
}

function classes(node: Element): string[] {
  const names: string[] = [];
  const walk = (item: Element) => {
    const own = item.properties.className;
    if (Array.isArray(own)) names.push(...own.map(String));
    for (const child of item.children) if (child.type === "element") walk(child);
  };
  walk(node);
  return names;
}

function text(node: Element): string {
  return node.children.map((child) => (child.type === "text" ? child.value : child.type === "element" ? text(child) : "")).join("");
}

describe("rehypeHighlightCode", () => {
  it("colours a registered language and keeps the exact source text", () => {
    const source = 'const answer: number = 42; // 注释\nconsole.log("好");';
    const tree = block("ts", source);
    rehypeHighlightCode()(tree);
    expect(classes(code(tree))).toEqual(expect.arrayContaining(["hljs", "language-ts", "hljs-keyword", "hljs-number", "hljs-comment", "hljs-string"]));
    expect(text(code(tree))).toBe(source);
  });

  it("leaves code without a language, a plain one, or an unknown one untouched", () => {
    for (const language of [null, "text", "brainfuck"]) {
      const tree = block(language, "const x = 1");
      rehypeHighlightCode()(tree);
      expect(code(tree).children).toEqual([{ type: "text", value: "const x = 1" }]);
    }
  });

  it("does not touch inline code outside a pre", () => {
    const tree: Root = {
      type: "root",
      children: [{ type: "element", tagName: "p", properties: {}, children: [{ type: "element", tagName: "code", properties: { className: ["language-ts"] }, children: [{ type: "text", value: "let a" }] }] }],
    };
    rehypeHighlightCode()(tree);
    const inline = (tree.children[0] as Element).children[0] as Element;
    expect(inline.children).toEqual([{ type: "text", value: "let a" }]);
  });

  it("is stable when it runs again on an already coloured tree", () => {
    const tree = block("python", "def f():\n    return 1");
    rehypeHighlightCode()(tree);
    const once = JSON.stringify(code(tree));
    rehypeHighlightCode()(tree);
    expect(JSON.stringify(code(tree))).toBe(once);
    expect(text(code(tree))).toBe("def f():\n    return 1");
  });
});
