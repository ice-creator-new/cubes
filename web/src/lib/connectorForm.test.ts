import { describe, expect, it } from "vitest";
import { parseArgs, parseEnv, parseHeaders } from "./connectorForm";

describe("headers", () => {
  it("reads one header per line and skips blank lines", () => {
    expect(parseHeaders("Authorization: Bearer abc\n\n  \nX-Api-Key: k1\n")).toEqual({
      values: { Authorization: "Bearer abc", "X-Api-Key": "k1" },
      error: null,
    });
  });

  it("keeps every colon that follows the first one", () => {
    expect(parseHeaders("X-Origin: https://example.com:8443/a")).toEqual({
      values: { "X-Origin": "https://example.com:8443/a" },
      error: null,
    });
  });

  it("accepts an empty value", () => {
    expect(parseHeaders("X-Empty:")).toEqual({ values: { "X-Empty": "" }, error: null });
  });

  it("names the line that has no colon", () => {
    const parsed = parseHeaders("Authorization: a\nnot-a-header");
    expect(parsed.error).toBe("第 2 行缺少冒号，请写成「名称: 值」。");
  });

  it("names the line that has no name", () => {
    expect(parseHeaders("  : value").error).toBe("第 1 行没写请求头名称。");
  });

  it("treats whitespace-only text as no headers", () => {
    expect(parseHeaders(" \n\t\n")).toEqual({ values: {}, error: null });
  });
});

describe("environment variables", () => {
  it("reads NAME=value lines and keeps equals signs in the value", () => {
    expect(parseEnv("API_KEY=a=b=c\nTOKEN=t1")).toEqual({
      values: { API_KEY: "a=b=c", TOKEN: "t1" },
      error: null,
    });
  });

  it("accepts an empty value", () => {
    expect(parseEnv("EMPTY=")).toEqual({ values: { EMPTY: "" }, error: null });
  });

  it("names the line that has no equals sign", () => {
    expect(parseEnv("A=1\nBROKEN")).toEqual({
      values: { A: "1" },
      error: "第 2 行缺少等号，请写成「NAME=value」。",
    });
  });

  it("names the line that has no variable name", () => {
    expect(parseEnv("=value").error).toBe("第 1 行没写变量名。");
  });
});

describe("arguments", () => {
  it("drops blank lines and trims what is left", () => {
    expect(parseArgs(" -y \n\n@modelcontextprotocol/server-github\n")).toEqual([
      "-y",
      "@modelcontextprotocol/server-github",
    ]);
  });

  it("returns nothing for empty text", () => {
    expect(parseArgs("")).toEqual([]);
  });
});
