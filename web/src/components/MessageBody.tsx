// Agent replies render as GitHub-flavoured Markdown; `@名字` tokens for real members become
// tappable capsules (the member's face and name). User text stays plain apart from the capsules,
// and `/技能名` there is shown as a small skill tag.
//
// A reply that streams while it is on screen is revealed at a steady pace, each batch of
// characters fading in on its own clock (see `lib/streamReveal.ts`). The source is cut into
// top-level blocks and each block is its own memoised Markdown render, so a frame only re-parses
// the block being written; finished blocks keep identical props and are skipped.

import { isValidElement, memo, useEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { rehypeHighlightCode } from "../lib/highlight";
import { normalizeMath } from "../lib/math";
import { openExternally } from "../lib/links";
import { mentionTokens } from "../lib/mentions";
import { slashTokens } from "../lib/skills";
import { batchesWithin, rehypeFadeIn, rehypeMentions, sameBatches, splitBlocks, type Batch } from "../lib/streamReveal";
import { useStreamReveal } from "../lib/useStreamReveal";
import type { Agent } from "../api/types";
import { useTeam } from "../state/store";
import { Avatar } from "./Avatar";
import { Icon } from "./Icon";

/** The text a node renders, for copying a code block exactly as written. */
function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

/** The fence's language, from the `language-…` class Markdown puts on the inner `<code>`. */
function languageOf(children: ReactNode): string {
  const code: ReactNode = Array.isArray(children) ? children[0] : children;
  if (!isValidElement<{ className?: string }>(code)) return "";
  const element: ReactElement<{ className?: string }> = code;
  return /language-([\w+#.-]+)/.exec(element.props.className ?? "")?.[1] ?? "";
}

/** A fenced code block: its language and a copy button sit on a quiet head row above it. */
function CodeBlock({ children }: { children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const language = languageOf(children);
  return (
    <div className="code-block">
      <div className="code-head">
        <span className="code-lang">{language || "代码"}</span>
        <button
          type="button"
          className={copied ? "code-copy done" : "code-copy"}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(textOf(children).replace(/\n$/, ""));
              setCopied(true);
            } catch {
              // No clipboard (an insecure context): the button just stays as it is.
            }
          }}
        >
          <Icon name={copied ? "check" : "copy"} size={13} />
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

/** `@名字` as a capsule: the member's face, then the name; without an agent it shows the `@`. */
function MentionChip({ name, agent, onMention }: { name: string; agent: Agent | undefined; onMention: (name: string) => void }) {
  return (
    <button type="button" className="mention" onClick={() => onMention(name)} title={`@${name}`}>
      {agent && (
        <span className="mention-face" aria-hidden="true">
          <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={16} name={agent.name} frozen />
        </span>
      )}
      <span className="mention-label">
        <span className="mention-at">@</span>
        {name}
      </span>
    </button>
  );
}

function withMentions(
  text: string,
  names: ReadonlySet<string>,
  agents: ReadonlyMap<string, Agent>,
  skills: ReadonlySet<string>,
  onMention: (name: string) => void,
): ReactNode[] {
  const tokens = [
    ...mentionTokens(text)
      .filter((token) => names.has(token.name))
      .map((token) => ({ ...token, kind: "mention" as const })),
    ...slashTokens(text, skills).map((token) => ({ ...token, kind: "skill" as const })),
  ].sort((a, b) => a.start - b.start);
  const out: ReactNode[] = [];
  let cursor = 0;
  for (const token of tokens) {
    if (token.start < cursor) continue;
    if (token.start > cursor) out.push(text.slice(cursor, token.start));
    out.push(
      token.kind === "mention" ? (
        <MentionChip key={`${token.start}-${token.name}`} name={token.name} agent={agents.get(token.name)} onMention={onMention} />
      ) : (
        <span key={`${token.start}-${token.name}`} className="skill-call" title="使用技能">
          <span className="skill-call-mark" aria-hidden="true">
            <Icon name="skills" size={12} />
          </span>
          <span className="mention-label">/{token.name}</span>
        </span>
      ),
    );
    cursor = token.end;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}

// Math: `$…$` inline and `$$…$$` display, typeset by KaTeX. A formula it cannot parse is shown
// as its source in the error colour instead of breaking the reply (rehype-katex never throws).
const REMARK = [remarkGfm, remarkMath];
const KATEX_OPTIONS = { strict: "ignore", output: "htmlAndMathml" } as const;

/** One top-level block. A finished block keeps the same props frame after frame and is skipped. */
const MarkdownBlock = memo(
  function MarkdownBlock({
    source,
    batches,
    names,
    components,
  }: {
    source: string;
    batches: readonly Batch[];
    names: ReadonlySet<string>;
    components: Components;
  }) {
    return (
      <ReactMarkdown
        remarkPlugins={REMARK}
        rehypePlugins={[
          [rehypeKatex, KATEX_OPTIONS],
          rehypeHighlightCode,
          [rehypeMentions, { names }],
          [rehypeFadeIn, { batches }],
        ]}
        components={components}
      >
        {source}
      </ReactMarkdown>
    );
  },
  (before, after) =>
    before.source === after.source &&
    before.names === after.names &&
    before.components === after.components &&
    sameBatches(before.batches, after.batches),
);

export function MessageBody({
  text,
  markdown,
  streaming = false,
  memberNames,
  onMention,
}: {
  text: string;
  markdown: boolean;
  streaming?: boolean;
  memberNames: string[];
  onMention: (name: string) => void;
}) {
  const nameKey = memberNames.join("\u0000");
  // Stable while the roster is unchanged, so finished blocks stay memoised.
  const names = useMemo(() => new Set(memberNames), [nameKey]);
  // The capsule's face: every agent by name (names are unique in the workspace).
  const workspace = useTeam((s) => s.workspace);
  const skillList = useTeam((s) => s.skills);
  const agents = useMemo(() => new Map((workspace?.agents ?? []).map((agent) => [agent.name, agent])), [workspace]);
  const skills = useMemo(() => new Set((skillList ?? []).map((skill) => skill.name)), [skillList]);
  // The chip renderer must stay the same object for memoisation; it reads the latest values.
  const mention = useRef(onMention);
  mention.current = onMention;
  const roster = useRef(agents);
  roster.current = agents;
  const components = useMemo<Components>(
    () => ({
      a: ({ href, children }) => (
        <a href={href} target="_blank" rel="noreferrer noopener" onClick={openExternally}>
          {children}
        </a>
      ),
      table: ({ children }) => (
        <div className="table-scroll">
          <table>{children}</table>
        </div>
      ),
      pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
      button: ({ node, children }) => {
        const name = node?.properties?.dataMention;
        if (typeof name !== "string") return <button type="button">{children}</button>;
        const agent = roster.current.get(name);
        // The text keeps its fade spans; only the leading `@` gives way to the face.
        return (
          <button type="button" className="mention" title={`@${name}`} onClick={() => mention.current(name)}>
            {agent && (
              <span className="mention-face" aria-hidden="true">
                <Avatar seed={agent.avatar_seed} appearance={agent.appearance} size={16} name={agent.name} frozen />
              </span>
            )}
            <span className="mention-label">{children}</span>
          </button>
        );
      },
    }),
    [],
  );
  const reveal = useStreamReveal(text, markdown && streaming);

  if (!markdown) {
    return <p className="plain">{withMentions(text, names, agents, skills, onMention)}</p>;
  }

  // The caret rides along until the reveal has caught up with a finished reply. An empty
  // streaming row has nothing to sit after, so it shows no caret at all.
  const active = text.trim() !== "" && (streaming || reveal.text.length < text.length);
  return (
    <div className={active ? "markdown streaming" : "markdown"}>
      {splitBlocks(normalizeMath(reveal.text)).map((block) => (
        <MarkdownBlock
          key={block.start}
          source={block.source}
          batches={batchesWithin(reveal.batches, block)}
          names={names}
          components={components}
        />
      ))}
    </div>
  );
}
