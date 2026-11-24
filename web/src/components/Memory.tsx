// 「记忆」: what the team keeps about the user, each bot and the team as a whole. The rows lie
// on three tabs, pinned ones first; an edit retires the old row and the server answers its
// replacement, so the list never shows the same fact twice.

import { useEffect, useRef, useState } from "react";
import type { Memory, MemoryScope } from "../api/types";
import { usePresence, useSegmentedThumb } from "../lib/motion";
import {
  groupByAgent,
  memoriesInScope,
  memorySpan,
  originLabel,
  retirementLabel,
  searchMemories,
  sortMemories,
  splitMemories,
} from "../lib/memories";
import { agentByID, store, useTeam } from "../state/store";
import { Switch } from "./Connectors";
import { Icon } from "./Icon";
import { Collapse, Dialog } from "./Panels";

type Tab = MemoryScope;

const TABS: readonly (readonly [Tab, string])[] = [
  ["user", "关于你"],
  ["agent", "成员"],
  ["team", "团队"],
];

const EMPTY_TEXT = "还没有记忆。对机器人说「记住……」，或者在这里自己加一条。";

export function MemoryPanel({
  onClose,
  onOpenConversation,
}: {
  onClose: () => void;
  /** Jumps to the conversation a memory was read from; the shell closes the panel for it. */
  onOpenConversation: (conversationID: string) => void;
}) {
  const state = useTeam((s) => s);
  const workspace = state.workspace;
  const data = state.memories;
  const settings = data?.settings ?? null;

  const [tab, setTab] = useState<Tab>("user");
  const [query, setQuery] = useState("");
  const [editingID, setEditingID] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState("");
  const [draftTopic, setDraftTopic] = useState("");
  const [confirmingID, setConfirmingID] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [addContent, setAddContent] = useState("");
  const [addTopic, setAddTopic] = useState("");
  const [retiredOpen, setRetiredOpen] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);

  // The delete step stays rendered through its collapse, one row at a time.
  const deleteStep = usePresence(confirmingID, 200);
  const tabs = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(tabs, tab);

  useEffect(() => {
    void store.loadMemories();
  }, []);

  // A new tab starts clean: no half-open editor or confirmation carried over.
  useEffect(() => {
    setEditingID(null);
    setConfirmingID(null);
    setError(null);
  }, [tab]);

  const agentName = (id: string | null) => (id ? agentByID(workspace, id)?.name ?? null : null);
  const names: Record<string, string> = Object.fromEntries(
    (workspace?.agents ?? []).map((agent) => [agent.id, agent.name]),
  );

  const { active, retired } = data
    ? splitMemories(data.memories)
    : { active: [] as Memory[], retired: [] as Memory[] };
  const rows = sortMemories(searchMemories(memoriesInScope(active, tab), query));
  const retiredRows = sortMemories(searchMemories(memoriesInScope(retired, tab), query));

  const pick = (next: Tab) => {
    if (next === tab) return;
    setTab(next);
  };
  const selectAt = (next: Tab) => {
    pick(next);
    document.getElementById(`memory-tab-${next}`)?.focus();
  };

  const beginEdit = (memory: Memory) => {
    setEditingID(memory.id);
    setDraftContent(memory.content);
    setDraftTopic(memory.topic);
    setError(null);
  };
  const cancelEdit = () => {
    setEditingID(null);
    setError(null);
  };

  const saveEdit = async (memory: Memory) => {
    const content = draftContent.trim();
    if (!content) return;
    setBusy(true);
    setError(null);
    const failure = await store.updateMemory(memory.id, { content, topic: draftTopic.trim() });
    setBusy(false);
    if (failure) {
      setError(failure);
      return;
    }
    setEditingID(null);
  };

  const togglePin = async (memory: Memory) => {
    setError(null);
    const failure = await store.updateMemory(memory.id, { pinned: !memory.pinned });
    if (failure) setError(failure);
  };

  const remove = async (memory: Memory) => {
    setBusy(true);
    setError(null);
    const failure = await store.deleteMemory(memory.id);
    setBusy(false);
    if (failure) {
      setError(failure);
      return;
    }
    setConfirmingID(null);
  };

  const add = async () => {
    const content = addContent.trim();
    if (!content) return;
    setBusy(true);
    setError(null);
    const failure = await store.addMemory({ content, topic: addTopic.trim(), scope: tab });
    setBusy(false);
    if (failure) {
      setError(failure);
      return;
    }
    setAddContent("");
    setAddTopic("");
  };

  const patchSettings = async (patch: { paused?: boolean; auto_extract?: boolean }) => {
    setSavingSettings(true);
    setError(null);
    const failure = await store.setMemorySettings(patch);
    setSavingSettings(false);
    if (failure) setError(failure);
  };

  const row = (memory: Memory) => {
    const editing = editingID === memory.id;
    const confirming = confirmingID === memory.id;
    return (
      <li key={memory.id} className={memory.pinned ? "memory-row pinned" : "memory-row"}>
        {editing ? (
          <div className="memory-edit">
            <label className="field-label" htmlFor={`memory-content-${memory.id}`}>
              内容
            </label>
            <textarea
              id={`memory-content-${memory.id}`}
              className="field memory-textarea"
              value={draftContent}
              autoFocus
              onChange={(event) => setDraftContent(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  cancelEdit();
                }
              }}
            />
            <label className="field-label" htmlFor={`memory-topic-${memory.id}`}>
              主题
            </label>
            <input
              id={`memory-topic-${memory.id}`}
              className="field"
              value={draftTopic}
              placeholder="可选，例如「饮食」"
              onChange={(event) => setDraftTopic(event.target.value)}
            />
            <div className="memory-row-actions">
              <button
                type="button"
                className="primary-button"
                disabled={busy || !draftContent.trim()}
                onClick={() => void saveEdit(memory)}
              >
                保存
              </button>
              <button type="button" className="ghost-button" disabled={busy} onClick={cancelEdit}>
                取消
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="memory-text">
              <p className="memory-content">{memory.content}</p>
              {memory.topic && <p className="memory-topic">{memory.topic}</p>}
            </div>
            <div className="memory-meta">
              <span className="memory-origin">{originLabel(memory, agentName(memory.author_id))}</span>
              {memory.pinned && (
                <span className="memory-pin" title="已置顶">
                  <Icon name="pin-solid" size={11} />
                  已置顶
                </span>
              )}
              {memory.source_conversation_id && (
                <button
                  type="button"
                  className="link-button memory-source"
                  onClick={() => onOpenConversation(memory.source_conversation_id!)}
                >
                  查看来源
                </button>
              )}
              <div className="memory-row-actions">
                <button type="button" className="memory-action" onClick={() => beginEdit(memory)}>
                  编辑
                </button>
                <button type="button" className="memory-action" onClick={() => void togglePin(memory)}>
                  {memory.pinned ? "取消置顶" : "置顶"}
                </button>
                <button
                  type="button"
                  className="memory-action danger"
                  onClick={() => setConfirmingID(confirming ? null : memory.id)}
                >
                  删除
                </button>
              </div>
            </div>
          </>
        )}
        <Collapse open={confirming}>
          {deleteStep.value === memory.id && (
            <div className="confirm-step memory-confirm" role="group" aria-label="确认删除记忆">
              <p className="confirm-prompt">删除这条记忆？删除后无法恢复。</p>
              <div className="confirm-actions">
                <button
                  type="button"
                  className="danger-button"
                  disabled={busy}
                  onClick={() => void remove(memory)}
                >
                  <Icon name="trash" size={15} />
                  {busy ? "删除中…" : "删除"}
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  disabled={busy}
                  autoFocus
                  onClick={() => setConfirmingID(null)}
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </Collapse>
      </li>
    );
  };

  return (
    <Dialog
      title="记忆"
      onClose={onClose}
      wide
      className="memory-dialog"
      closeOnly
      footer={
        <button type="button" className="ghost-button" onClick={onClose}>
          关闭
        </button>
      }
    >
      <p className="memory-lead">记忆只存在这台电脑上，机器人回答时会参考它们。</p>

      <div className="memory-switches">
        <div className="connector-option">
          <span className="connector-option-text">
            <span className="connector-option-label">暂停记忆</span>
            <span className="connector-option-note">打开后，机器人既不会读取已有的记忆，也不会记下新的。</span>
          </span>
          <Switch
            checked={settings?.paused ?? false}
            disabled={!settings || savingSettings}
            label={settings?.paused ? "恢复记忆" : "暂停记忆"}
            onChange={() => void patchSettings({ paused: !settings?.paused })}
          />
        </div>
        <div className="connector-option">
          <span className="connector-option-text">
            <span className="connector-option-label">对话后自动整理</span>
            <span className="connector-option-note">对话结束后，机器人把值得留下的整理成记忆。</span>
          </span>
          <Switch
            checked={settings?.auto_extract ?? false}
            disabled={!settings || savingSettings}
            label={settings?.auto_extract ? "关掉对话后自动整理" : "打开对话后自动整理"}
            onChange={() => void patchSettings({ auto_extract: !settings?.auto_extract })}
          />
        </div>
      </div>

      {settings?.paused && <p className="memory-paused">记忆已暂停：机器人不会读取，也不会记下新的。</p>}

      <div
        className={thumb ? "segmented memory-tabs has-thumb" : "segmented memory-tabs"}
        role="tablist"
        aria-label="记忆分类"
        ref={tabs}
      >
        {thumb && (
          <span
            className={ready ? "segmented-thumb" : "segmented-thumb instant"}
            aria-hidden="true"
            style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
          />
        )}
        {TABS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="tab"
            id={`memory-tab-${value}`}
            data-seg={value}
            aria-selected={tab === value}
            aria-controls="memory-panel"
            tabIndex={tab === value ? 0 : -1}
            className={tab === value ? "on" : ""}
            onClick={() => pick(value)}
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              const at = TABS.findIndex(([name]) => name === value);
              const step = event.key === "ArrowLeft" ? -1 : 1;
              selectAt(TABS[(at + step + TABS.length) % TABS.length]![0]);
            }}
          >
            {label}
          </button>
        ))}
      </div>

      <label className="search memory-search">
        <Icon name="search" size={15} />
        <input
          type="search"
          value={query}
          placeholder="搜索记忆"
          aria-label="搜索记忆"
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>

      {state.memoriesError && (
        <div className="inline-error" role="alert">
          <span>{state.memoriesError}</span>
          <button type="button" className="link-button" onClick={() => void store.loadMemories()}>
            重新读取
          </button>
        </div>
      )}

      <div className="memory-panel" id="memory-panel" role="tabpanel" aria-labelledby={`memory-tab-${tab}`}>
        {!data ? (
          state.memoriesError ? null : (
            <p className="muted">正在读取记忆…</p>
          )
        ) : rows.length === 0 ? (
          <p className="memory-empty">{query.trim() ? "没有找到匹配的记忆。" : EMPTY_TEXT}</p>
        ) : tab === "agent" ? (
          <div className="memory-groups">
            {groupByAgent(rows, names).map((group) => (
              <section key={group.agentID ?? "orphan"} className="memory-group" aria-label={group.name}>
                <h3 className="memory-group-label">{group.name}</h3>
                <ul className="memory-list">{group.memories.map(row)}</ul>
              </section>
            ))}
          </div>
        ) : (
          <ul className="memory-list">{rows.map(row)}</ul>
        )}

        {tab !== "agent" && data && (
          <form
            className="memory-add"
            onSubmit={(event) => {
              event.preventDefault();
              void add();
            }}
          >
            <label className="field-label" htmlFor={`memory-add-content-${tab}`}>
              加一条
            </label>
            <textarea
              id={`memory-add-content-${tab}`}
              className="field memory-textarea"
              rows={2}
              value={addContent}
              placeholder={tab === "user" ? "例如：我喜欢黑咖啡，不加糖。" : "例如：对外回复先给结论，再给理由。"}
              onChange={(event) => setAddContent(event.target.value)}
            />
            <div className="memory-add-foot">
              <input
                className="field memory-topic-field"
                value={addTopic}
                placeholder="主题（可选）"
                aria-label="主题（可选）"
                onChange={(event) => setAddTopic(event.target.value)}
              />
              <button type="submit" className="primary-button" disabled={busy || !addContent.trim()}>
                添加
              </button>
            </div>
          </form>
        )}

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}

        {data && retiredRows.length > 0 && (
          <section className="memory-retired">
            <button
              type="button"
              className="memory-retired-toggle"
              aria-expanded={retiredOpen}
              onClick={() => setRetiredOpen((open) => !open)}
            >
              <Icon name={retiredOpen ? "caret-down" : "chevron"} size={11} />
              已替换和已忘记的
              <span className="memory-retired-count">{retiredRows.length}</span>
            </button>
            <Collapse open={retiredOpen}>
              <ul className="memory-retired-list">
                {retiredRows.map((memory) => (
                  <li key={memory.id} className="memory-retired-row">
                    <p className="memory-retired-content">{memory.content}</p>
                    <p className="memory-retired-meta">
                      <span>{retirementLabel(memory)}</span>
                      <span>{memorySpan(memory)}</span>
                    </p>
                  </li>
                ))}
              </ul>
            </Collapse>
          </section>
        )}
      </div>
    </Dialog>
  );
}
