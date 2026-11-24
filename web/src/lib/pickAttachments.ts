// Turning a picked or pasted file into the bytes that go up: an image is decoded once and redrawn
// smaller, a PDF or text file goes as it is. Browser-only; the decisions live in attachments.ts.

import { planPick, prepareImage } from "./attachments";

export type Prepared =
  | { ok: true; blob: Blob; kind: "image" | "pdf" | "text" }
  | { ok: false; reason: string };

export async function prepareAttachment(file: File): Promise<Prepared> {
  const pick = planPick(file);
  if (pick.kind === "refuse") return { ok: false, reason: pick.reason };
  if (pick.kind !== "image") return { ok: true, blob: file, kind: pick.kind };
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    // The decoder could not read it: either not an image or a damaged one.
    const plan = prepareImage(file, 0, 0, false);
    return { ok: false, reason: plan.kind === "refuse" ? plan.reason : "这张图片打不开，换一张试试。" };
  }
  try {
    const plan = prepareImage(file, bitmap.width, bitmap.height, hasAlpha(file.type));
    if (plan.kind === "refuse") return { ok: false, reason: plan.reason };
    if (plan.kind === "as-is") return { ok: true, blob: file, kind: "image" };
    const canvas = document.createElement("canvas");
    canvas.width = plan.width;
    canvas.height = plan.height;
    const context = canvas.getContext("2d");
    if (!context) return { ok: true, blob: file, kind: "image" };
    context.drawImage(bitmap, 0, 0, plan.width, plan.height);
    const { promise, resolve } = Promise.withResolvers<Blob | null>();
    canvas.toBlob(resolve, plan.type, plan.type === "image/jpeg" ? 0.9 : undefined);
    const blob = await promise;
    return blob ? { ok: true, blob, kind: "image" } : { ok: false, reason: "这张图片处理失败，换一张试试。" };
  } finally {
    bitmap.close();
  }
}

/** Whether a type can carry transparency worth keeping as PNG. */
function hasAlpha(type: string): boolean {
  return type === "image/png" || type === "image/webp";
}

/** The files in a paste or a drop, in order; `prepareAttachment` decides which ones go. */
export function filesOf(items: DataTransfer | null): File[] {
  if (!items) return [];
  return [...items.files];
}

/** Whether a drag carries files at all: the composer opens to any of them, then filters. */
export function hasFiles(items: DataTransfer | null): boolean {
  return items !== null && [...items.items].some((item) => item.kind === "file");
}
