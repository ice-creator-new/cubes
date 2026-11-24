# Agent Roundtable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax for tracking.

**Goal:** Deliver a working native iOS multi-agent roundtable and a local Python model-proxy backend.

**Architecture:** SwiftUI consumes a FastAPI JSON/SSE API. Python owns sequential multi-agent generation and SQLite persistence; each role receives prior participants' messages. Independently implement backend and client against the contract below, then integrate and verify in the simulator.

**Tech Stack:** SwiftUI / URLSession / iOS 27, Python 3.11+, FastAPI, httpx, python-dotenv, sqlite3, pytest.

## Global Constraints

- Upstream default: `http://model.mify.ai.srv/v1`.
- API Key is read only by Python from `backend/.env`; `.env.example` leaves it empty.
- `MODEL_NAME` is configurable and empty in the example, not a fabricated model name.
- Default three editable roles, configurable 1–5 rounds, 2–6 agents.
- Shared conversation context, sequential role turns, final role summarizes each round.
- Support stop, follow-up continuation, and persisted history, without fake production replies.
- Use SwiftPieces StreamingReply, ThinkingState and PromptChips as interaction references. Per user feedback, actually install licensed GlassSurface via MCP and use it in the app; retain its original license.
- Respect Reduce Motion, Reduce Transparency and Dynamic Type.
- Support persisted System / Light / Dark appearance, including immediate updates inside the settings presentation.
- Local/trusted-network service only; no account or public deployment feature.
- All implementation workers skip build/lint/tests/formatters mid-flight; integration owner runs verification after the implementation wave. Write meaningful behavior tests before production implementation. Do not commit changes.

## Shared HTTP contract

All JSON keys use snake_case. IDs are strings. Dates are ISO-8601 strings. Nullable values are encoded as null. Error responses use FastAPI `{"detail":"actionable message"}`.

```json
{
  "id":"session-id", "title":"topic title", "topic":"full topic",
  "agents":[{"id":"a1","name":"探索者","role":"提出清晰可行的方案","color":"blue","symbol":"sparkles"}],
  "rounds":2,"status":"idle","error":null,
  "messages":[{"id":"m1","agent_id":null,"author":"你","content":"topic","kind":"user","status":"complete","round":0,"created_at":"2026-10-16T00:00:00Z"}],
  "created_at":"2026-10-16T00:00:00Z","updated_at":"2026-10-16T00:00:00Z"
}
```

`status`: `idle|running|completed|stopped|error`. Message `status`: `streaming|complete|interrupted`. `kind`: `user|agent`. Agent colors: `blue|orange|purple|green|pink|teal`. Symbols are SF Symbol names.

- `GET /health` → `{"status":"ok","configured":false,"model":"","base_url":"http://model.mify.ai.srv/v1","configuration_error":"..."}` (configuration_error nullable; no secret exposure).
- `GET /sessions` → array of full session objects, newest updated first.
- `POST /sessions` with `{"topic":"...","agents":[...],"rounds":2}` → 201 session. Persist initial user topic as first message.
- `GET /sessions/{id}` → session or 404.
- `POST /sessions/{id}/run` with `{"prompt":null}` for initial run or `{"prompt":"follow-up"}` → 202 running session. Missing model config returns 503 without starting. Running session returns 409. Non-empty follow-up is appended as a user message. No prompt is also valid to continue.
- `POST /sessions/{id}/stop` → terminal stopped session; idempotent for non-running sessions. Must await cancellation and persist partial text.
- `GET /sessions/{id}/events` → `text/event-stream`. Subscribe then immediately send `snapshot` containing the full session. If already terminal/idle, send `done` and finish. Active stream sends the following named events, each one JSON data line followed by a blank line:
  - `snapshot`: full session
  - `message_start`: full new message
  - `delta`: `{"message_id":"...","text":"incremental text"}`
  - `message_end`: full final message
  - `status`: `{"status":"running|completed|stopped|error","error":null}`
  - `done`: full final session, then end stream
- SSE comment heartbeats allowed. No custom replay or auto-retry protocol. Loading a saved session takes a fresh snapshot. SSE subscriber disconnection must not cancel the server-side discussion.

## Task 1: Python backend

**Files:** create `backend/app/{__init__,config,models,store,engine,main}.py`, `backend/requirements.txt`, `backend/requirements-dev.txt`, `backend/.env.example`, `backend/tests/test_roundtable.py`.

**Interfaces:** implement shared HTTP contract above. `app.main:app` is the uvicorn entry point; configuration resolves backend/.env regardless of current working directory. Environment variables override dotenv. `DATABASE_PATH` controls SQLite location, default backend/data/cubes.sqlite3. Tests use temporary databases and an isolated HTTP upstream.

- [x] Add behavior tests for second-role shared context, continuation, duplicate-run conflict, partial-output stop, restart recovery and upstream faults.
- [x] Implement Pydantic boundaries: trim topic/name/role; reject blank values and duplicate agent IDs, bound rounds and agent count. Do not put configuration secrets in API output or errors.
- [x] Implement SQLite session persistence and interrupted-startup recovery. Keep one worker process; do not claim multiworker coordination.
- [x] Implement httpx streamed POST to base URL + `/chat/completions` with Bearer key, model, messages, stream=true. Parse data events; require normal completion marker and nonempty content. Do not swallow upstream refusal/errors.
- [x] Implement background run registry, cancellation-safe persistence, and subscriber queues. Start and stop must prevent overlapping tasks for the same session.
- [x] Expose JSON routes, health and SSE. On lifespan shutdown cancel tasks and close resources.
- [x] Integration owner ran `backend/.venv/bin/python -m pytest backend/tests -q`: 34 passed after review fixes.

## Task 2: SwiftUI app

**Files:** replace `cubes/ContentView.swift`; create `cubes/{Models,APIClient,RoundtableStore,DesignSystem,HomeView,ConversationView,AgentEditorView,SettingsView}.swift`. Keep `cubesApp.swift` entry point using `ContentView()`; synchronized Xcode folder automatically includes new Swift files. Do not edit project.pbxproj; integration owner handles plist settings.

**Interfaces:** Codable DTOs mirror shared contract. @MainActor observable store handles create/run/stop/load/history and SSE application. Persist backend URL in UserDefaults; default `http://127.0.0.1:8000`. URLSession.bytes parses event/data frames, validates HTTP status, and uses a long enough request timeout for multi-round discussion. On stream error show real error and allow explicit reload; no automatic retries. Cancel old observation when switching session; reject stale async results from previous server/session using a request generation token or equivalent.

- [x] Implement API and strict DTO decoding with robust error detail handling; malformed frames and premature EOF surface real errors. Frame SSE from bytes because Foundation's line iterator drops empty boundaries.
- [x] Implement observable create/run/stop/history state; prevent stale writes, duplicate continuation requests and partial-text loss on failed reload.
- [x] After user feedback, redesign home around compact member identities, topic input, round selector, suggestions and real recent history. Remove the large orbit diagram and duplicated role cards.
- [x] Implement high-contrast timeline, role identity, thinking state, streaming text, copy, stop, follow-up and explicit reload; auto-scroll only near bottom and respect Reduce Motion.
- [x] Implement role sheet with editable names/prompts, 2–6 roles and meaningful validation; existing sessions retain role snapshots.
- [x] Implement backend settings, persisted System/Light/Dark appearance and real history listing. No preseeded fake production conversation.
- [x] Install and use SwiftPieces GlassSurface with original license; adapt colors and controls to light/dark and Reduce Transparency. Remove obsolete custom glass and orbit components.
- [x] Build actual project and run simulator scenarios; temporary UI automation lives outside the repository and is removed after proof.

## Task 3: Integration and exercised verification

**Files:** modify `cubes.xcodeproj/project.pbxproj` for local network/HTTP plist settings; create `.gitignore` and `README.md` with instructions after smoke proof.

- [x] Add tracked Config/Info.plist; restrict HTTP allowances to local networking, never disable ATS globally.
- [x] Install backend dependencies and build the final Xcode project for the available iPhone simulator with CODE_SIGNING_ALLOWED=NO.
- [x] Run backend behavior verification after implementation/review fixes: 34 passed.
- [x] Exercise real HTTP health/create/run/SSE/stop/continue, fault responses, persistence and missing-config behavior against isolated test upstreams.
- [x] Verify native home/settings/roles, light/dark/system appearance, streaming/follow-up/history, stop and real configuration errors; fix the alert's view-update publication warning and confirm a warning-free targeted rerun.
- [x] Review backend/client and resolve cancellation, snapshot ordering, refusal, redaction, malformed event and stale observation issues. Real configured xiaomi/mimo-v2.6-flash roundtable completed; its second role quoted prior-role content, and the native app rendered the saved real conversation.
- [x] Document startup, env handling, trusted-LAN use, licensing and HTTP security in README.md. Preserve existing user .env; remove temporary smoke sources/binaries and stop fixture servers. Keep only the user-configured backend running.
