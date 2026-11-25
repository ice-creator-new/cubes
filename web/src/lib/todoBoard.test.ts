import { describe, expect, it } from "vitest";
import type { ActivityTodo, Message } from "../api/types";
import {
  boardLine,
  boardProgress,
  boardTodos,
  isTodoBoard,
  planTodoBoards,
} from "./todoBoard";

let seq = 0;

function message(patch: Partial<Message>): Message {
  seq += 1;
  return {
    id: `m${seq}`,
    conversation_id: "c1",
    author_id: "a1",
    author_name: "匠师",
    avatar_seed: null,
    kind: "agent",
    content: "",
    status: "complete",
    seq,
    created_at: "2026-11-25T10:00:00Z",
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: "r1",
    ...patch,
  };
}

function board(id: string, items: ActivityTodo[], patch: Partial<Message> = {}): Message {
  return message({
    id,
    kind: "action",
    action: {
      id: `x-${id}`,
      run_id: "r1",
      tool_call_id: `call-${id}`,
      tool_name: "todo_write",
      status: "completed",
      summary: "更新了进度",
      target_agent_id: null,
      target_conversation_id: null,
      error: null,
      todos: items,
    },
    ...patch,
  });
}

const plan: ActivityTodo[] = [
  { text: "读依赖清单", status: "done" },
  { text: "升级 vite", status: "doing" },
  { text: "跑测试", status: "pending" },
];

describe("planTodoBoards", () => {
  it("keeps one row per turn, showing the newest board", () => {
    const first = board("b1", [{ text: "读依赖清单", status: "doing" }]);
    const second = board("b2", plan);
    const read = message({ kind: "action", id: "read1", action: {
      id: "x-read1",
      run_id: "r1",
      tool_call_id: "call-read1",
      tool_name: "file_read",
      status: "completed",
      summary: "读取了 package.json",
      target_agent_id: null,
      target_conversation_id: null,
      error: null,
    } });
    const plan_ = planTodoBoards([first, read, second]);

    expect(plan_.shown.get("b1")?.id).toBe("b2");
    expect(plan_.shown.size).toBe(1);
    // The later write's row folds away; the first row draws its newest items.
    expect(plan_.hidden).toEqual(new Set(["b2"]));
  });

  it("hides every board after the turn's first one", () => {
    const first = board("b1", plan);
    const second = board("b2", plan);
    const third = board("b3", plan);
    const result = planTodoBoards([first, message({ id: "read" , kind: "action", action: null }), second, third]);

    expect([...result.shown.keys()]).toEqual(["b1"]);
    expect(result.shown.get("b1")?.id).toBe("b3");
    expect(result.hidden).toEqual(new Set(["b2", "b3"]));
  });

  it("starts a new turn for another speaker and for the user", () => {
    const mine = board("b1", plan);
    const theirs = board("b2", plan, { author_id: "a2", author_name: "另一位" });
    const afterUser = board("b3", plan);
    const result = planTodoBoards([
      mine,
      theirs,
      message({ id: "u1", kind: "user", author_id: null, author_name: "你" }),
      afterUser,
    ]);

    expect([...result.shown.keys()]).toEqual(["b1", "b2", "b3"]);
    expect(result.hidden.size).toBe(0);
  });

  it("leaves a write without items in place, and lets the next board be the head", () => {
    const failed = board("b1", []);
    const real = board("b2", plan);
    const result = planTodoBoards([failed, real]);

    expect([...result.shown.keys()]).toEqual(["b2"]);
    expect(result.hidden.size).toBe(0);
  });

  it("reads only todo_write rows as boards", () => {
    expect(isTodoBoard(board("b1", plan))).toBe(true);
    expect(isTodoBoard(message({ kind: "action" }))).toBe(false);
    expect(isTodoBoard(message({ kind: "agent" }))).toBe(false);
  });
});

describe("board wording", () => {
  it("counts done items only; dropped ones are not finished", () => {
    expect(boardProgress(plan)).toEqual({ done: 1, total: 3 });
    expect(
      boardProgress([
        { text: "a", status: "done" },
        { text: "b", status: "dropped" },
        { text: "c", status: "done" },
      ]),
    ).toEqual({ done: 2, total: 3 });
    expect(boardProgress([])).toEqual({ done: 0, total: 0 });
  });

  it("says what is being worked on, else the next step, else that it finished", () => {
    expect(boardLine(plan)).toBe("正在：升级 vite");
    // The item's own 正在 gives way to the line's, so the card never reads 正在：正在…
    expect(boardLine([{ text: "正在填写 #4，点击 #7", status: "doing" }])).toBe(
      "正在：填写 #4，点击 #7",
    );
    expect(boardLine([{ text: "读清单", status: "done" }, { text: "跑测试", status: "pending" }])).toBe(
      "下一步：跑测试",
    );
    expect(boardLine([{ text: "跑测试", status: "done" }])).toBe("全部完成");
    expect(boardLine([{ text: "跑测试", status: "dropped" }])).toBe("没有要做的了");
    expect(boardLine([])).toBe("");
  });

  it("keeps the board's own order", () => {
    expect(boardTodos(board("b1", plan)).map((todo) => todo.text)).toEqual([
      "读依赖清单",
      "升级 vite",
      "跑测试",
    ]);
  });
});
