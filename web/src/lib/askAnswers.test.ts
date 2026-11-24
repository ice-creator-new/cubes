import { describe, expect, it } from "vitest";
import type { ActionQuestion } from "../api/types";
import {
  allAnswered,
  chooseOption,
  chooseOther,
  firstUnanswered,
  initialDrafts,
  isAnswered,
  pickAdvances,
  toAnswers,
  typeOther,
} from "./askAnswers";

function question(patch: Partial<ActionQuestion> = {}): ActionQuestion {
  return {
    header: "方案",
    question: "这次先做哪一块？",
    options: [
      { label: "后端", description: "先做接口" },
      { label: "前端", description: "先做界面" },
    ],
    multi: false,
    recommended: null,
    ...patch,
  };
}

describe("the ask picker's answers", () => {
  it("preselects the recommended option", () => {
    const drafts = initialDrafts([question({ recommended: "后端" })]);
    expect(drafts[0]!.labels).toEqual(["后端"]);
  });

  it("keeps a single question to one label, and 其他 clears it", () => {
    const q = question();
    let draft = chooseOption(initialDrafts([q])[0]!, q, "前端");
    draft = chooseOption(draft, q, "后端");
    expect(draft.labels).toEqual(["后端"]);
    draft = chooseOther(draft, q);
    expect(draft.labels).toEqual([]);
    expect(draft.otherPicked).toBe(true);
    expect(isAnswered(draft)).toBe(false);
    draft = typeOther(draft, "都行");
    expect(isAnswered(draft)).toBe(true);
  });

  it("toggles labels on a multi question and keeps 其他 beside them", () => {
    const q = question({ multi: true });
    let draft = chooseOption(initialDrafts([q])[0]!, q, "前端");
    draft = chooseOption(draft, q, "后端");
    draft = chooseOther(draft, q);
    draft = typeOther(draft, "后天");
    expect(draft.labels).toEqual(["前端", "后端"]);
    expect(isAnswered(draft)).toBe(true);
    draft = chooseOther(draft, q);
    expect(draft.other).toBe("");
    expect(draft.otherPicked).toBe(false);
  });

  it("moves on for a real single-choice pick, and only for that", () => {
    const single = question();
    expect(pickAdvances(single, 0)).toBe(true);
    expect(pickAdvances(single, 1)).toBe(true);
    // The 「其他」 row owns no label, so it waits for Enter behind its text field.
    expect(pickAdvances(single, single.options.length)).toBe(false);
    // A multi question toggles in place.
    expect(pickAdvances(question({ multi: true }), 0)).toBe(false);
  });

  it("reports the first question still waiting, so 提交 can point at it", () => {
    const questions = [question(), question({ header: "时间", multi: true })];
    const drafts = initialDrafts(questions);
    expect(firstUnanswered(questions, drafts)).toBe(0);
    drafts[0] = chooseOption(drafts[0]!, questions[0]!, "前端");
    expect(firstUnanswered(questions, drafts)).toBe(1);
    expect(allAnswered(questions, drafts)).toBe(false);
    drafts[1] = typeOther(chooseOther(drafts[1]!, questions[1]!), "明天");
    expect(firstUnanswered(questions, drafts)).toBe(-1);
    expect(allAnswered(questions, drafts)).toBe(true);
  });

  it("submits one entry per question with other text only when used", () => {
    const questions = [question(), question({ header: "时间", multi: true })];
    let drafts = initialDrafts(questions);
    drafts = [
      chooseOption(drafts[0]!, questions[0]!, "前端"),
      typeOther(drafts[1]!, "明天"),
    ];
    expect(allAnswered(questions, drafts)).toBe(true);
    expect(toAnswers(questions, drafts)).toEqual([
      { question_index: 0, labels: ["前端"], other: null },
      { question_index: 1, labels: [], other: "明天" },
    ]);
  });
});
