// First run: the user names the supervisor and picks its look before the workspace opens.
// The form writes through POST /onboarding; the workspace event it returns mounts the shell.

import { useState } from "react";
import { resolveAppearance } from "../bot/catalog";
import { agentByID, store, useTeam } from "../state/store";
import { AppearanceEditor } from "./AppearanceEditor";

const MAX_NAME = 64;

export function Onboarding() {
  const workspace = useTeam((s) => s.workspace);
  const supervisor = agentByID(workspace, workspace?.supervisor_id);
  const seed = supervisor?.avatar_seed ?? "";
  const [name, setName] = useState(supervisor?.name ?? "主管");
  const [look, setLook] = useState(() => resolveAppearance(supervisor?.appearance, seed));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const ready = name.trim().length > 0;

  const submit = async () => {
    if (!ready || saving) return;
    setSaving(true);
    setError(null);
    const failure = await store.completeOnboarding(name.trim(), look);
    setSaving(false);
    if (failure) setError(failure);
  };

  return (
    <main className="onboarding" data-drag-region>
      <div className="onboarding-card">
        <header className="onboarding-head">
          <span className="onboarding-eyebrow">第一次见面</span>
          <h1>给你的团队负责人起个名字</h1>
          <p>
            它会带着团队拆解目标、分派任务，再把结果汇总给你。名字和样子以后都能在「团队」里改。
          </p>
        </header>

        <form
          className="onboarding-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="field-label" htmlFor="onboarding-name">
            名字
          </label>
          <input
            id="onboarding-name"
            className="field onboarding-name"
            value={name}
            maxLength={MAX_NAME}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            aria-label="负责人的名字"
            onChange={(event) => setName(event.target.value)}
          />
          <p className="hint">它会用这个名字出现在消息署名、侧栏和团队里。之后也能改。</p>

          <AppearanceEditor
            seed={seed}
            name={name.trim() || "负责人"}
            agentID={supervisor?.id}
            value={look}
            onChange={setLook}
            previewSize={112}
          />

          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}

          <div className="onboarding-foot">
            <button type="submit" className="primary-button" disabled={!ready || saving}>
              {saving ? "保存中…" : "开始使用"}
            </button>
          </div>
        </form>
      </div>
    </main>
  );
}
