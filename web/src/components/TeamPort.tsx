// 团队导出 / 导入: one Markdown file carrying the team's shape, never its history or secrets.
// Import first shows what it would create, rename or leave missing; only then can it apply.

import { useRef, useState } from "react";
import type { TeamImportPlan } from "../api/types";
import { store } from "../state/store";
import { Dialog, Modal } from "./Dialog";

export function TeamPortActions({ onImported }: { onImported: () => void }) {
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const exportTeam = async () => {
    setExporting(true);
    setError(null);
    const result = await store.exportTeam();
    setExporting(false);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    const url = URL.createObjectURL(new Blob([result.text], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "cubes-team.md";
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="team-port-actions">
      <button type="button" className="ghost-button" disabled={exporting} onClick={() => void exportTeam()}>
        {exporting ? "导出中…" : "导出团队"}
      </button>
      <button type="button" className="ghost-button" onClick={() => setImporting(true)}>
        导入团队
      </button>
      {error && (
        <span className="form-error" role="alert">
          {error}
        </span>
      )}
      {importing && (
        <Modal>
          <TeamImportDialog
            onClose={() => setImporting(false)}
            onImported={() => {
              setImporting(false);
              onImported();
            }}
          />
        </Modal>
      )}
    </div>
  );
}

function TeamImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [markdown, setMarkdown] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [plan, setPlan] = useState<TeamImportPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);

  const pickFile = async (picked: File | null) => {
    if (!picked) return;
    setError(null);
    setPlan(null);
    try {
      setMarkdown(await picked.text());
    } catch {
      setError("读不了这个文件。");
    }
  };

  const preview = async () => {
    if (!markdown.trim()) {
      setError("先选择或粘贴团队文件。");
      return;
    }
    setBusy(true);
    setError(null);
    const result = await store.importTeam(markdown, { overwriteSupervisor: overwrite });
    setBusy(false);
    if ("error" in result) setError(result.error);
    else setPlan(result.plan);
  };

  const apply = async () => {
    setBusy(true);
    setError(null);
    const result = await store.importTeam(markdown, { apply: true, overwriteSupervisor: overwrite });
    setBusy(false);
    if ("error" in result) setError(result.error);
    else onImported();
  };

  const changeLine = (plan: TeamImportPlan) => {
    const changes = plan.members.filter((item) => item.action !== "skip");
    const skipped = plan.members.filter((item) => item.action === "skip");
    return (
      <>
        {changes.length > 0 && (
          <p>
            成员：{changes.map((item) => `${item.name}${item.action === "rename" ? ` → ${item.final_name}` : ""}`).join("、")}
          </p>
        )}
        {skipped.length > 0 && <p>保持原样：{skipped.map((item) => item.name).join("、")}</p>}
        {plan.groups.length > 0 && (
          <p>
            分组：{plan.groups.map((item) => `${item.title}${item.action === "rename" ? ` → ${item.final_title}` : ""}`).join("、")}
          </p>
        )}
        {plan.workflows.length > 0 && (
          <p>
            工作流：{plan.workflows.map((item) => `${item.name}${item.action === "rename" ? ` → ${item.final_name}` : ""}`).join("、")}
          </p>
        )}
        {plan.skills.missing.length > 0 && <p>本机没有的技能：{plan.skills.missing.join("、")}</p>}
        {plan.warnings.map((warning, index) => (
          <p key={index} className="muted">
            {warning}
          </p>
        ))}
      </>
    );
  };

  return (
    <Dialog
      title="导入团队"
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" className="ghost-button" disabled={busy} onClick={() => void preview()}>
            {busy ? "读取中…" : "预览"}
          </button>
          <button type="button" className="primary-button" disabled={busy || !plan} onClick={() => void apply()}>
            {busy ? "导入中…" : "应用"}
          </button>
        </>
      }
    >
      <p className="hint">选择一个从 Cubes 导出的团队文件。同名成员、分组和工作流会加上「（2）」而不是覆盖现有内容。</p>
      <input
        ref={file}
        type="file"
        accept=".md,text/markdown,text/plain"
        className="team-port-file"
        onChange={(event) => void pickFile(event.target.files?.[0] ?? null)}
      />
      <textarea
        className="field team-port-text"
        value={markdown}
        placeholder="也可以直接粘贴团队 Markdown"
        aria-label="团队文件内容"
        onChange={(event) => {
          setMarkdown(event.target.value);
          setPlan(null);
        }}
      />
      <label className="connector-option">
        <span className="connector-option-text">
          <span className="connector-option-label">覆盖主管设置</span>
          <span className="connector-option-note">默认不碰现有主管；勾选后用文件里的名字、职责、外观和模型。</span>
        </span>
        <input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} />
      </label>

      {plan && <div className="team-port-plan">{changeLine(plan)}</div>}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  );
}
