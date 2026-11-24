// Attachments in a message: what a picked file is, how an image is prepared before upload, and
// which bot will be able to read it. The decisions are pure so they can be tested without a canvas.

import type { Agent, ProvidersState } from "../api/types";

/** One message carries at most this many attachments; the server refuses more. */
export const MAX_ATTACHMENTS = 4;
/** The server's per-image limit. */
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
/** The server's per-file limit for PDFs and text. */
export const MAX_FILE_BYTES = 20 * 1024 * 1024;
/** Past either of these a picked image is redrawn smaller before upload. */
export const MAX_EDGE = 2048;
export const RESIZE_ABOVE_BYTES = 4 * 1024 * 1024;

export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"] as const;

const IMAGE_BY_EXTENSION: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
};

/** Types that are text even though they do not start with `text/`. */
const TEXT_TYPES: Record<string, true> = {
  "application/json": true,
  "application/ld+json": true,
  "application/xml": true,
  "application/xhtml+xml": true,
  "application/x-yaml": true,
  "application/yaml": true,
  "application/toml": true,
  "application/javascript": true,
  "application/x-javascript": true,
  "application/typescript": true,
  "application/sql": true,
  "application/x-sh": true,
  "application/x-httpd-php": true,
};

/** Extensions the + button and drops take as text; the server reads them as UTF-8. */
const TEXT_EXTENSIONS: Record<string, true> = Object.fromEntries(
  "txt md markdown csv tsv json jsonl yaml yml toml xml htm html css js jsx ts tsx py rb go rs java kt swift c h cpp hpp cs php sh zsh bash sql log ini env conf cfg"
    .split(" ")
    .map((extension) => [extension, true as const]),
);

/** What the server makes of a file, which is also how the client shows it. */
export type Kind = "image" | "pdf" | "text";

/** The file picker's `accept`: every kind the server takes, by type and by extension. */
export const ATTACHMENT_ACCEPT = [
  ...IMAGE_TYPES,
  "application/pdf",
  "text/*",
  ...Object.keys(TEXT_EXTENSIONS).map((extension) => `.${extension}`),
].join(",");

export const UNSUPPORTED = "只支持图片、PDF 和文本文件（代码、Markdown、CSV、JSON 等）。";

/** The extension of a name, lowercase and without the dot; "" when there is none. */
function extensionOf(name: string): string {
  const at = name.lastIndexOf(".");
  return at < 0 || at === name.length - 1 ? "" : name.slice(at + 1).toLowerCase();
}

/**
 * What the server will make of a file. The bytes decide the MIME in the end, but the client
 * needs a verdict before reading them: by type where the browser knows one, by extension
 * otherwise (a `.md` dropped from Finder often arrives with an empty type).
 */
export function attachmentKind(file: { name: string; type: string }): Kind | null {
  const type = file.type.toLowerCase();
  if (type === "application/pdf") return "pdf";
  const extension = extensionOf(file.name);
  if ((IMAGE_TYPES as readonly string[]).includes(type) || IMAGE_BY_EXTENSION[extension]) return "image";
  if (extension === "pdf") return "pdf";
  if (type.startsWith("text/") || TEXT_TYPES[type] || TEXT_EXTENSIONS[extension]) return "text";
  // No type and no known extension (a Makefile, a LICENSE): the bytes decide, and the server
  // refuses one that turns out to be binary.
  if (type === "") return "text";
  return null;
}

export type Pick =
  | { kind: "image" }
  | { kind: "pdf" }
  | { kind: "text" }
  /** Not something the server takes, or past its limit. */
  | { kind: "refuse"; reason: string };

/** What to do with one picked file, before any of its bytes are read. */
export function planPick(file: { name: string; type: string; size: number }): Pick {
  const kind = attachmentKind(file);
  if (!kind) return { kind: "refuse", reason: UNSUPPORTED };
  // An image past 5 MB is redrawn smaller first, so only other files are refused here.
  if (kind !== "image" && file.size > MAX_FILE_BYTES) {
    return { kind: "refuse", reason: "文件超过 20 MB，请分开发送。" };
  }
  return { kind };
}

export type ImagePlan =
  /** Send the file's bytes as they are. */
  | { kind: "as-is" }
  /** Redraw at this size and encode as this type. */
  | { kind: "resize"; width: number; height: number; type: "image/png" | "image/jpeg" }
  /** Not something the server takes. */
  | { kind: "refuse"; reason: string };

/**
 * What to do with one picked image. GIFs keep their frames, so they go as they are (and the
 * server refuses one past its limit). Others are redrawn when they are too large, keeping a
 * transparent picture as PNG and turning everything else into JPEG.
 */
export function prepareImage(file: { type: string; size: number }, width: number, height: number, alpha: boolean): ImagePlan {
  if (!(IMAGE_TYPES as readonly string[]).includes(file.type)) {
    return { kind: "refuse", reason: "只支持 PNG、JPEG、WebP 和 GIF 图片。" };
  }
  if (file.type === "image/gif") {
    return file.size > MAX_IMAGE_BYTES ? { kind: "refuse", reason: "图片超过 5 MB，请压缩后再发。" } : { kind: "as-is" };
  }
  const longest = Math.max(width, height);
  if (longest <= MAX_EDGE && file.size <= RESIZE_ABOVE_BYTES) return { kind: "as-is" };
  const scale = Math.min(1, MAX_EDGE / longest);
  return {
    kind: "resize",
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
    type: alpha ? "image/png" : "image/jpeg",
  };
}

/** The short kind badge on a file chip: PDF, or the extension in capitals. */
export function kindLabel(file: { name: string; mime: string }): string {
  if (file.mime === "application/pdf") return "PDF";
  const extension = extensionOf(file.name);
  return extension ? extension.toUpperCase() : "文本";
}

/** A size as a chip shows it: 640 B, 12 KB, 1.2 MB. */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  const megabytes = bytes / (1024 * 1024);
  return `${megabytes >= 10 ? Math.round(megabytes) : megabytes.toFixed(1)} MB`;
}

/** The caps of the model a bot answers with right now, or undefined when none resolves. */
function answererCaps(agent: Agent | undefined, providers: ProvidersState | null) {
  if (!agent || !providers) return undefined;
  // A fallback bot answers with the default, exactly as the backend resolves it.
  const ref = agent.model_status === "custom" ? agent.model_ref : providers.default;
  if (!ref) return undefined;
  return providers.providers.find((item) => item.id === ref.provider_id)?.model_caps?.[ref.model];
}

/** Whether the model a bot answers with right now can see images. */
export function agentSeesImages(agent: Agent | undefined, providers: ProvidersState | null): boolean {
  return answererCaps(agent, providers)?.images === true;
}

/** Whether the model a bot answers with right now can read a PDF's contents. */
export function agentReadsPDFs(agent: Agent | undefined, providers: ProvidersState | null): boolean {
  return answererCaps(agent, providers)?.pdf === true;
}

/** The note under the field when the bot about to answer cannot look at the images. */
export function blindNote(agent: Agent | undefined): string {
  if (!agent) return "这个机器人用的模型看不到图片内容，它只能把图片转给能看图的成员。";
  return `「${agent.name}」用的模型看不到图片内容，它只能把图片转给能看图的成员。`;
}

/** The note under the field when the bot about to answer gets a PDF it cannot read. */
export function pdfNote(agent: Agent | undefined): string {
  if (!agent) return "这个机器人用的模型读不到 PDF 内容，只能看到文件名。";
  return `「${agent.name}」用的模型读不到 PDF 内容，只能看到文件名。`;
}
