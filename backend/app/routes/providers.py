"""Model providers: keys reduced to a hint, probes, and the workspace default."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import model_catalog, models, providers
from ..engine import SupervisorEngine
from ..models import (
    ProviderCreateRequest,
    ProviderDefaultRequest,
    ProviderFallbackRequest,
    ProviderModelsOut,
    ProviderModelsRequest,
    ProviderPatchRequest,
    ProviderTestOut,
    ProviderTestRequest,
)
from ..providers import CLI_PROTOCOLS, ProviderInvalid, ProviderNameTaken, ProviderProbeError
from ..store import Store
from .common import get_engine, get_store, publish_workspace

router = APIRouter()


async def providers_state(store: Store, settings: Any | None = None) -> dict:
    """Every provider (key reduced to a hint) plus the resolved default and backup models."""
    from .. import cli_engines

    default = providers.resolve_default(store, settings)
    fallback = providers.resolve_fallback(store, settings)
    engines = await cli_engines.engine_statuses()
    rows = store.list_providers()
    details = await cli_model_details(rows)
    model_catalog.maybe_refresh(settings)
    return models.providers_payload(
        [{**row, "model_details": details.get(row["id"], [])} for row in rows],
        None
        if default is None
        else {"provider_id": default.provider_id, "model": default.model},
        None
        if fallback is None
        else {"provider_id": fallback.provider_id, "model": fallback.model},
        [
            {
                "kind": status.kind,
                "label": status.label,
                "installed": status.installed,
                "ready": status.ready,
                "detail": status.detail,
                "command": status.command,
                "version": status.version,
            }
            for status in engines
        ],
        settings,
    )


async def cli_model_details(rows: list[dict]) -> dict[str, list[dict]]:
    """What this machine says about each CLI provider's models: label, text, efforts.

    Read once per CLI protocol per call (the catalog itself is cached for a minute), so the
    bot editor can word a pick and offer exactly the efforts that model accepts.
    """
    from .. import cli_engines

    details: dict[str, list[dict]] = {}
    for kind in cli_engines.CLI_PROTOCOLS:
        listed = [row for row in rows if row["protocol"] == kind]
        if not listed:
            continue
        options = await cli_engines.model_catalog(kind)
        by_name = {option.name: option for option in options}
        for row in listed:
            entries: list[dict] = []
            for model in row["models"]:
                option = by_name.get(model)
                if option is not None:
                    entries.append(option.as_dict())
                else:
                    entries.append(
                        {
                            "model": model,
                            "label": (
                                "默认（用 CLI 自己配置的模型）"
                                if model == cli_engines.CLI_DEFAULT_MODEL
                                else model
                            ),
                            "description": "",
                            "efforts": list(cli_engines.effort_levels(kind, model)),
                            "default_effort": "",
                            "current": False,
                        }
                    )
            details[row["id"]] = entries
    return details


async def cli_models_for(kind: str, chosen: list[str]) -> list[str]:
    """A CLI provider's list: 「默认」 first, then every model this machine says it can run."""
    from .. import cli_engines

    options = await cli_engines.model_catalog(kind)
    names = [cli_engines.CLI_DEFAULT_MODEL]
    names += [option.name for option in options if option.name not in names]
    names += [name for name in chosen if name not in names]
    return names


def form_api_key(store: Store, provider_id: str | None, api_key: str | None) -> str:
    """The key a probe uses: the form's value, else the one already stored."""
    if api_key is not None:
        return providers.clean_api_key(api_key)
    if not provider_id:
        return ""
    stored = store.get_provider(provider_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="模型服务不存在。")
    return str(stored["api_key"])


@router.get("/providers")
async def list_providers(
    request: Request, store: Store = Depends(get_store)
) -> dict:
    return await providers_state(store, request.app.state.settings)


@router.post("/providers", status_code=201)
async def create_provider(
    payload: ProviderCreateRequest,
    request: Request,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    settings = request.app.state.settings
    try:
        fields = providers.clean_create_fields(
            name=payload.name,
            protocol=payload.protocol,
            base_url=payload.base_url,
            api_key=payload.api_key,
            models=payload.models,
        )
        # A CLI provider starts with everything this machine says it can run, so its bots
        # can pick a real model without anyone typing names by hand.
        if fields["protocol"] in CLI_PROTOCOLS:
            fields["models"] = providers.clean_models(
                await cli_models_for(fields["protocol"], fields["models"])
            )
        created = store.create_provider(
            **fields,
            model_overrides=providers.clean_model_overrides(
                {model: entry.model_dump() for model, entry in payload.model_overrides.items()}
            ),
        )
    except ProviderNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Detect capabilities from the service's own answer right away, so its models show
    # their real image support and window without the user ticking anything.
    await providers.refresh_provider_caps(engine.client, store, str(created["id"]))
    publish_workspace(store, engine)
    refreshed = store.get_provider(str(created["id"])) or created
    return models.provider_payload(refreshed, settings)


@router.put("/providers/default")
async def put_default_provider(
    payload: ProviderDefaultRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        chosen = store.set_default_model(
            payload.provider_id.strip(), providers.clean_model_name(payload.model)
        )
    except ProviderInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    publish_workspace(store, engine)
    return chosen


@router.put("/providers/fallback")
async def put_fallback_provider(
    payload: ProviderFallbackRequest,
    request: Request,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Set the optional backup model, or clear it when both halves are empty."""
    provider_id = (payload.provider_id or "").strip()
    model = (payload.model or "").strip()
    try:
        if not provider_id and not model:
            store.clear_fallback_model()
        else:
            if not provider_id:
                raise ProviderInvalid("请选择备用模型所属的模型服务。")
            store.set_fallback_model(provider_id, providers.clean_model_name(model))
    except ProviderInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    publish_workspace(store, engine)
    return await providers_state(store, request.app.state.settings)


@router.post("/providers/test", response_model=ProviderTestOut)
async def provider_test(
    payload: ProviderTestRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        protocol = providers.clean_protocol(payload.protocol)
        base_url = providers.clean_base_url(payload.base_url, protocol)
        model = providers.clean_model_name(payload.model)
    except ProviderInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    api_key = form_api_key(store, payload.id, payload.api_key)
    client = engine.client
    if client is None:  # pragma: no cover - startup always opens it
        raise HTTPException(status_code=503, detail="服务未就绪，请稍后重试。")
    return await providers.test_provider(
        client,
        protocol=protocol,
        base_url=base_url,
        api_key=api_key,
        model=model,
        supervisor_name=(store.supervisor() or {}).get("name", "负责人"),
    )


@router.post("/providers/models", response_model=ProviderModelsOut)
async def provider_models(
    payload: ProviderModelsRequest,
    request: Request,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    try:
        protocol = providers.clean_protocol(payload.protocol)
        base_url = providers.clean_base_url(payload.base_url, protocol)
    except ProviderInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    api_key = form_api_key(store, payload.id, payload.api_key)
    client = engine.client
    if client is None:  # pragma: no cover - startup always opens it
        raise HTTPException(status_code=503, detail="服务未就绪，请稍后重试。")
    try:
        found, caps = await providers.fetch_models(
            client, protocol=protocol, base_url=base_url, api_key=api_key
        )
    except ProviderProbeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Reading an existing provider's list is also how its capabilities are refreshed, so
    # the next request resolves from what was just learned instead of asking again.
    if payload.id:
        store.update_provider(payload.id, model_caps=caps)
    # The form previews the same resolution the saved provider will show, sources included.
    preview = providers.provider_caps(
        {"models": found, "model_caps": caps, "model_overrides": {}},
        request.app.state.settings,
    )
    return {"models": found, "caps": preview}


@router.patch("/providers/{provider_id}")
async def patch_provider(
    provider_id: str,
    payload: ProviderPatchRequest,
    request: Request,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    existing = store.get_provider(provider_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="模型服务不存在。")
    settings = request.app.state.settings
    updates: dict[str, Any] = {}
    protocol = str(existing["protocol"])
    # Changing where the models come from makes the stored capabilities stale; a rename of
    # the same endpoint does not.
    detect = False
    try:
        if payload.name is not None:
            updates["name"] = providers.clean_name(payload.name)
        if payload.protocol is not None:
            protocol = providers.clean_protocol(payload.protocol)
            updates["protocol"] = protocol
            detect = detect or protocol != existing["protocol"]
        if payload.base_url is not None:
            base_url = providers.clean_base_url(payload.base_url, protocol)
            updates["base_url"] = base_url
            detect = detect or base_url != existing["base_url"]
        if "api_key" in payload.model_fields_set:
            api_key = providers.clean_api_key(payload.api_key)
            updates["api_key"] = api_key
            detect = detect or api_key != existing["api_key"]
        if payload.models is not None:
            updates["models"] = providers.clean_provider_models(payload.models, protocol)
            detect = detect or updates["models"] != existing["models"]
        if payload.model_overrides is not None:
            updates["model_overrides"] = providers.clean_model_overrides(
                {model: entry.model_dump() for model, entry in payload.model_overrides.items()}
            )
    except ProviderInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        updated = store.update_provider(provider_id, **updates)
    except ProviderNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:  # pragma: no cover - existence checked above
        raise HTTPException(status_code=404, detail="模型服务不存在。")
    if detect:
        await providers.refresh_provider_caps(engine.client, store, provider_id)
        updated = store.get_provider(provider_id) or updated
    publish_workspace(store, engine)
    return models.provider_payload(updated, settings)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    """Removing a provider clears its bots' picks only in the client, never in rows."""
    if store.delete_provider(provider_id) is None:
        raise HTTPException(status_code=404, detail="模型服务不存在。")
    publish_workspace(store, engine)
    return Response(status_code=204)
