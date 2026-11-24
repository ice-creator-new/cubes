import { describe, expect, it } from "vitest";
import { formatZoneTime, formFromSchedule, lastResultText, nextRunText, schedulePayload } from "./schedules";

/** A fixed "now": 2026-10-23 10:00 in Shanghai (02:00 UTC). */
const NOW = new Date("2026-10-23T02:00:00Z");

describe("the next run in the rule's own zone", () => {
  it("says today for a moment later the same day", () => {
    expect(formatZoneTime("2026-10-23T05:30:00+00:00", "Asia/Shanghai", NOW)).toBe("今天 13:30");
  });

  it("says tomorrow for the next calendar day in that zone", () => {
    expect(formatZoneTime("2026-10-24T01:00:00+00:00", "Asia/Shanghai", NOW)).toBe("明天 09:00");
  });

  it("names the month and day once it is further out", () => {
    expect(formatZoneTime("2026-11-02T01:00:00+00:00", "Asia/Shanghai", NOW)).toBe("11月2日 09:00");
  });

  it("keeps the year when the run is in another one", () => {
    expect(formatZoneTime("2027-01-02T01:00:00+00:00", "Asia/Shanghai", NOW)).toBe("2027年1月2日 09:00");
  });

  it("reads the same instant in the zone it was given", () => {
    // 02:00 UTC is still the evening of the 22nd in New York, so 09:00 EDT there is tomorrow.
    expect(formatZoneTime("2026-10-23T13:00:00+00:00", "America/New_York", NOW)).toBe("明天 09:00");
    expect(formatZoneTime("2026-10-23T06:00:00+00:00", "America/New_York", NOW)).toBe("明天 02:00");
  });

  it("is quiet about a missing or unreadable time", () => {
    expect(formatZoneTime(null, "Asia/Shanghai", NOW)).toBeNull();
    expect(formatZoneTime("not a time", "Asia/Shanghai", NOW)).toBeNull();
    expect(formatZoneTime("2026-10-23T05:30:00+00:00", "Not/AZone", NOW)).toBeNull();
  });

  it("prefixes the row's line, and says nothing when there is no next run", () => {
    expect(nextRunText("2026-10-24T01:00:00+00:00", "Asia/Shanghai", NOW)).toBe("下次 明天 09:00");
    expect(nextRunText(null, "Asia/Shanghai", NOW)).toBe("");
  });
});

describe("the last result in one line", () => {
  it("names what happened without an error", () => {
    expect(lastResultText("completed", null)).toBe("上次已完成");
    expect(lastResultText("stopped", null)).toBe("上次被停止");
    expect(lastResultText("limited", null)).toBe("上次额度用完");
    expect(lastResultText(null, null)).toBe("");
  });

  it("carries the reason along when the run failed", () => {
    expect(lastResultText("error", "模型没有响应。")).toBe("上次出错：模型没有响应。");
    expect(lastResultText("error", null)).toBe("上次出错");
  });

  it("explains that a missed run did not happen", () => {
    expect(lastResultText("missed", null)).toBe("上次错过（后端当时没运行）");
  });
});

describe("the form to the payload", () => {
  const base = {
    title: " 早上看新闻 ",
    prompt: "整理五条要点 ",
    conversation_id: "c_1",
    kind: "once" as const,
    at: "2026-10-24T09:00",
    every_minutes: "",
    cron: "",
    timezone: "Asia/Shanghai",
    use_computer: false,
  };

  it("trims the text fields and sends only the rule in use", () => {
    expect(schedulePayload(base)).toEqual({
      ok: true,
      body: {
        title: "早上看新闻",
        prompt: "整理五条要点",
        conversation_id: "c_1",
        kind: "once",
        timezone: "Asia/Shanghai",
        use_computer: false,
        at: "2026-10-24T09:00",
        every_minutes: null,
        cron: null,
      },
    });
  });

  it("turns an interval into a number and clears the other rules", () => {
    const parsed = schedulePayload({ ...base, kind: "interval", every_minutes: " 30 ", at: "2026-10-24T09:00" });
    expect(parsed.ok && parsed.body).toMatchObject({ kind: "interval", every_minutes: 30, at: null, cron: null });
  });

  it("collapses the spaces inside a cron rule", () => {
    const parsed = schedulePayload({ ...base, kind: "cron", cron: "  0   9  *  *  1-5 " });
    expect(parsed.ok && parsed.body).toMatchObject({ kind: "cron", cron: "0 9 * * 1-5", at: null });
  });

  it("refuses what the server would refuse, in Chinese", () => {
    expect(schedulePayload({ ...base, title: "  " })).toEqual({ ok: false, error: "请给这个定时任务起个名字。" });
    expect(schedulePayload({ ...base, prompt: "" })).toEqual({ ok: false, error: "请写下到时候要它做什么。" });
    expect(schedulePayload({ ...base, conversation_id: "" })).toEqual({
      ok: false,
      error: "请选择在哪个会话里运行。",
    });
    expect(schedulePayload({ ...base, at: "" })).toEqual({ ok: false, error: "请选择运行时间。" });
    expect(schedulePayload({ ...base, at: "24/10/2026 9:00" })).toEqual({
      ok: false,
      error: "运行时间看不懂，请重新选择。",
    });
    expect(schedulePayload({ ...base, kind: "interval", every_minutes: "3" })).toEqual({
      ok: false,
      error: "间隔至少 5 分钟。",
    });
    expect(schedulePayload({ ...base, kind: "interval", every_minutes: "2.5" })).toEqual({
      ok: false,
      error: "间隔要写成一个整数分钟数。",
    });
    expect(schedulePayload({ ...base, kind: "cron", cron: "0 9 * *" })).toEqual({
      ok: false,
      error: "cron 表达式要写成 5 段：分 时 日 月 周，例如 0 9 * * 1-5。",
    });
    expect(schedulePayload({ ...base, timezone: " " })).toEqual({
      ok: false,
      error: "请填写时区，例如 Asia/Shanghai。",
    });
  });
});

describe("editing an existing rule", () => {
  it("fills the form with the stored values, numbers as text", () => {
    expect(
      formFromSchedule({
        title: "每半小时",
        prompt: "看看进展",
        conversation_id: "c_1",
        kind: "interval",
        at: null,
        every_minutes: 30,
        cron: null,
        timezone: "Asia/Shanghai",
        use_computer: true,
      }),
    ).toEqual({
      title: "每半小时",
      prompt: "看看进展",
      conversation_id: "c_1",
      kind: "interval",
      at: "",
      every_minutes: "30",
      cron: "",
      timezone: "Asia/Shanghai",
      use_computer: true,
    });
  });
});
