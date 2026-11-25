// One turn's progress board in the chat: a thin bar, its count, and the item being worked on
// in words. Collapsed it says what is happening; opened it lists every item with its status.
// The same list is drawn in the activity sheet, so a board reads the same in both places.

import { useState } from "react";
import type { ActivityTodo } from "../api/types";
import { TODO_STATUS_META } from "../lib/activities";
import { boardLine, boardProgress } from "../lib/todoBoard";
import { Icon } from "./Icon";

/** Every item of one board, oldest first: done checked and muted, the doing one lit. */
export function TodoItemList({ todos }: { todos: ActivityTodo[] }) {
  return (
    <ul className="todo-board">
      {todos.map((todo, index) => {
        const meta = TODO_STATUS_META[todo.status];
        return (
          <li key={`${index}-${todo.text}`} className={`todo-row ${todo.status}`}>
            <span className="todo-icon" aria-hidden="true">
              {todo.status === "doing" ? (
                <span className="activity-step-spinner" />
              ) : (
                <Icon name={meta.glyph} size={13} />
              )}
            </span>
            <span className="todo-text">{todo.text}</span>
            <span className="todo-state">{meta.label}</span>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The board row in a timeline. It shares the action row's box, hover and caret, so it opens
 * like the file and sandbox rows around it. ``live`` breathes the line while the lane runs.
 */
export function TodoBoardRow({
  id,
  todos,
  live,
  continues,
}: {
  /** The action id the panel is labelled by; unique per board. */
  id: string;
  todos: ActivityTodo[];
  /** The lane is still running: the line shimmers like a live activity's. */
  live: boolean;
  /** The previous row was by the same speaker: tuck under it, as every action row does. */
  continues: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const panelID = `todo-board-${id}`;
  const { done, total } = boardProgress(todos);
  const line = boardLine(todos);
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className={`action-block todo-board-block${continues ? " continues" : ""}`}>
      <button
        type="button"
        className={`action-row todo-board-row${live ? " running" : ""}`}
        aria-expanded={expanded}
        aria-controls={panelID}
        title={total > 0 ? `进度：${done}/${total} 项完成` : "进度"}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="action-icon" aria-hidden="true">
          <Icon name="tasks" size={14} />
        </span>
        <span className="todo-board-summary">
          <span className="todo-board-top">
            <span className="todo-board-title">进度</span>
            <span
              className="todo-board-bar"
              role="progressbar"
              aria-label={`${done}/${total} 项完成`}
              aria-valuemin={0}
              aria-valuemax={total}
              aria-valuenow={done}
            >
              <span className="todo-board-fill" style={{ width: `${percent}%` }} />
            </span>
            <span className="todo-board-count">{total > 0 ? `${done}/${total}` : "—"}</span>
          </span>
          {line !== "" && (
            <span className={live ? "todo-board-line shimmer" : "todo-board-line"}>{line}</span>
          )}
        </span>
        <span className={expanded ? "action-caret open" : "action-caret"} aria-hidden="true">
          <Icon name="chevron" size={12} />
        </span>
      </button>
      <div
        className={expanded ? "action-detail-collapse collapse open" : "action-detail-collapse collapse"}
        aria-hidden={!expanded}
        inert={!expanded}
      >
        <div className="collapse-inner">
          <div className="action-detail todo-board-detail" id={panelID}>
            {todos.length > 0 ? (
              <TodoItemList todos={todos} />
            ) : (
              <p className="hint">这次没有记下待办内容。</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
