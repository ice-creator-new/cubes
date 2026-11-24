import { describe, expect, it } from "vitest";
import type { Agent, Provider, ProvidersState } from "../api/types";
import {
  agentReadsPDFs,
  agentSeesImages,
  attachmentKind,
  formatSize,
  kindLabel,
  MAX_FILE_BYTES,
  planPick,
  prepareImage,
  pdfNote,
} from "./attachments";

const MB = 1024 * 1024;

describe("attachmentKind", () => {
  it("knows the four image types, by type or by extension", () => {
    expect(attachmentKind({ name: "a.png", type: "image/png" })).toBe("image");
    expect(attachmentKind({ name: "a", type: "image/jpeg" })).toBe("image");
    // Finder sometimes hands over a file with no type at all.
    expect(attachmentKind({ name: "shot.JPG", type: "" })).toBe("image");
    expect(attachmentKind({ name: "anim.gif", type: "" })).toBe("image");
  });

  it("knows a PDF by type or by extension", () => {
    expect(attachmentKind({ name: "paper.pdf", type: "application/pdf" })).toBe("pdf");
    expect(attachmentKind({ name: "paper.bin", type: "application/pdf" })).toBe("pdf");
    expect(attachmentKind({ name: "论文.PDF", type: "application/octet-stream" })).toBe("pdf");
  });

  it("takes code and data files as text", () => {
    expect(attachmentKind({ name: "README.md", type: "" })).toBe("text");
    expect(attachmentKind({ name: "app.tsx", type: "" })).toBe("text");
    expect(attachmentKind({ name: "rows.csv", type: "application/vnd.ms-excel" })).toBe("text");
    expect(attachmentKind({ name: "x.json", type: "application/json" })).toBe("text");
    expect(attachmentKind({ name: "x.yaml", type: "application/x-yaml" })).toBe("text");
    expect(attachmentKind({ name: "x.unknownext", type: "text/plain" })).toBe("text");
    expect(attachmentKind({ name: ".env", type: "" })).toBe("text");
    expect(attachmentKind({ name: "Makefile", type: "text/plain" })).toBe("text");
    // No type at all: the server sniffs the bytes, so it may still be text.
    expect(attachmentKind({ name: "Makefile", type: "" })).toBe("text");
  });

  it("has no answer for anything the server would refuse", () => {
    expect(attachmentKind({ name: "a.zip", type: "application/zip" })).toBeNull();
    expect(attachmentKind({ name: "a.mp4", type: "video/mp4" })).toBeNull();
    expect(attachmentKind({ name: "a.svg", type: "image/svg+xml" })).toBeNull();
  });
});

describe("planPick", () => {
  it("lets images through, whatever their size: they are redrawn first", () => {
    expect(planPick({ name: "a.png", type: "image/png", size: 30 * MB })).toEqual({ kind: "image" });
  });

  it("lets PDFs and text files through up to 20 MB", () => {
    expect(planPick({ name: "a.pdf", type: "application/pdf", size: MAX_FILE_BYTES })).toEqual({ kind: "pdf" });
    expect(planPick({ name: "a.md", type: "", size: 1000 })).toEqual({ kind: "text" });
  });

  it("refuses a file past 20 MB with the server's own limit", () => {
    expect(planPick({ name: "a.pdf", type: "application/pdf", size: MAX_FILE_BYTES + 1 })).toEqual({
      kind: "refuse",
      reason: "文件超过 20 MB，请分开发送。",
    });
  });

  it("refuses anything else with the server's sentence", () => {
    expect(planPick({ name: "a.zip", type: "application/zip", size: 10 })).toEqual({
      kind: "refuse",
      reason: "只支持图片、PDF 和文本文件（代码、Markdown、CSV、JSON 等）。",
    });
  });
});

describe("prepareImage", () => {
  it("sends small images untouched", () => {
    expect(prepareImage({ type: "image/png", size: MB }, 1200, 800, true)).toEqual({ kind: "as-is" });
  });

  it("shrinks a large picture to a 2048px long edge, keeping its proportions", () => {
    expect(prepareImage({ type: "image/jpeg", size: MB }, 4096, 3072, false)).toEqual({
      kind: "resize",
      width: 2048,
      height: 1536,
      type: "image/jpeg",
    });
  });

  it("redraws a heavy file even at its own size, and keeps transparency as PNG", () => {
    expect(prepareImage({ type: "image/webp", size: 6 * MB }, 1600, 900, true)).toEqual({
      kind: "resize",
      width: 1600,
      height: 900,
      type: "image/png",
    });
  });

  it("never redraws a GIF, and refuses one past the server limit", () => {
    expect(prepareImage({ type: "image/gif", size: 3 * MB }, 4000, 4000, true)).toEqual({ kind: "as-is" });
    expect(prepareImage({ type: "image/gif", size: 6 * MB }, 100, 100, true).kind).toBe("refuse");
  });

  it("refuses anything that is not one of the four image types", () => {
    expect(prepareImage({ type: "image/svg+xml", size: 10 }, 10, 10, true)).toEqual({
      kind: "refuse",
      reason: "只支持 PNG、JPEG、WebP 和 GIF 图片。",
    });
  });
});

describe("kindLabel and formatSize", () => {
  it("labels a PDF, a code file and a text file without an extension", () => {
    expect(kindLabel({ name: "a.pdf", mime: "application/pdf" })).toBe("PDF");
    expect(kindLabel({ name: "a.md", mime: "text/plain" })).toBe("MD");
    expect(kindLabel({ name: "README", mime: "text/plain" })).toBe("文本");
  });

  it("shows sizes the way a chip reads them", () => {
    expect(formatSize(640)).toBe("640 B");
    expect(formatSize(12 * 1024)).toBe("12 KB");
    expect(formatSize(1.2 * MB)).toBe("1.2 MB");
    expect(formatSize(12 * MB)).toBe("12 MB");
  });
});

describe("agent capabilities", () => {
  const caps = (images: boolean, pdf = false) => ({
    images,
    video: false,
    audio: false,
    pdf,
    context_window: 128000,
    max_output: 8192,
    reasoning: false,
    reasoning_levels: [],
    sources: {},
  });
  const provider = (seeing: string[], pdf: string[] = []): Provider => ({
    id: "p1",
    name: "服务",
    protocol: "openai-chat",
    base_url: "http://x",
    api_key_hint: null,
    models: ["see", "blind"],
    model_caps: {
      see: caps(seeing.includes("see"), pdf.includes("see")),
      blind: caps(seeing.includes("blind"), pdf.includes("blind")),
    },
    model_overrides: {},
    created_at: "",
    updated_at: "",
  });
  const state = (vision: string[], model = "see", pdf: string[] = []): ProvidersState => ({
    providers: [provider(vision, pdf)],
    default: { provider_id: "p1", model },
  });
  const agent = (patch: Partial<Agent>): Agent =>
    ({ id: "a1", name: "主管", kind: "supervisor", model_ref: null, model_status: "default", ...patch }) as Agent;

  it("follows the default model when the bot has no pick of its own", () => {
    expect(agentSeesImages(agent({}), state(["see"]))).toBe(true);
    expect(agentSeesImages(agent({}), state(["see"], "blind"))).toBe(false);
  });

  it("uses the bot's own pick, and the default once that pick no longer resolves", () => {
    const picked = agent({ model_ref: { provider_id: "p1", model: "blind" }, model_status: "custom" });
    expect(agentSeesImages(picked, state(["see"]))).toBe(false);
    const fallback = agent({ model_ref: { provider_id: "gone", model: "x" }, model_status: "fallback" });
    expect(agentSeesImages(fallback, state(["see"]))).toBe(true);
  });

  it("is false with no providers loaded", () => {
    expect(agentSeesImages(agent({}), null)).toBe(false);
    expect(agentReadsPDFs(agent({}), null)).toBe(false);
  });

  it("reads PDFs from the same resolved model's caps", () => {
    expect(agentReadsPDFs(agent({}), state([], "see", ["see"]))).toBe(true);
    expect(agentReadsPDFs(agent({}), state([], "see", ["blind"]))).toBe(false);
    const picked = agent({ model_ref: { provider_id: "p1", model: "blind" }, model_status: "custom" });
    expect(agentReadsPDFs(picked, state([], "see", ["see"]))).toBe(false);
  });

  it("says which model cannot read a PDF, by the bot's name when there is one", () => {
    expect(pdfNote(agent({ name: "研究员" }))).toBe("「研究员」用的模型读不到 PDF 内容，只能看到文件名。");
    expect(pdfNote(undefined)).toBe("这个机器人用的模型读不到 PDF 内容，只能看到文件名。");
  });
});
