import { describe, expect, it } from "vitest";
import { browserRowSummary, pageHost } from "./actionSummary";

describe("pageHost", () => {
  it("keeps the port and drops a leading www", () => {
    expect(pageHost("https://www.example.com/a?b=1")).toBe("example.com");
    expect(pageHost("http://localhost:3000/chat")).toBe("localhost:3000");
  });

  it("says nothing for a missing or unparsable url", () => {
    expect(pageHost(null)).toBe("");
    expect(pageHost("not a url")).toBe("");
  });
});

describe("browserRowSummary", () => {
  it("puts the page title and site after the backend's sentence", () => {
    expect(
      browserRowSummary("点击 #12", { title: "订单详情", url: "https://shop.example.com/orders/1" }),
    ).toBe("点击 #12 · 订单详情 · shop.example.com");
  });

  it("cuts a long title at the limit, with an ellipsis", () => {
    const title = "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十";
    const row = browserRowSummary("打开页面", { title, url: "https://example.com/" }, 20)!;
    expect(row).toBe(`打开页面 · ${title.slice(0, 19)}… · example.com`);
  });

  it("collapses the newlines a page title may carry", () => {
    expect(browserRowSummary("打开页面", { title: "  后台\n\n管理  " })).toBe("打开页面 · 后台 管理");
  });

  it("does not repeat a title or host the sentence already says", () => {
    expect(
      browserRowSummary("打开 https://a.example.com/x，页面是「登录」", {
        title: "登录",
        url: "https://a.example.com/x",
      }),
    ).toBeNull();
    expect(
      browserRowSummary("打开 https://a.example.com/x，页面是「登录」", {
        title: "注册",
        url: "https://a.example.com/x",
      }),
    ).toBe("打开 https://a.example.com/x，页面是「登录」 · 注册");
  });

  it("falls back to the plain sentence when the detail names no page", () => {
    expect(browserRowSummary("点击 #12", {})).toBeNull();
    expect(browserRowSummary("点击 #12", { title: "  " })).toBeNull();
  });
});
