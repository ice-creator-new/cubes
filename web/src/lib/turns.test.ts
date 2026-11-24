import { describe, expect, it } from "vitest";
import type { Message } from "../api/types";
import type { TimelineRow } from "./turns";
import { buildTimeline, commentaryBlocks, computerSpans, quoteExcerpt, replyText } from "./turns";

let seq = 0;

function message(patch: Partial<Message>): Message {
  seq += 1;
  return {
    id: `m${seq}`,
    conversation_id: "c1",
    author_id: "a1",
    author_name: "小北",
    avatar_seed: null,
    kind: "agent",
    content: "",
    status: "complete",
    seq,
    created_at: "2026-10-20T10:00:00Z",
    action: null,
    mentions: [],
    reply_to_id: null,
    mood: null,
    relay: null,
    run_id: null,
    ...patch,
  };
}

function action(patch: Partial<Message> = {}): Message {
  return message({
    kind: "action",
    content: "搜索「Tauri」",
    action: {
      id: `ac${seq}`,
      run_id: "r1",
      tool_call_id: `t${seq}`,
      tool_name: "web_search",
      status: "completed",
      summary: "搜索「Tauri」",
      target_agent_id: null,
      target_conversation_id: null,
      error: null,
      confirmation: null,
      detail: null,
    },
    ...patch,
  });
}

function toolRows(rows: TimelineRow[]): string[] {
  return rows.filter((row) => row.tools).map((row) => row.message.id);
}

describe("buildTimeline", () => {
  it("puts the tools under the last line of a mid-run burst only", () => {
    const first = message({ content: "我先查一下。" });
    const second = message({ content: "第一个群组已解散，继续解散第二个。" });
    const rows = buildTimeline([message({ kind: "user", content: "开始" }), first, second]);

    expect(toolRows(rows)).toEqual([second.id]);
    expect(rows[2]?.continues).toBe(true);
    expect(rows[2]?.showAuthor).toBe(false);
  });

  it("keeps tool rows with the prose around them and still ends the reply once", () => {
    const prose = message({ content: "先搜索。" });
    const search = action();
    const answer = message({ content: "找到了两个结果。" });
    const rows = buildTimeline([message({ kind: "user", content: "查" }), prose, search, answer]);

    expect(toolRows(rows)).toEqual([answer.id]);
  });

  it("introduces the author on a tool row that opens the reply, not on the prose after it", () => {
    const search = action();
    const answer = message({ content: "结果如下。" });
    const rows = buildTimeline([message({ kind: "user", content: "搜一下" }), search, answer]);

    expect(rows[1]?.showAuthor).toBe(true);
    expect(rows[2]?.showAuthor).toBe(false);
    expect(rows[2]?.continues).toBe(true);
    expect(toolRows(rows)).toEqual([answer.id]);
  });

  it("keeps one author line while text streams after a tool, carrying the live state", () => {
    // The bug: a streaming row under a tool row opened a second author line until it finished.
    const search = action();
    const answer = message({ content: "", status: "streaming" });
    const user = message({ kind: "user", content: "搜一下" });
    const thinking = buildTimeline([user, search, answer]);
    expect(thinking.filter((row) => row.showAuthor).map((row) => row.message.id)).toEqual([search.id]);
    expect(thinking[1]?.live).toBe("thinking");
    expect(thinking[2]?.live).toBeNull();

    const speaking = buildTimeline([user, search, { ...answer, content: "结果如下" }]);
    expect(speaking[1]?.live).toBe("speaking");
    const stopped = buildTimeline([user, search, { ...answer, content: "结果", status: "interrupted" }]);
    expect(stopped[1]?.live).toBe("interrupted");
    const done = buildTimeline([user, search, { ...answer, content: "结果如下", status: "complete" }]);
    expect(done[1]?.live).toBeNull();
  });

  it("renders no tools while the reply is still streaming", () => {
    const done = message({ content: "先说一句。" });
    const running = message({ content: "还在写", status: "streaming" });
    const rows = buildTimeline([message({ kind: "user", content: "问" }), done, running]);

    expect(toolRows(rows)).toEqual([]);

    const tool_running = action({ status: "streaming" });
    tool_running.action!.status = "running";
    const rows2 = buildTimeline([message({ kind: "user", content: "问" }), done, tool_running]);
    expect(toolRows(rows2)).toEqual([]);
  });

  it("keeps the newest reply open between steps while its run is still going", () => {
    // A tool finished and the next model turn has not started: every row is complete.
    const earlier = message({ content: "上一轮的结论。" });
    const step = message({ content: "拿到了基础语法，再查一下。" });
    const messages = [
      message({ kind: "user", content: "第一问" }),
      earlier,
      message({ kind: "user", content: "第二问" }),
      step,
      action(),
    ];

    expect(toolRows(buildTimeline(messages, { running: true }))).toEqual([earlier.id]);
    expect(toolRows(buildTimeline(messages))).toEqual([earlier.id, step.id]);
    expect(buildTimeline(messages, { running: true })[3]?.live).toBe("thinking");
    expect(buildTimeline(messages)[3]?.live).toBeNull();

    // The new question was just sent and nothing of its reply has landed: the reply above it
    // is finished and keeps its tools.
    const asked = [...messages, message({ kind: "user", content: "第三问" })];
    expect(toolRows(buildTimeline(asked, { running: true }))).toEqual([earlier.id, step.id]);
  });

  it("splits a private chat into same-author runs", () => {
    const a1 = message({ author_id: "a1", content: "小北第一句" });
    const a2 = message({ author_id: "a1", content: "小北第二句" });
    const b1 = message({ author_id: "a2", author_name: "小南", content: "小南回应" });
    const rows = buildTimeline([a1, a2, b1], { privateChat: true });

    expect(toolRows(rows)).toEqual([a2.id, b1.id]);

    // The same thread viewed as one conversation keeps a single ending.
    expect(toolRows(buildTimeline([a1, a2, b1]))).toEqual([b1.id]);
  });

  it("treats relay markers as their own rows, not part of a reply", () => {
    const before = message({ content: "我先问问小南。" });
    const marker = message({
      relay: { conversation_id: "p1", direction: "in", peer_id: "a2", peer_name: "小南" },
      content: "旧版本存下的转述",
      author_id: "a1",
    });
    const after = message({ content: "小南说可以。" });
    const rows = buildTimeline([before, marker, after]);

    expect(toolRows(rows)).toEqual([before.id, after.id]);
    expect(rows[1]?.tools).toBe(false);
    expect(replyText([before, marker, after], after.id)).toBe("小南说可以。");
  });

  it("marks a run of the user's back-to-back messages, from first to last", () => {
    const one = message({ kind: "user", content: "继续" });
    const two = message({ kind: "user", content: "继续" });
    const three = message({ kind: "user", content: "继续" });

    expect(buildTimeline([one, two, three]).map((row) => row.run)).toEqual(["first", "middle", "last"]);
    // One message alone keeps the tail; an agent row is never part of the user's run.
    expect(buildTimeline([one]).map((row) => row.run)).toEqual(["single"]);
    expect(buildTimeline([one, message({ content: "好。" })]).map((row) => row.run)).toEqual(["single", null]);
  });

  it("lets a reminder break the user's run instead of continuing it", () => {
    const one = message({ kind: "user", content: "继续" });
    const two = message({ kind: "user", content: "继续" });
    const three = message({ kind: "user", content: "继续" });
    const reminder = message({ kind: "user", content: "该休息了", reminder: true });
    const reply = message({ content: "好。" });

    // The reminder is the system speaking: the bubbles on either side are separate runs.
    expect(buildTimeline([one, reminder, two, three]).map((row) => row.run)).toEqual([
      "single",
      null,
      "first",
      "last",
    ]);
    // A reply between two of the user's messages ends the run just as well.
    expect(buildTimeline([one, two, reply, three]).map((row) => row.run)).toEqual([
      "first",
      "last",
      null,
      "single",
    ]);
  });
});

describe("replyText", () => {
  it("joins every agent line of the reply, skipping tools and relay markers", () => {
    const opener = message({ content: "我分两步做。" });
    const search = action();
    const first = message({ content: "第一步完成了。" });
    const marker = message({
      relay: { conversation_id: "p1", direction: "out", peer_id: "a2", peer_name: "小南" },
      content: "不该出现",
    });
    const second = message({ content: "第二步也完成了。" });
    const messages = [message({ kind: "user", content: "开始" }), opener, search, first, marker, second];

    expect(replyText(messages, opener.id)).toBe("我分两步做。\n\n第一步完成了。");
    expect(replyText(messages, second.id)).toBe("第二步也完成了。");
  });

  it("keeps a summary relay line as text", () => {
    const summary = message({
      content: "和小南定了方案。",
      relay: { conversation_id: "p1", direction: "summary", peer_id: "a2", peer_name: "小南" },
    });
    const messages = [message({ kind: "user", content: "问" }), summary];
    expect(replyText(messages, summary.id)).toBe("和小南定了方案。");
  });

  it("returns nothing for a user row", () => {
    const user = message({ kind: "user", content: "开始" });
    expect(replyText([user], user.id)).toBe("");
  });
});

describe("computerSpans", () => {
  const step = (patch: Partial<Message> = {}, tool = "computer") => {
    const row = action(patch);
    return { ...row, action: { ...row.action!, tool_name: tool, summary: "点击 (1, 2)" } };
  };

  it("folds commands, browser steps, screen steps and looked-at images of one stretch together", () => {
    const user = message({ kind: "user", content: "下载并打开" });
    const run = step({}, "computer_shell");
    const page = step({}, "computer_browser");
    const look = step();
    const shot = step({}, "view_image");
    const answer = message({ content: "好了。" });
    expect(computerSpans([user, run, page, look, shot, answer])).toEqual([{ start: 1, end: 4 }]);
  });

  it("folds from the first to the last sandbox step, narration included, never the answer", () => {
    const user = message({ kind: "user", content: "去沙盒里建个文件" });
    const intro = message({ content: "我先看看屏幕。" });
    const first = step();
    const between = message({ content: "打开终端了。" });
    const last = step();
    const answer = message({ content: "建好了。" });
    expect(computerSpans([user, intro, first, between, last, answer])).toEqual([{ start: 2, end: 4 }]);
  });

  it("a web search between sandbox steps ends one line and starts the next", () => {
    const user = message({ kind: "user", content: "查价格" });
    const before = step();
    const said = message({ content: "官网没有，我搜一下。" });
    const search = action();
    const after = step();
    expect(computerSpans([user, before, said, search, after])).toEqual([
      { start: 1, end: 1 },
      { start: 4, end: 4 },
    ]);
  });

  it("starts a new fold for another speaker or after the user speaks", () => {
    const mine = step();
    const theirs = step({ author_id: "a2", author_name: "小南" });
    const user = message({ kind: "user", content: "继续" });
    const again = step();
    expect(computerSpans([message({ kind: "user", content: "开始" }), mine, theirs, user, again])).toEqual([
      { start: 1, end: 1 },
      { start: 2, end: 2 },
      { start: 4, end: 4 },
    ]);
  });

  it("leaves a reply without sandbox steps alone", () => {
    expect(computerSpans([message({ kind: "user", content: "查" }), action(), message({ content: "好" })])).toEqual([]);
  });

  it("keeps text whose own response called tools in the fold, as its latest note", () => {
    const user = message({ kind: "user", content: "打开日志" });
    const first = step();
    const narration = message({ content: "超时发生在配置里，我打开日志看看：", leads_to_tools: true });
    expect(computerSpans([user, first, narration])).toEqual([{ start: 1, end: 2, noteStart: 2, noteEnd: 2 }]);
  });

  it("ends the fold at an answer the model response finished without tools", () => {
    const user = message({ kind: "user", content: "打开日志" });
    const first = step();
    const answer = message({ content: "日志里没有报错。", leads_to_tools: false });
    expect(computerSpans([user, first, answer])).toEqual([{ start: 1, end: 1 }]);
  });

  it("falls back to the old rule for rows written before the flag existed", () => {
    const user = message({ kind: "user", content: "打开日志" });
    const first = step();
    const old = message({ content: "看看是不是这里。" });
    // Settled and undecided: the answer stays out; with a step after it, it was narration.
    expect(computerSpans([user, first, old])).toEqual([{ start: 1, end: 1 }]);
    const next = step();
    expect(computerSpans([user, first, old, next])).toEqual([{ start: 1, end: 3 }]);
  });

  it("holds a streaming row with no verdict yet in the fold's live note slot", () => {
    const user = message({ kind: "user", content: "打开日志" });
    const first = step();
    const live = message({ content: "超时发生在配置里，现在打开服务器日志：", status: "streaming" });
    expect(computerSpans([user, first, live])).toEqual([{ start: 1, end: 2, noteStart: 2, noteEnd: 2 }]);
  });

  it("folds a committed note into the step log once the next step lands", () => {
    const user = message({ kind: "user", content: "打开日志" });
    const first = step();
    const narration = message({ content: "我再看一眼配置。", leads_to_tools: true });
    const next = step();
    expect(computerSpans([user, first, narration, next])).toEqual([{ start: 1, end: 3 }]);
  });
});

describe("quoteExcerpt", () => {
  it("keeps a short message whole, on one line", () => {
    expect(quoteExcerpt("先做登录。\n再做设置。")).toBe("先做登录。 再做设置。");
    expect(quoteExcerpt("  两端留白  ")).toBe("两端留白");
  });

  it("turns markdown into plain words", () => {
    const text = [
      "## 标题",
      "",
      "- **第一点**",
      "1. 第二点（见 [文档](https://example.com)）",
      "| 特点 | 说明 |",
      "|------|------|",
      "| 安全 | 强类型 |",
      "> 引用一句",
      "`npm test` 和 ~~旧写法~~",
    ].join("\n");
    const excerpt = quoteExcerpt(text, 200);
    expect(excerpt).not.toMatch(/[*#`|~>]/u);
    expect(excerpt).toContain("标题");
    expect(excerpt).toContain("第一点");
    expect(excerpt).toContain("第二点（见 文档）");
    expect(excerpt).toContain("安全 强类型");
    expect(excerpt).toContain("npm test 和 旧写法");
  });

  it("cuts a long reply to the limit with an ellipsis", () => {
    const excerpt = quoteExcerpt("一".repeat(200), 60);
    expect(excerpt).toHaveLength(60);
    expect(excerpt.endsWith("…")).toBe(true);
    expect(quoteExcerpt("", 60)).toBe("");
  });
});

describe("commentary merge", () => {
  /** The row index of one message's text row. */
  const at = (rows: TimelineRow[], id: string) => rows.findIndex((row) => row.message.id === id);

  function layout(...messages: Message[]): TimelineRow[] {
    return buildTimeline([message({ kind: "user", content: "开始干活" }), ...messages]);
  }

  it("folds consecutive short notes into one block and leaves the conclusion alone", () => {
    const first = message({ content: "我先看看目录" });
    const second = message({ content: "找到入口了" });
    const conclusion = message({ content: "结论：这个函数没问题，问题出在调用方。" });
    const rows = layout(first, second, conclusion);
    const blocks = commentaryBlocks(rows);

    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.head).toBe(at(rows, first.id));
    expect(blocks[0]!.members).toEqual([at(rows, first.id), at(rows, second.id)]);
    expect(blocks[0]!.latest).toBe(at(rows, second.id));
    // The conclusion keeps its own full bubble: no block holds the burst's last text row.
    expect(blocks[0]!.members).not.toContain(at(rows, conclusion.id));
  });

  it("keeps a single short note as the bubble it already is", () => {
    const note = message({ content: "马上开始" });
    const conclusion = message({ content: "做完了。" });
    expect(commentaryBlocks(layout(note, conclusion))).toHaveLength(0);
  });

  it("a long note between short ones cuts the run in two", () => {
    const first = message({ content: "先看看" });
    const long = message({ content: "这".repeat(90) });
    const third = message({ content: "找到问题了" });
    const fourth = message({ content: "改一行就好" });
    const conclusion = message({ content: "已经改好，跑完测试了。" });
    const rows = layout(first, long, third, fourth, conclusion);
    const blocks = commentaryBlocks(rows);

    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.members).toEqual([at(rows, third.id), at(rows, fourth.id)]);
  });

  it("a code block is content, not a progress note", () => {
    const first = message({ content: "先运行一下" });
    const code = message({ content: "```ts\nconst x = 1;\n```" });
    const third = message({ content: "输出正常" });
    const fourth = message({ content: "准备总结" });
    const conclusion = message({ content: "总结：一切正常。" });
    const rows = layout(first, code, third, fourth, conclusion);
    const blocks = commentaryBlocks(rows);

    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.members).toEqual([at(rows, third.id), at(rows, fourth.id)]);
  });

  it("an attachment keeps its message's own bubble", () => {
    const first = message({ content: "先看看" });
    const second = message({ content: "给你看这张图" });
    const image = message({
      content: "截图在下面",
      attachments: [{ id: "att1", mime: "image/png", size: 1200, name: "shot.png" }],
    });
    const fourth = message({ content: "看出问题了" });
    const fifth = message({ content: "正在改" });
    const conclusion = message({ content: "改好了。" });
    const rows = layout(first, second, image, fourth, fifth, conclusion);
    const blocks = commentaryBlocks(rows);

    expect(blocks.map((block) => block.members)).toEqual([
      [at(rows, first.id), at(rows, second.id)],
      [at(rows, fourth.id), at(rows, fifth.id)],
    ]);
  });

  it("the burst's last text row always stands alone, however short", () => {
    const first = message({ content: "马上" });
    const conclusion = message({ content: "好了" });
    expect(commentaryBlocks(layout(first, conclusion))).toHaveLength(0);

    const third = message({ content: "真的好了" });
    const rows = layout(first, conclusion, third);
    const blocks = commentaryBlocks(rows);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.members).toEqual([at(rows, first.id), at(rows, conclusion.id)]);
    expect(blocks[0]!.members).not.toContain(at(rows, third.id));
  });

  it("never merges two voices into one bubble", () => {
    const a1 = message({ author_id: "a1", content: "我看看" });
    const a2 = message({ author_id: "a1", content: "在看了" });
    const b1 = message({ author_id: "a2", author_name: "阿青", content: "我也看看" });
    const b2 = message({ author_id: "a2", author_name: "阿青", content: "找到了" });
    expect(commentaryBlocks(layout(a1, a2, b1, b2))).toHaveLength(0);

    const a3 = message({ author_id: "a1", content: "我看完了" });
    const rows = layout(a1, a2, a3);
    const blocks = commentaryBlocks(rows);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.members).toEqual([at(rows, a1.id), at(rows, a2.id)]);
  });

  it("tool rows between notes do not break them", () => {
    const first = message({ content: "先搜一下" });
    const search = action();
    const second = message({ content: "找到了两个结果" });
    const conclusion = message({ content: "结论：用第二个。" });
    const rows = layout(first, search, second, conclusion);
    const blocks = commentaryBlocks(rows);

    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.members).toEqual([at(rows, first.id), at(rows, second.id)]);
  });

  it("a task card ends the burst it sits in", () => {
    const first = message({ content: "先看看" });
    const card = message({ kind: "task", content: "修复登录", task_id: "t1" });
    const second = message({ content: "看完了" });
    const conclusion = message({ content: "结论写在任务里了。" });
    const rows = layout(first, card, second, conclusion);
    expect(commentaryBlocks(rows)).toHaveLength(0);
    expect(rows[at(rows, card.id)]!.message.kind).toBe("task");
  });
});
