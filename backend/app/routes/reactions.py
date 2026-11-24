"""The user's reactions: add and remove one emoji on a message.

Adding publishes the ``reactions`` event and, when the message belongs to an agent that
still exists, queues that agent's debounced reaction response. Removing is idempotent and
cancels a still-pending response that no longer has anything to respond to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import reactions as reaction_rules
from ..engine import SupervisorEngine
from ..store import Store
from ..store.reactions import USER_ACTOR_ID, USER_ACTOR_NAME
from .common import get_engine, get_store

router = APIRouter()


def _message_or_404(store: Store, message_id: str) -> dict:
    message = store.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="消息不存在。")
    return message


@router.put("/messages/{message_id}/reactions/{emoji}")
async def add_reaction(
    message_id: str,
    emoji: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """The user adds ``emoji`` (URL-encoded path segment) to a message. Idempotent."""
    message = _message_or_404(store, message_id)
    reason = reaction_rules.reactable_reason(
        message, store.get_conversation(message["conversation_id"])
    )
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)
    emoji = reaction_rules.normalize_emoji(emoji)
    if emoji is None:
        raise HTTPException(status_code=422, detail="这不是一个合法的表情。")
    if not store.add_reaction(message_id, emoji, USER_ACTOR_ID, USER_ACTOR_NAME):
        raise HTTPException(status_code=409, detail="这条消息的表情已经达到上限（20 个）。")
    reactions = store.reactions_payload(message_id)
    engine.publish(
        "reactions",
        {
            "conversation_id": message["conversation_id"],
            "message_id": message_id,
            "reactions": reactions,
        },
    )
    engine.schedule_reaction_response(message)
    return {
        "message_id": message_id,
        "conversation_id": message["conversation_id"],
        "reactions": reactions,
    }


@router.delete("/messages/{message_id}/reactions/{emoji}")
async def remove_reaction(
    message_id: str,
    emoji: str,
    store: Store = Depends(get_store),
    engine: SupervisorEngine = Depends(get_engine),
) -> dict:
    """The user removes their own ``emoji``; idempotent (absent still returns the list)."""
    message = _message_or_404(store, message_id)
    emoji = reaction_rules.normalize_emoji(emoji)
    if emoji is None:
        raise HTTPException(status_code=422, detail="这不是一个合法的表情。")
    store.remove_reaction(message_id, emoji, USER_ACTOR_ID)
    reactions = store.reactions_payload(message_id)
    engine.publish(
        "reactions",
        {
            "conversation_id": message["conversation_id"],
            "message_id": message_id,
            "reactions": reactions,
        },
    )
    engine.cancel_reaction_response(message)
    return {
        "message_id": message_id,
        "conversation_id": message["conversation_id"],
        "reactions": reactions,
    }
