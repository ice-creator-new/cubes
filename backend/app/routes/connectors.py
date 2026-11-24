"""Connectors: every route here goes through the connector manager, not the store."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from ..connectors import ConnectorInvalid, ConnectorNameTaken, ConnectorUnavailable
from ..engine import SupervisorEngine
from ..models import ConnectorRequest
from .common import get_engine

router = APIRouter()


def connector_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ConnectorNameTaken):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ConnectorUnavailable):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/connectors")
async def list_connectors(engine: SupervisorEngine = Depends(get_engine)) -> dict:
    return engine.connectors.state()


@router.post("/connectors", status_code=201)
async def create_connector(
    payload: ConnectorRequest, engine: SupervisorEngine = Depends(get_engine)
) -> dict:
    """Connects and lists tools first: only a connection that worked is saved."""
    try:
        return await engine.connectors.create(payload.model_dump(exclude_unset=True))
    except (ConnectorInvalid, ConnectorNameTaken, ConnectorUnavailable) as exc:
        raise connector_error(exc) from exc


@router.patch("/connectors/{connector_id}")
async def patch_connector(
    connector_id: str,
    payload: ConnectorRequest,
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """A change to how it connects is verified first; a failed one changes nothing."""
    try:
        updated = await engine.connectors.update(
            connector_id, payload.model_dump(exclude_unset=True)
        )
    except (ConnectorInvalid, ConnectorNameTaken, ConnectorUnavailable) as exc:
        raise connector_error(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="连接器不存在。")
    return updated


@router.post("/connectors/{connector_id}/reconnect")
async def reconnect_connector(
    connector_id: str, engine: SupervisorEngine = Depends(get_engine)
) -> dict:
    """Tries the stored settings again; a failure is reported on the connector."""
    result = await engine.connectors.reconnect(connector_id)
    if result is None:
        raise HTTPException(status_code=404, detail="连接器不存在。")
    return result


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: str, engine: SupervisorEngine = Depends(get_engine)
) -> Response:
    if not await engine.connectors.delete(connector_id):
        raise HTTPException(status_code=404, detail="连接器不存在。")
    return Response(status_code=204)
