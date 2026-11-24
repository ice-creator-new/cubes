"""The sidebar layout: one PUT replaces it, folders are created, renamed and deleted."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import models
from ..engine import SupervisorEngine
from ..models import FolderNameRequest, SidebarPutRequest
from ..sidebar import FolderNameTaken, SidebarInvalid
from ..store import Store
from .common import get_engine, get_store, publish_sidebar

router = APIRouter()


@router.get("/sidebar")
async def get_sidebar(store: Store = Depends(get_store)) -> dict:
    return models.sidebar_payload(store.sidebar_layout())


@router.put("/sidebar")
async def put_sidebar(
    payload: SidebarPutRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        store.replace_sidebar(
            [item.model_dump() for item in payload.items], payload.folder_ids
        )
    except SidebarInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return publish_sidebar(store, engine)


@router.post("/sidebar/folders", status_code=201)
async def create_sidebar_folder(
    payload: FolderNameRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        store.create_folder(payload.name)
    except FolderNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return publish_sidebar(store, engine)


@router.patch("/sidebar/folders/{folder_id}")
async def rename_sidebar_folder(
    folder_id: str,
    payload: FolderNameRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        renamed = store.rename_folder(folder_id, payload.name)
    except FolderNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if renamed is None:
        raise HTTPException(status_code=404, detail="分组不存在。")
    return publish_sidebar(store, engine)


@router.delete("/sidebar/folders/{folder_id}")
async def delete_sidebar_folder(
    folder_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    deleted = store.delete_folder(folder_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="分组不存在。")
    return publish_sidebar(store, engine)
