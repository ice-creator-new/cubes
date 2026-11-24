import { describe, expect, it } from "vitest";
import { runNotice } from "./runNotice";

describe("runNotice", () => {
  it("leaves a limited run to the notice the engine already wrote in the thread", () => {
    expect(runNotice("limited", "已达本次任务的模型调用上限（64），任务已结束。")).toBeNull();
    expect(runNotice("limited", null)).toBeNull();
  });

  it("leaves a stop and an error sounding like what they are", () => {
    expect(runNotice("stopped", null)).toEqual({ title: "上一次任务已停止", detail: "" });
    expect(runNotice("error", "模型连不上")).toEqual({
      title: "上一次任务出错",
      detail: "模型连不上",
    });
  });
});
