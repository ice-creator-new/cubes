// A `kind: "status"` row: the dispatcher's progress card, built from task data and written by no
// member. It reads as a compact card, not as a bot bubble: one row per task, then the idle line.

import type { Agent, StatusCard } from "../api/types";
import { statusCardRows } from "../lib/statusCard";
import { Avatar } from "./Avatar";

export function StatusCardView({ card, agents }: { card: StatusCard; agents: Agent[] }) {
  const { rows, idleNames } = statusCardRows(card, (id) => agents.find((agent) => agent.id === id)?.name);
  const byID = new Map(agents.map((agent) => [agent.id, agent]));
  return (
    <div className="status-card" role="group" aria-label="群里的进度">
      <div className="status-card-head">
        <span>群里的进度</span>
      </div>
      {rows.length === 0 ? (
        <p className="status-card-empty">现在没有人在做任务。</p>
      ) : (
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
