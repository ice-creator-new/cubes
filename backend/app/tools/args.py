"""Argument plumbing shared by every tool family: identity, parsing, validation.

A tool call's arguments arrive as a JSON string the model wrote. Two things happen here:
the string is canonicalised into the identity a rerun is compared against, and it is parsed
strictly so a malformed or over-reaching call becomes a real tool result the model can fix.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from ..errors import ToolError
from ..models import MAX_NAME_CHARS, trim_field
from ..toolkit import ToolInputError


def canonical_arguments(raw: str | None) -> str:
    """Stable identity string for one tool call's arguments.

    Identical argument objects stay identical regardless of key order, which is what the
    per-run ``tool_call_id`` identity check compares.
    """
    text = (raw or "").strip() or "{}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"raw:{text}"
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def parse_json_object(raw: str | None, *, label: str) -> dict[str, Any]:
    """Parse one tool call's arguments; a bad call is a message for the model, not a crash."""
    text = (raw or "").strip() or "{}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolInputError(
            f"{label}的参数不是合法 JSON（{exc.msg}），本次调用未执行。"
        ) from exc
    if not isinstance(payload, dict):
        raise ToolInputError(f"{label}的参数必须是 JSON 对象，本次调用未执行。")
    return payload


def parse_arguments(raw: str | None, model: type[BaseModel]) -> dict[str, Any]:
    """Parse and strictly validate one tool call's arguments against its model."""
    text = (raw or "").strip()
    if not text:
        raise ToolError("工具调用缺少参数 JSON，本次调用未执行。")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"工具参数不是合法 JSON（{exc.msg}），本次调用未执行。") from exc
    if not isinstance(payload, dict):
        raise ToolError("工具参数必须是 JSON 对象，本次调用未执行。")
    try:
        validated = model.model_validate(payload)
    except ValidationError as exc:
        raise ToolError(f"工具参数无效：{_first_error(exc)}") from exc
    return validated.model_dump(mode="json")


def model_parser(model: type[BaseModel]):
    """A ``parse`` callable for one tool, bound to the pydantic model it validates with."""

    def parse(raw: str | None) -> dict[str, Any]:
        return parse_arguments(raw, model)

    return parse


def json_parser(label: str):
    """A ``parse`` callable for a tool whose arguments are only a loose JSON object."""

    def parse(raw: str | None) -> dict[str, Any]:
        return parse_json_object(raw, label=label)

    return parse


def clean_ids(values: list[str]) -> list[str]:
    """Member ids as the model wrote them, trimmed and de-duplicated in order."""
    cleaned: list[str] = []
    for item in values:
        text = trim_field(item, field="成员 ID", limit=MAX_NAME_CHARS)
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "参数不符合工具要求。"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "参数"
    message = str(first.get("msg", "不符合要求"))
    prefix = "Value error, "
    if message.startswith(prefix):
        message = message[len(prefix) :]
    return f"{location}：{message}"
