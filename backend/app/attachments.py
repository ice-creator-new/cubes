"""Files users pass around: kind sniffing, the files on disk, and model blocks.

A file is stored once under its SHA-256 next to the database; the ``attachments`` table maps
ids to it, and ``message_attachments`` says which messages carry which files. Three kinds are
accepted — images, PDFs and text (code, Markdown, CSV, JSON …) — and each reaches a model in
the form it can take: pixels, a document, or inline text. Bots never create files: they can
only pass on ids they have seen in their own conversations.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

# Images are meant to be pasted and resized by the client; everything else is a real file.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 4
# How many images, newest first, a model receives as pixels; older ones become text labels.
MAX_IMAGES_IN_CONTEXT = 4
# How many PDFs, newest first, a request carries as documents; older ones become labels.
MAX_PDFS_IN_CONTEXT = 2
# A text file's content is cut here: enough for most sources, small enough to keep a turn's
# context usable when several files are sent.
TEXT_FILE_CHARS = 40_000
# A filename is a label, not a path: keep it short enough for a header and a chat line.
MAX_NAME_CHARS = 120
# Unbound uploads are dropped at startup once they are older than this.
ORPHAN_SECONDS = 24 * 60 * 60

TOO_LARGE_IMAGE = "图片超过 5 MB，请压缩后再发。"
TOO_LARGE_FILE = "文件超过 20 MB，请分开发送。"
UNSUPPORTED = "只支持图片、PDF 和文本文件（代码、Markdown、CSV、JSON 等）。"
TOO_MANY = "一条消息最多带 4 个附件。"
STALE = "附件已失效，请重新添加。"

ID_PATTERN = re.compile(r"att_[0-9a-f]{12}")
IMAGES_OPEN = "<images>"
IMAGES_CLOSE = "</images>"

PDF_MAGIC = b"%PDF-"
PDF_MIME = "application/pdf"
TEXT_MIME = "text/plain"


def sniff_mime(data: bytes) -> str | None:
    """The image type from the file's own header, or ``None`` when it is not a known image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_text(data: bytes) -> bool:
    """Whether the bytes are a text file: valid UTF-8 (a BOM is fine) without a NUL byte."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def classify(data: bytes) -> tuple[str, str] | None:
    """``(kind, mime)`` from the bytes themselves, or ``None`` for an unsupported file.

    The declared content type is never trusted: a PNG sent as ``text/plain`` is still a PNG,
    and a binary blob named ``.md`` is still refused.
    """
    mime = sniff_mime(data)
    if mime is not None:
        return "image", mime
    if data.startswith(PDF_MAGIC):
        return "pdf", PDF_MIME
    if is_text(data):
        return "text", TEXT_MIME
    return None


def limit_of(kind: str) -> int:
    """The size limit for one kind, in bytes."""
    return MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES


def too_large(kind: str) -> str:
    return TOO_LARGE_IMAGE if kind == "image" else TOO_LARGE_FILE


def kind_of(mime: str) -> str:
    """The kind a stored attachment's mime says it is."""
    if mime.startswith("image/"):
        return "image"
    if mime == PDF_MIME:
        return "pdf"
    return "text"


def clean_name(value: str) -> str:
    """A filename as this app stores it: basename only, no control characters, capped.

    The name comes from a client header and is shown to models and in ``Content-Disposition``,
    so a path or a newline in it would leak a location or split a header.
    """
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character >= " " and character != "\x7f")
    name = name.strip()
    if name in ("", ".", ".."):
        return ""
    return name[:MAX_NAME_CHARS]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_file(directory: Path, data: bytes) -> str:
    """Store ``data`` under its hash (once) and return the hash."""
    name = digest(data)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if not target.exists():
        temporary = target.with_name(f"{name}.tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
    return name


def data_url(directory: Path, sha256: str, mime: str) -> str | None:
    """The file inline as a ``data:`` URL for a model request, or ``None`` if it is gone."""
    path = directory / sha256
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def read_text(directory: Path, sha256: str) -> str | None:
    """A stored text file's content, or ``None`` when its file is gone."""
    try:
        raw = (directory / sha256).read_bytes()
    except OSError:
        return None
    return raw.decode("utf-8", "replace").lstrip("\ufeff")


def attachment_label(attachment: dict, *, visible: bool) -> str:
    """The one line that stands in for a file in a model's history.

    ``visible=False`` means this model cannot take the file itself, and the label says so
    instead of pretending the content arrived.
    """
    kind = kind_of(str(attachment.get("mime") or ""))
    name = str(attachment.get("name") or "").strip()
    attachment_id = str(attachment.get("id") or "")
    if kind == "image":
        return f"[图片 {attachment_id}]" if visible else f"[图片 {attachment_id}，这里看不到内容]"
    label = "PDF" if kind == "pdf" else "文本文件"
    named = f"[{label} {name}（{attachment_id}）]" if name else f"[{label} {attachment_id}]"
    if visible:
        return named
    return f"{named[:-1]}，这里读不到内容]"


def images_tag(ids: list[str]) -> str:
    return f"{IMAGES_OPEN}{' '.join(ids)}{IMAGES_CLOSE}"


def split_images_tag(text: str) -> tuple[str, list[str]]:
    """Strip one trailing ``<images>…</images>`` line; return ``(body, ids in order)``."""
    trimmed = text.rstrip()
    if not trimmed.endswith(IMAGES_CLOSE):
        return text, []
    start = trimmed.rfind(IMAGES_OPEN, 0, len(trimmed) - len(IMAGES_CLOSE))
    if start < 0:
        return text, []
    inner = trimmed[start + len(IMAGES_OPEN) : len(trimmed) - len(IMAGES_CLOSE)]
    ids: list[str] = []
    for found in ID_PATTERN.findall(inner):
        if found not in ids:
            ids.append(found)
    return trimmed[:start].rstrip(), ids[:MAX_ATTACHMENTS_PER_MESSAGE]
