"""Attachments: one file in as the raw request body, the stored bytes back out.

The body is sniffed here — image, PDF or UTF-8 text — because the declared content type is
whatever the client felt like saying. A text file may also carry its original name in the
percent-encoded ``X-Attachment-Name`` header, which the clients show next to the file and
models read in its label.
"""

from __future__ import annotations

from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import attachments as attachment_rules
from ..store import Store
from .common import get_store

router = APIRouter()

NAME_HEADER = "X-Attachment-Name"


@router.post("/attachments", status_code=201)
async def upload_attachment(request: Request, store: Store = Depends(get_store)) -> dict:
    """One file as the raw request body; its own bytes decide the kind."""
    data = await request.body()
    # Checked before classification: decoding a 25 MB body only to call it "too large" is
    # wasted work, and an oversized image has its own sentence.
    if len(data) > attachment_rules.MAX_FILE_BYTES:
        kind = "image" if attachment_rules.sniff_mime(data) else "file"
        raise HTTPException(status_code=413, detail=attachment_rules.too_large(kind))
    detected = attachment_rules.classify(data)
    if detected is None:
        raise HTTPException(status_code=415, detail=attachment_rules.UNSUPPORTED)
    kind, mime = detected
    if len(data) > attachment_rules.limit_of(kind):
        raise HTTPException(status_code=413, detail=attachment_rules.too_large(kind))
    name = attachment_rules.clean_name(unquote(request.headers.get(NAME_HEADER, "")))
    saved = store.save_attachment(data, mime, name)
    return {"id": saved["id"], "mime": saved["mime"], "size": saved["size"], "name": saved["name"]}


@router.get("/attachments/{attachment_id}")
async def get_attachment(attachment_id: str, store: Store = Depends(get_store)) -> Response:
    attachment = store.get_attachment(attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在。")
    try:
        data = store.attachment_path(attachment).read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="附件文件已丢失。") from exc
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    name = str(attachment.get("name") or "")
    if name:
        # RFC 5987: the name may hold any character, so it travels percent-encoded.
        headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(name, safe='')}"
    return Response(content=data, media_type=attachment["mime"], headers=headers)
