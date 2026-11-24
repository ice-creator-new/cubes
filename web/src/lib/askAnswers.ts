// The picker's answer state, kept apart from the panel so the rules stay testable: a
// recommended option starts picked, 「其他」 is a row of its own with free text behind it,
// and a question counts as answered only when it has a label or some typed words.

import type { ActionQuestion, AskAnswer } from "../api/types";

export interface AskDraft {
  labels: string[];
  other: string;
  /** Whether the 「其他」 row is picked. Typing without picking it never answers. */
  otherPicked: boolean;
}

export function initialDrafts(questions: ActionQuestion[]): AskDraft[] {
  return questions.map((question) => ({
    labels:
      question.recommended && question.options.some((option) => option.label === question.recommended)
        ? [question.recommended]
        : [],
    other: "",
    otherPicked: false,
  }));
}

/** The label on the 「其他」 row; the bot never writes it itself. */
export const OTHER_LABEL = "其他";

export function chooseOption(
  draft: AskDraft,
  question: ActionQuestion,
  label: string,
): AskDraft {
  if (question.multi) {
    const active = draft.labels.includes(label);
    return {
      ...draft,
      labels: active ? draft.labels.filter((item) => item !== label) : [...draft.labels, label],
    };
  }
  return { ...draft, labels: [label], other: "", otherPicked: false };
}

export function chooseOther(draft: AskDraft, question: ActionQuestion): AskDraft {
  if (question.multi) {
    return { ...draft, otherPicked: !draft.otherPicked, other: draft.otherPicked ? "" : draft.other };
  }
  return { ...draft, labels: [], otherPicked: true };
}

export function typeOther(draft: AskDraft, text: string): AskDraft {
  return { ...draft, other: text, otherPicked: true };
}

export function isAnswered(draft: AskDraft): boolean {
  if (draft.labels.length > 0) return true;
  return draft.otherPicked && draft.other.trim().length > 0;
}

export function allAnswered(questions: ActionQuestion[], drafts: AskDraft[]): boolean {
  return firstUnanswered(questions, drafts) === -1;
}

/**
 * Whether picking this row moves the picker on by itself. A single choice is the whole
 * answer, so the panel confirms it and leaves; 「其他」 waits for typed words and Enter, and
 * a multi question is never finished by one click.
 */
export function pickAdvances(question: ActionQuestion, row: number): boolean {
  return !question.multi && row >= 0 && row < question.options.length;
}

/** The first question still waiting for an answer, or -1 when the set is complete. */
export function firstUnanswered(questions: ActionQuestion[], drafts: AskDraft[]): number {
  return questions.findIndex((_, index) => {
    const draft = drafts[index];
    return draft ? !isAnswered(draft) : true;
  });
}

/** The wire shape: one entry per question, the free text only when 「其他」 was used. */
export function toAnswers(questions: ActionQuestion[], drafts: AskDraft[]): AskAnswer[] {
  return questions.map((_, index) => {
    const draft = drafts[index];
    return {
      question_index: index,
      labels: draft?.labels ?? [],
      other: draft?.otherPicked ? draft.other.trim() || null : null,
    };
  });
}
