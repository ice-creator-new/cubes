"""Agent skills: the list, the detail, and the four import paths plus edit/delete.

Every change publishes a ``skills`` event so every window stays in sync; the list is not in
the snapshot — the web loads ``GET /skills`` at boot, exactly like connectors.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import models, skills as skills_rules
from ..engine import SupervisorEngine
from ..models import (
    SkillCreateRequest,
    SkillGitHubRequest,
    SkillLocalRequest,
    SkillPatchRequest,
)
from ..store import Store
from .common import get_engine, get_store

router = APIRouter()


def _detail(store: Store, skill: dict) -> dict:
    return models.skill_detail_payload(
        skill, store.skill_body(skill), store.skill_files(skill)
    )


@router.get("/skills")
async def list_skills(store: Store = Depends(get_store)) -> dict:
    return models.skills_payload(store)


@router.get("/skills/local")
async def list_local_skills(store: Store = Depends(get_store)) -> dict:
    """Skills other agents keep on this machine (Claude Code, Codex, …), for the import picker."""
    found = skills_rules.scan_local_skills(Path.home())
    for entry in found:
        entry["installed"] = store.skill_name_taken(entry["name"])
    return {"skills": found}


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, store: Store = Depends(get_store)) -> dict:
    skill = store.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="技能不存在。")
    return _detail(store, skill)


@router.post("/skills", status_code=201)
async def create_skill(
    payload: SkillCreateRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        name = skills_rules.validate_name(payload.name)
        description = skills_rules.validate_description(payload.description)
        body = skills_rules.validate_body(payload.body)
    except skills_rules.SkillInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if store.skill_name_taken(name):
        raise HTTPException(status_code=409, detail=f"技能「{name}」已经存在。")
    skill = store.create_skill(
        name=name,
        description=description,
        body=body,
        source="user",
        enabled=True,
        has_scripts=False,
        file_count=1,
    )
    engine.publish("skills", models.skills_payload(store))
    return _detail(store, skill)


@router.post("/skills/upload", status_code=201)
async def upload_skill(
    request: Request,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """One zip or one SKILL.md as the raw body; imports land disabled for the user to review."""
    data = await request.body()
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    try:
        if content_type == "application/zip" or content_type == "application/x-zip-compressed":
            imported = skills_rules.extract_archive(data)
        elif content_type == "text/markdown":
            imported = skills_rules.extract_markdown(data)
        else:
            raise HTTPException(
                status_code=415,
                detail="只支持 application/zip 或 text/markdown 上传。",
            )
    except skills_rules.SkillTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except skills_rules.SkillInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _create_imported(store, engine, imported, source="upload")


@router.post("/skills/github", status_code=201)
async def import_github_skill(
    payload: SkillGitHubRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        imported = await skills_rules.import_from_github(payload.url, engine.client)
    except skills_rules.SkillDownloadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except skills_rules.SkillTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except skills_rules.SkillInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _create_imported(store, engine, imported, source="github")


@router.post("/skills/local", status_code=201)
async def import_local_skill(
    payload: SkillLocalRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Copy one listed folder in; like every import it lands disabled for the user to review."""
    try:
        imported = skills_rules.read_local_skill(payload.path, Path.home())
    except skills_rules.SkillTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except skills_rules.SkillInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _create_imported(store, engine, imported, source="local")


def _create_imported(store: Store, engine: SupervisorEngine, imported: dict, *, source: str) -> dict:
    if store.skill_name_taken(imported["name"]):
        raise HTTPException(status_code=409, detail=f"技能「{imported['name']}」已经存在。")
    skill = store.create_skill(
        name=imported["name"],
        description=imported["description"],
        body=imported["body"],
        metadata=imported["metadata"],
        source=source,
        source_url=imported["source_url"],
        enabled=False,
        files=imported["files"],
        has_scripts=imported["has_scripts"],
        file_count=imported["file_count"],
    )
    engine.publish("skills", models.skills_payload(store))
    return _detail(store, skill)


@router.patch("/skills/{skill_id}")
async def patch_skill(
    skill_id: str,
    payload: SkillPatchRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    updates: dict = {}
    if payload.enabled is not None:
        updates["enabled"] = payload.enabled
    if payload.description is not None:
        updates["description"] = payload.description
    if payload.body is not None:
        updates["body"] = payload.body
    try:
        updated = store.update_skill(skill_id, **updates)
    except skills_rules.SkillInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="技能不存在。")
    engine.publish("skills", models.skills_payload(store))
    return _detail(store, updated)


@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    if not store.delete_skill(skill_id):
        raise HTTPException(status_code=404, detail="技能不存在。")
    engine.publish("skills", models.skills_payload(store))
    return Response(status_code=204)
