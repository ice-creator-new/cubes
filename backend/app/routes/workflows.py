"""Workflows: the list, the editor's detail, starting a walk, and the controls on it.

A definition is edited from the panel, so every change publishes ``workflows``; a walk
publishes ``workflow_run`` on every step boundary, so an open tab follows it live. The
controls (pause, skip, retry, resume, answer) go through the engine when the walk is live and
touch the row directly when it is parked — the user may press them in either state.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import models
from .. import workflows as rules
from ..agent.run_state import RunLauncher
from ..engine import SupervisorEngine
from ..errors import WorkflowBusy
from ..models import WorkflowCreateRequest, WorkflowPatchRequest, WorkflowRunRequest
from ..store import Store, now_iso
from .common import get_engine, get_launcher, get_store

router = APIRouter()

LIVE = frozenset(rules.LIVE_STATUSES)


def _require(store: Store, workflow_id: str) -> dict:
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="工作流不存在。")
    return workflow


def _require_run(store: Store, workflow_run_id: str) -> dict:
    workflow_run = store.get_workflow_run(workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="这次工作流运行不存在。")
    return workflow_run


def _graph(payload: WorkflowCreateRequest | WorkflowPatchRequest) -> tuple[list, list] | None:
    """The graph a request carries, validated; ``None`` when it carries no nodes."""
    raw = getattr(payload, "steps", None)
    if raw is None:
        return None
    edges = getattr(payload, "edges", None)
    try:
        return rules.normalize_graph(
            [step.model_dump() for step in raw],
            None if edges is None else [edge.model_dump(by_alias=True) for edge in edges],
        )
    except rules.WorkflowInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validated(store: Store, payload: WorkflowCreateRequest | WorkflowPatchRequest) -> dict:
    changes: dict = {}
    name = getattr(payload, "name", None)
    if isinstance(name, str):
        try:
            changes["name"] = rules.validate_name(name)
        except rules.WorkflowInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(getattr(payload, "description", None), str):
        try:
            changes["description"] = rules.validate_description(payload.description)
        except rules.WorkflowInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    graph = _graph(payload)
    if graph is not None:
        changes["steps"], changes["edges"] = graph
    if isinstance(getattr(payload, "enabled", None), bool):
        changes["enabled"] = bool(payload.enabled)
    owner = getattr(payload, "owner_agent_id", None)
    if isinstance(owner, str) and owner:
        if store.get_agent(owner) is None:
            raise HTTPException(status_code=422, detail="这个成员不存在。")
        changes["owner_agent_id"] = owner
    return changes


@router.get("/workflows")
async def list_workflows(store: Store = Depends(get_store)) -> dict:
    return models.workflows_payload(store)


@router.post("/workflows", status_code=201)
async def create_workflow(
    payload: WorkflowCreateRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    changes = _validated(store, payload)
    if "name" not in changes or "steps" not in changes:
        raise HTTPException(status_code=422, detail="需要 name 和 steps。")
    owner = store.get_agent(changes.pop("owner_agent_id", "") or "") or store.supervisor()
    if owner is None:  # pragma: no cover - bootstrap always creates the supervisor
        raise HTTPException(status_code=503, detail="工作区还没有主管。")
    if store.workflow_by_name(changes["name"], owner_agent_id=owner["id"]) is not None:
        raise HTTPException(
            status_code=409, detail=f"「{owner['name']}」名下已经有叫「{changes['name']}」的工作流。"
        )
    workflow = store.create_workflow(
        name=changes["name"],
        description=changes.get("description", ""),
        owner_agent_id=owner["id"],
        created_by="user",
        nodes=changes["steps"],
        edges=changes["edges"],
        source="user",
        enabled=changes.get("enabled", True),
    )
    engine.publish("workflows", models.workflows_payload(store))
    return models.workflow_payload(store, workflow)


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, store: Store = Depends(get_store)) -> dict:
    workflow = _require(store, workflow_id)
    payload = models.workflow_payload(store, workflow)
    payload["markdown"] = rules.render_markdown(workflow)
    payload["runs"] = [
        models.workflow_run_payload(store, run)
        for run in store.list_workflow_runs(workflow_id, limit=10)
    ]
    return payload


@router.patch("/workflows/{workflow_id}")
async def patch_workflow(
    workflow_id: str,
    payload: WorkflowPatchRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    workflow = _require(store, workflow_id)
    changes = _validated(store, payload)
    if not changes:
        raise HTTPException(status_code=422, detail="没有要改的内容。")
    if "name" in changes and changes["name"] != workflow["name"]:
        owner_id = changes.get("owner_agent_id", workflow["owner_agent_id"])
        if store.workflow_by_name(changes["name"], owner_agent_id=owner_id) is not None:
            raise HTTPException(status_code=409, detail=f"已经有叫「{changes['name']}」的工作流。")
    updated = store.update_workflow(
        workflow_id,
        **{
            ("nodes" if key == "steps" else key): value
            for key, value in changes.items()
        },
    )
    engine.publish("workflows", models.workflows_payload(store))
    return models.workflow_payload(store, updated or workflow)


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    _require(store, workflow_id)
    store.delete_workflow(workflow_id)
    engine.publish("workflows", models.workflows_payload(store))
    return Response(status_code=204)


@router.post("/workflows/{workflow_id}/run", status_code=202)
async def run_workflow(
    workflow_id: str,
    payload: WorkflowRunRequest,
    store: Store = Depends(get_store),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    workflow = _require(store, workflow_id)
    if not workflow["enabled"]:
        raise HTTPException(status_code=409, detail="这个工作流还没启用，先启用再运行。")
    text = " ".join(payload.input.split())
    try:
        conversation_id, workflow_run_id = await launcher.start_workflow(
            workflow, input=text, started_by="user"
        )
    except WorkflowBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    workflow_run = store.get_workflow_run(workflow_run_id)
    assert workflow_run is not None
    return {
        "workflow_run": models.workflow_run_payload(store, workflow_run),
        "conversation_id": conversation_id,
    }


@router.get("/workflow-runs/{workflow_run_id}")
async def get_workflow_run(workflow_run_id: str, store: Store = Depends(get_store)) -> dict:
    return models.workflow_run_payload(store, _require_run(store, workflow_run_id))


@router.get("/conversations/{conversation_id}/workflow-run")
async def conversation_workflow_run(
    conversation_id: str, store: Store = Depends(get_store)
) -> dict:
    """The walk this chat is serving right now, if any: the floating panel reads this."""
    workflow_run = store.workflow_run_by_conversation(conversation_id)
    return {
        "workflow_run": (
            None
            if workflow_run is None
            else models.workflow_run_payload(store, workflow_run)
        )
    }


@router.post("/workflow-runs/{workflow_run_id}/pause")
async def pause_workflow_run(
    workflow_run_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    workflow_run = _require_run(store, workflow_run_id)
    if workflow_run["status"] not in LIVE:
        raise HTTPException(status_code=409, detail="这次运行已经结束了。")
    if engine.workflow_control(workflow_run["run_id"] or "", "pause"):
        return models.workflow_run_payload(store, _require_run(store, workflow_run_id))
    updated = store.update_workflow_run(workflow_run_id, status=rules.STATUS_PAUSED)
    engine.publish("workflow_run", models.workflow_run_payload(store, updated or workflow_run))
    engine.publish_decisions()
    return models.workflow_run_payload(store, updated or workflow_run)


@router.post("/workflow-runs/{workflow_run_id}/stop")
async def stop_workflow_run(
    workflow_run_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    workflow_run = _require_run(store, workflow_run_id)
    if workflow_run["status"] not in LIVE:
        raise HTTPException(status_code=409, detail="这次运行已经结束了。")
    # Ended first, so the run that stops with it cannot park the walk as merely paused.
    store.update_workflow_run(
        workflow_run_id, status=rules.STATUS_STOPPED, finished_at=now_iso()
    )
    if workflow_run["run_id"]:
        await launcher.finish_stop(workflow_run["run_id"])
    updated = store.get_workflow_run(workflow_run_id)
    engine.publish(
        "workflow_run", models.workflow_run_payload(store, updated or workflow_run)
    )
    engine.publish_decisions()
    return models.workflow_run_payload(store, updated or workflow_run)


@router.post("/workflow-runs/{workflow_run_id}/resume")
async def resume_workflow_run(
    workflow_run_id: str,
    store: Store = Depends(get_store),
    launcher: RunLauncher = Depends(get_launcher),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Continue a paused walk from its cursor; the step it stopped at runs again."""
    workflow_run = _require_run(store, workflow_run_id)
    if workflow_run["status"] != rules.STATUS_PAUSED:
        raise HTTPException(status_code=409, detail="这次运行没有暂停。")
    try:
        await launcher.resume_workflow(workflow_run)
    except WorkflowBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return models.workflow_run_payload(store, _require_run(store, workflow_run_id))


@router.post("/workflow-runs/{workflow_run_id}/answer")
async def answer_workflow_run(
    workflow_run_id: str,
    payload: WorkflowRunRequest,
    store: Store = Depends(get_store),
    launcher: RunLauncher = Depends(get_launcher),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """The user's answer to an ``ask_user`` step: it resumes the walk as that step's result."""
    workflow_run = _require_run(store, workflow_run_id)
    if workflow_run["status"] != rules.STATUS_WAITING:
        raise HTTPException(status_code=409, detail="这一步没有在等你回答。")
    text = payload.input.strip()
    if not text:
        raise HTTPException(status_code=422, detail="先写下你的回答。")
    try:
        await launcher.answer_workflow(workflow_run, text)
    except WorkflowBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return models.workflow_run_payload(store, _require_run(store, workflow_run_id))


@router.post("/workflow-runs/{workflow_run_id}/nodes/{node_id}/{command}")
async def control_workflow_node(
    workflow_run_id: str,
    node_id: str,
    command: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """``retry`` or ``skip`` one node. A live walk applies it at its next boundary.

    An index names the node the same way, so a client that still counts nodes keeps working:
    it is resolved against the run's own copy of the graph.
    """
    if command not in ("retry", "skip"):
        raise HTTPException(status_code=404, detail="没有这个操作。")
    workflow_run = _require_run(store, workflow_run_id)
    if workflow_run["status"] not in LIVE:
        raise HTTPException(status_code=409, detail="这次运行已经结束了。")
    nodes = workflow_run["steps"]
    resolved = node_id
    if node_id not in {node["id"] for node in nodes}:
        if not node_id.isdigit() or int(node_id) >= len(nodes):
            raise HTTPException(status_code=422, detail="这个节点不存在。")
        resolved = nodes[int(node_id)]["id"]
    states = {state["node_id"]: state for state in store.workflow_step_states(workflow_run_id)}
    landed = (states.get(resolved) or {}).get("status")
    if landed in rules.SETTLED:
        raise HTTPException(status_code=409, detail="这个节点已经结束了。")
    if engine.workflow_control(workflow_run["run_id"] or "", command, resolved):
        # A live walk applies it at its next node boundary, never mid-node.
        return models.workflow_run_payload(store, _require_run(store, workflow_run_id))
    # Parked: nothing will pick the command up, so the rows are edited here.
    if command == "skip":
        store.update_workflow_step(
            workflow_run_id, resolved, status=rules.STEP_SKIPPED, output="你跳过了这一步。"
        )
    else:
        store.update_workflow_step(workflow_run_id, resolved, status=rules.STEP_PENDING)
    updated = _require_run(store, workflow_run_id)
    engine.publish("workflow_run", models.workflow_run_payload(store, updated))
    return models.workflow_run_payload(store, updated)
