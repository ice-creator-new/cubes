// 「技能」panel, opened from the rail: reusable Agent Skills (https://agentskills.io/specification)
// every bot can use. Each card carries the skill's name, where it came from, its description and
// how often it was used; the switch enables it, the disclosure expands to the rendered SKILL.md
// and its file list. Adding opens a dialog over the panel with four ways in: write it here,
// upload a zip/SKILL.md, import from GitHub, or copy one another agent on this machine keeps
// (Claude Code, Codex, …). Bot-written and imported skills land disabled with a 「待你启用」 badge
// until the user has looked them over.

import { useEffect, useRef, useState, type ChangeEvent } from "react";
import type { LocalSkill, Skill, SkillDetail } from "../api/types";
import { skillSourceLine } from "../lib/skills";
import { store, useTeam } from "../state/store";
import { Modal } from "./Dialog";
import { Icon } from "./Icon";
import { MessageBody } from "./MessageBody";
import { Dialog } from "./Panels";
import { Switch } from "./Connectors";

/** Which dialog is open above the panel: the choices, or one of their forms. */
type AddStep =
  | { step: "choices" }
  | { step: "write"; skill: Skill | null }
  | { step: "upload" }
  | { step: "github" }
  | { step: "local" };

export function SkillsPanel({ onClose }: { onClose: () => void }) {
  const skills = useTeam((s) => s.skills);
  const skillsError = useTeam((s) => s.skillsError);
  const [add, setAdd] = useState<AddStep | null>(null);
  const [confirmingID, setConfirmingID] = useState<string | null>(null);
  const [working, setWorking] = useState<{ id: string; kind: "toggle" | "delete" } | null>(null);
  const [openID, setOpenID] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const list = skills ?? [];

  const toggle = (skill: Skill) => {
    if (openID === skill.id) {
      setOpenID(null);
      setDetail(null);
      return;
    }
    setOpenID(skill.id);
    setDetail(null);
    void store
      .skillDetail(skill.id)
      .then(setDetail)
      .catch((failure: unknown) => {
        setDetail(null);
        setError(typeof failure === "string" ? failure : "没有读到这个技能。");
      });
  };

  return (
    <>
      {add && (
        <Modal>
          <AddSkillDialog add={add} onBack={(step) => setAdd(step)} onClose={() => setAdd(null)} />
        </Modal>
      )}
      <Dialog title="技能" onClose={onClose} wide>
        <p className="hint">所有 bot都能用这里的技能；技能里的脚本只会在沙盒电脑里运行。</p>

        {skillsError && !skills && (
          <p className="form-error" role="alert">
            {skillsError}
          </p>
        )}

        {!skills && !skillsError && <p className="hint">正在读取技能…</p>}

        {skills && list.length === 0 && <p className="hint">还没有技能。自己写一个，或从别处导入一个。</p>}

        {list.length > 0 && (
          <ul className="provider-list connector-list">
            {list.map((skill) => {
              const busy = working?.id === skill.id;
              const expanded = openID === skill.id;
              const bodyID = `skill-body-${skill.id}`;
              return (
                <li key={skill.id} className="provider-card connector-card skill-card">
                  <div className="connector-head">
                    <button
                      type="button"
                      className="connector-head-main"
                      aria-expanded={expanded}
                      aria-controls={bodyID}
                      onClick={() => toggle(skill)}
                    >
                      <span className="connector-mark">
                        <Icon name="skills" size={18} />
                      </span>
                      <span className="connector-head-text">
                        <span className="skill-name-row">
                          <span className="connector-name">{skill.name}</span>
                          {!skill.enabled && <span className="skill-badge">待你启用</span>}
                        </span>
                        <span className="skill-meta">
                          <span className="skill-source">{skillSourceLine(skill)}</span>
                          {skill.has_scripts && (
                            <span className="skill-script-tag">含脚本 · 只在沙盒电脑里运行</span>
                          )}
                          <span className="skill-uses">用了 {skill.use_count} 次</span>
                        </span>
                        <span className="skill-desc" title={skill.description}>
                          {skill.description}
                        </span>
                      </span>
                    </button>
                    <div className="connector-tail">
                      <Switch
                        checked={skill.enabled}
                        disabled={busy}
                        label={skill.enabled ? "停用这个技能" : "启用这个技能"}
                        onChange={() => {
                          setWorking({ id: skill.id, kind: "toggle" });
                          setError(null);
                          void store.updateSkill(skill.id, { enabled: !skill.enabled }).then((failure) => {
                            setWorking(null);
                            if (failure) setError(failure);
                          });
                        }}
                      />
                      <button
                        type="button"
                        className={expanded ? "connector-disclose open" : "connector-disclose"}
                        aria-expanded={expanded}
                        aria-controls={bodyID}
                        aria-label={expanded ? "收起技能" : "展开技能"}
                        title={expanded ? "收起技能" : "展开技能"}
                        onClick={() => toggle(skill)}
                      >
                        <Icon name="chevron" size={12} />
                      </button>
                    </div>
                  </div>

                  {expanded && (
                    <div className="skill-body" id={bodyID}>
                      {detail && detail.id === skill.id ? (
                        <>
                          <div className="skill-markdown">
                            <MessageBody text={detail.body} markdown memberNames={[]} onMention={() => undefined} />
                          </div>
                          {detail.files.length > 0 && (
                            <ul className="skill-files">
                              {detail.files.map((file) => (
                                <li key={file.path} className="skill-file">
                                  <span className="skill-file-path">{file.path}</span>
                                  <span className="skill-file-size">{formatSize(file.size)}</span>
                                </li>
                              ))}
                            </ul>
                          )}
                        </>
                      ) : (
                        <p className="hint">正在读取 SKILL.md…</p>
                      )}
                    </div>
                  )}

                  {confirmingID === skill.id ? (
                    <div className="confirm-step" role="group" aria-label={`确认删除${skill.name}`}>
                      <p className="confirm-prompt">删除「{skill.name}」后，所有 bot都不能再用它，文件也会一起删掉。</p>
                      <div className="confirm-actions">
                        <button
                          type="button"
                          className="danger-button"
                          disabled={busy}
                          onClick={async () => {
                            setWorking({ id: skill.id, kind: "delete" });
                            setError(null);
                            const failure = await store.deleteSkill(skill.id);
                            setWorking(null);
                            setConfirmingID(null);
                            if (failure) setError(failure);
                          }}
                        >
                          <Icon name="trash" size={15} />
                          {busy ? "删除中…" : "删除"}
                        </button>
                        <button type="button" className="ghost-button" disabled={busy} onClick={() => setConfirmingID(null)}>
                          取消
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="connector-actions">
                      <button
                        type="button"
                        className="connector-action"
                        disabled={busy}
                        onClick={() => {
                          setAdd({ step: "write", skill });
                          setError(null);
                        }}
                      >
                        <Icon name="rename" size={14} />
                        编辑
                      </button>
                      <button
                        type="button"
                        className="connector-action danger"
                        disabled={busy}
                        onClick={() => setConfirmingID(skill.id)}
                      >
                        <Icon name="trash" size={14} />
                        删除
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        <button
          type="button"
          className="connector-add"
          onClick={() => {
            setAdd({ step: "choices" });
            setError(null);
          }}
        >
          <Icon name="plus" size={15} />
          添加技能
        </button>

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </Dialog>
    </>
  );
}

/** The three ways in, then the chosen form, all in one sheet over the panel. */
function AddSkillDialog({
  add,
  onBack,
  onClose,
}: {
  add: AddStep;
  onBack: (step: AddStep) => void;
  onClose: () => void;
}) {
  if (add.step === "choices") {
    const choices: {
      step: "write" | "upload" | "github" | "local";
      title: string;
      desc: string;
      icon: "rename" | "file" | "link" | "computer";
    }[] = [
      { step: "local", title: "从本机导入", desc: "从 Claude Code、Codex 等已装好的技能里挑。", icon: "computer" },
      { step: "write", title: "自己写", desc: "写一份 SKILL.md，教 bot 做一件事。", icon: "rename" },
      { step: "upload", title: "上传文件", desc: "上传一个 .zip 或 SKILL.md。", icon: "file" },
      { step: "github", title: "从 GitHub 导入", desc: "从 GitHub 仓库导入一个技能。", icon: "link" },
    ];
    return (
      <Dialog title="添加技能" onClose={onClose} className="skill-choice-dialog">
        <p className="hint">选一种方式添加技能。</p>
        <div className="skill-choices">
          {choices.map((choice) => (
            <button
              key={choice.step}
              type="button"
              className="skill-choice"
              onClick={() => {
                if (choice.step === "write") onBack({ step: "write", skill: null });
                else onBack({ step: choice.step });
              }}
            >
              <span className="connector-mark">
                <Icon name={choice.icon} size={18} />
              </span>
              <span className="skill-choice-text">
                <span className="skill-choice-title">{choice.title}</span>
                <span className="skill-choice-desc">{choice.desc}</span>
              </span>
            </button>
          ))}
        </div>
      </Dialog>
    );
  }
  if (add.step === "write") return <WriteSkillForm skill={add.skill} onClose={onClose} />;
  if (add.step === "upload") return <UploadSkillForm onClose={onClose} />;
  if (add.step === "local") return <LocalSkillPicker onClose={onClose} />;
  return <GitHubSkillForm onClose={onClose} />;
}

/** The write form (new or edit): name, description and the SKILL.md body with a live preview. */
function WriteSkillForm({ skill, onClose }: { skill: Skill | null; onClose: () => void }) {
  const editing = skill !== null;
  const [name, setName] = useState(skill?.name ?? "");
  const [description, setDescription] = useState(skill?.description ?? "");
  const [body, setBody] = useState("");
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    const trimmedName = name.trim();
    const trimmedDescription = description.trim();
    if (!editing) {
      if (!trimmedName) return setError("请给这个技能起个名字。");
      if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(trimmedName)) {
        return setError("技能名只能用小写字母、数字和连字符，且不能以连字符开头或结尾。");
      }
    }
    if (!trimmedDescription) return setError("请写一句这个技能的用途。");
    if (!body.trim()) return setError("请填写 SKILL.md 的内容。");

    setBusy(true);
    const failure = editing
      ? await store.updateSkill(skill.id, { description: trimmedDescription, body })
      : await store.createSkill({ name: trimmedName, description: trimmedDescription, body });
    setBusy(false);
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <Dialog
      title={editing ? `编辑「${skill.name}」` : "写一个新技能"}
      mark={<Icon name="skills" size={18} />}
      onClose={onClose}
      className="skill-form-dialog"
      footer={
        <>
          <button type="button" className="ghost-button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={busy} onClick={() => void save()}>
            {busy ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <div className="provider-form skill-form">
        <div className="connector-field">
          <label className="field-label" htmlFor="skill-name">
            名称
          </label>
          <input
            id="skill-name"
            className="field"
            value={name}
            disabled={editing}
            spellCheck={false}
            placeholder="weekly-report"
            onChange={(event) => setName(event.target.value)}
          />
          <p className="hint">{editing ? "名称不能修改。" : "小写字母、数字和连字符，例如 weekly-report。"}</p>
        </div>

        <div className="connector-field">
          <label className="field-label" htmlFor="skill-description">
            用途
          </label>
          <input
            id="skill-description"
            className="field"
            value={description}
            placeholder="一句话说明这个技能做什么"
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>

        <div className="connector-field">
          <div className="skill-body-head">
            <label className="field-label" htmlFor="skill-body">
              内容
            </label>
            <button type="button" className="link-button" onClick={() => setPreview((value) => !value)}>
              {preview ? "编辑" : "预览"}
            </button>
          </div>
          {preview ? (
            <div className="skill-markdown skill-markdown-preview">
              <MessageBody text={body} markdown memberNames={[]} onMention={() => undefined} />
            </div>
          ) : (
            <textarea
              id="skill-body"
              className="field skill-textarea"
              rows={10}
              spellCheck={false}
              value={body}
              placeholder={"## 做法\n\n一步一步写下怎么完成这件事。"}
              onChange={(event) => setBody(event.target.value)}
            />
          )}
          <p className="hint">这就是 SKILL.md 的正文，用 Markdown 写做法和步骤。</p>
        </div>

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}

/** Uploads a .zip or a single SKILL.md; lands disabled until the user looks it over. */
function UploadSkillForm({ onClose }: { onClose: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const upload = async (file: File) => {
    setError(null);
    setBusy(true);
    const failure = await store.uploadSkill(file);
    setBusy(false);
    if (failure) setError(failure);
    else onClose();
  };

  const pick = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setFileName(file.name);
    void upload(file);
  };

  return (
    <Dialog
      title="上传技能"
      mark={<Icon name="file" size={18} />}
      onClose={onClose}
      className="skill-form-dialog"
      footer={
        <button type="button" className="ghost-button" disabled={busy} onClick={onClose}>
          取消
        </button>
      }
    >
      <div className="provider-form skill-form">
        <p className="hint">上传一个 .zip（含 SKILL.md 的技能文件夹）或单个 SKILL.md。</p>
        <input
          ref={input}
          type="file"
          accept=".zip,.md"
          className="skill-file-input"
          aria-label="选择技能文件"
          onChange={pick}
        />
        <button
          type="button"
          className="primary-button"
          disabled={busy}
          onClick={() => input.current?.click()}
        >
          {busy ? "上传中…" : "选择文件"}
        </button>
        {fileName && busy && <p className="hint">正在上传 {fileName}…</p>}
        <p className="hint">上传后技能会先停用，看过内容后再启用。</p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}

/** Imports a skill from a GitHub URL; lands disabled until the user looks it over. */
function GitHubSkillForm({ onClose }: { onClose: () => void }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    const trimmed = url.trim();
    if (!trimmed) return setError("请填写 GitHub 仓库地址。");
    setBusy(true);
    const failure = await store.importSkillFromGitHub(trimmed);
    setBusy(false);
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <Dialog
      title="从 GitHub 导入"
      mark={<Icon name="link" size={18} />}
      onClose={onClose}
      className="skill-form-dialog"
      footer={
        <>
          <button type="button" className="ghost-button" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={busy} onClick={() => void save()}>
            {busy ? "导入中…" : "导入"}
          </button>
        </>
      }
    >
      <div className="provider-form skill-form">
        <div className="connector-field">
          <label className="field-label" htmlFor="skill-github-url">
            仓库地址
          </label>
          <input
            id="skill-github-url"
            className="field"
            value={url}
            spellCheck={false}
            placeholder="https://github.com/owner/repo"
            onChange={(event) => setUrl(event.target.value)}
          />
          <p className="hint">可以填整个仓库，或仓库里某个技能文件夹的地址。</p>
        </div>
        <p className="hint">导入后技能会先停用，看过内容后再启用。</p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}

/** Lists the skills other agents on this machine keep; each one imports on its own button. */
function LocalSkillPicker({ onClose }: { onClose: () => void }) {
  const [found, setFound] = useState<LocalSkill[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  // Imported this session, so the row flips to 已导入 without re-reading the folders.
  const [done, setDone] = useState<Set<string>>(() => new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    let live = true;
    store
      .localSkills()
      .then((list) => live && setFound(list))
      .catch((failure: unknown) => live && setLoadError(failure instanceof Error ? failure.message : String(failure)));
    return () => {
      live = false;
    };
  }, []);

  const importOne = async (skill: LocalSkill) => {
    setWorking(skill.path);
    const failure = await store.importLocalSkill(skill.path);
    setWorking(null);
    if (failure) setErrors((current) => ({ ...current, [skill.path]: failure }));
    else setDone((current) => new Set(current).add(skill.path));
  };

  return (
    <Dialog
      title="从本机导入"
      mark={<Icon name="computer" size={18} />}
      onClose={onClose}
      className="skill-form-dialog"
      footer={
        <button type="button" className="ghost-button" onClick={onClose}>
          完成
        </button>
      }
    >
      <p className="hint">
        读取 ~/.claude/skills、~/.codex/skills、~/.agents/skills 等文件夹。导入的是一份拷贝，先停用，看过内容后再启用。
      </p>
      {loadError && (
        <p className="form-error" role="alert">
          {loadError}
        </p>
      )}
      {found === null && !loadError && <p className="hint">正在查找…</p>}
      {found?.length === 0 && <p className="hint">这台电脑上没有找到其他应用的技能。</p>}
      {found && found.length > 0 && (
        <ul className="local-skills">
          {found.map((skill) => {
            const imported = skill.installed || done.has(skill.path);
            const error = errors[skill.path] ?? skill.error;
            return (
              <li key={skill.path} className="local-skill">
                <div className="local-skill-text">
                  <span className="local-skill-head">
                    <span className="local-skill-name">{skill.name}</span>
                    <span className="local-skill-app">{skill.app}</span>
                  </span>
                  {skill.description && <span className="skill-desc">{skill.description}</span>}
                  {error && <span className="local-skill-error">{error}</span>}
                </div>
                <button
                  type="button"
                  className="ghost-button"
                  disabled={imported || skill.error !== null || working !== null}
                  onClick={() => void importOne(skill)}
                >
                  {imported ? "已导入" : working === skill.path ? "导入中…" : "导入"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Dialog>
  );
}

/** A file size in a readable unit, for the expanded file list. */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
