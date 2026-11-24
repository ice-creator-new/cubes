import { describe, expect, it } from "vitest";
import { normalizeMath } from "./math";

describe("normalizeMath", () => {
  it("turns bracket math into dollar math", () => {
    expect(normalizeMath("面积 \\(S = \\pi r^2\\) 平方米")).toBe("面积 $S = \\pi r^2$ 平方米");
    expect(normalizeMath("公式：\\[ E = mc^2 \\]之后")).toBe("公式：\n$$\nE = mc^2\n$$\n之后");
  });

  it("leaves code exactly as written", () => {
    const fenced = "```latex\n\\[ a \\]\n```\n";
    expect(normalizeMath(fenced)).toBe(fenced);
    expect(normalizeMath("写 `\\(x\\)` 就行，\\(y\\)")).toBe("写 `\\(x\\)` 就行，$y$");
  });

  it("returns text without math untouched", () => {
    const text = "括号 (a) [b]，没有公式";
    expect(normalizeMath(text)).toBe(text);
  });

  it("escapes dollars that are prices, keeps the ones that are formulas", () => {
    expect(normalizeMath("价格 $5 和 $10")).toBe("价格 \\$5 和 \\$10");
    expect(normalizeMath("从 $5 涨到 $x^2$ 元")).toBe("从 \\$5 涨到 $x^2$ 元");
    expect(normalizeMath("$a$ 和 $b$")).toBe("$a$ 和 $b$");
    expect(normalizeMath("花了 $ 20")).toBe("花了 \\$ 20");
  });

  it("never touches dollars in code or display math", () => {
    const source = "命令 `echo $HOME`\n\n```sh\nprice=$5\n```\n\n$$\na = $b\n$$\n";
    expect(normalizeMath(source)).toBe(source);
  });
});
