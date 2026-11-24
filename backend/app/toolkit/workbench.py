"""Pack B (part 2) — the workbench: shared notes and tasks with REST-shaped payloads."""

from __future__ import annotations

import re
from typing import Any

from .base import MAX_RESULT_CHARS, ToolContext, ToolInputError, ToolSpec, cap_text

MAX_NOTE_TITLE = 120
MAX_NOTE_CONTENT = 20_000
MAX_TASK_TITLE = 120
MAX_TASK_DETAIL = 4_000
MAX_TASK_NOTE = 4_000
PREVIEW_CHARS = 120
DEFAULT_NOTES = 20
DEFAULT_TASKS = 50

TASK_STATUSES = ("todo", "doing", "done", "blocked")
STATUS_LABELS = {"todo": "待办", "doing": "进行中", "done": "已完成", "blocked": "受阻"}

NOTE_CREATE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "description": "笔记标题，一句话。"},
        "content": {"type": "string", "description": "笔记正文，可以是 Markdown。"},
    },
    "required": ["title", "content"],
}

NOTE_UPDATE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "note_id": {"type": "string", "description": "要修改的笔记 ID。"},
        "content": {"type": "string", "description": "新的完整正文，会替换旧正文。"},
        "title": {"type": "string", "description": "新的标题，可省略。"},
    },
    "required": ["note_id"],
}

NOTE_READ_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"note_id": {"type": "string", "description": "要读取的笔记 ID。"}},
    "required": ["note_id"],
}

NOTE_DELETE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "note_id": {"type": "string", "description": "要删除的笔记 ID。"},
        "reason": {"type": "string", "description": "为什么删，可省略。"},
    },
    "required": ["note_id"],
}

NOTE_LIST_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "description": "可选，只在标题和正文里包含这个词的笔记。"},
    },
}

TASK_CREATE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "description": "任务标题，一句话说清要做什么。"},
        "detail": {"type": "string", "description": "补充说明，可省略。"},
        "assignee_id": {"type": "string", "description": "负责人 ID，可省略。"},
    },
    "required": ["title"],
}

TASK_UPDATE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_id": {"type": "string", "description": "要更新的任务 ID。"},
        "status": {
            "type": "string",
            "enum": list(TASK_STATUSES),
            "description": "新状态：todo 待办、doing 进行中、done 已完成、blocked 受阻。",
        },
        "note": {"type": "string", "description": "进度说明，可省略。"},
    },
    "required": ["task_id", "status"],
}

TASK_LIST_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": list(TASK_STATUSES),
            "description": "只看某个状态，可省略。",
        },
    },
}


# ------------------------------------------------------------------ payloads


def collapse(text: str) -> str:
    return " ".join(str(text or "").split())


def plain_text(markdown: str) -> str:
    """Readable plain text for list previews: markers, fences, pipes and links removed."""
    lines: list[str] = []
    for raw in str(markdown or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^>\s*", "", line)
        line = re.sub(r"^(?:[-*+]|\d{1,2}[.)])\s+", "", line)
        if re.fullmatch(r"[-:|\s]+", line):
            continue
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"`([^`]*)`", r"\1", line)
        line = line.replace("**", "").replace("__", "").replace("~~", "")
        line = line.replace("|", " ")
        if line:
            lines.append(line)
    return collapse(" ".join(lines))


def note_preview(content: str) -> str:
    return plain_text(content)[:PREVIEW_CHARS]


def notes_payload(store: Any) -> dict:
    """``GET /notes`` and the ``notes`` SSE event: title, preview, author and time."""
    return {
        "notes": [
            {
                "id": note["id"],
                "title": note["title"],
                "preview": note_preview(note["content"]),
                "author_id": note["author_id"],
                "updated_at": note["updated_at"],
            }
            for note in store.list_notes()
        ]
    }


def note_payload(note: dict) -> dict:
    """``GET /notes/{id}``: the whole note."""
    return {
        "id": note["id"],
        "title": note["title"],
        "content": note["content"],
        "author_id": note["author_id"],
        "created_at": note["created_at"],
        "updated_at": note["updated_at"],
    }


def tasks_payload(store: Any) -> dict:
    """``GET /tasks`` and the ``tasks`` SSE event."""
    return {"tasks": [task for task in store.list_tasks()]}


# --------------------------------------------------------------- validation


def _text(args: dict, key: str, *, limit: int, field: str, required: bool = True) -> str | None:
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ToolInputError(f"{field}不能为空。")
        return None
    if not isinstance(raw, str):
        raise ToolInputError(f"{field}必须是文本。")
    text = raw.strip()
    if len(text) > limit:
        raise ToolInputError(f"{field}不能超过 {limit} 个字符。")
    return text


def _identifier(args: dict, key: str, field: str) -> str:
    value = _text(args, key, limit=80, field=field)
    assert value is not None
    return value


def _status(args: dict, required: bool = True) -> str | None:
    raw = args.get("status")
    if raw is None:
        if required:
            raise ToolInputError("状态不能为空。")
        return None
    if raw not in TASK_STATUSES:
        raise ToolInputError("状态只能是 todo、doing、done 或 blocked。")
    return str(raw)


def _when(value: str) -> str:
    return collapse(value)[:16].replace("T", " ")


def _agent_name(store: Any, agent_id: str | None) -> str:
    if not agent_id:
        return "未指派"
    agent = store.get_agent(agent_id)
    return str(agent["name"]) if agent is not None else agent_id


# -------------------------------------------------------------------- notes


async def run_note_create(ctx: ToolContext, args: dict) -> str:
    title = _text(args, "title", limit=MAX_NOTE_TITLE, field="笔记标题")
    content = _text(args, "content", limit=MAX_NOTE_CONTENT, field="笔记正文")
    assert title is not None and content is not None
    note = ctx.store.create_note(title=title, content=content, author_id=ctx.agent_id)
    ctx.publish("notes", notes_payload(ctx.store))
    return f"已创建笔记「{note['title']}」（ID：{note['id']}）。"


async def run_note_update(ctx: ToolContext, args: dict) -> str:
    note_id = _identifier(args, "note_id", "笔记 ID")
    content = _text(args, "content", limit=MAX_NOTE_CONTENT, field="笔记正文", required=False)
    title = _text(args, "title", limit=MAX_NOTE_TITLE, field="笔记标题", required=False)
    if content is None and title is None:
        raise ToolInputError("需要提供要修改的标题或正文。")
    note = ctx.store.update_note(
        note_id,
        **({} if title is None else {"title": title}),
        **({} if content is None else {"content": content}),
    )
    if note is None:
        raise ToolInputError(f"笔记「{note_id}」不存在。")
    ctx.publish("notes", notes_payload(ctx.store))
    body = collapse(note["content"])[:600]
    return f"已更新笔记「{note['title']}」（ID：{note_id}）。\n{body}"


async def run_note_delete(ctx: ToolContext, args: dict) -> str:
    note_id = _identifier(args, "note_id", "笔记 ID")
    reason = _text(args, "reason", limit=200, field="删除原因", required=False)
    note = ctx.store.get_note(note_id)
    if note is None or not ctx.store.delete_note(note_id):
        raise ToolInputError(f"笔记「{note_id}」不存在。")
    title = str(note["title"])
    # The engine asks for the summary after the run, when the title is already gone; the
    # payload carries it back under a private key the model can never send.
    args["_note_title"] = title
    ctx.publish("notes", notes_payload(ctx.store))
    return f"已删除笔记「{title}」（ID：{note_id}）。" + (f"原因：{reason}" if reason else "")


async def run_note_read(ctx: ToolContext, args: dict) -> str:
    note_id = _identifier(args, "note_id", "笔记 ID")
    note = ctx.store.get_note(note_id)
    if note is None:
        raise ToolInputError(f"笔记「{note_id}」不存在。")
    author = _agent_name(ctx.store, note["author_id"])
    return cap_text(
        f"笔记「{note['title']}」（作者：{author}，更新于 {_when(note['updated_at'])}，ID：{note_id}）\n"
        f"{note['content']}",
        MAX_RESULT_CHARS,
    )


async def run_note_list(ctx: ToolContext, args: dict) -> str:
    query = _text(args, "query", limit=100, field="关键词", required=False)
    notes = ctx.store.list_notes(query) if query else ctx.store.list_notes()
    if not notes:
        return "还没有笔记。" if not query else f"没有包含「{query}」的笔记。"
    lines = [f"共 {len(notes)} 条笔记："]
    for index, note in enumerate(notes[:DEFAULT_NOTES], start=1):
        lines.append(
            f"{index}. 「{note['title']}」{_agent_name(ctx.store, note['author_id'])}"
            f"（{_when(note['updated_at'])}，ID：{note['id']}）"
        )
        lines.append(f" {note_preview(note['content'])}")
    if len(notes) > DEFAULT_NOTES:
        lines.append(f"还有 {len(notes) - DEFAULT_NOTES} 条未显示。")
    return cap_text("\n".join(lines), MAX_RESULT_CHARS)


# -------------------------------------------------------------------- tasks


async def run_task_create(ctx: ToolContext, args: dict) -> str:
    title = _text(args, "title", limit=MAX_TASK_TITLE, field="任务标题")
    detail = _text(args, "detail", limit=MAX_TASK_DETAIL, field="任务说明", required=False) or ""
    assignee = _text(args, "assignee_id", limit=80, field="负责人 ID", required=False)
    if assignee is not None and ctx.store.get_agent(assignee) is None:
        raise ToolInputError(f"成员「{assignee}」不存在。")
    assert title is not None
    task = ctx.store.create_task(
        title=title,
        detail=detail,
        status="todo",
        assignee_id=assignee,
        author_id=ctx.agent_id,
    )
    ctx.publish("tasks", tasks_payload(ctx.store))
    return (
        f"已创建任务「{task['title']}」（ID：{task['id']}，负责人："
        f"{_agent_name(ctx.store, task['assignee_id'])}，状态：待办）。"
    )


async def run_task_update(ctx: ToolContext, args: dict) -> str:
    task_id = _identifier(args, "task_id", "任务 ID")
    status = _status(args)
    note = _text(args, "note", limit=MAX_TASK_NOTE, field="进度说明", required=False)
    assert status is not None
    task = ctx.store.update_task(
        task_id, status=status, **({} if note is None else {"note": note})
    )
    if task is None:
        raise ToolInputError(f"任务「{task_id}」不存在。")
    ctx.publish("tasks", tasks_payload(ctx.store))
    return f"任务「{task['title']}」已更新为{STATUS_LABELS[task['status']]}。"


async def run_task_list(ctx: ToolContext, args: dict) -> str:
    status = _status(args, required=False)
    tasks = ctx.store.list_tasks(status) if status else ctx.store.list_tasks()
    if not tasks:
        return "还没有任务。" if not status else f"没有{STATUS_LABELS[status]}的任务。"
    lines: list[str] = []
    index = 0
    for group in TASK_STATUSES:
        group_tasks = [task for task in tasks if task["status"] == group]
        if not group_tasks:
            continue
        lines.append(f"{STATUS_LABELS[group]}：")
        for task in group_tasks:
            index += 1
            lines.append(
                f"{index}. 「{task['title']}」负责人：{_agent_name(ctx.store, task['assignee_id'])}"
                f"（ID：{task['id']}）"
            )
            if (task.get("detail") or "").strip():
                lines.append(f" {collapse(task['detail'])[:200]}")
            if (task.get("note") or "").strip():
                lines.append(f" 进展：{collapse(task['note'])[:200]}")
        if index >= DEFAULT_TASKS:
            break
    if len(tasks) > index:
        lines.append(f"还有 {len(tasks) - index} 条任务未显示。")
    return cap_text("\n".join(lines), MAX_RESULT_CHARS)


def _summary(value: Any, limit: int) -> str:
    text = collapse(str(value or ""))
    return text if len(text) <= limit else text[: limit - 1] + "…"


WORKBENCH_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="note_create",
        label="新建笔记",
        description=(
            "把值得长期保留的结论写进团队笔记，所有人可见，用户也能在工作台里看到。"
            "结构性成果、关键数据和最终决定都该落成笔记。"
        ),
        parameters=NOTE_CREATE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_note_create,
        summary=lambda args: f"新建笔记「{_summary(args.get('title'), 30)}」",
    ),
    ToolSpec(
        name="note_update",
        label="更新笔记",
        description="修改已有笔记的标题或正文；正文会整体替换，先读一遍再改。",
        parameters=NOTE_UPDATE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_note_update,
        summary=lambda args: "更新笔记",
    ),
    ToolSpec(
        name="note_delete",
        label="删除笔记",
        description=(
            "删除一篇已经过时的笔记，删除后不能恢复，也没有撤销。"
            "只删确定不再需要的那篇，并在原因里写清楚为什么删。"
        ),
        parameters=NOTE_DELETE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_note_delete,
        summary=lambda args: f"删除笔记「{args.get('_note_title') or args.get('note_id')}」",
    ),
    ToolSpec(
        name="note_read",
        label="读取笔记",
        description="按 ID 读取一篇笔记的完整正文。",
        parameters=NOTE_READ_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_note_read,
        summary=lambda args: "读取笔记",
    ),
    ToolSpec(
        name="note_list",
        label="查看笔记",
        description="列出团队笔记的标题和摘要，可以按关键词筛选，用来找回之前的结论。",
        parameters=NOTE_LIST_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_note_list,
        summary=lambda args: "查看笔记",
    ),
    ToolSpec(
        name="task_create",
        label="新建任务",
        description=(
            "把要做的事登记成任务，可以指派给某位成员。"
            "需要跟踪进度、多人协作时用它，不要只在聊天里口头交代。"
        ),
        parameters=TASK_CREATE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_task_create,
        summary=lambda args: f"新建任务「{_summary(args.get('title'), 30)}」",
    ),
    ToolSpec(
        name="task_update",
        label="更新任务",
        description="更新任务状态和进度说明。开始做、做完或卡住时都该更新。",
        parameters=TASK_UPDATE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_task_update,
        summary=lambda args: f"任务改为{STATUS_LABELS.get(str(args.get('status')), '新状态')}",
    ),
    ToolSpec(
        name="task_list",
        label="查看任务",
        description="按状态查看任务清单，了解谁在做什么、还剩什么。",
        parameters=TASK_LIST_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_task_list,
        summary=lambda args: "查看任务",
    ),
)
