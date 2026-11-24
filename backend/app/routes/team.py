"""Team export and import: one Markdown file, previewed before it is applied."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import models
from .. import team_export
from ..engine import SupervisorEngine
from ..models import TeamImportRequest
from ..store import Store
from .common import get_engine, get_store, publish_workspace

router = APIRouter()


@router.get("/team/export")
async def export_team(store: Store = Depends(get_store)) -> Response:
    """The team's structure as one downloadable Markdown file; never any secret."""
    text = team_export.export_markdown(store)
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cubes-team.md"'},
    )


@router.post("/team/import")
async def import_team(
    payload: TeamImportRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Preview what the file would create, or with ``apply`` write it and refresh every window."""
    try:
        if payload.apply:
            result = team_export.apply_import(
                store, payload.markdown, overwrite_supervisor=payload.overwrite_supervisor
            )
            publish_workspace(store, engine)
            engine.publish("workflows", models.workflows_payload(store))
        else:
            result = team_export.preview_import(
                store, payload.markdown, overwrite_supervisor=payload.overwrite_supervisor
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Internal planning keys stay on the server.
    result.pop("_data", None)
    result.pop("_final_by_name", None)
    return result
