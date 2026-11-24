// Attachments in a message: images in a small grid with a full-window viewer, PDFs and text
// files as chips below it. Everything comes through the API with the launch token, so each file
// is fetched once and shown or opened from a blob: URL.

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import type { Attachment } from "../api/types";
import { formatSize, kindLabel } from "../lib/attachments";
import { store } from "../state/store";
import { Icon } from "./Icon";

// One object URL per attachment for the life of the page: the same image in the message, the
// thumbnail and the viewer never downloads twice, and an attachment never changes.
const urls = new Map<string, Promise<string>>();

/** The file behind ``id`` as a blob: URL; a local file just picked can seed it first. */
export function attachmentURL(id: string): Promise<string> {
  let url = urls.get(id);
  if (!url) {
    url = store.attachmentBlob(id).then((blob) => URL.createObjectURL(blob));
    // A failed fetch must not stick: the next render tries again.
    url.catch(() => urls.delete(id));
    urls.set(id, url);
  }
  return url;
}

/** A picked file shows from its own bytes, with no round trip, once the upload answers. */
export function seedAttachmentURL(id: string, file: Blob): void {
  if (!urls.has(id)) urls.set(id, Promise.resolve(URL.createObjectURL(file)));
}

export function useAttachmentURL(id: string): string | null {
  const [url, setURL] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    attachmentURL(id).then(
      (value) => live && setURL(value),
      () => live && setURL(null),
    );
    return () => {
      live = false;
    };
  }, [id]);
  return url;
}

/**
 * Opening a text file or a PDF: a new window on its blob: URL, and a download carrying the name
 * it was sent with when no window opens — the desktop shell refuses them, and a browser blocks
 * one asked for after the fetch.
 */
export async function openAttachment(file: Attachment): Promise<void> {
  let url: string;
  try {
    url = await attachmentURL(file.id);
  } catch {
    return;
  }
  if (window.open(url, "_blank")) return;
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.name || "附件";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

/** Everything one message carries: images in the grid, PDFs and text files as chips below. */
export function MessageAttachments({ attachments, from }: { attachments: Attachment[]; from: string }) {
  const files = attachments.filter((item) => !item.mime.startsWith("image/"));
  return (
    <>
      <ImageGrid images={attachments} from={from} />
      {files.length > 0 && <AttachedFiles files={files} />}
    </>
  );
}

/** The PDFs and text files of one message, one chip each. */
export function AttachedFiles({ files }: { files: Attachment[] }) {
  return (
    <ul className="file-chips" aria-label="文件附件">
      {files.map((file) => (
        <li key={file.id}>
          <FileChip file={file} />
        </li>
      ))}
    </ul>
  );
}

function FileChip({ file }: { file: Attachment }) {
  const name = file.name || "未命名文件";
  return (
    <button
      type="button"
      className="file-chip"
      title={`打开 ${name}`}
      onClick={() => void openAttachment(file)}
    >
      <span className="file-chip-icon" aria-hidden="true">
        <Icon name="file" size={15} />
      </span>
      <span className="file-chip-text">
        <span className="file-chip-name">{name}</span>
        <span className="file-chip-meta">
          {kindLabel(file)} · {formatSize(file.size)}
        </span>
      </span>
    </button>
  );
}

/** The images of one message. One image keeps its proportions; several share a square grid. */
export function ImageGrid({ images, from }: { images: Attachment[]; from: string }) {
  const pictures = images.filter((image) => image.mime.startsWith("image/"));
  const [open, setOpen] = useState<number | null>(null);
  if (pictures.length === 0) return null;
  return (
    <>
      <div className={pictures.length === 1 ? "image-grid single" : "image-grid"}>
        {pictures.map((image, index) => (
          <ImageTile key={image.id} id={image.id} label={`${from}的图片 ${index + 1}`} onOpen={() => setOpen(index)} />
        ))}
      </div>
      {open !== null && pictures[open] && (
        <ImageViewer
          images={pictures}
          index={open}
          from={from}
          onMove={setOpen}
          onClose={() => setOpen(null)}
        />
      )}
    </>
  );
}

function ImageTile({ id, label, onOpen }: { id: string; label: string; onOpen: () => void }) {
  const url = useAttachmentURL(id);
  return (
    <button type="button" className="image-tile" onClick={onOpen} aria-label={`查看${label}`}>
      {url ? <img src={url} alt={label} draggable={false} /> : <span className="image-tile-wait" aria-hidden="true" />}
    </button>
  );
}

function ImageViewer({
  images,
  index,
  from,
  onMove,
  onClose,
}: {
  images: Attachment[];
  index: number;
  from: string;
  onMove: (index: number) => void;
  onClose: () => void;
}) {
  const image = images[index]!;
  const url = useAttachmentURL(image.id);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      else if (event.key === "ArrowRight" && index < images.length - 1) onMove(index + 1);
      else if (event.key === "ArrowLeft" && index > 0) onMove(index - 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, images.length, onClose, onMove]);
  return createPortal(
    <div
      className="image-viewer"
      role="dialog"
      aria-modal="true"
      aria-label={`${from}的图片 ${index + 1} / ${images.length}`}
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      {url && <img src={url} alt={`${from}的图片 ${index + 1}`} />}
      <button type="button" className="image-viewer-close" onClick={onClose} aria-label="关闭" autoFocus>
        <Icon name="close" size={18} />
      </button>
      {images.length > 1 && <span className="image-viewer-count">{index + 1} / {images.length}</span>}
    </div>,
    document.body,
  );
}
