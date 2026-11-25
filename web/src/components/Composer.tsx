// Grok-style composer: one 48px pill that grows upward, @ and / pickers above it, an icon-only @
// and the send/stop button on the right. Enter sends, Shift+Enter breaks a line, ↑↓ + Enter/Tab
// pick a member (after @) or a skill (after /) while a picker is open. Replying shows the quoted
// message above the field.
// Images, PDFs and text files come in by paste, drop or the + button; they upload at once and
// wait above the text (images as thumbnails, files as chips) until the message goes.

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Agent, Run } from "../api/types";
import { errorMessage } from "../api/client";
import {
  MAX_ATTACHMENTS,
  ATTACHMENT_ACCEPT,
  agentReadsPDFs,
  agentSeesImages,
  blindNote,
  formatSize,
  kindLabel,
  pdfNote,
  type Kind,
} from "../lib/attachments";
import { composingKey } from "../lib/ime";
import { insertMention, mentionCandidates, mentionedIDs, mentionQuery } from "../lib/mentions";
import { insertSlash, slashQuery } from "../lib/skills";
import { useAnimatedHeight, usePresence } from "../lib/motion";
import { filesOf, hasFiles, prepareAttachment } from "../lib/pickAttachments";
import { stopChoices } from "../lib/sendMode";
import { drafts, moodFor, store, useDraft, useTeam } from "../state/store";
import { Avatar } from "./Avatar";
import { AskPanel, type AskQuestion } from "./AskPanel";
import { ComposerField, fieldSelection } from "./ComposerField";
import { ContextBar } from "./ContextBar";
import { Icon } from "./Icon";
import { seedAttachmentURL } from "./Images";

/** One attachment waiting in the composer: its local bytes until (and after) the upload answers. */
interface Draft {
  key: string;
  /** A blob: URL of the prepared file; an image shows it, a file chip needs only the name. */
  url: string;
  name: string;
  mime: string;
  size: number;
  kind: Kind;
  id: string | null;
}

let pendingSeq = 0;

/** `@所有人`: every member answers on its own; the server reads it from the text. */
const EVERYONE = "所有人";
const EVERYONE_ID = "__everyone__";

export function Composer({
  conversationID,
  roster,
  outside = [],
  runs,
  placeholder,
  autoFocus,
  fallbackAnswerer,
  ask,
}: {
  conversationID: string;
  roster: Agent[];
  /** Team agents not in this group: offered under 「不在群里」, and pulled in when @-ed. */
  outside?: Agent[];
  /** Every live lane here: a bot may be working while another answers. */
  runs: Run[];
  placeholder: string;
  autoFocus?: boolean;
  /** Who answers when the draft names nobody: a direct chat's member, else the supervisor. */
  fallbackAnswerer?: Agent;
  /** A bot's question this run is parked on: the panel takes the text box's place. */
  ask?: AskQuestion | null;
}) {
  const state = useTeam((s) => s);
  const draft = useDraft(conversationID);
  const reply = state.replying[conversationID] ?? null;
  /** Correcting one of your own messages: the text sits here and the send re-answers it. */
  const editing = state.editing[conversationID] ?? null;
  const sending = Boolean(state.sending[conversationID]);
  const stopping = Boolean(state.stopping[conversationID]);
  // Who answers the next message: the first member the draft names, else the fallback.
  const named = mentionedIDs(draft, roster);
  const answerer = (named.length > 0 ? roster.find((agent) => agent.id === named[0]) : undefined) ?? fallbackAnswerer;
  const answererSees = agentSeesImages(answerer, state.providers);
  const [caret, setCaret] = useState(draft.length);
  const [highlight, setHighlight] = useState(0);
  const [pickerDismissed, setPickerDismissed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tall, setTall] = useState(false);
  const field = useRef<HTMLDivElement>(null);
  // The input method's own state; WebKit's Enter flags can't be trusted (see lib/ime).
  const composition = useRef<{ open: boolean; endedAt: number | null }>({ open: false, endedAt: null });
  const picker = useRef<HTMLInputElement>(null);
  const [attachments, setAttachments] = useState<Draft[]>([]);
  const [dragging, setDragging] = useState(false);
  // The quote chip stays mounted while it folds away.
  const quote = usePresence(editing ? null : reply, 200);
  const editChip = usePresence(editing, 200);
  const uploading = attachments.some((item) => item.id === null);
  const hasImages = attachments.some((item) => item.kind === "image");
  const hasPDFs = attachments.some((item) => item.kind === "pdf");

  const query = mentionQuery(draft, caret);
  const candidates = useMemo(() => {
    if (!query) return [];
    const needle = query.text.toLowerCase();
    const found = mentionCandidates(roster, outside, needle, 6);
    // A group (more than the supervisor and one member) can be addressed as a whole.
    const members = roster.filter((agent) => agent.kind === "member").length;
    if (members > 1 && EVERYONE.includes(needle)) {
      return [
        {
          agent: { ...roster[0], id: EVERYONE_ID, name: EVERYONE, instructions: "每位成员各自独立回答" } as Agent,
          outside: false,
        },
        ...found,
      ];
    }
    return found;
  }, [query, roster, outside]);
  // `/名称` calls a skill; only enabled ones are offered, best match (name prefix) first.
  const slash = query ? null : slashQuery(draft, caret);
  const skillCandidates = useMemo(() => {
    if (!slash) return [];
    const needle = slash.text.toLowerCase();
    const enabled = (state.skills ?? []).filter((skill) => skill.enabled && skill.name.includes(needle));
    return enabled
      .sort((a, b) => Number(!a.name.startsWith(needle)) - Number(!b.name.startsWith(needle)))
      .slice(0, 6);
  }, [slash, state.skills]);
  const mentionOpen = query !== null && candidates.length > 0 && !pickerDismissed;
  const skillOpen = slash !== null && skillCandidates.length > 0 && !pickerDismissed;
  const pickerOpen = mentionOpen || skillOpen;
  const pickerCount = mentionOpen ? candidates.length : skillCandidates.length;
  const busy = runs.length > 0;
  // An edit carries only text: the attachments of the message being corrected stay with it.
  const hasDraft = editing ? draft.trim().length > 0 : draft.trim().length > 0 || attachments.length > 0;
  const canSend = hasDraft && !sending && !uploading;
  // While a reply runs with nothing typed the button stops it; with text it sends in the
  // mode the bot's and the team's settings resolve to.
  const stopMode = busy && !hasDraft && !sending && !editing;
  // Several lanes at once make "stop" ambiguous: the button then opens a menu that names each.
  // The menu lives on `document.body` at the button's screen point: the composer's frosted card
  // and the chat's own layers would otherwise paint over a popup nested inside them.
  const [stopMenu, setStopMenu] = useState(false);
  const [stopAnchor, setStopAnchor] = useState<{ right: number; bottom: number } | null>(null);
  const stopMenuRef = useRef<HTMLDivElement>(null);
  const stopOptions = stopChoices(
    runs,
    (run) =>
      roster.find((agent) => agent.id === run.current_agent_id)?.name ??
      // A run's turn has not landed in current_agent_id yet: its own activity names the bot.
      Object.values(state.activities).find((activity) => activity.run_id === run.id)?.agent_name ??
      "机器人",
  );

  useEffect(() => {
    if (!stopMenu) return;
    const close = () => setStopMenu(false);
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (!target?.closest(".stop-menu") && !target?.closest(".stop-menu-anchor")) close();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("pointerdown", onPointer);
    window.addEventListener("keydown", onKey);
    // A fixed menu would drift away from its button: the first scroll or resize puts it away.
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("pointerdown", onPointer);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [stopMenu]);

  useEffect(() => {
    if (!stopMode) setStopMenu(false);
  }, [stopMode]);

  const toggleStopMenu = () => {
    if (runs.length <= 1) {
      void store.stop(conversationID, runs[0]?.id);
      return;
    }
    const rect = stopMenuRef.current?.getBoundingClientRect();
    if (rect) setStopAnchor({ right: window.innerWidth - rect.right, bottom: window.innerHeight - rect.top + 10 });
    setStopMenu((open) => !open);
  };

  const stopLane = (id?: string) => {
    setStopMenu(false);
    void store.stop(conversationID, id);
  };

  // A conversation's tray belongs to it: switching away drops what was not sent.
  useEffect(() => {
    setAttachments([]);
  }, [conversationID]);

  // Correcting a message takes the tray over: its attachments stay on the message, not here.
  useEffect(() => {
    if (editing) setAttachments([]);
  }, [editing?.messageID]);

  const addAttachments = async (files: File[]) => {
    if (files.length === 0) return;
    setError(null);
    const room = MAX_ATTACHMENTS - attachments.length;
    if (room <= 0) {
      setError("一条消息最多带 4 个附件。");
      return;
    }
    if (files.length > room) setError("一条消息最多带 4 个附件，多出来的没有加进来。");
    for (const file of files.slice(0, room)) {
      const prepared = await prepareAttachment(file);
      if (!prepared.ok) {
        setError(prepared.reason);
        continue;
      }
      const key = `a${++pendingSeq}`;
      const url = URL.createObjectURL(prepared.blob);
      const item: Draft = {
        key,
        url,
        name: file.name || (prepared.kind === "image" ? "图片" : "文件"),
        mime: prepared.blob.type || (prepared.kind === "pdf" ? "application/pdf" : "text/plain"),
        size: prepared.blob.size,
        kind: prepared.kind,
        id: null,
      };
      setAttachments((list) => [...list, item]);
      try {
        // The name rides along so the message shows the file as it was called.
        const saved = await store.uploadAttachment(prepared.blob, file.name);
        seedAttachmentURL(saved.id, prepared.blob);
        setAttachments((list) => list.map((entry) => (entry.key === key ? { ...entry, id: saved.id } : entry)));
      } catch (failure) {
        setAttachments((list) => list.filter((entry) => entry.key !== key));
        URL.revokeObjectURL(url);
        setError(errorMessage(failure));
      }
    }
  };

  const removeAttachment = (key: string) => {
    setAttachments((list) => {
      const gone = list.find((item) => item.key === key);
      if (gone) URL.revokeObjectURL(gone.url);
      return list.filter((item) => item.key !== key);
    });
  };

  // The field glides to its measured height; past one line the pill eases into a softer radius
  // alongside it. A key repeat snaps, so typing never trails the caret.
  useAnimatedHeight(field, draft, { max: 200, onChange: (height) => setTall(height > 36) });

  // Who a capsule in the field shows, and which `/名称` become one: enabled skills only, the
  // same list the / picker offers.
  const agentsByName = useMemo(() => new Map(roster.map((agent) => [agent.name, agent])), [roster]);
  const skillNames = useMemo(
    () => new Set((state.skills ?? []).filter((skill) => skill.enabled).map((skill) => skill.name)),
    [state.skills],
  );

  useEffect(() => {
    if (autoFocus) field.current?.focus();
  }, [autoFocus, conversationID]);

  // Choosing 回复 puts the caret where the answer goes, without stealing it on every render.
  const replyingTo = reply?.messageID ?? null;
  useEffect(() => {
    if (replyingTo) field.current?.focus();
  }, [replyingTo]);

  // An edit takes the field too: the text the user is about to correct must be right there.
  const editingID = editing?.messageID ?? null;
  useEffect(() => {
    if (editingID) field.current?.focus();
  }, [editingID]);

  const update = (text: string, nextCaret: number) => {
    drafts.set(conversationID, text);
    setCaret(nextCaret);
    setHighlight(0);
    setPickerDismissed(false);
    setError(null);
  };

  // Where the caret goes once the draft a pick wrote has rendered. The field applies it in its
  // layout effect, before the next keystroke can land: a frame later, fast typing would already
  // have moved on and the stale position would split what was typed.
  const pendingCaret = useRef<number | null>(null);
  // The caret in draft offsets: from the field when it has the selection, else the last known.
  const caretNow = () => fieldSelection(field.current)?.[1] ?? caret;

  const place = (result: { text: string; caret: number }) => {
    pendingCaret.current = result.caret;
    update(result.text, result.caret);
  };

  function pick(name: string) {
    place(insertMention(drafts.get(conversationID), name, caretNow()));
  }

  function pickSkill(name: string) {
    place(insertSlash(drafts.get(conversationID), name, caretNow()));
  }

  // A send leaves the mode to the server, which reads the bot's and the team's settings: the
  // message never interrupts work unless 「停下再发」 is the chosen mode.
  const send = async () => {
    if (!canSend) return;
    if (editing) {
      const failure = await store.editMessage(conversationID, editing.messageID, draft);
      if (failure) setError(failure);
      else setCaret(0);
      return;
    }
    const ids = mentionedIDs(draft, roster);
    const attachmentIDs = attachments.map((item) => item.id).filter((id): id is string => id !== null);
    const failure = await store.send(conversationID, draft, ids, reply?.messageID, attachmentIDs);
    if (failure) setError(failure);
    else {
      setCaret(0);
      // The previews are not revoked: the sent message shows the same files from the cache.
      setAttachments([]);
    }
  };

  const openPicker = () => {
    const at = caretNow();
    const head = draft.slice(0, at);
    const spacer = head === "" || /\s$/u.test(head) ? "" : " ";
    place({ text: `${head}${spacer}@${draft.slice(at)}`, caret: head.length + spacer.length + 1 });
  };

  // A question takes the whole bottom line: the field cannot send a message while a run is
  // parked on an answer, and the question's options are what the user needs at hand.
  if (ask) {
    return (
      <div className="composer-wrap">
        <AskPanel
          ask={ask}
          onAnswer={(answers, skip) => store.answerAction(ask.id, { answers, skip })}
        />
      </div>
    );
  }

  return (
    <div className="composer-wrap">
      {mentionOpen && (
        <div className="mention-picker" role="listbox" aria-label="点名成员">
          {candidates.map(({ agent, outside: isOutside }, index) => (
            <Fragment key={agent.id}>
              {isOutside && !candidates[index - 1]?.outside && (
                <div className="mention-divider" role="presentation">
                  不在群里
                </div>
              )}
              <button
                type="button"
                role="option"
                aria-selected={index === highlight}
                className={index === highlight ? "mention-option active" : "mention-option"}
                onMouseEnter={() => setHighlight(index)}
                onMouseDown={(event) => {
                  event.preventDefault();
                  pick(agent.name);
                }}
              >
                {agent.id === EVERYONE_ID ? (
                  <Icon name="team" size={20} />
                ) : (
                  <Avatar
                    seed={agent.avatar_seed}
                    appearance={agent.appearance}
                    size={24}
                    name={agent.name}
                    mood={moodFor(state, agent.id, conversationID)}
                  />
                )}
                <span className="mention-name">{agent.name}</span>
                <span className="mention-role">
                  {agent.id === EVERYONE_ID
                    ? agent.instructions
                    : isOutside
                      ? "不在群里"
                      : agent.kind === "supervisor"
                        ? "负责人"
                        : agent.instructions.split("\n")[0]}
                </span>
              </button>
            </Fragment>
          ))}
        </div>
      )}
      {skillOpen && (
        <div className="mention-picker" role="listbox" aria-label="使用技能">
          {skillCandidates.map((skill, index) => (
            <button
              key={skill.id}
              type="button"
              role="option"
              aria-selected={index === highlight}
              className={index === highlight ? "mention-option active" : "mention-option"}
              onMouseEnter={() => setHighlight(index)}
              onMouseDown={(event) => {
                event.preventDefault();
                pickSkill(skill.name);
              }}
            >
              <span className="slash-mark" aria-hidden="true">
                <Icon name="skills" size={14} />
              </span>
              <span className="mention-name">/{skill.name}</span>
              <span className="mention-role">{skill.description}</span>
            </button>
          ))}
        </div>
      )}

      <form
        className={["composer", busy ? "busy" : "", tall ? "tall" : "", reply ? "replying" : "", dragging ? "dropping" : ""]
          .filter(Boolean)
          .join(" ")}
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
        onDragOver={(event) => {
          if (!hasFiles(event.dataTransfer)) return;
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
        }}
        onDrop={(event) => {
          const files = filesOf(event.dataTransfer);
          setDragging(false);
          if (files.length === 0) return;
          event.preventDefault();
          void addAttachments(files);
        }}
      >
        {/* The capacity line is the card's own top edge, not a separate row above it. */}
        <ContextBar conversationID={conversationID} runs={runs} busy={busy} />
        <div
          className={quote.open ? "reply-slot collapse open" : "reply-slot collapse"}
          inert={!quote.open}
        >
          <div className="collapse-inner">
            {quote.value && (
              <div className="reply-chip">
                <span className="reply-chip-icon" aria-hidden="true">
                  <Icon name="reply" size={14} />
                </span>
                <span className="reply-chip-body">
                  <span className="reply-chip-label" id={`reply-chip-${conversationID}`}>
                    回复 {quote.value.name}
                  </span>
                  <span className="reply-chip-excerpt">{quote.value.excerpt}</span>
                </span>
                <button
                  type="button"
                  className="reply-chip-close"
                  aria-label="取消回复"
                  title="取消回复"
                  onClick={() => store.cancelReply(conversationID)}
                >
                  <Icon name="close" size={13} />
                </button>
              </div>
            )}
          </div>
        </div>
        <div
          className={editChip.open ? "reply-slot collapse open" : "reply-slot collapse"}
          inert={!editChip.open}
        >
          <div className="collapse-inner">
            {editChip.value && (
              <div className="reply-chip editing">
                <span className="reply-chip-icon" aria-hidden="true">
                  <Icon name="compose" size={14} />
                </span>
                <span className="reply-chip-body">
                  <span className="reply-chip-label" id={`edit-chip-${conversationID}`}>
                    正在编辑消息
                  </span>
                  <span className="reply-chip-excerpt">
                    发送后重新生成回复，这条消息后面的内容会被换掉
                  </span>
                </span>
                <button
                  type="button"
                  className="reply-chip-close"
                  aria-label="取消编辑"
                  title="取消编辑（Esc）"
                  onClick={() => store.cancelEdit(conversationID)}
                >
                  <Icon name="close" size={13} />
                </button>
              </div>
            )}
          </div>
        </div>
        {attachments.length > 0 && (
          <ul className="composer-attachments" aria-label="要发送的附件">
            {attachments.map((item, index) =>
              item.kind === "image" ? (
                <li key={item.key} className={item.id ? "composer-image" : "composer-image uploading"}>
                  <img src={item.url} alt={`图片 ${index + 1}`} draggable={false} />
                  {!item.id && <span className="composer-image-wait" role="status" aria-label="正在上传" />}
                  <button
                    type="button"
                    className="composer-image-remove"
                    aria-label={`移除图片 ${index + 1}`}
                    title="移除"
                    onClick={() => removeAttachment(item.key)}
                  >
                    <Icon name="close" size={11} />
                  </button>
                </li>
              ) : (
                <li key={item.key} className={item.id ? "composer-file" : "composer-file uploading"}>
                  <span className="composer-file-icon" aria-hidden="true">
                    <Icon name="file" size={16} />
                  </span>
                  <span className="composer-file-text">
                    <span className="composer-file-name" title={item.name}>
                      {item.name}
                    </span>
                    <span className="composer-file-meta">
                      {kindLabel(item)} · {formatSize(item.size)}
                    </span>
                  </span>
                  {!item.id && <span className="composer-file-wait" role="status" aria-label="正在上传" />}
                  <button
                    type="button"
                    className="composer-file-remove"
                    aria-label={`移除 ${item.name}`}
                    title="移除"
                    onClick={() => removeAttachment(item.key)}
                  >
                    <Icon name="close" size={11} />
                  </button>
                </li>
              ),
            )}
          </ul>
        )}
        <div className="composer-row">
          <button
            type="button"
            className="add-button"
            onClick={() => picker.current?.click()}
            disabled={sending || Boolean(editing) || attachments.length >= MAX_ATTACHMENTS}
            aria-label="添加图片或文件"
            title={
              editing
                ? "编辑消息时不能再加附件"
                : attachments.length >= MAX_ATTACHMENTS
                  ? "一条消息最多带 4 个附件"
                  : "添加图片或文件"
            }
          >
            <Icon name="plus" size={18} />
          </button>
          <input
            ref={picker}
            type="file"
            accept={ATTACHMENT_ACCEPT}
            multiple
            hidden
            onChange={(event) => {
              const files = [...(event.currentTarget.files ?? [])];
              event.currentTarget.value = "";
              void addAttachments(files);
            }}
          />
          <ComposerField
            fieldRef={field}
            value={draft}
            caretTarget={pendingCaret}
            agents={agentsByName}
            skills={skillNames}
            composing={composition}
            placeholder={editing ? "修改这条消息，回车重新生成" : placeholder}
            describedBy={
              editing
                ? `edit-chip-${conversationID}`
                : reply
                  ? `reply-chip-${conversationID}`
                  : undefined
            }
            onChange={update}
            onCaret={setCaret}
            onCompositionStart={() => {
              composition.current = { open: true, endedAt: null };
            }}
            onCompositionEnd={() => {
              composition.current = { open: false, endedAt: performance.now() };
            }}
            onPaste={(event) => {
              const files = filesOf(event.clipboardData);
              if (files.length === 0) return;
              event.preventDefault();
              void addAttachments(files);
            }}
            onKeyDown={(event) => {
              // Keys the input method uses to pick or confirm a candidate are never ours.
              const { isComposing, keyCode } = event.nativeEvent;
              const { open, endedAt } = composition.current;
              if (composingKey({ isComposing, keyCode, composing: open, composedAt: endedAt, now: performance.now() })) return;
              if (pickerOpen) {
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  const step = event.key === "ArrowDown" ? 1 : -1;
                  setHighlight((value) => (value + step + pickerCount) % pickerCount);
                  return;
                }
                if (event.key === "Enter" || event.key === "Tab") {
                  event.preventDefault();
                  if (mentionOpen) {
                    const chosen = candidates[highlight];
                    if (chosen) pick(chosen.agent.name);
                  } else {
                    const chosen = skillCandidates[highlight];
                    if (chosen) pickSkill(chosen.name);
                  }
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setPickerDismissed(true);
                  return;
                }
              }
              if (event.key === "Escape" && editing) {
                event.preventDefault();
                store.cancelEdit(conversationID);
                return;
              }
              if (event.key === "Escape" && reply) {
                event.preventDefault();
                store.cancelReply(conversationID);
                return;
              }
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-actions">
            {roster.length > 0 && (
              <button
                type="button"
                className="at-button"
                onClick={openPicker}
                disabled={sending}
                aria-label="点名成员"
                title="点名成员"
              >
                <Icon name="at" size={16} />
              </button>
            )}
            <div className="stop-menu-anchor" ref={stopMenuRef}>
              <button
                type={stopMode ? "button" : "submit"}
                className="send-button"
                onClick={stopMode ? toggleStopMenu : undefined}
                disabled={stopMode ? stopping : !canSend}
                aria-haspopup={stopMode && runs.length > 1 ? "menu" : undefined}
                aria-expanded={stopMode && runs.length > 1 ? stopMenu : undefined}
                aria-label={
                  stopMode ? (stopping ? "正在停止" : "停止") : editing ? "保存并重新生成" : "发送"
                }
                title={
                  stopMode
                    ? stopping
                      ? "正在停止"
                      : runs.length > 1
                        ? "停下哪一路"
                        : "停止"
                    : editing
                      ? "保存并重新生成（Esc 取消）"
                      : "发送"
                }
              >
                <span className={stopMode || sending ? "send-glyph" : "send-glyph on"}>
                  <Icon name="send" size={18} />
                </span>
                <span className={stopMode ? "send-glyph on" : "send-glyph"}>
                  <Icon name="stop" size={14} />
                </span>
                {sending && (
                  <span className="send-glyph on">
                    <span className="spinner" />
                  </span>
                )}
              </button>
            </div>
          </div>
        </div>
      </form>
      {stopMode &&
        stopMenu &&
        stopAnchor &&
        runs.length > 1 &&
        createPortal(
          <div
            className="stop-menu"
            role="menu"
            aria-label="停下哪一路"
            style={{ right: stopAnchor.right, bottom: stopAnchor.bottom }}
          >
            {stopOptions.map((option) => (
              <button
                key={option.id || "all"}
                type="button"
                role="menuitem"
                className={option.id ? "stop-menu-item" : "stop-menu-item all"}
                disabled={stopping}
                onClick={() => stopLane(option.id || undefined)}
              >
                <Icon name="stop" size={11} />
                <span>{option.label}</span>
              </button>
            ))}
          </div>,
          document.body,
        )}
      {hasImages && !answererSees && <p className="composer-note">{blindNote(answerer)}</p>}
      {hasPDFs && !agentReadsPDFs(answerer, state.providers) && <p className="composer-note">{pdfNote(answerer)}</p>}
      {error && (
        <p className="composer-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
