"""Resolving what a tool names: agents, groups, sandbox-visible images.

Tool calls arrive with whatever the model wrote — an id, a name, a title. These helpers
turn that into a row or a correctable error, so every family reports the same Chinese
message for the same mistake instead of each writing its own.
"""

from __future__ import annotations

from typing import Sequence

from ..errors import ToolError, UpstreamError
from ..providers import resolve_model
from ..toolkit import ToolInputError


def supervisor(store) -> dict:
    """The workspace's supervisor; bootstrap guarantees exactly one."""
    agent = store.supervisor()
    if agent is None:  # pragma: no cover - bootstrap guarantees one supervisor
        raise UpstreamError("工作区没有主管，无法开始任务。")
    return agent


def resolve_agent(store, ref: str) -> dict | None:
    """An agent by id, or by name (trimmed, case-insensitive) when the model wrote one."""
    agent = store.get_agent(ref.strip())
    if agent is not None:
        return agent
    wanted = ref.strip().strip("@「」").casefold()
    for candidate in store.list_agents():
        if candidate["name"].strip().casefold() == wanted:
            return candidate
    return None


def resolve_group(store, ref: str) -> dict | None:
    """A group by id, or by exact title (trimmed, case-insensitive) when one was written."""
    conversation = store.get_conversation(ref.strip())
    if conversation is not None and conversation["kind"] == "group":
        return conversation
    wanted = ref.strip().strip("@「」").casefold()
    for candidate in store.list_conversations():
        if candidate["kind"] == "group" and candidate["title"].strip().casefold() == wanted:
            return candidate
    return None


def require_group(store, group_id: str) -> dict:
    """The group a tool names, or the error the model can correct."""
    conversation = store.get_conversation(group_id)
    if conversation is None:
        raise ToolError(f"群组 ID「{group_id}」不存在，本次调用未执行。")
    if conversation["kind"] != "group":
        raise ToolError("该 ID 不是群组，本工具只能在群组中使用。")
    return conversation


def group_members(store, member_refs: Sequence[str]) -> list[str]:
    """Resolve a group's members by id or name; the supervisor is optional, empty is refused.

    The model writes whatever it saw in the team directory — an id, a bare name, 「@名字」 —
    so both forms resolve. A group with nobody in it cannot route a message, so it is refused
    with a sentence the model can act on.
    """
    members: list[str] = []
    for ref in member_refs:
        agent = resolve_agent(store, str(ref))
        if agent is None:
            raise ToolError(
                f"成员「{ref}」不存在，请使用当前团队目录中的名字或 ID，本次调用未执行。"
            )
        if agent["id"] not in members:
            members.append(agent["id"])
    if not members:
        raise ToolError("群组至少要有一位成员，本次调用未执行。")
    return members


def roster(store, conversation: dict) -> list[dict]:
    """Every member of one conversation that still exists, in the group's own order."""
    members: list[dict] = []
    for agent_id in conversation["member_ids"]:
        agent = store.get_agent(agent_id)
        if agent is not None:
            members.append(agent)
    return members


def agent_name(store, agent_id: str) -> str:
    agent = store.get_agent(agent_id)
    return agent["name"] if agent is not None else agent_id


def peer(store, conversation: dict, agent: dict) -> dict | None:
    """The other bot in a private chat, or ``None`` outside one."""
    for agent_id in conversation["member_ids"]:
        if agent_id != agent["id"]:
            return store.get_agent(agent_id)
    return None


def sees_images(store, agent: dict) -> bool:
    """Whether the model this bot answers with right now was marked as able to see images."""
    try:
        return resolve_model(store, agent).vision
    except UpstreamError:
        return False


def sees_images_by_id(store, agent_id: str) -> bool:
    agent = store.get_agent(agent_id)
    return agent is not None and sees_images(store, agent)


def reads_pdf(store, agent: dict) -> bool:
    """Whether the model this bot answers with right now can be handed a PDF document."""
    try:
        return resolve_model(store, agent).pdf
    except UpstreamError:
        return False


def passable_images(store, sender: dict, image_ids: list[str]) -> list[str]:
    """``image_ids`` when the sender has seen every one, else a correctable tool error."""
    visible = store.visible_attachments(sender["id"], image_ids)
    unseen = [image for image in image_ids if image not in visible]
    if unseen:
        raise ToolInputError(f"图片 {unseen[0]} 不在你能看到的对话里，本次调用未执行。")
    return image_ids
