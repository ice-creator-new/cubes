"""Agent skills as registry handlers: read, create and copy into the sandbox.

A skill is available to a bot when it is enabled and its name is not in the bot's
``tool_permissions.skills_disabled``. The three read/use tools appear only while at least
one skill is available; ``skill_create`` is always offered (any bot may write one, and it
starts disabled until the user enables it). ``skill_to_computer`` goes through the exact
same sandbox gate as the computer tools — connected sandbox, per-member computer
permission, the per-run consent card and the single holder — so its scripts can run there.
"""

from __future__ import annotations

import base64
from typing import Any

from .. import computer as computer_rules
from .. import models
from .. import skills as skills_rules
from ..errors import RunStopped, ToolError
from ..toolkit import ToolInputError
from . import computer as computer_tools
from .args import json_parser
from .names import READ_SKILL_FILE, SKILL_CREATE, SKILL_TO_COMPUTER, USE_SKILL
from .types import Runner, ToolHandler, ToolOutcome, cap_tool_text

ROLES = frozenset({"supervisor", "member"})
# Base64 chunks stay a whole number of 4-char groups and well under MAX_COMMAND_CHARS.
CHUNK_CHARS = 6_000
SKILLS_DEST = "/home/cubes/skills"


def _has_available(store, offer) -> bool:
    author = offer.author
    return author is not None and bool(store.available_skills(author))


def _available(store, author: dict, skill: dict) -> bool:
    if not skill["enabled"]:
        return False
    permissions = author.get("tool_permissions")
    if isinstance(permissions, dict):
        disabled = permissions.get("skills_disabled") or []
        if skill["name"] in disabled:
            return False
    return True


def _name_arg(payload: dict[str, Any]) -> str:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolInputError("需要 name（技能名称）。")
    return name.strip()


def _require_available(ctx, name: str) -> dict:
    skill = ctx.store.skill_by_name(name)
    if skill is None:
        raise ToolError(f"没有名为「{name}」的技能。")
    if not _available(ctx.store, ctx.author, skill):
        raise ToolError(f"技能「{name}」不可用：可能已停用，或这位成员禁用了它。")
    return skill


USE_SKILL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": USE_SKILL,
        "description": (
            "读取一个已启用技能的完整做法（SKILL.md 正文）并照着做。遇到技能对应的任务时先调用它。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "description": "技能名称"}},
            "required": ["name"],
        },
    },
}

READ_SKILL_FILE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_SKILL_FILE,
        "description": "读取技能文件夹里的一个文本文件（脚本、参考资料等）。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "技能名称"},
                "path": {"type": "string", "description": "相对路径，例如 scripts/extract.py"},
            },
            "required": ["name", "path"],
        },
    },
}

SKILL_CREATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SKILL_CREATE,
        "description": (
            "把一套可复用的做法写成技能。写好后需要用户在「技能」面板里启用，其他人才用得了。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名称：1 到 64 个小写字母、数字和连字符，如 weekly-report",
                },
                "description": {"type": "string", "description": "一句话说明这个技能是做什么的"},
                "body": {"type": "string", "description": "技能的完整做法（Markdown）"},
            },
            "required": ["name", "description", "body"],
        },
    },
}

SKILL_TO_COMPUTER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SKILL_TO_COMPUTER,
        "description": "把一个技能的文件夹放进沙盒电脑，让里面的脚本能在那里运行。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "description": "技能名称"}},
            "required": ["name"],
        },
    },
}


def handlers(store, hub) -> list[ToolHandler]:
    """The four skill tools; ``skill_to_computer`` rides the computer tools' gate."""
    return [
        ToolHandler(
            name=USE_SKILL,
            label="使用技能",
            schema=USE_SKILL_SCHEMA,
            roles=ROLES,
            parse=json_parser("使用技能"),
            run=_use_skill,
            offered_when=lambda offer: _has_available(store, offer),
        ),
        ToolHandler(
            name=READ_SKILL_FILE,
            label="读取技能文件",
            schema=READ_SKILL_FILE_SCHEMA,
            roles=ROLES,
            parse=json_parser("读取技能文件"),
            run=_read_skill_file,
            offered_when=lambda offer: _has_available(store, offer),
        ),
        ToolHandler(
            name=SKILL_CREATE,
            label="新建技能",
            schema=SKILL_CREATE_SCHEMA,
            roles=ROLES,
            parse=json_parser("新建技能"),
            run=_skill_create,
        ),
        ToolHandler(
            name=SKILL_TO_COMPUTER,
            label="技能放进沙盒",
            schema=SKILL_TO_COMPUTER_SCHEMA,
            roles=ROLES,
            parse=json_parser("技能放进沙盒"),
            run=_skill_to_computer,
            offered_when=lambda offer: hub.connected and _has_available(store, offer),
            computer_step=True,
        ),
    ]


async def _use_skill(ctx, payload: dict[str, Any]) -> ToolOutcome:
    name = _name_arg(payload)
    skill = _require_available(ctx, name)
    body = ctx.store.skill_body(skill)
    files = ctx.store.skill_files(skill)
    ctx.store.bump_skill_use(skill["id"])
    ctx.publish("skills", models.skills_payload(ctx.store))
    result: dict[str, Any] = {
        "ok": True,
        "name": name,
        "body": body,
        "files": files,
        "has_scripts": skill["has_scripts"],
    }
    if skill["has_scripts"]:
        result["note"] = (
            "这个技能带有脚本，脚本只能在沙盒电脑里运行：先用 skill_to_computer 把它放进沙盒，"
            "再用 computer_shell 运行。"
        )
    return ToolOutcome(summary=f"使用了技能「{name}」", result=result)


async def _read_skill_file(ctx, payload: dict[str, Any]) -> ToolOutcome:
    name = _name_arg(payload)
    path = payload.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ToolInputError("需要 path（相对路径）。")
    path = path.strip()
    skill = _require_available(ctx, name)
    if path == skills_rules.SKILL_FILE_NAME:
        raise ToolInputError("SKILL.md 请用 use_skill 读取。")
    parts = path.replace("\\", "/").split("/")
    if path.startswith("/") or ".." in parts:
        raise ToolInputError("路径不能是绝对路径，也不能包含 ..。")
    folder = ctx.store.skill_folder(name)
    root = folder.resolve()
    target = (folder / path).resolve()
    if target != root and root not in target.parents:
        raise ToolInputError("路径不在这个技能的目录里。")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ToolError(f"技能「{name}」里没有这个文件：{path}") from exc
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("这是二进制文件，读不了内容。") from exc
    result = {
        "ok": True,
        "name": name,
        "path": path,
        "content": cap_tool_text(content),
    }
    return ToolOutcome(summary=f"读取了「{name}」的 {path}", result=result)


async def _skill_create(ctx, payload: dict[str, Any]) -> ToolOutcome:
    raw_name = payload.get("name")
    raw_description = payload.get("description")
    raw_body = payload.get("body")
    if not isinstance(raw_name, str) or not isinstance(raw_description, str):
        raise ToolInputError("需要 name 和 description。")
    body = raw_body if isinstance(raw_body, str) else ""
    try:
        name = skills_rules.validate_name(raw_name)
        description = skills_rules.validate_description(raw_description)
        body = skills_rules.validate_body(body)
    except skills_rules.SkillInvalid as exc:
        raise ToolInputError(str(exc)) from exc
    if ctx.store.skill_name_taken(name):
        raise ToolInputError(f"技能「{name}」已经存在。")
    ctx.store.create_skill(
        name=name,
        description=description,
        body=body,
        source="agent",
        author_id=ctx.author["id"],
        enabled=False,
        has_scripts=False,
        file_count=1,
    )
    ctx.publish("skills", models.skills_payload(ctx.store))
    result = {
        "ok": True,
        "name": name,
        "note": "技能已创建，但需要用户在「技能」面板里启用后，大家才能使用。",
    }
    return ToolOutcome(summary=f"写了新技能「{name}」，等你在「技能」里启用", result=result)


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


async def _skill_to_computer(ctx, payload: dict[str, Any]) -> ToolOutcome:
    name = _name_arg(payload)
    skill = _require_available(ctx, name)
    hub = ctx.computer
    info = hub.info
    if info is None:
        raise ToolError(computer_rules.NOT_CONNECTED)
    run = ctx.run
    if run.computer_denied:
        raise ToolInputError(computer_tools.DENIED)
    gated = await computer_tools.consent_and_acquire(
        ctx, run, ctx.author, info, resume=f"把技能「{name}」放进沙盒电脑"
    )
    if gated is not None:
        return gated

    files = ctx.store.skill_files(skill)
    folder = ctx.store.skill_folder(name)
    dest_root = f"{info.get('skills_dir') or SKILLS_DEST}/{name}"
    commands: list[str] = []
    for entry in files:
        relative = entry["path"]
        data = (folder / relative).read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        dest = f"{dest_root}/{relative}"
        parent = dest.rsplit("/", 1)[0]
        for offset in range(0, len(encoded), CHUNK_CHARS):
            chunk = encoded[offset : offset + CHUNK_CHARS]
            redirect = ">" if offset == 0 else ">>"
            commands.append(
                f"mkdir -p {_sh_quote(parent)} && printf '%s' {_sh_quote(chunk)} "
                f"| base64 -d {redirect} {_sh_quote(dest)}"
            )
    scripts = [entry["path"] for entry in files if entry["path"].startswith("scripts/")]
    if scripts:
        commands.append("chmod +x " + " ".join(_sh_quote(f"{dest_root}/{p}") for p in scripts))

    stopped = lambda: run.stop_requested  # noqa: E731 - read at every poll
    for command in commands:
        reply = await hub.call("shell", {"command": command, "timeout": 60}, stopped=stopped)
        if reply is None:
            raise RunStopped("任务已停止。")
        if not reply.get("ok") or reply.get("exit_code") != 0:
            detail = str(reply.get("stderr") or reply.get("stdout") or "").strip()
            raise ToolError(detail or "把技能放进沙盒电脑时失败。")
    return ToolOutcome(
        summary=f"把技能「{name}」放进了沙盒电脑",
        result={"ok": True, "path": dest_root, "files": len(files)},
    )
