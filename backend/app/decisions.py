"""What the sidebar lists while something is waiting on the user.

A confirmation card and a workflow question are the same kind of wait from the user's
side: someone, somewhere, needs an answer, and silence means a specific thing. The
words live here so the workspace payload and the live event say the same sentence.
"""

from __future__ import annotations

from typing import Any

from .workflows import STATUS_WAITING

# Tool names, copied from the modules that own them so this file does not import those
# packages. A card whose tool is not listed falls through to "this call does not run".
_KEEP_MEMBER = frozenset({"delete_agent"})
_KEEP_GROUP = frozenset({"dissolve_group"})
_STOP_REPLY = frozenset({"token_budget"})
_NO_COMPUTER = frozenset({"computer", "computer_shell", "computer_browser", "schedule_computer"})
_PAUSE_STEP = frozenset({"workflow_step"})
_NO_SCHEDULE = frozenset({"schedule_create", "schedule_update", "schedule_delete"})

_QUESTION = "一直等到你回答，不会自己继续。"


def _window(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole >= 60 and whole % 60 == 0:
        return f"{whole // 60} 分钟内不答"
    return f"{whole} 秒内不答"


def _if_unanswered(tool_name: str, window: str) -> str:
    if tool_name in _KEEP_MEMBER:
        return f"{window}就保留这位成员。"
    if tool_name in _KEEP_GROUP:
        return f"{window}就保留这个群组。"
    if tool_name in _STOP_REPLY:
        return f"{window}，这次回复会停下。"
    if tool_name in _NO_COMPUTER:
        return f"{window}，这次就不用沙盒电脑。"
    if tool_name in _PAUSE_STEP:
        return f"{window}，这一步会暂停，不会替你跳过。"
    if tool_name in _NO_SCHEDULE:
        return f"{window}，这次改动不会保存。"
    return f"{window}，这次就不执行。"


def decision_rows(
    *,
    actions: list[dict],
    questions: list[dict],
    confirm_timeout_seconds: float,
) -> list[dict]:
    """One row per waiting card and one per workflow question, oldest first."""
    window = _window(confirm_timeout_seconds)
    rows: list[dict] = []
    for action in actions:
        confirmation = action.get("confirmation") or {}
        question = confirmation.get("kind") == "question"
        prompt = str(confirmation.get("prompt") or action.get("summary") or "").strip()
        if question:
            first = (confirmation.get("questions") or [{}])[0]
            prompt = str(first.get("question") or prompt).strip()
        rows.append(
            {
                "id": action["id"],
                "kind": "question" if question else "action",
                "conversation_id": action["conversation_id"],
                "message_id": action.get("message_id"),
                "who": action.get("author_name") or "",
                "where": action.get("conversation_title") or "",
                "prompt": prompt,
                # A question waits for as long as it takes; silence never decides it.
                "if_unanswered": _QUESTION
                if question
                else _if_unanswered(str(action.get("tool_name") or ""), window),
                "_at": action.get("created_at") or "",
            }
        )
    for question in questions:
        rows.append(
            {
                "id": question["id"],
                "kind": "workflow",
                "conversation_id": question["conversation_id"],
                "message_id": question.get("message_id"),
                "who": question.get("who") or "",
                "where": question.get("where") or "",
                "prompt": str(question.get("prompt") or "").strip(),
                "if_unanswered": _QUESTION,
                "_at": question.get("updated_at") or "",
            }
        )
    rows.sort(key=lambda row: (str(row["_at"]), str(row["id"])))
    for row in rows:
        row.pop("_at", None)
    return rows


def collect_decisions(store: Any, *, confirm_timeout_seconds: float) -> list[dict]:
    """The waiting rows as the database has them now."""
    questions: list[dict] = []
    for walk in store.live_workflow_runs():
        if walk["status"] != STATUS_WAITING:
            continue
        waiting = next(
            (
                step
                for step in store.workflow_step_states(walk["id"])
                if step.get("status") == "waiting"
            ),
            None,
        )
        if waiting is None:
            continue
        message_id = waiting.get("message_id")
        message = store.get_message(message_id) if message_id else None
        conversation = store.get_conversation(walk["conversation_id"]) or {}
        questions.append(
            {
                "id": walk["id"],
                "conversation_id": walk["conversation_id"],
                "message_id": message_id,
                "who": (message or {}).get("author_name") or "",
                "where": conversation.get("title") or "工作流",
                "prompt": ((message or {}).get("content") or waiting.get("title") or "等你回答"),
                "updated_at": walk.get("updated_at") or "",
            }
        )
    return decision_rows(
        actions=store.awaiting_actions(),
        questions=questions,
        confirm_timeout_seconds=confirm_timeout_seconds,
    )
