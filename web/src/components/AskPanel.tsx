// A bot's questions, answered where the user types: the panel takes the composer's place
// while the run waits. One question at a time — a single choice lands with a short
// confirmation and moves on by itself, 其他 waits behind its text field, and a multi question
// is finished by 下一题. 「跳过」 leaves every question unanswered instead of guessing.

import { useEffect, useRef, useState } from "react";

import type { ActionQuestion, AskAnswer } from "../api/types";
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
  type AskDraft,
} from "../lib/askAnswers";
import { composingKey } from "../lib/ime";
import { Icon } from "./Icon";

export interface AskQuestion {
  id: string;
  who: string;
  questions: ActionQuestion[];
}

/** How long a single-choice pick shows as chosen before the next question slides in. */
const PICK_CONFIRM_MS = 160;

export function AskPanel({
  ask,
  onAnswer,
}: {
  ask: AskQuestion;
  onAnswer: (answers: AskAnswer[], skip: boolean) => Promise<string | null>;
}) {
  const questions = ask.questions;
  const [drafts, setDrafts] = useState<AskDraft[]>(() => initialDrafts(questions));
  // The cursor row only gets a ring while the keyboard is driving: over a mouse it would
  // read as a focus outline around whatever happens to sit under the pointer.
  const [keyboard, setKeyboard] = useState(false);
  const [active, setActive] = useState(0);
  const [focusRow, setFocusRow] = useState(0);
  const [direction, setDirection] = useState<"fwd" | "back">("fwd");
  // The row a single pick just landed on, lit until the next question takes over.
  const [flashRow, setFlashRow] = useState<number | null>(null);
  const [pending, setPending] = useState<"answer" | "skip" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const panel = useRef<HTMLElement>(null);
  const submitButton = useRef<HTMLButtonElement>(null);
  const otherFields = useRef<(HTMLInputElement | null)[]>([]);
  const focusOther = useRef(false);
  // Set synchronously: the window's Enter and a focused button's own click can both land
  // in the same frame, and a state update is too late to stop the second.
  const busy = useRef(false);
  const advanceTimer = useRef<number | null>(null);
  const composition = useRef<{ open: boolean; endedAt: number | null }>({ open: false, endedAt: null });
  const complete = allAnswered(questions, drafts);
  const missing = firstUnanswered(questions, drafts);
  const question = questions[active];
  const draft = drafts[active];
  const last = active === questions.length - 1;

  const clearAdvance = () => {
    if (advanceTimer.current !== null) {
      window.clearTimeout(advanceTimer.current);
      advanceTimer.current = null;
    }
  };

  // A new call starts clean and takes the keyboard, however the previous one ended.
  useEffect(() => {
    setDrafts(initialDrafts(questions));
    setActive(0);
    setFocusRow(0);
    setKeyboard(false);
    setFlashRow(null);
    setDirection("fwd");
    setError(null);
    setPending(null);
    busy.current = false;
    clearAdvance();
    const frame = requestAnimationFrame(() => panel.current?.focus());
    return () => cancelAnimationFrame(frame);
    // Only a new action is a new set of questions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ask.id]);

  useEffect(() => clearAdvance, []);

  // Picking 「其他」 puts the caret in the field it opens, once that field exists.
  useEffect(() => {
    if (!focusOther.current) return;
    focusOther.current = false;
    otherFields.current[active]?.focus();
  }, [active, draft?.otherPicked]);

  const update = (next: AskDraft) => {
    setDrafts((current) => current.map((item, index) => (index === active ? next : item)));
    setError(null);
  };

  const move = (index: number, back = false) => {
    clearAdvance();
    setFlashRow(null);
    setDirection(back ? "back" : "fwd");
    setActive(index);
    setFocusRow(0);
    requestAnimationFrame(() => panel.current?.focus());
  };

  // 「下一题」 leaves only when this question is settled: the picker never guesses.
  const step = () => {
    if (!draft || !isAnswered(draft)) return;
    if (active + 1 < questions.length) {
      move(active + 1);
      return;
    }
    finish();
  };

  // 「提交」 refuses a half-answered set; it points the user at the question still waiting.
  const finish = () => {
    if (missing === -1) {
      void submit();
      return;
    }
    if (missing !== active) move(missing, missing < active);
  };

  const pick = (row: number) => {
    if (!question || !draft) return;
    clearAdvance();
    if (row >= question.options.length) {
      const next = chooseOther(draft, question);
      update(next);
      if (next.otherPicked) {
        focusOther.current = true;
      } else {
        requestAnimationFrame(() => panel.current?.focus());
      }
      return;
    }
    const option = question.options[row];
    if (!option) return;
    update(chooseOption(draft, question, option.label));
    if (!pickAdvances(question, row)) {
      focusOther.current = false;
      requestAnimationFrame(() => panel.current?.focus());
      return;
    }
    // One beat on the chosen row before moving on, so the pick reads as landed.
    setFlashRow(row);
    advanceTimer.current = window.setTimeout(() => {
      advanceTimer.current = null;
      setFlashRow(null);
      if (active + 1 < questions.length) move(active + 1);
      else submitButton.current?.focus();
    }, PICK_CONFIRM_MS);
  };

  const submit = async () => {
    if (busy.current || !allAnswered(questions, drafts)) return;
    busy.current = true;
    setPending("answer");
    setError(null);
    const failure = await onAnswer(toAnswers(questions, drafts), false);
    if (failure) {
      busy.current = false;
      setError(failure);
      setPending(null);
    }
  };

  const skip = async () => {
    if (busy.current) return;
    busy.current = true;
    setPending("skip");
    setError(null);
    const failure = await onAnswer([], true);
    if (failure) {
      busy.current = false;
      setError(failure);
      setPending(null);
    }
  };

  // Keys live on the panel, not the window: focus lands here when it appears, and returns
  // here after every pick. Typing in the 「其他」 field keeps its words; only Enter advances.
  const onKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const native = event.nativeEvent;
    if (
      composingKey({
        isComposing: native.isComposing,
        keyCode: native.keyCode,
        composing: composition.current.open,
        composedAt: composition.current.endedAt,
        now: performance.now(),
      })
    ) {
      return;
    }
    const target = event.target as HTMLElement;
    const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
    if (typing) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (last) finish();
        else step();
      }
      return;
    }
    // A focused button (跳转 tab, 下一题, 提交…) owns Enter and Space: let it click itself.
    if ((event.key === "Enter" || event.key === " ") && target.closest("button")) return;
    const rows = (question?.options.length ?? 0) + 1;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setKeyboard(true);
      const step = event.key === "ArrowDown" ? 1 : -1;
      setFocusRow((row) => (row + step + rows) % rows);
      return;
    }
    if (event.key === "ArrowLeft") {
      if (active === 0) return;
      event.preventDefault();
      move(active - 1, true);
      return;
    }
    if (event.key === "ArrowRight") {
      if (active + 1 >= questions.length) return;
      event.preventDefault();
      move(active + 1);
      return;
    }
    if (/^[1-9]$/.test(event.key)) {
      const row = Number(event.key) - 1;
      if (row >= rows) return;
      event.preventDefault();
      setKeyboard(true);
      pick(row);
      return;
    }
    if (event.key === " " && question?.multi) {
      event.preventDefault();
      setKeyboard(true);
      pick(focusRow);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      setKeyboard(true);
      if (last) {
        if (draft && isAnswered(draft)) finish();
        else pick(focusRow);
        return;
      }
      // An answered multi question leaves on Enter; one still empty takes the cursor row.
      if (question?.multi && draft && isAnswered(draft)) {
        step();
        return;
      }
      pick(focusRow);
    }
  };

  if (!question || !draft) return null;
  const answeredAt = (index: number) => {
    const item = drafts[index];
    return item ? isAnswered(item) : false;
  };
  const hint = question.multi
    ? last
      ? "数字键多选 · Enter 提交"
      : "数字键多选 · Enter 下一题"
    : last
      ? "数字键选择 · Enter 提交"
      : "数字键选择 · Enter 下一题";

  return (
    <section
      ref={panel}
      className="ask-panel"
      aria-label="回答机器人的问题"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      onPointerDown={() => setKeyboard(false)}
    >
      <header className="ask-head">
        <span className="ask-who">{ask.who ? `${ask.who} 想问你` : "想问你"}</span>
        {questions.length > 1 && (
          <span className="ask-step">
            第 {active + 1} / {questions.length} 题
          </span>
        )}
      </header>

      {questions.length > 1 && (
        <div className="ask-tabs" role="tablist" aria-label="问题">
          {questions.map((item, index) => {
            const done = answeredAt(index);
            return (
              <button
                key={`${index}-${item.header}`}
                type="button"
                role="tab"
                aria-selected={index === active}
                aria-label={`第 ${index + 1} 题：${item.header}${done ? "，已答" : ""}`}
                className={["ask-tab", done && "done", index === active && "active"]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => move(index, index < active)}
              >
                <span className="ask-tab-num" aria-hidden="true">
                  {done ? <Icon name="check" size={10} /> : index + 1}
                </span>
                <span>{item.header}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* Every question sits in one grid cell, so the panel keeps the tallest height and the
          chat below it never jumps when the step changes. */}
      <div className="ask-body" data-dir={direction}>
        {questions.map((item, index) => {
          const itemDraft = drafts[index];
          if (!itemDraft) return null;
          const current = index === active;
          const otherIndex = item.options.length;
          return (
            <div
              key={`${index}-${item.header}`}
              className={current ? "ask-q is-active" : "ask-q"}
              aria-hidden={!current}
              inert={!current}
            >
              <div className="ask-q-head">
                <p className="ask-q-text">{item.question}</p>
                <span className="ask-q-mode">{item.multi ? "可多选" : "单选"}</span>
              </div>
              <div
                className={item.multi ? "ask-options multi" : "ask-options"}
                role={item.multi ? "group" : "radiogroup"}
                aria-label={item.header}
              >
                {item.options.map((option, row) => {
                  const picked = itemDraft.labels.includes(option.label);
                  return (
                    <button
                      key={option.label}
                      type="button"
                      role={item.multi ? "checkbox" : "radio"}
                      aria-checked={picked}
                      className={[
                        "ask-option",
                        picked && "active",
                        current && keyboard && focusRow === row && "focused",
                        current && flashRow === row && "just-picked",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onMouseEnter={() => {
                        if (!current) return;
                        setKeyboard(false);
                        setFocusRow(row);
                      }}
                      onClick={() => pick(row)}
                    >
                      <span className="ask-key" aria-hidden="true">
                        {row + 1}
                      </span>
                      <span className="ask-option-body">
                        <span className="ask-option-label">
                          {option.label}
                          {option.label === item.recommended && <span className="ask-rec">推荐</span>}
                        </span>
                        {option.description && (
                          <span className="ask-option-desc">{option.description}</span>
                        )}
                      </span>
                      <span className={picked ? "ask-mark on" : "ask-mark"} aria-hidden="true">
                        {picked && <Icon name="check" size={10} />}
                      </span>
                    </button>
                  );
                })}
                <button
                  type="button"
                  role={item.multi ? "checkbox" : "radio"}
                  aria-checked={itemDraft.otherPicked}
                  aria-label="其他，自己写一个回答"
                  className={[
                    "ask-option",
                    "ask-other",
                    itemDraft.otherPicked && "active",
                    current && keyboard && focusRow === otherIndex && "focused",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onMouseEnter={() => {
                    if (!current) return;
                    setKeyboard(false);
                    setFocusRow(otherIndex);
                  }}
                  onClick={() => pick(otherIndex)}
                >
                  <span className="ask-key" aria-hidden="true">
                    {otherIndex + 1}
                  </span>
                  <span className="ask-option-body">
                    <span className="ask-option-label">其他</span>
                    {!itemDraft.otherPicked && (
                      <span className="ask-option-desc">自己写一个回答</span>
                    )}
                  </span>
                  <span
                    className={itemDraft.otherPicked ? "ask-mark on" : "ask-mark"}
                    aria-hidden="true"
                  >
                    {itemDraft.otherPicked && <Icon name="check" size={10} />}
                  </span>
                </button>
                {itemDraft.otherPicked && (
                  <input
                    ref={(element) => {
                      otherFields.current[index] = element;
                    }}
                    className="ask-text"
                    value={itemDraft.other}
                    placeholder="写下你的回答…"
                    autoComplete="off"
                    onChange={(event) => {
                      if (current) update(typeOther(itemDraft, event.currentTarget.value));
                    }}
                    onCompositionStart={() => {
                      composition.current = { open: true, endedAt: null };
                    }}
                    onCompositionEnd={() => {
                      composition.current = { open: false, endedAt: performance.now() };
                    }}
                  />
                )}
              </div>
            </div>
          );
        })}
      </div>

      <footer className="ask-foot">
        <div className="ask-foot-left">
          {active > 0 && (
            <button type="button" className="ask-nav" onClick={() => move(active - 1, true)}>
              <Icon name="chevron" size={11} />
              <span>上一题</span>
            </button>
          )}
          <span className="ask-keys" aria-hidden="true">
            {hint}
          </span>
        </div>
        <div className="ask-foot-right">
          <button
            type="button"
            className="ask-skip"
            disabled={pending !== null}
            onClick={() => void skip()}
          >
            {pending === "skip" ? "正在跳过…" : "跳过"}
          </button>
          {last ? (
            <>
              {missing !== -1 && missing !== active && (
                <button
                  type="button"
                  className="ask-missing"
                  disabled={pending !== null}
                  onClick={() => move(missing, missing < active)}
                >
                  还差「{questions[missing]?.header}」
                </button>
              )}
              <button
                ref={submitButton}
                type="button"
                className="ask-answer"
                disabled={!complete || pending !== null}
                onClick={() => void submit()}
              >
                {pending === "answer" ? "正在提交…" : "提交"}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="ask-next"
              disabled={!isAnswered(draft) || pending !== null}
              onClick={() => step()}
            >
              <span>下一题</span>
              <Icon name="chevron" size={11} />
            </button>
          )}
        </div>
      </footer>

      {error && (
        <p className="form-error ask-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
