"""Isolated OpenAI-compatible upstream used by the backend tests.

Runs for real over HTTP so the engine's streaming, tool-call accumulation, refusal,
truncation and cancellation handling is exercised end to end. Each request is answered from
``script`` (a list of dicts, or callables of ``(requests, index)`` for replies that depend on
the ids the tools really returned); without a script entry, ``mode`` decides the answer.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse


class FakeUpstream:
    def __init__(self) -> None:
        self.mode = "ok"
        # The first this many chat requests answer HTTP 500, like a flaky gateway.
        self.fail_first = 0
        self.script: list[Any] | None = None
        self.chunk_delay = 0.0
        self.chunk_count = 3
        self.text = "回复正文"
        # Anthropic Messages has its own script and records, so one fixture serves both
        # protocols at once (a group where two bots use two providers).
        self.anthropic_mode = "ok"
        self.anthropic_script: list[Any] | None = None
        # The model listing both protocols read (``GET /v1/models``).
        self.model_names = ["test-model", "second-model"]
        self.model_list_mode = "ok"
        # ``POST /providers/test`` sends a non-streamed request; this decides its answer.
        self.probe_supports_tools = True
        # Memory extraction is a housekeeping call that asks for no tools at all. It is
        # recorded apart from ``requests`` so a run's own calls stay indexable and
        # countable, and it is answered from its own script; ``memory_text`` is what a
        # default answer says, i.e. "nothing worth remembering".
        self.memory_script: list[Any] | None = None
        self.memory_text = '[{"op": "noop"}]'
        self.memory_mode = "ok"
        # A bot's reaction response is a small no-tool call; recorded apart and answered
        # from its own script. The default answer is "do nothing".
        self.reaction_script: list[Any] | None = None
        self.reaction_text = '{"action": "none"}'
        self._reaction_requests: list[dict] = []
        # A group's invisible dispatcher is one small no-tool call. It is recorded apart from
        # ``requests`` too, so a run's own replies stay indexable, and answered from its own
        # script. The default answer routes exactly as the old deterministic queue did: the
        # addressed member, else the last speaker, else the group's first member.
        self.dispatch_script: list[Any] | None = None
        self.dispatch_text: str | None = None
        # ``garbage`` makes it answer unreadable prose, i.e. a decision the engine falls
        # back on; ``error`` takes the provider down for that one call.
        self.dispatch_mode = "ok"
        self._dispatch_requests: list[dict] = []
        # An activity's one naming call (title, and the summary when the turn ended) is a
        # tiny no-tool call of its own, recorded apart and answered from its own script so a
        # run's own replies stay indexable. The default is two usable lines.
        self.activity_script: list[Any] | None = None
        self.activity_text = "核对数据\n把发布页的数据核对了一遍。"
        self.activity_mode = "ok"
        self._activity_requests: list[dict] = []
        # Token usage every streamed reply reports when the request asked for it; ``None``
        # keeps the upstream silent, like a gateway that never sends a usage chunk.
        self.usage: dict | None = None
        self._lock = threading.Lock()
        self._requests: list[dict] = []
        self._anthropic_requests: list[dict] = []
        self._memory_requests: list[dict] = []
        self._model_requests: list[dict] = []
        self.app = self._build()

    # ------------------------------------------------------------------ state

    @property
    def requests(self) -> list[dict]:
        with self._lock:
            return list(self._requests)

    @property
    def anthropic_requests(self) -> list[dict]:
        with self._lock:
            return list(self._anthropic_requests)

    @property
    def model_requests(self) -> list[dict]:
        with self._lock:
            return list(self._model_requests)

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)

    @property
    def anthropic_request_count(self) -> int:
        with self._lock:
            return len(self._anthropic_requests)

    @property
    def memory_requests(self) -> list[dict]:
        """The tool-less housekeeping calls, e.g. a memory extraction."""
        with self._lock:
            return list(self._memory_requests)

    @property
    def memory_request_count(self) -> int:
        with self._lock:
            return len(self._memory_requests)

    @property
    def reaction_requests(self) -> list[dict]:
        with self._lock:
            return list(self._reaction_requests)

    @property
    def reaction_request_count(self) -> int:
        with self._lock:
            return len(self._reaction_requests)

    @property
    def activity_requests(self) -> list[dict]:
        """The activity naming calls, in order."""
        with self._lock:
            return list(self._activity_requests)

    @property
    def activity_request_count(self) -> int:
        with self._lock:
            return len(self._activity_requests)

    @property
    def dispatch_requests(self) -> list[dict]:
        """The group dispatcher calls, in order."""
        with self._lock:
            return list(self._dispatch_requests)

    @property
    def dispatch_request_count(self) -> int:
        with self._lock:
            return len(self._dispatch_requests)

    def request(self, index: int) -> dict:
        return self.requests[index]

    def anthropic_request(self, index: int) -> dict:
        return self.anthropic_requests[index]

    def memory_request(self, index: int) -> dict:
        return self.memory_requests[index]

    # ------------------------------------------------------------------ app

    def _build(self) -> FastAPI:
        app = FastAPI(title="fake-upstream")

        @app.post("/v1/chat/completions")
        async def completions(request: Request) -> Any:
            body = await request.json()
            record = {
                "model": body.get("model"),
                "stream": body.get("stream"),
                "messages": body.get("messages"),
                "tools": body.get("tools"),
                "tool_choice": body.get("tool_choice"),
                "stream_options": body.get("stream_options"),
                "max_tokens": body.get("max_tokens"),
                "reasoning_effort": body.get("reasoning_effort"),
                "authorization": request.headers.get("authorization"),
            }
            if self._housekeeping(request):
                return self._memory_answer(record, self._frames)
            if self._reaction(request):
                return self._reaction_answer(record, self._frames)
            if self._dispatch(request):
                return self._dispatch_answer(record, self._frames)
            if self._activity_title(request):
                return self._activity_answer(record, self._frames)
            with self._lock:
                self._requests.append(record)
                index = len(self._requests)
            mode = self.mode
            if mode == "error" or index <= self.fail_first:
                return JSONResponse(
                    status_code=500, content={"error": {"message": "upstream exploded"}}
                )
            if mode == "unavailable":
                # A provider that is down: 503, the case the backup model exists for.
                return JSONResponse(
                    status_code=503, content={"error": {"message": "service unavailable"}}
                )
            if mode == "bad_request":
                return JSONResponse(
                    status_code=400, content={"error": {"message": "invalid request"}}
                )
            if mode == "unauthorized":
                # A bad key, the request's own fault: a fallback model must not hide it.
                return JSONResponse(
                    status_code=401, content={"error": {"message": "invalid api key"}}
                )
            if mode == "quota":
                # Some providers report a spent balance as a 403 with a billing sentence.
                return JSONResponse(
                    status_code=403,
                    content={"error": {"message": "insufficient balance, please top up"}},
                )
            if mode == "error_echoing_key":
                # Simulates a gateway that echoes the request headers back in its error page.
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": {
                            "message": (
                                "proxy rejected request with "
                                f"{request.headers.get('authorization')}"
                            )
                        }
                    },
                )
            if mode == "error_plain_text_key":
                # The key starts at offset 295 of the body, so a 300-char cut would expose part
                # of it unless redaction happens before truncation.
                key = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
                return PlainTextResponse("z" * 295 + key, status_code=502)
            if not body.get("stream"):
                return JSONResponse(content=self._openai_probe_body())
            spec = self._spec(index, mode)

            async def event_stream():
                async for frame in self._frames(spec):
                    yield frame

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        @app.post("/v1/messages")
        async def anthropic_messages(request: Request) -> Any:
            incoming = {key.lower(): value for key, value in request.headers.items()}
            body = await request.json()
            record = {
                "path": "/v1/messages",
                "protocol": "anthropic-messages",
                "model": body.get("model"),
                "stream": body.get("stream"),
                "max_tokens": body.get("max_tokens"),
                "system": body.get("system"),
                "messages": body.get("messages"),
                "tools": body.get("tools"),
                "tool_choice": body.get("tool_choice"),
                "x_api_key": incoming.get("x-api-key"),
                "anthropic_version": incoming.get("anthropic-version"),
            }
            if self._housekeeping(request):
                return self._memory_answer(record, self._anthropic_frames)
            if self._reaction(request):
                return self._reaction_answer(record, self._anthropic_frames)
            if self._dispatch(request):
                return self._dispatch_answer(record, self._anthropic_frames)
            if self._activity_title(request):
                return self._activity_answer(record, self._anthropic_frames)
            with self._lock:
                self._anthropic_requests.append(record)
                index = len(self._anthropic_requests)
            mode = self.anthropic_mode
            if mode == "error":
                return JSONResponse(
                    status_code=500,
                    content={"type": "error", "error": {"type": "api_error", "message": "upstream exploded"}},
                )
            if mode == "error_echoing_key":
                return JSONResponse(
                    status_code=401,
                    content={
                        "type": "error",
                        "error": {
                            "type": "authentication_error",
                            "message": f"invalid key {incoming['x-api-key']}",
                        },
                    },
                )
            if mode == "overloaded":
                # Anthropic reports mid-stream trouble as a normal SSE ``error`` event.
                async def error_stream():
                    yield _event(
                        {
                            "type": "message_start",
                            "message": {
                                "id": "msg_error",
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "stop_reason": None,
                            },
                        }
                    )
                    yield _event(
                        {
                            "type": "error",
                            "error": {"type": "overloaded_error", "message": "Overloaded"},
                        }
                    )

                return StreamingResponse(error_stream(), media_type="text/event-stream")
            if not body.get("stream"):
                return JSONResponse(content=self._anthropic_probe_body())
            spec = self._anthropic_spec(index, mode)

            async def anthropic_stream():
                async for frame in self._anthropic_frames(spec):
                    yield frame

            return StreamingResponse(
                anthropic_stream(), media_type="text/event-stream"
            )

        @app.get("/v1/models")
        async def models(request: Request) -> Any:
            incoming = {key.lower(): value for key, value in request.headers.items()}
            with self._lock:
                self._model_requests.append(
                    {
                        "authorization": incoming.get("authorization"),
                        "x_api_key": incoming.get("x-api-key"),
                        "anthropic_version": incoming.get("anthropic-version"),
                    }
                )
            if self.model_list_mode == "error_echoing_key":
                key = incoming.get("x-api-key") or (
                    incoming.get("authorization") or ""
                ).removeprefix("Bearer ")
                return JSONResponse(
                    status_code=401,
                    content={"error": {"message": f"invalid key {key.strip()}"}},
                )
            if self.model_list_mode == "not_json":
                return PlainTextResponse("<html>not json</html>", status_code=200)
            return {"data": [{"id": name} for name in self.model_names]}

        return app

    @staticmethod
    def _housekeeping(request: Request) -> bool:
        """A background model call, not one of a run's replies."""
        return (request.headers.get("x-cubes-purpose") or "").strip() == "memory-extract"

    @staticmethod
    def _reaction(request: Request) -> bool:
        """A bot's reaction-response call: answered from ``reaction_script``, kept apart."""
        return (request.headers.get("x-cubes-purpose") or "").strip() == "reaction"

    def _reaction_answer(self, record: dict, frames) -> Any:
        """Serve one reaction-response call from ``reaction_script`` (or ``reaction_text``)."""
        with self._lock:
            self._reaction_requests.append(record)
            index = len(self._reaction_requests)
        spec = self._reaction_spec(index)

        async def stream():
            async for frame in frames(spec):
                yield frame

        return StreamingResponse(stream(), media_type="text/event-stream")

    def _reaction_spec(self, index: int) -> dict:
        script = self.reaction_script
        if script:
            entry = script[min(index - 1, len(script) - 1)]
            if callable(entry):
                entry = entry(self.reaction_requests, index)
            if isinstance(entry, dict):
                return entry
        return {"content": self.reaction_text}

    @staticmethod
    def _dispatch(request: Request) -> bool:
        """A group's dispatcher call: the purpose header marks it, as the old planner's did."""
        return (request.headers.get("x-cubes-purpose") or "").strip() == "dispatch"

    def _dispatch_answer(self, record: dict, frames) -> Any:
        """Serve one dispatcher call from ``dispatch_script`` (or the default route)."""
        with self._lock:
            self._dispatch_requests.append(record)
            index = len(self._dispatch_requests)
        if self.dispatch_mode == "error":
            return JSONResponse(
                status_code=500, content={"error": {"message": "dispatch exploded"}}
            )
        spec = self._dispatch_spec(record, index)

        async def stream():
            async for frame in frames(spec):
                yield frame

        return StreamingResponse(stream(), media_type="text/event-stream")

    def _dispatch_spec(self, record: dict, index: int) -> dict:
        script = self.dispatch_script
        if script:
            entry = script[min(index - 1, len(script) - 1)]
            if callable(entry):
                entry = entry(self.dispatch_requests, index)
            if isinstance(entry, dict):
                return entry
        if self.dispatch_mode == "garbage":
            return {"content": "我觉得应该让最有空的人来回答。"}
        text = self.dispatch_text
        if text is None:
            text = self._default_dispatch_text(record)
        # A dispatcher answer is a tiny JSON object; pacing set for visible replies must not
        # turn it into a timeout, or every group test would pay the fallback.
        return {"content": text, "chunk_count": 1, "chunk_delay": 0}

    @staticmethod
    def dispatch_context(record: dict) -> dict:
        """The ``<dispatch_context>`` JSON the engine put in the dispatcher's user turn."""
        for message in reversed(record.get("messages") or []):
            content = message.get("content")
            if not isinstance(content, str):
                continue
            start = content.find("<dispatch_context>")
            end = content.find("</dispatch_context>")
            if 0 <= start < end:
                try:
                    return json.loads(content[start + len("<dispatch_context>") : end])
                except ValueError:
                    return {}
        return {}

    def _default_dispatch_text(self, record: dict) -> str:
        """Route like the old queue: the addressed member, else the last speaker, else first.

        A test that never scripts the dispatcher therefore keeps the routing it was written
        against; only tests that opt in see a different decision.
        """
        context = self.dispatch_context(record)
        members = context.get("members") or []
        known = {str(member.get("id")) for member in members}
        speakers: list[str] = []
        for addressed in context.get("addressed") or []:
            agent_id = str(addressed.get("id") or "")
            if agent_id in known and agent_id not in speakers:
                speakers.append(agent_id)
        if not speakers:
            last = context.get("last_speaker")
            match = next((m for m in members if m.get("name") == last), None)
            if match is not None:
                speakers.append(str(match.get("id")))
        if not speakers and members:
            speakers.append(str(members[0].get("id")))
        return json.dumps(
            {"kind": "answer", "speakers": speakers, "reason": "默认分发给点名或最近发言的成员。"},
            ensure_ascii=False,
        )

    @staticmethod
    def _activity_title(request: Request) -> bool:
        """An activity's naming call: the purpose header marks it, like the dispatcher's."""
        return (request.headers.get("x-cubes-purpose") or "").strip() == "activity-title"

    def _activity_answer(self, record: dict, frames) -> Any:
        """Serve one naming call from ``activity_script`` (or ``activity_text``)."""
        with self._lock:
            self._activity_requests.append(record)
            index = len(self._activity_requests)
        if self.activity_mode == "error":
            return JSONResponse(
                status_code=500, content={"error": {"message": "naming exploded"}}
            )
        spec = self._activity_spec(index)

        async def stream():
            async for frame in frames(spec):
                yield frame

        return StreamingResponse(stream(), media_type="text/event-stream")

    def _activity_spec(self, index: int) -> dict:
        script = self.activity_script
        if script:
            entry = script[min(index - 1, len(script) - 1)]
            if callable(entry):
                entry = entry(self.activity_requests, index)
            if isinstance(entry, dict):
                return entry
        # A naming answer is two short lines; pacing set for visible replies must not make
        # every turn wait on it.
        return {"content": self.activity_text, "chunk_count": 1, "chunk_delay": 0}

    def _memory_answer(self, record: dict, frames) -> Any:
        """Serve one housekeeping call from ``memory_script`` (or ``memory_text``)."""
        with self._lock:
            self._memory_requests.append(record)
            index = len(self._memory_requests)
        if self.memory_mode == "error":
            return JSONResponse(
                status_code=500, content={"error": {"message": "upstream exploded"}}
            )
        spec = self._memory_spec(index)

        async def stream():
            async for frame in frames(spec):
                yield frame

        return StreamingResponse(stream(), media_type="text/event-stream")

    def _memory_spec(self, index: int) -> dict:
        script = self.memory_script
        if script:
            entry = script[min(index - 1, len(script) - 1)]
            if callable(entry):
                entry = entry(self.memory_requests, index)
            if isinstance(entry, dict):
                return entry
        return {"content": self.memory_text}

    def _spec(self, index: int, mode: str) -> dict:
        script = self.script
        if script:
            entry = script[min(index - 1, len(script) - 1)]
            if callable(entry):
                entry = entry(self.requests, index)
            if isinstance(entry, dict):
                return entry
        return self._mode_spec(mode)

    def _mode_spec(self, mode: str) -> dict:
        if mode == "empty":
            return {"empty": True}
        if mode == "truncated":
            return {"content": self.text, "done": False}
        if mode == "length":
            return {"content": self.text, "finish_reason": "length"}
        if mode == "content_filter":
            return {"content": self.text, "finish_reason": "content_filter"}
        if mode == "refusal":
            return {"error_text": "content policy refusal"}
        if mode == "refusal_partial":
            return {"content": self.text[:3], "refusal": "I cannot help with that"}
        if mode == "invalid_json":
            return {"content": self.text[:3], "invalid_json": True}
        if mode == "tool_call_missing_id":
            return {
                "tool_calls": [
                    {
                        "id": "call_missing",
                        "name": "create_agent",
                        "arguments": {"name": "甲", "instructions": "x"},
                    }
                ],
                "omit_id": True,
            }
        if mode == "tool_call_missing_name":
            return {
                "tool_calls": [
                    {
                        "id": "call_missing",
                        "name": "create_agent",
                        "arguments": {"name": "甲", "instructions": "x"},
                    }
                ],
                "omit_name": True,
            }
        if mode == "slow":
            return {"content": None, "slow": True}
        return {"content": self.text}

    # -------------------------------------------------------------- anthropic mode

    def _openai_probe_body(self) -> dict:
        if not self.probe_supports_tools:
            return {
                "id": "chatcmpl_probe",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
            }
        return {
            "id": "chatcmpl_probe",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_ping",
                                "type": "function",
                                "function": {"name": "ping", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }

    def _anthropic_probe_body(self) -> dict:
        if not self.probe_supports_tools:
            return {
                "id": "msg_probe",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "pong"}],
                "stop_reason": "end_turn",
            }
        return {
            "id": "msg_probe",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_ping", "name": "ping", "input": {}}
            ],
            "stop_reason": "tool_use",
        }

    def _anthropic_spec(self, index: int, mode: str) -> dict:
        script = self.anthropic_script
        if script:
            entry = script[min(index - 1, len(script) - 1)]
            if callable(entry):
                entry = entry(self.anthropic_requests, index)
            if isinstance(entry, dict):
                return entry
        if mode == "empty":
            return {"content": ""}
        if mode == "truncated":
            return {"content": self.text, "done": False}
        if mode == "max_tokens":
            return {"content": self.text, "stop_reason": "max_tokens"}
        if mode == "refusal":
            return {"content": self.text, "stop_reason": "refusal"}
        if mode == "invalid_json":
            return {"invalid_json": True}
        if mode == "slow":
            return {"content": None, "slow": True}
        return {"content": self.text}

    async def _anthropic_frames(self, spec: dict):
        # A spec may override the fixture's pacing: dispatcher calls must stay fast even in a
        # test that slows every visible reply down.
        count = spec.get("chunk_count", self.chunk_count)
        delay = spec.get("chunk_delay", self.chunk_delay)
        yield _event(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fake",
                    "type": "message",
                    "role": "assistant",
                    "model": "fake",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            }
        )
        yield _event({"type": "ping"})
        if spec.get("invalid_json"):
            yield "event: content_block_delta\ndata: {not valid json\n\n"
            return
        index = 0
        content = spec.get("content")
        if isinstance(content, str) and content:
            yield _event(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            for piece in _pieces(content, count):
                if delay:
                    await asyncio.sleep(delay)
                yield _event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": piece},
                    }
                )
            yield _event({"type": "content_block_stop", "index": index})
            index += 1
        for call in spec.get("tool_calls") or []:
            arguments = call.get("arguments")
            text = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
            )
            yield _event(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": call.get("name"),
                        "input": {},
                    },
                }
            )
            for part in _pieces(text, 2) or [""]:
                yield _event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": part},
                    }
                )
            yield _event({"type": "content_block_stop", "index": index})
            index += 1
        stop_reason = spec.get("stop_reason") or (
            "tool_use" if spec.get("tool_calls") else "end_turn"
        )
        yield _event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            }
        )
        if spec.get("done", True):
            yield _event({"type": "message_stop"})

    async def _frames(self, spec: dict):
        count = spec.get("chunk_count", self.chunk_count)
        delay = spec.get("chunk_delay", self.chunk_delay)
        if spec.get("usage_chunk"):
            # Some providers emit a trailing usage chunk with an empty choices array.
            yield _data({"choices": [], "usage": {"total_tokens": 1}})
        if spec.get("error_text"):
            yield _data({"error": {"message": spec["error_text"]}})
            return
        if spec.get("empty"):
            yield "data: [DONE]\n\n"
            return
        if spec.get("invalid_json"):
            yield "data: {not valid json\n\n"
            return
        if spec.get("silent"):
            # Headers sent, then nothing: a gateway that hangs mid-reply.
            await asyncio.sleep(spec["silent"])
        if spec.get("cut"):
            # A gateway that drops the connection before the body ends: the server aborts the
            # chunked response, so the client sees an incomplete chunked read.
            yield _data({"choices": [{"index": 0, "delta": {"reasoning_content": "嗯"}}]})
            raise ConnectionResetError("upstream dropped the connection")
        if spec.get("cut_after_content"):
            for piece in _pieces(str(spec.get("content") or ""), self.chunk_count):
                yield _data({"choices": [{"index": 0, "delta": {"content": piece}}]})
            raise ConnectionResetError("upstream dropped the connection")
        for _ in range(spec.get("reasoning") or 0):
            # Thinking streams before any visible text, as reasoning models do.
            await asyncio.sleep(self.chunk_delay)
            yield _data({"choices": [{"index": 0, "delta": {"reasoning_content": "嗯"}}]})
        content = spec.get("content")
        if isinstance(content, str) and content:
            for piece in _pieces(content, count):
                if delay:
                    await asyncio.sleep(delay)
                yield _data({"choices": [{"index": 0, "delta": {"content": piece}}]})
        elif spec.get("slow"):
            for number in range(1, count + 1):
                if delay:
                    await asyncio.sleep(delay)
                yield _data({"choices": [{"index": 0, "delta": {"content": f"[{number}]"}}]})
        refusal = spec.get("refusal")
        if isinstance(refusal, str) and refusal:
            yield _data({"choices": [{"index": 0, "delta": {"refusal": refusal}}]})
            return
        for position, call in enumerate(spec.get("tool_calls") or []):
            arguments = call.get("arguments")
            if spec.get("object_arguments") and isinstance(arguments, dict):
                # Non-conforming gateway: already-parsed arguments instead of a JSON string.
                direct: dict[str, Any] = {
                    "index": position,
                    "type": "function",
                    "function": {"name": call.get("name"), "arguments": arguments},
                }
                if not spec.get("omit_id"):
                    direct["id"] = call.get("id")
                yield _data({"choices": [{"index": 0, "delta": {"tool_calls": [direct]}}]})
                yield _data(
                    {"choices": [{"index": 0, "delta": {"content": None, "tool_calls": None}}]}
                )
                continue
            text = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
            )
            parts = _pieces(text, 2) or [""]
            fragment: dict[str, Any] = {
                "index": position,
                "type": "function",
                "function": {
                    "name": None if spec.get("omit_name") else call.get("name"),
                    "arguments": parts[0],
                },
            }
            if not spec.get("omit_id"):
                fragment["id"] = call.get("id")
            yield _data({"choices": [{"index": 0, "delta": {"tool_calls": [fragment]}}]})
            for part in parts[1:]:
                yield _data(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"index": position, "function": {"arguments": part}}
                                    ]
                                },
                            }
                        ]
                    }
                )
            # Real providers send explicit nulls for fields that are absent here.
            yield _data({"choices": [{"index": 0, "delta": {"content": None, "tool_calls": None}}]})
        finish = spec.get("finish_reason") or ("tool_calls" if spec.get("tool_calls") else "stop")
        yield _data({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        usage = spec.get("usage", self.usage)
        if usage is not None and spec.get("done", True):
            # OpenAI's last chunk before [DONE] carries the usage and an empty choices list.
            yield _data({"choices": [], "usage": usage})
        if spec.get("done", True):
            yield "data: [DONE]\n\n"


def tool_calls(*calls: dict) -> dict:
    """Reply spec that only returns native tool calls."""
    return {"tool_calls": list(calls)}


def call(call_id: str, name: str, arguments: Any) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


def _pieces(text: str, count: int = 3) -> list[str]:
    if not text:
        return []
    size = max(1, len(text) // count)
    return [text[index : index + size] for index in range(0, len(text), size)]


def _data(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _event(payload: dict) -> str:
    """An Anthropic SSE frame: the event name repeats inside the payload's ``type``."""
    return f"event: {payload.get('type')}\ndata: {json.dumps(payload)}\n\n"
