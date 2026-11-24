"""Conversations and the runs a message starts in them."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import models, providers
from ..agent.context import speaker_for
from ..agent.run_state import RunLauncher, resolve_send_mode
from ..engine import SupervisorEngine
from ..errors import EngineBusy, GroupDissolveRefused, UpstreamError
from ..mentions import opening_mentions, parse_mentions
from ..models import (
    ConversationPatchRequest,
    ConversationReadRequest,
    GroupCreateRequest,
    MessageEditRequest,
    RerouteRequest,
    RunStartRequest,
)
from ..store import Store
from ..task_scheduler import TaskScheduler
from .common import (
    get_engine,
    get_launcher,
    get_store,
    get_task_scheduler,
    publish_sidebar,
    publish_workspace,
)

router = APIRouter()


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    limit: int | None = None,
    before: int | None = None,
    store: Store = Depends(get_store),
) -> dict:
    """One conversation: every message, or one page of them when ``limit`` is given."""
    if store.is_background_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在。")
    if limit is not None and not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="「limit」需要在 1 到 200 之间。")
    detail = models.conversation_detail_payload(
        store, conversation_id, limit=limit, before=before
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    return detail


@router.post("/conversations/{conversation_id}/read", status_code=204)
async def mark_conversation_read(
    conversation_id: str,
    payload: ConversationReadRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    """The user has seen this chat up to ``seq``: its unread dot clears everywhere."""
    if not store.mark_conversation_read(conversation_id, payload.seq):
        raise HTTPException(status_code=404, detail="会话不存在。")
    publish_workspace(store, engine)
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/context")
async def conversation_context(
    conversation_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """How full the bot's window is, with the composer panel's category breakdown."""
    conversation = store.get_conversation(conversation_id)
    if conversation is None or conversation["kind"] == "background":
        raise HTTPException(status_code=404, detail="会话不存在。")
    author = speaker_for(store, conversation)
    if author is None:  # pragma: no cover - every conversation has members
        raise HTTPException(status_code=409, detail="这个会话还没有成员。")
    try:
        return engine.context_usage(conversation, author)
    except UpstreamError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/compact")
async def compact_conversation(
    conversation_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Fold older turns into a summary now: the context panel's 立即压缩 button.

    The same compaction a run performs before a big request, forced past its threshold. A
    live run owns the conversation's request stream, so it must be stopped first. Both ends
    of the fold are published as ``context`` events, so every open composer follows it.
    """
    conversation = store.get_conversation(conversation_id)
    if conversation is None or conversation["kind"] == "background":
        raise HTTPException(status_code=404, detail="会话不存在。")
    if engine.is_running(conversation_id):
        raise HTTPException(
            status_code=409, detail="这个会话正在回复，停下之后再压缩。"
        )
    author = speaker_for(store, conversation)
    if author is None:  # pragma: no cover - every conversation has members
        raise HTTPException(status_code=409, detail="这个会话还没有成员。")
    try:
        return await engine.compact_now(conversation, author)
    except UpstreamError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def group_member_ids(
    store: Store, requested: list[str], *, include_supervisor: bool = False
) -> list[str]:
    """The group's members: the supervisor only when asked for, then every id once.

    An unknown id is a 400; a group with nobody in it is refused (422). The supervisor is no
    longer forced in: it is an optional organiser, not the group's prerequisite.
    """
    members: list[str] = []
    if include_supervisor:
        supervisor = store.supervisor()
        if supervisor is not None:
            members.append(supervisor["id"])
    for agent_id in requested:
        agent = store.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=400, detail=f"成员 ID「{agent_id}」不存在。")
        if agent["id"] not in members:
            members.append(agent["id"])
    if not members:
        raise HTTPException(status_code=422, detail="群组至少要有一位成员。")
    return members


def addressed_ids(
    roster: list[dict], content: str, picker: list[str], quoted_author: str | None
) -> list[str]:
    """Who the sender explicitly addressed: the @ picker's picks, opening @ names, a quote.

    A name in the middle of a sentence is usually a reference (「按照 @幕僚长 的方案」), so it
    stays out and the dispatcher decides about it.
    """
    known = {str(agent["id"]) for agent in roster}
    ids: list[str] = []

    def add(agent_id: str | None) -> None:
        if agent_id and agent_id in known and agent_id not in ids:
            ids.append(agent_id)

    for agent_id in picker:
        add(agent_id)
    for agent_id in parse_mentions(opening_mentions(content), roster):
        add(agent_id)
    add(quoted_author)
    return ids


@router.post("/conversations", status_code=201)
async def create_group(
    payload: GroupCreateRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """The user opens a group themselves; the supervisor always sits in it."""
    group = store.create_conversation(
        kind="group",
        title=payload.title,
        purpose=payload.purpose,
        member_ids=group_member_ids(
            store, payload.member_ids, include_supervisor=payload.include_supervisor
        ),
    )
    if payload.rules:
        group = store.update_conversation(group["id"], rules=payload.rules) or group
    publish_workspace(store, engine)
    publish_sidebar(store, engine)
    return models.conversation_payload(group)


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: str,
    payload: ConversationPatchRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    conversation = store.get_conversation(conversation_id)
    if conversation is None or conversation["kind"] == "background":
        raise HTTPException(status_code=404, detail="会话不存在。")
    if engine.is_running(conversation_id) or store.active_run(conversation_id):
        raise HTTPException(
            status_code=409, detail="这个会话的任务正在运行，请先停止当前任务再修改。"
        )
    updates: dict[str, Any] = {}
    if payload.rules is not None:
        if conversation["kind"] != "group":
            raise HTTPException(status_code=400, detail="只有群组能设群规则。")
        updates["rules"] = payload.rules
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.purpose is not None:
        updates["purpose"] = payload.purpose
    if payload.member_ids is not None:
        if conversation["kind"] != "group":
            raise HTTPException(status_code=400, detail="私聊的成员不能修改。")
        updates["member_ids"] = group_member_ids(store, payload.member_ids)
    updated = store.update_conversation(conversation_id, **updates)
    if updated is None:  # pragma: no cover - existence checked above
        raise HTTPException(status_code=404, detail="会话不存在。")
    return models.conversation_payload(updated)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def dissolve_group(
    conversation_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    """User-initiated dissolve: the client already asked, so no server-side confirm."""
    try:
        removed = store.dissolve_group(conversation_id)
    except GroupDissolveRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if removed is None:
        raise HTTPException(status_code=404, detail="群组不存在。")
    publish_workspace(store, engine)
    publish_sidebar(store, engine)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/runs", status_code=202)
async def start_run(
    conversation_id: str,
    payload: RunStartRequest,
    store: Store = Depends(get_store),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    if not payload.content and not payload.attachment_ids:
        raise HTTPException(status_code=422, detail="请先写下要说的话，或者附上图片。")
    conversation = store.get_conversation(conversation_id)
    if conversation is None or conversation["kind"] == "background":
        raise HTTPException(status_code=404, detail="会话不存在。")
    if conversation["kind"] == "private":
        raise HTTPException(status_code=403, detail="私聊只能旁观，不能发言。")
    if providers.resolve_default(store) is None:
        raise HTTPException(status_code=503, detail=providers.NO_MODEL)
    quoted: dict | None = None
    if payload.reply_to_id is not None:
        quoted = store.get_message(payload.reply_to_id)
        if quoted is None or quoted["conversation_id"] != conversation_id:
            raise HTTPException(status_code=422, detail="引用的消息不在这个会话里。")
        relay = quoted.get("relay") or {}
        if quoted["kind"] != "agent" or relay.get("direction") in ("in", "out"):
            raise HTTPException(status_code=422, detail="只能回复成员的消息。")
    mentions: list[str] = []
    for agent_id in payload.mentions:
        if agent_id not in conversation["member_ids"]:
            raise HTTPException(
                status_code=422, detail=f"成员 ID「{agent_id}」不在这个会话里，无法点名。"
            )
        if agent_id not in mentions:
            mentions.append(agent_id)
    roster = [
        agent
        for agent in (store.get_agent(member) for member in conversation["member_ids"])
        if agent is not None
    ]
    for agent_id in parse_mentions(payload.content, roster):
        if agent_id not in mentions:
            mentions.append(agent_id)
    quoted_author = quoted.get("author_id") if quoted is not None else None
    if quoted is not None and conversation["kind"] == "group":
        # Replying to a member's message is a way of addressing that member: it answers,
        # even though the user did not type an @ name.
        if (
            quoted_author
            and quoted_author in conversation["member_ids"]
            and quoted_author not in mentions
        ):
            mentions.append(quoted_author)
    addresses = addressed_ids(roster, payload.content, list(payload.mentions), quoted_author)
    # What the send does when it meets a running reply: an explicit choice from an API
    # caller wins; otherwise the bot it will meet decides, falling back to the team setting.
    # Images cannot join a request already on the wire, so such a send waits instead.
    mode = payload.mode or resolve_send_mode(store, conversation, store.active_run(conversation_id))
    if mode == "steer" and payload.attachment_ids:
        mode = "queue"
    run = await launcher.launch(
        conversation,
        content=payload.content,
        mentions=mentions,
        reply_to_id=payload.reply_to_id,
        attachment_ids=payload.attachment_ids,
        mode=mode,
        addresses=addresses,
    )
    assert run is not None  # only the scheduler path can find the conversation busy
    return models.run_payload(run)


def message_target(store: Store, conversation_id: str, message_id: str) -> tuple[dict, dict]:
    """The conversation and the message an edit/rewind names; 404 otherwise."""
    conversation = store.get_conversation(conversation_id)
    if conversation is None or conversation["kind"] == "background":
        raise HTTPException(status_code=404, detail="会话不存在。")
    message = store.get_message(message_id)
    if message is None or message["conversation_id"] != conversation_id:
        raise HTTPException(status_code=404, detail="这条消息不在这个会话里。")
    return conversation, message


def refuse_while_running(
    store: Store, engine: SupervisorEngine, conversation_id: str
) -> None:
    """A tail can only be taken back while nothing in it is still being written."""
    if engine.is_running(conversation_id) or store.active_run(conversation_id):
        raise HTTPException(
            status_code=409, detail="这个会话的任务正在运行，请先停止当前任务再改。"
        )


def announce_removed(engine: SupervisorEngine, conversation_id: str, message_ids: list[str]) -> None:
    """Tell every open window which rows went, one frame each, as the store already does."""
    for message_id in message_ids:
        engine.publish(
            "message_removed",
            {"conversation_id": conversation_id, "message_id": message_id},
        )


@router.post("/conversations/{conversation_id}/messages/{message_id}/rewind")
async def rewind_from_message(
    conversation_id: str,
    message_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """Delete everything said after one message; that message stays the newest.

    What goes is the whole tail: the replies, the tool rows, the images bound to them and
    the runs that only lived there. The conversation itself is untouched.
    """
    conversation, _ = message_target(store, conversation_id, message_id)
    if conversation["kind"] not in ("direct", "group"):
        raise HTTPException(status_code=403, detail="只有单独对话和群组可以这样做。")
    refuse_while_running(store, engine, conversation_id)
    removed = store.rewind_conversation(conversation_id, message_id)
    assert removed is not None  # existence checked above
    announce_removed(engine, conversation_id, removed)
    publish_workspace(store, engine)
    detail = models.conversation_detail_payload(store, conversation_id)
    if detail is None:  # pragma: no cover - the conversation cannot vanish here
        raise HTTPException(status_code=404, detail="会话不存在。")
    return detail


@router.post("/conversations/{conversation_id}/messages/{message_id}/edit", status_code=202)
async def edit_message(
    conversation_id: str,
    message_id: str,
    payload: MessageEditRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    """Correct one of your own messages: the tail goes, and the team answers the new text.

    The old row is replaced rather than edited in place, so nothing that answered the
    version the user took back stays in the history; its images move to the new message.
    """
    conversation, message = message_target(store, conversation_id, message_id)
    if conversation["kind"] == "private":
        raise HTTPException(status_code=403, detail="私聊只能旁观，不能发言。")
    if message["kind"] != "user":
        raise HTTPException(status_code=422, detail="只能修改你自己发的消息。")
    refuse_while_running(store, engine, conversation_id)
    if providers.resolve_default(store) is None:
        raise HTTPException(status_code=503, detail=providers.NO_MODEL)
    attachment_ids = [item["id"] for item in message["attachments"]]
    quoted_id = message["reply_to_id"]
    if quoted_id and store.get_message(quoted_id) is None:
        quoted_id = None
    removed = store.rewind_conversation(conversation_id, message_id, inclusive=True)
    assert removed is not None  # existence checked above
    announce_removed(engine, conversation_id, removed)
    roster = [
        agent
        for agent in (store.get_agent(member) for member in conversation["member_ids"])
        if agent is not None
    ]
    run = await launcher.launch(
        conversation,
        content=payload.content,
        mentions=parse_mentions(payload.content, roster),
        reply_to_id=quoted_id,
        attachment_ids=attachment_ids,
        addresses=addressed_ids(
            roster,
            payload.content,
            [],
            (store.get_message(quoted_id) or {}).get("author_id") if quoted_id else None,
        ),
    )
    assert run is not None  # interrupt=True never returns "busy"
    publish_workspace(store, engine)
    return models.run_payload(run)


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/reroute", status_code=202
)
async def reroute_message(
    conversation_id: str,
    message_id: str,
    payload: RerouteRequest,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    """The user re-dispatches one of their group messages to chosen members.

    No new user message is written: the same message gets a new ``route`` (by ``user``) and a
    fresh run answers it with exactly those speakers. A run still working on that message is
    stopped first, through the normal stop path, so its partial work never mixes with the new
    answer.
    """
    conversation, message = message_target(store, conversation_id, message_id)
    if conversation["kind"] != "group":
        raise HTTPException(status_code=400, detail="只有群组的消息可以重新派发。")
    if message["kind"] != "user":
        raise HTTPException(status_code=422, detail="只能重新派发你自己发的消息。")
    speakers: list[str] = []
    for agent_id in payload.speakers:
        if agent_id not in conversation["member_ids"]:
            raise HTTPException(
                status_code=422, detail=f"成员 ID「{agent_id}」不在这个群里，无法点名。"
            )
        if agent_id not in speakers:
            speakers.append(agent_id)
    if not speakers:  # pragma: no cover - the request model already requires one
        raise HTTPException(status_code=422, detail="至少选一位成员。")
    # Whatever was answering this message stops first (a queued send is simply refused).
    previous = store.get_run(message["run_id"]) if message.get("run_id") else None
    if previous is not None and previous["status"] == "queued":
        raise HTTPException(status_code=409, detail="这条消息还在排队，等它开始后再重新派发。")
    if previous is not None and (previous["status"] == "running" or launcher.is_live(previous["id"])):
        await launcher.finish_stop(previous["id"])
    if engine.is_running(conversation_id) or store.active_run(conversation_id) is not None:
        raise HTTPException(status_code=409, detail="这个群组正在回复，等它结束再重新派发。")
    updated = store.set_message_route(
        message_id,
        {
            "speakers": speakers,
            "kind": "answer",
            "task_id": None,
            "reason": "按你重新选择的人派发。",
            "by": "user",
        },
    )
    if updated is not None:
        engine.publish("message_end", models.message_payload(updated))
    run = store.begin_bare_run(conversation_id, str(message["content"]) or "重新派发")
    store.update_run(run["id"], current_agent_id=speakers[0])
    try:
        engine.start(
            run["id"],
            conversation_id,
            message_id=message_id,
            route_override=speakers,
        )
    except EngineBusy as exc:  # pragma: no cover - the slot was checked free above
        store.discard_run(run["id"])
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    publish_workspace(store, engine)
    return models.run_payload(store.get_run(run["id"]) or run)


@router.get("/conversations/{conversation_id}/tasks")
async def list_group_tasks(conversation_id: str, store: Store = Depends(get_store)) -> dict:
    """The work this group took on, newest first: who owns it, how it stands, its parts."""
    conversation = store.get_conversation(conversation_id)
    if conversation is None or conversation["kind"] != "group":
        raise HTTPException(status_code=404, detail="群组不存在。")
    return {"conversation_id": conversation_id, "tasks": store.group_tasks_payload(conversation_id)}


@router.post("/conversations/{conversation_id}/tasks/{task_id}/merge")
async def merge_group_task(
    conversation_id: str,
    task_id: str,
    task_scheduler: TaskScheduler = Depends(get_task_scheduler),
) -> dict:
    """「并入正在做的那件」: fold a queued task into its owner's running thread."""
    merged = await task_scheduler.merge(conversation_id, task_id)
    if merged is None:
        raise HTTPException(status_code=409, detail="这个任务不在排队，或它没有正在做的任务可以合并。")
    return merged


@router.post("/conversations/{conversation_id}/tasks/{task_id}/stop")
async def stop_group_task(
    conversation_id: str,
    task_id: str,
    task_scheduler: TaskScheduler = Depends(get_task_scheduler),
) -> dict:
    """Stop just this task: dequeue it, or stop only its own thread run."""
    stopped = await task_scheduler.stop(conversation_id, task_id)
    if stopped is None:
        raise HTTPException(status_code=409, detail="这个任务已经结束，不能再停止。")
    return stopped


@router.post("/conversations/{conversation_id}/tasks/{task_id}/resume")
async def resume_group_task(
    conversation_id: str,
    task_id: str,
    task_scheduler: TaskScheduler = Depends(get_task_scheduler),
) -> dict:
    """「接着做」: resume this exact task in its own thread (never posts 「继续」)."""
    resumed = await task_scheduler.resume(conversation_id, task_id)
    if resumed is None:
        raise HTTPException(status_code=409, detail="这个任务不在停下或失败的状态，不能接着做。")
    return resumed


@router.post("/conversations/{conversation_id}/tasks/{task_id}/dismiss")
async def dismiss_group_task(
    conversation_id: str,
    task_id: str,
    task_scheduler: TaskScheduler = Depends(get_task_scheduler),
) -> dict:
    """「不做了」: put a stopped/failed task down; its card stays in the timeline, greyed."""
    dismissed = task_scheduler.dismiss(conversation_id, task_id)
    if dismissed is None:
        raise HTTPException(status_code=409, detail="只有停下或失败的任务可以不做了。")
    return dismissed


@router.post("/conversations/{conversation_id}/tasks/{task_id}/seen")
async def mark_group_task_seen(
    conversation_id: str,
    task_id: str,
    task_scheduler: TaskScheduler = Depends(get_task_scheduler),
) -> dict:
    """The result was seen: a finished task leaves the sidebar and the chip list."""
    seen = task_scheduler.seen(conversation_id, task_id)
    if seen is None:
        raise HTTPException(status_code=409, detail="只有做完的任务可以标记为看过。")
    return seen


@router.get("/conversations/{conversation_id}/runs")
async def list_conversation_runs(
    conversation_id: str,
    limit: int | None = None,
    store: Store = Depends(get_store),
) -> dict:
    """The conversation's runs, newest first, optionally capped to ``limit``."""
    if store.is_background_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在。")
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    runs = store.list_conversation_runs(conversation_id)
    if limit is not None:
        runs = runs[:limit]
    return {"runs": [models.run_payload(run) for run in runs]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, store: Store = Depends(get_store)) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return models.run_payload(run)


@router.get("/runs/{run_id}/trace")
async def run_trace(run_id: str, store: Store = Depends(get_store)) -> dict:
    """The run, its hand-off graph and every message it wrote, oldest first."""
    trace = store.run_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return {
        "run": models.run_payload(trace["run"]),
        "conversation_id": trace["conversation_id"],
        "nodes": trace["nodes"],
        "edges": trace["edges"],
        "messages": [models.message_payload(message) for message in trace["messages"]],
    }


@router.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: str,
    store: Store = Depends(get_store),
    launcher: RunLauncher = Depends(get_launcher),
) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    # A task can still be working under a row that already says it ended: stop it too.
    if run["status"] == "running" or launcher.is_live(run_id):
        await launcher.finish_stop(run_id)
        run = store.get_run(run_id) or run
    return models.run_payload(run)


@router.delete("/runs/{run_id}", status_code=204)
async def cancel_queued_run(
    run_id: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> Response:
    """Take back a send that is still waiting for its turn; a running one must be stopped."""
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    removed = store.cancel_queued_run(run_id)
    if removed is None:
        raise HTTPException(status_code=409, detail="这条消息已经开始发送，不能取消了。")
    engine.publish(
        "message_removed",
        {"conversation_id": removed["conversation_id"], "message_id": removed["id"]},
    )
    publish_workspace(store, engine)
    return Response(status_code=204)
