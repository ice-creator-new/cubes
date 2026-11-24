// Syntax highlighting for fenced code in replies: a rehype pass over `<pre><code class=
// "language-…">`, coloured by highlight.js through lowlight. Only the languages chats use are
// registered, so the bundle carries 17 grammars instead of highlight.js's 37 defaults.
//
// Code with no language, a plain-text language, or one that is not registered is left as is.

import bash from "highlight.js/lib/languages/bash";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import dart from "highlight.js/lib/languages/dart";
import diff from "highlight.js/lib/languages/diff";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import type { Element, ElementContent, Root, RootContent } from "hast";
import { createLowlight } from "lowlight";

const lowlight = createLowlight({ bash, cpp, css, dart, diff, go, java, javascript, json, kotlin, python, rust, sql, swift, typescript, xml, yaml });
lowlight.registerAlias({
  bash: ["sh", "shell", "zsh", "console"],
  cpp: ["c", "cc", "h", "hpp"],
  javascript: ["js", "jsx", "mjs"],
  typescript: ["ts", "tsx"],
  python: ["py"],
  rust: ["rs"],
  kotlin: ["kt", "kts"],
  xml: ["html", "svg", "vue"],
  yaml: ["yml"],
});

/** The fence's language, from the `language-…` class Markdown put on the `<code>`. */
function languageOf(code: Element): string | null {
  const names = code.properties.className;
  if (!Array.isArray(names)) return null;
  for (const name of names) {
    if (typeof name === "string" && name.startsWith("language-")) return name.slice("language-".length);
  }
  return null;
}

/** The code's own text: highlighting replaces children, so it must start from plain text. */
function textOf(node: Element | ElementContent): string {
  if (node.type === "text") return node.value;
  if (node.type === "element") return node.children.map(textOf).join("");
  return "";
}

function highlightIn(node: Root | Element): void {
  for (const child of node.children as RootContent[]) {
    if (child.type !== "element") continue;
    if (child.tagName === "pre") {
      const code = child.children.find((item): item is Element => item.type === "element" && item.tagName === "code");
      const language = code ? languageOf(code) : null;
      if (code && language && lowlight.registered(language)) {
        const tree = lowlight.highlight(language, textOf(code));
        code.children = tree.children as ElementContent[];
        const names = Array.isArray(code.properties.className) ? code.properties.className : [];
        code.properties.className = names.includes("hljs") ? names : ["hljs", ...names];
      }
      continue;
    }
    highlightIn(child);
  }
}

/** A rehype plugin: colour every fenced block whose language is registered. */
export function rehypeHighlightCode() {
  return (tree: Root) => highlightIn(tree);
}
