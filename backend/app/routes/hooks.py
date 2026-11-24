"""Webhook triggers: managing the secret URLs, and the public call that fires one.

Two router halves share this module:

- ``/webhooks`` is ordinary management behind the launch token: read the hook of one
  schedule or workflow, create it, rotate its secret, delete it;
- ``/hooks/{id}`` is the only public path in the API (see :mod:`app.auth`): the caller
  proves itself with the hook's secret and the body becomes the run's input.

Creating or rotating is the one moment the secret is readable; the database keeps a hash.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import providers, webhooks
from ..agent.run_state import RunLauncher
from ..errors import WorkflowBusy
from ..models import WebhookCreateRequest
from ..scheduler import Scheduler
from ..store import Store
from .common import get_launcher, get_scheduler, get_store

router = APIRouter()

TARGET_KINDS = ("schedule", "workflow")


def _target(store: Store, target_kind: str, target_id: str) -> dict:
    """The schedule or workflow a hook points at; 404 when it is not there."""
    if target_kind == "schedule":
        schedule = store.get_schedule(target_id)
        if schedule is None or schedule.get("system"):
            raise HTTPException(status_code=404, detail="定时任务不存在。")
        return schedule
    if target_kind == "workflow":
        workflow = store.get_workflow(target_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail="工作流不存在。")
        return workflow
    raise HTTPException(status_code=422, detail="target_kind 只能是 schedule 或 workflow。")


# ------------------------------------------------------------------ management


@router.get("/webhooks")
def read_webhook(
    target_kind: str,
    target_id: str,
    store: Store = Depends(get_store),
) -> dict:
    """The hook a schedule or workflow owns, if it has one. The secret is never here."""
    _target(store, target_kind, target_id)
    row = store.get_webhook_for(target_kind, target_id)
    return {"webhook": None if row is None else webhooks.webhook_payload(row)}


@router.post("/webhooks", status_code=201)
def create_webhook(
    payload: WebhookCreateRequest,
    store: Store = Depends(get_store),
) -> dict:
    """Give a schedule or workflow its secret URL; the only answer that carries the secret."""
    _target(store, payload.target_kind, payload.target_id)
    secret = webhooks.new_secret()
    row = store.create_webhook(
        target_kind=payload.target_kind,
        target_id=payload.target_id,
        secret_hash=webhooks.hash_secret(secret),
    )
    if row is None:
        raise HTTPException(
            status_code=409, detail="它已经有 Webhook 了，要换密钥请用「轮换密钥」。"
        )
    return webhooks.created_payload(row, secret)


@router.post("/webhooks/{webhook_id}/rotate")
def rotate_webhook(
    webhook_id: str,
    store: Store = Depends(get_store),
) -> dict:
    """Replace the secret; the URL stays, the old secret stops working at once."""
    secret = webhooks.new_secret()
    row = store.rotate_webhook(webhook_id, webhooks.hash_secret(secret))
    if row is None:
        raise HTTPException(status_code=404, detail="Webhook 不存在。")
    webhooks.throttle.forget(webhook_id)
    return webhooks.created_payload(row, secret)


@router.delete("/webhooks/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: str,
    store: Store = Depends(get_store),
) -> Response:
    """Delete the hook: its URL answers 404 from the next call on."""
    if not store.delete_webhook(webhook_id):
        raise HTTPException(status_code=404, detail="Webhook 不存在。")
    webhooks.throttle.forget(webhook_id)
    return Response(status_code=204)


# --------------------------------------------------------------------- calling


@router.post("/hooks/{webhook_id}", status_code=202)
async def call_webhook(
    webhook_id: str,
    request: Request,
    store: Store = Depends(get_store),
    launcher: RunLauncher = Depends(get_launcher),
    scheduler: Scheduler = Depends(get_scheduler),
) -> JSONResponse:
    """Start the hook's schedule or workflow now; the body is that run's input.

    The secret comes in ``Authorization: Bearer <secret>`` or ``?token=``. A wrong secret
    is 401, a call too soon after the last one is 429, and a started run is 202 with the
    run id so the caller can follow it up.
    """
    hook = store.get_webhook(webhook_id)
    if hook is None:
        raise HTTPException(status_code=404, detail="Webhook 不存在。")
    if not webhooks.secret_matches(webhooks.presented_secret(request), hook["secret_hash"]):
        raise HTTPException(status_code=401, detail="Webhook 密钥不正确。")
    wait = webhooks.throttle.take(webhook_id)
    if wait > 0:
        return JSONResponse(
            status_code=429,
            content={"detail": f"调用太频繁了，{wait:.1f} 秒后再试。"},
            headers={"Retry-After": f"{max(1, int(wait + 0.999))}"},
        )
    text = await webhooks.read_input(request)

    if hook["target_kind"] == "schedule":
        schedule = store.get_schedule(hook["target_id"])
        if schedule is None or schedule.get("system"):
            raise HTTPException(status_code=404, detail="这个定时任务已经不在了。")
        updated = await scheduler.fire_now(schedule["id"], text)
        if updated is None:  # pragma: no cover - checked just above
            raise HTTPException(status_code=404, detail="这个定时任务已经不在了。")
        started = not updated["queued"]
        if started and (
            updated["last_status"] == "error"
            and updated["last_run_id"] == schedule["last_run_id"]
        ):
            raise HTTPException(
                status_code=503, detail=updated["last_error"] or "定时任务没能启动。"
            )
        store.touch_webhook(webhook_id)
        return JSONResponse(
            status_code=202,
            content={
                "kind": "schedule",
                "hook_id": webhook_id,
                "schedule_id": schedule["id"],
                "status": "started" if started else "queued",
                "run_id": updated["last_run_id"] if started else None,
            },
        )

    workflow = store.get_workflow(hook["target_id"])
    if workflow is None:
        raise HTTPException(status_code=404, detail="这个工作流已经不在了。")
    if not workflow["enabled"]:
        raise HTTPException(status_code=409, detail="这个工作流还没启用，先启用再运行。")
    if providers.resolve_default(store) is None:
        raise HTTPException(status_code=503, detail=providers.NO_MODEL)
    try:
        conversation_id, workflow_run_id = await launcher.start_workflow(
            workflow, input=text, started_by="webhook"
        )
    except WorkflowBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    walk = store.get_workflow_run(workflow_run_id)
    store.touch_webhook(webhook_id)
    return JSONResponse(
        status_code=202,
        content={
            "kind": "workflow",
            "hook_id": webhook_id,
            "workflow_id": workflow["id"],
            "workflow_run_id": workflow_run_id,
            "conversation_id": conversation_id,
            "status": "started",
            "run_id": walk["run_id"] if walk is not None else None,
        },
    )
