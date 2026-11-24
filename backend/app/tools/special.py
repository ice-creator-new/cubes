"""The two chat tools: private messages between bots, and reporting back to the user.

Both live in the engine's loop because a private message runs a whole nested reply inside
the current run; they are registry handlers so schemas, labels and routing stay in one table.
"""

from __future__ import annotations

import json
from typing import Any

from ..attachments import MAX_ATTACHMENTS_PER_MESSAGE
from ..errors import ToolError
from ..mentions import parse_mentions
from ..models import MAX_TOOL_TEXT_CHARS
from ..toolkit import ToolInputError
from . import resolve
from .args import parse_json_object
from .names import ASK_GROUP, DIRECT_MESSAGE, NOTIFY_USER
from .types import ToolHandler, ToolOutcome

ROLES = frozenset({"supervisor", "member"})
DIRECT_MESSAGE_LABEL = "私信成员"
# Private messages and group questions nest inside one run; both count against this depth.
MAX_DIRECT_MESSAGE_DEPTH = 2
ASK_GROUP_LABEL = "在群里问"
NOTIFY_USER_LABEL = "告诉用户"
NOTIFY_SUMMARY = "向你汇报了一件事"
MAX_NOTIFY_CHARS = 600

ASK_GROUP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_GROUP,
        "description": (
            "把一个问题带到你所在的某个群里让大家讨论：问题以你的名义发到群里，"
            "群里合适的同事会依次回答、互相补充，主管最后收尾；大家的回复会作为工具结果返回给你。"
            "适合需要多人看法的开放问题；只想问某一个人时用 direct_message。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "group_id": {
                    "type": "string",
                    "description": "群组 ID（你所在的群组列表里括号中的 id）；也可以直接写群组名称",
                },
                "question": {"type": "string", "description": "要在群里问大家的问题，写清背景"},
            },
            "required": ["group_id", "question"],
        },
    },
}

DIRECT_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DIRECT_MESSAGE,
        "description": (
            "给另一位成员发一条私信。用户的对话里只会留下来源标记，看不到你们说了什么；"
            "对方的真实回复会作为工具结果返回给你。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "对方的成员 ID（团队目录里括号中的 id）；也可以直接写对方的名字",
                },
                "content": {"type": "string", "description": "要私下告诉对方的内容"},
                "image_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要附带的图片 ID（att_ 开头，最多 4 个，只能用对话里出现过的）。"
                        "问题和图片有关时一定要附上原图，不要只用文字描述图片"
                    ),
                },
            },
            "required": ["agent_id", "content"],
        },
    },
}

NOTIFY_USER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": NOTIFY_USER,
        "description": (
            "向你自己的用户汇报这次私聊里值得知道的事：用你自己的话总结一两句。"
            "每次回复最多调用一次；大多数私聊不需要调用。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "content": {
                    "type": "string",
                    "description": "写给用户看的一两句总结（1-600 字），不要转发或引用对方的原话",
                }
            },
            "required": ["content"],
        },
    },
}

def handlers() -> list[ToolHandler]:
    """The chat tools; ``notify_user`` only exists where a private bot chat exists."""
    return [
        ToolHandler(
            name=DIRECT_MESSAGE,
            label=DIRECT_MESSAGE_LABEL,
            schema=DIRECT_MESSAGE_SCHEMA,
            roles=ROLES,
            parse=_direct_message_parser,
            run=_direct_message,
        ),
        ToolHandler(
            name=ASK_GROUP,
            label=ASK_GROUP_LABEL,
            schema=ASK_GROUP_SCHEMA,
            roles=ROLES,
            parse=_ask_group_parser,
            run=_ask_group,
            offered_when=_outside_groups,
            callable_when=_outside_groups,
        ),
        ToolHandler(
            name=NOTIFY_USER,
            label=NOTIFY_USER_LABEL,
            schema=NOTIFY_USER_SCHEMA,
            roles=ROLES,
            parse=_notify_parser,
            run=_notify_user,
            offered_when=_in_private,
            callable_when=_in_private,
        ),
    ]


def _in_private(offer) -> bool:
    """A private bot-to-bot chat is the only place ``notify_user`` means anything."""
    return offer.conversation is not None and offer.conversation["kind"] == "private"


def _outside_groups(offer) -> bool:
    """Inside a group a bot asks there with @ instead; elsewhere it may take a question to one."""
    return offer.conversation is None or offer.conversation["kind"] != "group"


def _direct_message_parser(raw: str | None) -> dict[str, Any]:
    return _direct_message_args(parse_json_object(raw, label=DIRECT_MESSAGE_LABEL))


def _ask_group_parser(raw: str | None) -> dict[str, Any]:
    return _ask_group_args(parse_json_object(raw, label=ASK_GROUP_LABEL))


def _notify_parser(raw: str | None) -> dict[str, Any]:
    return _notify_args(parse_json_object(raw, label=NOTIFY_USER_LABEL))


async def _direct_message(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Open (or reuse) the caller's private chat with one bot and let it answer."""
    run = ctx.run
    caller = ctx.author
    if run.dm_depth >= MAX_DIRECT_MESSAGE_DEPTH:
        raise ToolError(f"私信嵌套已达 {MAX_DIRECT_MESSAGE_DEPTH} 层上限，本次调用未执行。")
    target = resolve.resolve_agent(ctx.store, payload["agent_id"])
    if target is None:
        raise ToolError(
            f"找不到成员「{payload['agent_id']}」，请用团队目录里的名字或 ID，本次调用未执行。"
        )
    if target["id"] == caller["id"]:
        raise ToolInputError("不能给自己发私信，本次调用未执行。")
    image_ids = resolve.passable_images(ctx.store, caller, payload.get("image_ids") or [])
    known = {c["id"] for c in ctx.store.list_conversations()}
    private = ctx.store.ensure_private_conversation(caller["id"], target["id"])
    if private is None:  # pragma: no cover - both agents were just loaded
        raise ToolError("无法建立这两位成员的私聊，本次调用未执行。")
    private_id = private["id"]
    direct = ctx.store.ensure_direct_conversation(target["id"])
    # Open windows can only open a chat they know: announce any chat this call created
    # before the first message lands in it.
    if private_id not in known or (direct is not None and direct["id"] not in known):
        ctx.publish_workspace()
    # Clients can open the private chat while the other bot is still answering.
    targeted = ctx.store.set_action_target(
        ctx.action["id"], target_agent_id=target["id"], target_conversation_id=private_id
    )
    if targeted is not None:
        ctx.publish_message("message_end", targeted[1])

    origin = ctx.origin_message_id() if ctx.origin_message_id is not None else None
    outgoing = _append_private_message(
        ctx,
        private_id,
        caller,
        payload["content"],
        origin_message_id=origin,
        image_ids=image_ids,
    )
    # The target's own chat with the user shows only that a private message arrived;
    # whatever the bots actually said stays in the private chat.
    if direct is not None and direct["id"] != private_id:
        _append_relay_message(
            ctx,
            direct["id"],
            caller,
            "",
            {
                "conversation_id": private_id,
                "direction": "in",
                "peer_id": caller["id"],
                "peer_name": caller["name"],
            },
        )

    roster = ctx.roster(private)
    ctx.set_step(f"正在和「{target['name']}」私聊")
    run.dm_depth += 1
    review: dict[str, Any] = {}
    try:
        if target["kind"] == "supervisor":
            reply = await _loop(ctx, "supervisor_reply")(private, roster, outgoing["id"])
        else:
            messages = _loop(ctx, "member_context")(private, roster, target)
            _, reply = await _loop(ctx, "member_call")(
                private, target, messages, outgoing["id"], roster
            )
            if caller["kind"] == "supervisor" and ctx.review_member is not None:
                text, reply, review = await ctx.review_member(
                    private,
                    target,
                    payload["content"],
                    messages,
                    outgoing["id"],
                    roster,
                    (reply or {}).get("content", ""),
                    reply or {},
                )
    finally:
        run.dm_depth -= 1

    text = reply["content"] if reply is not None else ""
    if direct is not None and direct["id"] != private_id:
        _append_relay_message(
            ctx,
            direct["id"],
            target,
            "",
            {
                "conversation_id": private_id,
                "direction": "out",
                "peer_id": caller["id"],
                "peer_name": caller["name"],
            },
        )
    ctx.set_step("正在整理私聊结果")
    reply_images = [image["id"] for image in (reply or {}).get("attachments") or []]
    result: dict[str, Any] = {
        "ok": True,
        "agent_id": target["id"],
        "conversation_id": private_id,
        "message_id": reply["id"] if reply is not None else outgoing["id"],
        "content": text,
    }
    if review:
        result["review"] = review
    if reply_images:
        result["image_ids"] = reply_images
    if image_ids and not resolve.sees_images(ctx.store, target):
        result["note"] = f"「{target['name']}」用的模型看不到图片内容，只能看到图片 ID。"
    return ToolOutcome(
        summary=f"私信 {target['name']}",
        result=result,
        target_agent_id=target["id"],
        target_conversation_id=private_id,
        images=reply_images,
    )


async def _ask_group(ctx, payload: dict[str, Any]) -> ToolOutcome:
    """Post the caller's question in one of its groups and let that group discuss it."""
    run = ctx.run
    caller = ctx.author
    if run.dm_depth >= MAX_DIRECT_MESSAGE_DEPTH:
        raise ToolError(f"私信与群聊提问嵌套已达 {MAX_DIRECT_MESSAGE_DEPTH} 层上限，本次调用未执行。")
    group = resolve.resolve_group(ctx.store, payload["group_id"])
    if group is None:
        raise ToolError(f"找不到群组「{payload['group_id']}」，请用你所在群组列表里的名字或 ID，本次调用未执行。")
    if caller["id"] not in group["member_ids"]:
        raise ToolError(f"你不在群组「{group['title']}」里，只能在自己所在的群里提问，本次调用未执行。")
    if group["id"] == ctx.conversation["id"]:
        raise ToolInputError("你就在这个群里，直接用 @名字 问大家，本次调用未执行。")
    if ctx.group_busy is not None and ctx.group_busy(group["id"]):
        raise ToolError(f"群组「{group['title']}」里正有一场对话在进行，稍后再问或改用 direct_message，本次调用未执行。")
    discuss = _loop(ctx, "group_discussion")
    roster = ctx.roster(group)
    origin = ctx.origin_message_id() if ctx.origin_message_id is not None else None
    question = ctx.store.append_message(
        conversation_id=group["id"],
        author_id=caller["id"],
        author_name=caller["name"],
        avatar_seed=caller["avatar_seed"],
        kind="agent",
        content=payload["question"],
        status="complete",
        mentions=parse_mentions(payload["question"], roster),
        origin_message_id=origin,
        run_id=run.run_id,
    )
    # The calling chat links to the group while the discussion is still going.
    targeted = ctx.store.set_action_target(
        ctx.action["id"], target_agent_id=None, target_conversation_id=group["id"]
    )
    if targeted is not None:
        ctx.publish_message("message_end", targeted[1])
    ctx.publish_message("message_start", question)
    ctx.publish_message("message_end", question)
    ctx.set_step(f"正在「{group['title']}」里问大家")
    run.dm_depth += 1
    try:
        replies = await discuss(group, caller, question)
    finally:
        run.dm_depth -= 1
    ctx.set_step("正在整理群里的回答")
    answers = [{"from": reply["author_name"], "content": reply["content"]} for reply in replies]
    result: dict[str, Any] = {
        "ok": True,
        "conversation_id": group["id"],
        "message_id": question["id"],
        "answers": answers,
    }
    if not answers:
        result["note"] = "群里暂时没有人能回答这个问题。"
    return ToolOutcome(
        summary=f"在「{group['title']}」里问了大家",
        result=result,
        target_conversation_id=group["id"],
    )


def _loop(ctx, name: str):
    """A loop service the engine binds for this call; a chat tool cannot work without it."""
    service = getattr(ctx, name)
    if service is None:  # pragma: no cover - the engine always binds these
        raise ToolError("私信通道没有就绪，本次调用未执行。")
    return service


def _append_private_message(
    ctx,
    conversation_id: str,
    author: dict,
    content: str,
    *,
    origin_message_id: str | None = None,
    image_ids: list[str] | tuple[str, ...] = (),
) -> dict:
    message = ctx.store.append_message(
        conversation_id=conversation_id,
        author_id=author["id"],
        author_name=author["name"],
        avatar_seed=author["avatar_seed"],
        kind="agent",
        content=content,
        status="complete",
        origin_message_id=origin_message_id,
        run_id=ctx.run.run_id,
    )
    if image_ids:
        message = ctx.store.attach_to_message(message["id"], list(image_ids)) or message
    ctx.publish_message("message_start", message)
    ctx.publish_message("message_end", message)
    return message


def _append_relay_message(
    ctx, conversation_id: str, author: dict, content: str, relay: dict
) -> dict:
    message = ctx.store.append_message(
        conversation_id=conversation_id,
        author_id=author["id"],
        author_name=author["name"],
        avatar_seed=author["avatar_seed"],
        kind="agent",
        content=content,
        status="complete",
        relay=relay,
        run_id=ctx.run.run_id,
    )
    ctx.publish_message("message_start", message)
    ctx.publish_message("message_end", message)
    return message


async def _notify_user(ctx, payload: dict[str, str]) -> ToolOutcome:
    """Write the caller's own one-line summary into its chat with the user."""
    conversation, author = ctx.conversation, ctx.author
    peer = resolve.peer(ctx.store, conversation, author)
    key = (conversation["id"], author["id"])
    if key in ctx.run.notified:
        raise ToolError("这次回复已经告诉过用户了，不要重复汇报。")
    direct = (
        ctx.store.direct_conversation()
        if author["kind"] == "supervisor"
        else ctx.store.ensure_direct_conversation(author["id"])
    )
    if direct is None:  # pragma: no cover - every agent has a chat with the user
        raise ToolError("找不到你和用户的私聊，本次调用未执行。")
    message = ctx.store.append_message(
        conversation_id=direct["id"],
        author_id=author["id"],
        author_name=author["name"],
        avatar_seed=author["avatar_seed"],
        kind="agent",
        content=payload["content"],
        status="complete",
        run_id=ctx.run.run_id,
        relay={
            "conversation_id": conversation["id"],
            "direction": "summary",
            "peer_id": peer["id"] if peer is not None else author["id"],
            "peer_name": peer["name"] if peer is not None else "另一位成员",
        },
    )
    ctx.run.notified.add(key)
    ctx.publish_message("message_start", message)
    ctx.publish_message("message_end", message)
    return ToolOutcome(
        summary=NOTIFY_SUMMARY,
        result={"ok": True},
        text=json.dumps(
            {"ok": True, "message": "已经把这件事告诉用户了。"}, ensure_ascii=False
        ),
    )


def _direct_message_args(payload: dict[str, Any]) -> dict[str, Any]:
    agent_id = payload.get("agent_id")
    content = payload.get("content")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ToolInputError("私信缺少对方成员 ID，本次调用未执行。")
    if not isinstance(content, str) or not content.strip():
        raise ToolInputError("私信正文不能为空，本次调用未执行。")
    body = content.strip()
    if len(body) > MAX_TOOL_TEXT_CHARS:
        raise ToolInputError(
            f"私信正文不能超过 {MAX_TOOL_TEXT_CHARS} 个字符，本次调用未执行。"
        )
    return {"agent_id": agent_id.strip(), "content": body, "image_ids": _image_ids_arg(payload)}


def _ask_group_args(payload: dict[str, Any]) -> dict[str, Any]:
    group_id = payload.get("group_id")
    question = payload.get("question")
    if not isinstance(group_id, str) or not group_id.strip():
        raise ToolInputError("在群里提问缺少群组 ID 或名称，本次调用未执行。")
    if not isinstance(question, str) or not question.strip():
        raise ToolInputError("要在群里问的问题不能为空，本次调用未执行。")
    body = question.strip()
    if len(body) > MAX_TOOL_TEXT_CHARS:
        raise ToolInputError(f"问题不能超过 {MAX_TOOL_TEXT_CHARS} 个字符，本次调用未执行。")
    return {"group_id": group_id.strip(), "question": body}


def _image_ids_arg(payload: dict[str, Any]) -> list[str]:
    """The optional ``image_ids`` of a messaging tool, cleaned; a bad value is the model's to fix."""
    raw = payload.get("image_ids")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ToolInputError("image_ids 必须是图片 ID 组成的数组，本次调用未执行。")
    ids = list(dict.fromkeys(item.strip() for item in raw if item.strip()))
    if len(ids) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ToolInputError("一次最多附带 4 张图片，本次调用未执行。")
    return ids


def _notify_args(payload: dict[str, Any]) -> dict[str, str]:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ToolInputError("汇报内容不能为空，本次调用未执行。")
    body = content.strip()
    if len(body) > MAX_NOTIFY_CHARS:
        raise ToolInputError(
            f"汇报内容不能超过 {MAX_NOTIFY_CHARS} 个字符，本次调用未执行。"
        )
    return {"content": body}
