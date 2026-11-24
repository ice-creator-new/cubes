// 「设置 → 审批」: every remembered 「总是允许」, what it covers, and a way to forget it.
// A rule is written by a card's third button (or the POST route); deleting it brings the
// card back for that tool.

import { useEffect, useState } from "react";
import { store, useTeam } from "../state/store";
import { Icon } from "./Icon";

export function ApprovalRulesSection() {
  const rules = useTeam((s) => s.approvalRules);
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  useEffect(() => {
    void store.loadApprovalRules();
  }, []);

  const remove = async (id: string) => {
    setRemoving(id);
    setError(null);
    const failure = await store.deleteApprovalRule(id);
    if (failure) setError(failure);
    setRemoving(null);
  };

  return (
    <section className="settings-section">
      <div className="settings-heading">
        <h4>总是允许</h4>
        <p>确认卡上点「总是允许」会记住一条规则：同一位成员的同一种操作以后直接执行；删掉一条，这种操作又会需要确认。</p>
      </div>
      {rules === null ? (
        <p className="hint">正在读取批准规则…</p>
      ) : rules.length === 0 ? (
        <p className="hint">还没有规则。卡片上的「总是允许」会加在这里。</p>
      ) : (
        <ul className="approval-rules" aria-label="批准规则">
          {rules.map((rule) => (
            <li key={rule.id} className="approval-rule">
              <span className="approval-rule-body">
                <span className="approval-rule-title">{rule.title}</span>
                <span className="approval-rule-who">
                  {rule.agent_name ? `「${rule.agent_name}」` : "所有成员"}
                </span>
              </span>
              <button
                type="button"
                className="ghost-button"
                disabled={removing === rule.id}
                onClick={() => void remove(rule.id)}
                aria-label={`删除规则：${rule.title}${rule.agent_name ? `（${rule.agent_name}）` : ""}`}
              >
                <Icon name="trash" size={14} />
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
