// A `kind: "status"` row: the dispatcher's progress card, built from task data and written by no
// member. It reads as a compact card, not as a bot bubble: one row per task, then the idle line.

import { useState } from "react";
import type { Agent, StatusCard } from "../api/types";
import { statusCardRows, type StatusCardActivityRow } from "../lib/statusCard";
import { boardLine, boardProgress } from "../lib/todoBoard";
import { Avatar } from "./Avatar";
import { Icon } from "./Icon";
import { TodoItemList } from "./TodoBoard";

/**
 * One live lane: who, what, and its board — the same bar and words the chat's 进度 row uses.
 * The full list opens under the row, so the card stays one line per lane until asked.
 */
function LiveRow({
  row,
  face,
  index,
}: {
  row: StatusCardActivityRow;
  face: React.ReactNode;
  index: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const { done, total } = boardProgress(row.todos);
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;
  const line = boardLine(row.todos) || (row.current ? `当前：${row.current}` : "");
  const panelID = `status-board-${index}`;
  const head = (
    <>
      <span className="task-card-dot running" aria-hidden="true" />
      <span className="status-card-owner">{row.ownerName}</span>
      <span className="status-card-title">{row.title}</span>
      {total > 0 && (
        <span className="status-card-meter">
          <span
            className="todo-board-bar status-card-bar"
            role="progressbar"
            aria-label={`${done}/${total} 项完成`}
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={done}
          >
            <span className="todo-board-fill" style={{ width: `${percent}%` }} />
          </span>
          <span className="status-card-count">{`${done}/${total}`}</span>
          <span className={expanded ? "action-caret open" : "action-caret"} aria-hidden="true">
            <Icon name="chevron" size={12} />
          </span>
        </span>
      )}
    </>
  );
  return (
    <li className="status-card-row">
      <span className="status-card-face">{face}</span>
      <span className="status-card-main">
        {total > 0 ? (
          <button
            type="button"
            className="status-card-top status-card-toggle"
            aria-expanded={expanded}
            aria-controls={panelID}
            onClick={() => setExpanded((value) => !value)}
          >
            {head}
          </button>
        ) : (
          <span className="status-card-top">{head}</span>
        )}
        {line !== "" && <span className="status-card-step todo-board-line shimmer">{line}</span>}
        {total > 0 && (
          <div
            className={expanded ? "collapse open" : "collapse"}
            aria-hidden={!expanded}
            inert={!expanded}
          >
            <div className="collapse-inner">
              <div className="status-card-board" id={panelID}>
                <TodoItemList todos={row.todos} />
              </div>
            </div>
          </div>
        )}
      </span>
    </li>
  );
}

export function StatusCardView({ card, agents }: { card: StatusCard; agents: Agent[] }) {
  const { live, rows, idleNames } = statusCardRows(
    card,
    (id) => agents.find((agent) => agent.id === id)?.name,
    (name) => agents.find((agent) => agent.name === name)?.id,
  );
  const byID = new Map(agents.map((agent) => [agent.id, agent]));
  const face = (ownerID: string | null, name: string) => {
    const owner = ownerID ? byID.get(ownerID) : undefined;
    return owner ? (
      <Avatar seed={owner.avatar_seed} appearance={owner.appearance} size={20} name={owner.name} agentID={owner.id} />
    ) : (
      <span className="work-gone" aria-hidden="true" title={name} />
    );
  };
  return (
    <div className="status-card" role="group" aria-label="群里的进度">
      <div className="status-card-head">
        <span>群里的进度</span>
      </div>
      {live.length > 0 && (
        <ul className="status-card-rows">
          {live.map((row, index) => (
            <LiveRow key={row.key} row={row} index={index} face={face(row.ownerID, row.ownerName)} />
          ))}
        </ul>
      )}
      {rows.length === 0 && live.length === 0 ? (
        <p className="status-card-empty">现在没有人在做任务。</p>
      ) : rows.length === 0 ? null : (
        <ul className="status-card-rows">
          {rows.map((row) => {
            const owner = row.ownerID ? byID.get(row.ownerID) : undefined;
            return (
              <li key={row.key} className="status-card-row">
                <span className="status-card-face">
                  {owner ? (
                    <Avatar
                      seed={owner.avatar_seed}
                      appearance={owner.appearance}
                      size={20}
                      name={owner.name}
                      agentID={owner.id}
                    />
                  ) : (
                    <span className="work-gone" aria-hidden="true" />
                  )}
                </span>
                <span className="status-card-main">
                  <span className="status-card-top">
                    <span
                      className={`task-card-dot ${row.tone}${
                        row.tone === "running" || row.tone === "awaiting" ? "" : " still"
                      }`}
                      aria-hidden="true"
                    />
                    <span className="status-card-owner">{row.ownerName}</span>
                    <span className="status-card-title">{row.title}</span>
                    <span className={`status-card-status ${row.tone}`}>{row.label}</span>
                  </span>
                  {row.stepLine && <span className="status-card-step">{row.stepLine}</span>}
                  {row.latest && <span className="status-card-latest">{row.latest}</span>}
                  {row.waitingFor && <span className="status-card-waiting">{row.waitingFor}</span>}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {idleNames.length > 0 && <p className="status-card-idle">空闲：{idleNames.join("、")}</p>}
    </div>
  );
}
