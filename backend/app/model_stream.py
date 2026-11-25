"""Protocol adapters for streamed model replies.

``ModelStream`` is the only surface the engine depends on: ``chunks()`` yields assistant
text as it arrives over real HTTP/SSE and accumulates native tool-call fragments,
``validate()`` turns refusal / truncation / length / empty replies into honest errors, and
the accumulated result is read from the same object, so callers can persist exactly what
the upstream produced. Two adapters implement it — :class:`OpenAIChatStream` for
OpenAI-compatible Chat Completions and :class:`AnthropicMessagesStream` (in
``anthropic_stream``) for Anthropic Messages — and ``open_stream`` picks one from the
resolved provider protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from .errors import StreamAborted, UpstreamError
from .providers import ResolvedModel

POLL_SECONDS = 0.25
MAX_ERROR_DETAIL = 300
# A model call that is not part of the conversation (today: the memory extraction) names
# its purpose in this header, so the behaviour is visible to a gateway and a test alike.
PURPOSE_HEADER = "X-Cubes-Purpose"
# A stream that sends nothing at all (no text, no reasoning, no keep-alive) this long is dead.
SILENCE_SECONDS = 90.0
# Waits before each retry of a call the service refused with 5xx or 429.
RETRY_DELAYS = (1.0, 3.0)


class StreamSilent(UpstreamError):
    """The model service went quiet mid-reply; one retry is worth it before giving up."""

    # Set once part of the reply already reached the user: a retry would repeat it.
    shown = False


class StreamBroken(StreamSilent):
    """The connection to the model service broke mid-reply (a reset, a cut chunked body)."""


@dataclass
class ToolCallFragment:
    """One accumulated tool call, completed across streamed delta fragments."""

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.id.strip()) and bool(self.name.strip())


@dataclass
class Usage:
    """What one call really cost, as the provider reported it.

    ``input_tokens`` counts the whole prompt (cached part included, whichever protocol
    reports it), ``cached_tokens`` the part the provider served from its prefix cache.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def absorb(self, other: "Usage") -> None:
        """Merge a later report of the same call (Anthropic sends input and output apart)."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class CompletionResult:
    content: str = ""
    finish_reason: str | None = None
    saw_done: bool = False
    # Characters of reasoning the model streamed before (or between) its answer; the text
    # itself is never kept, only that the model is still working.
    reasoning_chars: int = 0
    # Tokens the provider reported; zeros when it reported none, which the engine reads as
    # "no usage" and estimates instead.
    usage: Usage = field(default_factory=Usage)


@runtime_checkable
class ModelStream(Protocol):
    """What the engine needs from one protocol adapter; nothing else may be assumed."""

    result: CompletionResult

    @property
    def tool_calls(self) -> list[ToolCallFragment]: ...

    def chunks(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> AsyncIterator[str]: ...

    def validate(self, *, allow_tool_calls: bool) -> None: ...

    async def aclose(self) -> None: ...

    # Set by the engine before the stream runs: called once, the moment the model first emits
    # a tool-call fragment. That is the exact point a reply stopped being an answer and its
    # text became narration, and it lands before the tool row exists.
    on_tool_start: Callable[[], None] | None


class _BaseStream:
    """Shared streaming skeleton: connect, poll-while-pumping, accumulate, validate.

    Subclasses build the request (:meth:`_request`) and decode upstream events
    (:meth:`_pump`); everything else — cancellation, error text, redaction and the
    verdict on an incomplete answer — stays identical across protocols.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        resolved: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        purpose: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._client = client
        self._resolved = resolved
        self._messages = messages
        self._tools = tools
        self._purpose = purpose
        self._max_tokens = max_tokens
        self._response: httpx.Response | None = None
        self._error: str | None = None
        # A broken connection, as opposed to an answer the service chose to give.
        self._broken = False
        self._fragments: dict[int, ToolCallFragment] = {}
        self.result = CompletionResult()
        self._fragment_size = 0
        self.on_tool_start: Callable[[], None] | None = None
        self.saw_tool_call = False

    # ------------------------------------------------------------------ streaming

    async def chunks(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> AsyncIterator[str]:
        """Yield assistant text chunks; raises once a requested stop is observed."""
        stopped = should_stop if should_stop is not None else (lambda: False)
        if self._client is None:
            raise UpstreamError("服务未就绪，请稍后重试。")

        response = await self._open(stopped)
        self._response = response

        try:
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            pump = asyncio.create_task(self._pump(response, queue))
            loop = asyncio.get_running_loop()
            heard = loop.time()
            seen = self.result.reasoning_chars
            try:
                while True:
                    if stopped():
                        raise StreamAborted("任务已停止。")
                    try:
                        item = await asyncio.wait_for(queue.get(), POLL_SECONDS)
                    except asyncio.TimeoutError:
                        # Reasoning counts as life even though it never reaches the queue.
                        if self.result.reasoning_chars != seen or self._fragments_grew():
                            seen = self.result.reasoning_chars
                            heard = loop.time()
                        elif loop.time() - heard >= SILENCE_SECONDS:
                            raise StreamSilent(
                                f"模型服务 {int(SILENCE_SECONDS)} 秒没有任何回应，本次回复已放弃。"
                            )
                        continue
                    heard = loop.time()
                    if item is None:
                        break
                    self.result.content += item
                    yield item
            finally:
                if not pump.done():
                    pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
        finally:
            await self.aclose()

    async def _open(self, stopped: Callable[[], bool]) -> httpx.Response:
        """Send the request; a transient refusal (5xx, 429) before any byte is retried.

        Nothing has reached the user yet, so a retry is invisible. Gateways in front of a
        model fail a few percent of calls this way; a long tool loop would hit one almost
        every time without it. Anything else (4xx, a timeout) is final.
        """
        assert self._client is not None  # guarded by chunks()
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = await self._client.send(self._request(), stream=True)
            except httpx.HTTPError as exc:
                raise UpstreamError(f"无法连接模型服务：{exc.__class__.__name__}") from exc
            if response.status_code < 400:
                return response
            body = self._clip((await response.aread()).decode("utf-8", "replace").strip())
            await response.aclose()
            transient = response.status_code >= 500 or response.status_code == 429
            if not transient or attempt == len(RETRY_DELAYS) or stopped():
                raise UpstreamError(f"模型服务返回 HTTP {response.status_code}：{body or '无响应正文'}")
            await asyncio.sleep(RETRY_DELAYS[attempt])
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises

    async def aclose(self) -> None:
        response, self._response = self._response, None
        if response is not None:
            with contextlib.suppress(Exception):
                await response.aclose()

    def _fragments_grew(self) -> bool:
        """Whether a tool call's arguments grew since the last check: a long call streams quietly."""
        size = sum(len(fragment.arguments) for fragment in self._fragments.values())
        grew = size != self._fragment_size
        self._fragment_size = size
        return grew

    # --------------------------------------------------------------------- result

    @property
    def tool_calls(self) -> list[ToolCallFragment]:
        return [self._fragments[key] for key in sorted(self._fragments)]

    def validate(self, *, allow_tool_calls: bool) -> None:
        """Turn every non-answer outcome into an explicit error; never silently succeed."""
        if self._error:
            raise (StreamBroken if self._broken else UpstreamError)(self._error)
        if self.result.finish_reason in ("content_filter", "refusal"):
            raise UpstreamError("模型服务因内容策略拒绝了本次回复。")
        if not self.result.saw_done:
            raise UpstreamError("模型服务未发送完成标记，本次回复被截断。")
        if self.result.finish_reason == "length":
            raise UpstreamError("模型返回被长度限制截断，本次回复不完整。")
        if self.tool_calls and not allow_tool_calls:
            raise UpstreamError("成员回复中出现了工具调用，本次回复不受支持，也未执行。")
        if not self.result.content.strip() and not self.tool_calls:
            raise UpstreamError("模型返回了空内容，本次回复未完成。")

    # ---------------------------------------------------------------- subclasses

    def _request(self) -> httpx.Request:
        raise NotImplementedError

    async def _pump(
        self, response: httpx.Response, queue: asyncio.Queue[str | None]
    ) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ internals

    def _fragment(self, index: int) -> ToolCallFragment:
        if index < 0:
            index = len(self._fragments)
        fragment = self._fragments.get(index)
        if fragment is not None:
            return fragment
        if not self.saw_tool_call:
            self.saw_tool_call = True
            hook = self.on_tool_start
            if hook is not None:
                hook()
        fragment = ToolCallFragment(index=index)
        self._fragments[index] = fragment
        return fragment

    def _clip(self, text: str, limit: int = MAX_ERROR_DETAIL) -> str:
        """Redact secrets *before* truncating, so a cut can never expose a key prefix."""
        redacted = self._redact(text)
        if len(redacted) <= limit:
            return redacted
        return f"{redacted[:limit]}…"

    def _redact(self, text: str) -> str:
        key = self._resolved.api_key.strip()
        if key:
            return text.replace(key, "***")
        return text


class OpenAIChatStream(_BaseStream):
    """One streamed Chat Completions reply against ``resolved.base_url``."""

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        resolved: ResolvedModel,
        **options: Any,
    ) -> None:
        # Only this protocol has a reasoning-effort field; the base class ignores the rest.
        self._reasoning_effort = options.pop("reasoning_effort", None)
        super().__init__(client, resolved, **options)

    def _request(self) -> httpx.Request:
        assert self._client is not None  # guarded by chunks()
        payload: dict[str, Any] = {
            "model": self._resolved.model,
            "messages": self._messages,
            "stream": True,
            # Without this the provider never reports what the call cost, and the budget has
            # nothing to check itself against.
            "stream_options": {"include_usage": True},
        }
        if self._reasoning_effort:
            # A cheap routing call asks for the least thinking the model names.
            payload["reasoning_effort"] = self._reasoning_effort
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if self._tools:
            payload["tools"] = self._tools
            payload["tool_choice"] = "auto"
        headers = {"Accept": "text/event-stream"}
        if self._purpose:
            headers[PURPOSE_HEADER] = self._purpose
        if self._resolved.api_key:
            headers["Authorization"] = f"Bearer {self._resolved.api_key}"
        return self._client.build_request(
            "POST",
            f"{self._resolved.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

    async def _pump(
        self, response: httpx.Response, queue: asyncio.Queue[str | None]
    ) -> None:
        try:
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    self.result.saw_done = True
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    self._error = "模型服务返回了无法解析的数据流，本次回复不完整。"
                    return
                if not isinstance(event, dict):
                    self._error = "模型服务返回了非预期的数据流，本次回复不完整。"
                    return
                if event.get("error"):
                    self._error = f"模型服务返回错误：{self._clip(_error_detail(event['error']))}"
                    return
                reported = event.get("usage")
                if isinstance(reported, dict):
                    self.result.usage.absorb(openai_usage(reported))
                for choice in event.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        delta = {}
                    refusal = delta.get("refusal")
                    if isinstance(refusal, str) and refusal.strip():
                        self._error = f"模型服务拒绝了本次回复：{self._clip(refusal)}"
                        return
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        queue.put_nowait(content)
                    for field in ("reasoning_content", "reasoning"):
                        thought = delta.get(field)
                        if isinstance(thought, str) and thought:
                            self.result.reasoning_chars += len(thought)
                    fragments = delta.get("tool_calls")
                    if isinstance(fragments, list):
                        self._accumulate(fragments)
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str) and finish_reason:
                        self.result.finish_reason = finish_reason
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

    def _accumulate(self, fragments: list[Any]) -> None:
        for position, raw in enumerate(fragments):
            if not isinstance(raw, dict):
                continue
            index = raw.get("index")
            if not isinstance(index, int) or index < 0:
                index = position
            fragment = self._fragment(index)
            identifier = raw.get("id")
            if isinstance(identifier, str) and identifier.strip():
                fragment.id = identifier.strip()
            function = raw.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name.strip():
                piece = name.strip()
                if not fragment.name:
                    fragment.name = piece
                elif not fragment.name.endswith(piece):
                    fragment.name += piece
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                fragment.arguments += arguments
            elif isinstance(arguments, dict):
                # Some gateways send an already-parsed object instead of a JSON string.
                fragment.arguments = json.dumps(arguments, ensure_ascii=False)


def open_stream(
    client: httpx.AsyncClient | None,
    resolved: ResolvedModel,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    purpose: str | None = None,
    max_tokens: int | None = None,
    session: Any = None,
    prompt_messages: list[dict[str, Any]] | None = None,
    cli_tools: Any = None,
    reasoning_effort: str | None = None,
) -> ModelStream:
    """One stream for ``resolved.protocol``; the engine never knows which adapter ran.

    ``session`` and ``prompt_messages`` only reach a CLI engine: the id of the conversation
    it continues, and just the new part of the history for a resumed one. ``cli_tools`` is
    that turn's bridge to Cubes' own tools (``app.cli_tools``), handed to the CLI as an MCP
    server; it stays out of the HTTP adapters, which get tools in the request itself.
    """
    if resolved.protocol in ("claude-code", "codex"):
        # Imported here, like the Anthropic adapter: the CLI module depends on this one.
        from .cli_engines import CLIStream

        return CLIStream(
            resolved.protocol,
            resolved,
            messages=messages,
            session=session,
            prompt_messages=prompt_messages,
            cli_tools=cli_tools,
        )
    if resolved.protocol == "anthropic-messages":
        # Imported here: ``anthropic_stream`` depends on this module's shared base class.
        from .anthropic_stream import AnthropicMessagesStream

        return AnthropicMessagesStream(
            client,
            resolved,
            messages=messages,
            tools=tools,
            purpose=purpose,
            max_tokens=max_tokens,
        )
    return OpenAIChatStream(
        client,
        resolved,
        messages=messages,
        tools=tools,
        purpose=purpose,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )


def openai_usage(reported: dict[str, Any]) -> Usage:
    """One OpenAI-compatible ``usage`` object, cached tokens included when reported.

    Gateways disagree on the field names: ``prompt_tokens_details.cached_tokens`` is OpenAI's,
    ``prompt_cache_hit_tokens`` is DeepSeek's, and both mean the same thing.
    """
    details = reported.get("prompt_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return Usage(
        input_tokens=_as_int(reported.get("prompt_tokens")),
        output_tokens=_as_int(reported.get("completion_tokens")),
        cached_tokens=cached or _as_int(reported.get("prompt_cache_hit_tokens")),
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _error_detail(detail: Any) -> str:
    """Readable message from an upstream ``error`` payload; clipping/redaction is separate."""
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return json.dumps(detail, ensure_ascii=False)
    return str(detail)
