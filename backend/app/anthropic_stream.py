"""Anthropic Messages streaming for the same engine-facing ``ModelStream`` surface.

The engine keeps speaking the OpenAI chat shape (system / user / assistant-with-tool_calls
/ tool), so this adapter owns the whole translation: one top-level ``system`` string, an
alternating ``user``/``assistant`` message list with ``tool_use`` and ``tool_result``
blocks, the ``name``/``input_schema`` tool schema, and Anthropic's SSE events. Streaming
text, accumulated tool-call arguments and the final verdict stay identical to the
OpenAI-compatible adapter.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .model_stream import PURPOSE_HEADER, Usage, _BaseStream, _error_detail
from .providers import ANTHROPIC_VERSION, ResolvedModel

# Anthropic's stop reasons mapped onto the verdict the shared validator already knows.
STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "refusal",
}

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _text_of(content: Any) -> str:
    """Flatten an OpenAI message's content (a string or a list of parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
        return "".join(pieces)
    return ""


def _user_blocks(content: Any) -> list[dict[str, Any]]:
    """A user turn's text, images and PDFs as Anthropic blocks, in their original order.

    Images arrive as OpenAI ``image_url`` parts and PDFs as ``file`` parts, both holding a
    ``data:`` URL; Anthropic wants the bytes as a base64 ``source`` under its own block
    type. Anything else that is not text is dropped.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            header, separator, data = url.partition(",")
            if separator and header.startswith("data:") and header.endswith(";base64"):
                media_type = header[len("data:") : -len(";base64")]
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
                )
            continue
        if part.get("type") == "file":
            file = part.get("file") if isinstance(part.get("file"), dict) else {}
            url = str(file.get("file_data") or "")
            header, separator, data = url.partition(",")
            if separator and header.startswith("data:") and header.endswith(";base64"):
                media_type = header[len("data:") : -len(";base64")]
                if media_type == "application/pdf":
                    blocks.append(
                        {
                            "type": "document",
                            "source": {"type": "base64", "media_type": media_type, "data": data},
                        }
                    )
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
    return blocks


def _tool_input(arguments: Any) -> dict[str, Any]:
    """Parse an OpenAI ``arguments`` JSON string into a tool_use ``input`` object.

    A malformed call already went back to the model as a tool error, so its history entry
    carries ``{}`` and the following ``tool_result`` explains why.
    """
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = _text_of(message.get("content"))
    if text.strip():
        blocks.append({"type": "text", "text": text})
    calls = message.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
        identifier = str(call.get("id") or "")
        name = str(function.get("name") or "")
        if not identifier or not name:
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": identifier,
                "name": name,
                "input": _tool_input(function.get("arguments")),
            }
        )
    return blocks


def to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Translate the engine's OpenAI-shaped messages into ``(system, messages)``.

    System messages merge into one cached text block, consecutive ``tool`` messages become
    one user turn of ``tool_result`` blocks, and any adjacent same-role pair merges so
    user and assistant always alternate.

    Two cache breakpoints are marked: the end of the system prompt and the message just
    before the newest turn. Everything up to the second breakpoint is byte-identical between
    two turns (the engine keeps the clock and live hints in the newest user turn), so the
    whole conversation prefix is served from the prompt cache.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            text = _text_of(message.get("content"))
            if text.strip():
                system_parts.append(text)
            continue
        if role == "tool":
            identifier = str(message.get("tool_call_id") or "")
            if not identifier:
                continue
            append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": identifier,
                        "content": _text_of(message.get("content")),
                    }
                ],
            )
            continue
        if role == "assistant":
            append("assistant", _assistant_blocks(message))
            continue
        if role == "user":
            append("user", _user_blocks(message.get("content")))
    # Anthropic requires the conversation to open with a user turn; a context limit can
    # truncate the leading question away, and the request would otherwise be refused.
    while converted and converted[0]["role"] == "assistant":
        converted.pop(0)
    system = None
    if system_parts:
        system = [
            {
                "type": "text",
                "text": "\n\n".join(system_parts),
                "cache_control": {"type": "ephemeral"},
            }
        ]
    if len(converted) >= 2:
        previous = converted[-2]
        if previous["content"]:
            previous["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return system, converted


def to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI ``{"type":"function","function":…}`` schemas as Anthropic tool definitions."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        entry: dict[str, Any] = {
            "name": name,
            "input_schema": schema if isinstance(schema, dict) else EMPTY_SCHEMA,
        }
        description = function.get("description")
        if isinstance(description, str) and description.strip():
            entry["description"] = description.strip()
        converted.append(entry)
    return converted


def anthropic_usage(usage: dict[str, Any]) -> Usage:
    """One Anthropic ``usage`` object; the prompt count includes what the cache served.

    Anthropic reports input tokens *without* the cached part, so the three fields are added
    back together: a run's ``input_tokens`` is what the call really read.
    """
    cached = _count(usage.get("cache_read_input_tokens"))
    return Usage(
        input_tokens=(
            _count(usage.get("input_tokens"))
            + cached
            + _count(usage.get("cache_creation_input_tokens"))
        ),
        output_tokens=_count(usage.get("output_tokens")),
        cached_tokens=cached,
    )


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class AnthropicMessagesStream(_BaseStream):
    """One streamed Messages reply against ``{base_url}/v1/messages``."""

    def _request(self) -> httpx.Request:
        assert self._client is not None  # guarded by chunks()
        system, messages = to_anthropic_messages(self._messages)
        payload: dict[str, Any] = {
            "model": self._resolved.model,
            "max_tokens": self._max_tokens or self._resolved.max_output,
            "messages": messages,
            "stream": True,
        }
        if system is not None:
            payload["system"] = system
        tools = to_anthropic_tools(self._tools)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = {"type": "auto"}
        headers = {
            "Accept": "text/event-stream",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self._purpose:
            headers[PURPOSE_HEADER] = self._purpose
        if self._resolved.api_key:
            headers["x-api-key"] = self._resolved.api_key
        return self._client.build_request(
            "POST",
            f"{self._resolved.base_url}/v1/messages",
            json=payload,
            headers=headers,
        )

    async def _pump(
        self, response: httpx.Response, queue: asyncio.Queue[str | None]
    ) -> None:
        try:
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    self._error = "模型服务返回了无法解析的数据流，本次回复不完整。"
                    return
                if not isinstance(event, dict):
                    self._error = "模型服务返回了非预期的数据流，本次回复不完整。"
                    return
                if self._handle(event, queue):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # transport faults must surface as an honest error
            self._broken = isinstance(exc, httpx.TransportError)
            self._error = (
                f"和模型服务的连接中途断开了（{exc.__class__.__name__}），本次回复不完整。"
                if self._broken
                else f"读取模型服务响应失败：{exc.__class__.__name__}：{self._clip(str(exc))}"
            )
        finally:
            queue.put_nowait(None)

    def _handle(self, event: dict[str, Any], queue: asyncio.Queue[str | None]) -> bool:
        """Apply one SSE event; ``True`` means the stream must stop (error)."""
        kind = event.get("type")
        if kind == "error":
            detail = _error_detail(event.get("error") or event)
            self._error = f"模型服务返回错误：{self._clip(detail)}"
            return True
        if kind == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                fragment = self._fragment(self._block_index(event))
                identifier = str(block.get("id") or "")
                name = str(block.get("name") or "")
                if identifier.strip():
                    fragment.id = identifier.strip()
                if name.strip():
                    fragment.name = name.strip()
                initial = block.get("input")
                if isinstance(initial, dict) and initial:
                    fragment.arguments = json.dumps(initial, ensure_ascii=False)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    queue.put_nowait(text)
            return False
        if kind == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return False
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    queue.put_nowait(text)
            elif delta_type == "thinking_delta":
                thought = delta.get("thinking")
                if isinstance(thought, str) and thought:
                    self.result.reasoning_chars += len(thought)
            elif delta_type == "input_json_delta":
                piece = delta.get("partial_json")
                if isinstance(piece, str) and piece:
                    self._fragment(self._block_index(event)).arguments += piece
            return False
        if kind == "message_start":
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self.result.usage.absorb(anthropic_usage(usage))
            return False
        if kind == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    self.result.finish_reason = STOP_REASONS.get(stop_reason, stop_reason)
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.result.usage.absorb(anthropic_usage(usage))
            return False
        if kind == "message_stop":
            self.result.saw_done = True
        return False

    @staticmethod
    def _block_index(event: dict[str, Any]) -> int:
        index = event.get("index")
        return index if isinstance(index, int) and index >= 0 else 0
