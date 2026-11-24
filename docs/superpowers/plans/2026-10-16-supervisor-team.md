# Supervisor Team Implementation Plan

> **For agentic workers:** Execute independent backend/client slices against the frozen shared contract. Steps use checkbox syntax. No commits. Integration owner runs checks after the implementation wave.

**Goal:** Replace fixed roundtable roles with one persistent supervisor that autonomously creates agents/groups and delegates real model work, with native SwiftUI team chat and stable Blobatar avatars.

**Architecture:** Python owns SQLite entities, native OpenAI tool-call orchestration and one workspace-wide task. SwiftUI renders exact persistent state and SSE events. Parent owns the Blobatar PNG router, dependency integration, migration/runtime verification and README; backend and native client are independent implementation slices.

**Tech Stack:** SwiftUI / URLSession / iOS 27; Python 3.11+, FastAPI/httpx/sqlite3/python-dotenv; resvg-py 0.5 for cached PNG avatars.

## Global constraints

- Approved spec: `docs/superpowers/specs/2026-10-16-supervisor-team-design.md`.
- Default exactly one editable supervisor, no fixed member types, no fixed rounds/minimum-two launch requirement.
- Task-driven autonomy only. Model uses native `tools/tool_calls`; no keyword routing or fake creation messages.
- Existing MODEL_BASE_URL default remains `http://model.mify.ai.srv/v1`; preserve user backend/.env and never reveal API key.
- Deep/light/system modes and the installed SwiftPieces GlassSurface remain. Use real Blobatar images, no SF Symbol stand-ins for agent faces.
- One active task in the entire workspace. Explicit conflict, cancellation and bounded execution; all entities/actions persist through stop/error.
- No account/public deployment, no browser/shell/file tools, no recurring background scheduler, no automatic retries or legacy endpoint shims.
- Every implementation worker skips build/lint/tests/formatters mid-flight. Write meaningful behavior tests first; parent executes them after the wave. Do not touch user `.env`, migrate the live DB during development, or commit.

## Shared HTTP / JSON contract

All keys snake_case, IDs strings, dates ISO-8601 strings, optional keys encode null. FastAPI errors: `{"detail":"actionable message"}`.

```json
{
  "agent": {"id":"a1","name":"主管","instructions":"帮助用户完成目标，自主组建团队","kind":"supervisor","avatar_seed":"uuid-seed","created_at":"...","updated_at":"..."},
  "conversation": {"id":"c1","kind":"direct","title":"主管","purpose":"主管私聊","member_ids":["a1"],"last_message":null,"created_at":"...","updated_at":"..."},
  "run": {"id":"r1","conversation_id":"c1","status":"running","current_step":"正在分析目标","error":null,"model_calls":0,"tool_calls":0,"created_at":"...","updated_at":"...","finished_at":null},
  "action": {"id":"x1","run_id":"r1","tool_call_id":"call1","tool_name":"create_agent","status":"completed","summary":"创建了需求研究员","target_agent_id":"a2","target_conversation_id":null,"error":null},
  "message": {"id":"m1","conversation_id":"c1","author_id":"a1","author_name":"主管","avatar_seed":"uuid-seed","kind":"agent","content":"正文","status":"complete","seq":1,"created_at":"...","action":null}
}
```

- Agent kind `supervisor|member`.
- Conversation kind `direct|group`. Direct is unique and belongs to supervisor; all groups include supervisor, can initially have supervisor alone.
- Message kind `user|agent|action`; message status `streaming|complete|interrupted`. User author_id/avatar_seed null. Migrated historical agents may have null author_id but retain author_name/avatar_seed. Action messages embed Action; tool arguments/results also persist internally.
- Run status `running|completed|stopped|error|limited`. `limited` is budget exhaustion, not success.
- Action status `running|completed|error`.
- `Workspace = {"agents":[Agent],"conversations":[Conversation],"supervisor_id":"...","direct_conversation_id":"...","active_run":Run|null}`. active_run is only a currently running task; conversations newest first except supervisor direct pinned first.
- `ConversationDetail = {"conversation":Conversation,"messages":[Message],"latest_run":Run|null,"legacy_roles":[{"name":"...","instructions":"...","avatar_seed":"..."}]}`. legacy_roles empty for new conversations; for migrated groups preserve old role snapshots read-only.
- `Snapshot = {"workspace":Workspace,"conversation":ConversationDetail|null}`.

### Routes

- `GET /health`: preserve `{status:"ok",configured:bool,model:string,base_url:string,configuration_error:string|null}`.
- `GET /workspace`: Workspace; creates default supervisor/direct thread once during bootstrap.
- `PATCH /agents/{id}` body `{name?:string,instructions?:string}` → Agent. IDs, avatar seed and kind immutable. Rename supervisor updates direct title as appropriate. Reject empty values. Changes affect later calls, never rewrite message author snapshots.
- `GET /conversations/{id}` → ConversationDetail or 404.
- `PATCH /conversations/{id}` body `{title?:string,purpose?:string,member_ids?:[string]}` → Conversation. Group updates require existing unique agent IDs, supervisor automatically included; direct membership cannot change. Reject changes while a run is active (409) to avoid active-context races.
- `POST /conversations/{id}/runs` body `{"content":"user goal"}` → 202 Run. Validate configuration before appending message; global task running → 409 without appending. Initial supervisor-only chat works. No legacy /sessions routes.
- `GET /runs/{id}` → Run (useful after disconnect/stop).
- `POST /runs/{id}/stop` body `{}` → Run; await full cancellation and final persistence; idempotent for terminal run.
- `GET /events?conversation_id=<optional>` → SSE. Subscribe, collect workspace + selected conversation snapshot, and drain any already-reflected queued state in a single await-free block BEFORE first yield. Initial `snapshot`: Snapshot. If idle/terminal send `done`: Snapshot and close. Otherwise forward:
  - `workspace`: Workspace after agent/group/member changes.
  - `run`: Run on progress/counter/state changes.
  - `message_start`: Message (including action messages, whose status changes use message_end).
  - `delta`: `{conversation_id,message_id,text}`.
  - `message_end`: complete/partial Message, authoritative final text/action status.
  - `done`: Snapshot of current workspace + requested selected conversation, then close.
- Events for other conversations include their IDs; client updates directory summaries but only applies bodies to the selected conversation. Switching conversation re-subscribes with the new ID and gets its authoritative snapshot.
- `GET /avatars/{seed}.png` → image/png from actual Blobatar SVG rasterized/cached. Parent implements `app.avatars.create_avatar_router(cache_dir:Path)`; backend includes this router using `settings.database_path.parent / "avatars"`. PNG cache headers immutable because seed+gen2 fixed. No client-supplied remote URL.

### Native tool schemas

Only supervisor receives tools. Tool args are objects with additionalProperties false:

```text
create_agent(name:string, instructions:string)
create_group(title:string, purpose:string, member_ids:string[])
set_group_members(group_id:string, member_ids:string[])
post_group_message(group_id:string, content:string)
ask_agent(group_id:string, agent_id:string, task:string)
```

Tools return real IDs/state, not an imagined narrative. `ask_agent` requires the agent exists, is a non-supervisor member of that group, reads only that group's completed user/agent history + its instructions + assigned task, and writes a streamed agent message there. Post-group message is authored by supervisor. Action message lives in originating run conversation so user can follow progress; target IDs allow navigation.

Supervisor context includes selected conversation history, team/group directory and current tool results. Members cannot spawn tools recursively. Accumulate streamed tool_call index/id/name/arguments fragments, require completed upstream response before executing, then append correct assistant tool_calls and role=tool/tool_call_id messages for next supervisor step. Content-only final answer ends the task. Unknown tools/invalid args produce persisted error action and tool result so supervisor may correct them; repeated same tool_call_id with identical args reuses stored result, different args for same ID errors rather than repeats side effects.

### Budget / configuration

Retain existing Settings DI (`create_app(Settings(...))`, app.main:app). Extend Settings with defaults `max_model_calls=16`, `max_tool_calls=32`, `max_new_agents=8`, `max_new_groups=4`; environment names `MAX_MODEL_CALLS_PER_RUN`, `MAX_TOOL_CALLS_PER_RUN`, `MAX_NEW_AGENTS_PER_RUN`, `MAX_NEW_GROUPS_PER_RUN`. Count all supervisor/member model calls. Stop budget BEFORE starting work that would exceed it; terminal `limited` with explicit error. Do not use budgets to impose fixed roles. Timeouts, truncated/empty/refused content and secret redaction remain truthful.

## Task 1: Backend autonomous team cutover

**Owner/files:** backend worker owns `backend/app/{config,models,store,engine,main}.py`, new focused tools/model-stream modules as needed, `backend/tests/**`, `.env.example`, requirements files. Parent owns app/avatars.py and test_avatars.py, root README/third-party notice. Do not edit parent files.

- [ ] Write behavior tests for bootstrap one supervisor, independent identities/groups, true tools creating persistent entities, chronological tool replay, member-context isolation, budget boundaries, global 409, stop preserving partial output/actions, repeated tool_call_id, malformed/refused/truncated upstream, migration/restart.
- [ ] Replace store with versioned transactional schema. Before live migration, use sqlite backup to `<database>.pre-team-backup.sqlite3` once. Tests use temp DBs; no import-time live migration. Do initialization/migration/bootstrap during FastAPI lifespan, not module import. Old sessions/messages → group histories with author and role snapshots; no automatic old agents in active team. Preserve timestamps/order and existing stopped/error/partial text. Drop obsolete execution tables only within successful migration after backup; no shim.
- [ ] Implement strict shared models/routes, one global run registry and event ordering. Store pending action before executing and finish it only after side effects persist; cancellation marks active messages/actions interrupted/error with explicit stopped reason, never fakes completion.
- [ ] Replace fixed round loop with real supervisor tool loop; preserve proven cancellation/refusal/truncation handling, close HTTP streams/tasks on all paths. Strict parse failures are real errors, not silent skipping. No automatic retries.
- [ ] Include parent's avatar router without owning its implementation; old roundtable tests must be migrated to new consumer contracts or removed when solely testing obsolete fixed-role behavior. Test fixture upstream stays under tests only.
- [ ] Report `create_app` details, test command, migration behavior and remaining risks. Skip tests/build/lint/formatters until parent verification. Write report `local://team-backend-report.md`.

## Task 2: SwiftUI team messenger cutover

**Owner/files:** native worker owns `cubes/*.swift` except cubesApp.swift entry may remain; can add new Swift files and remove obsolete fixed-round files. Preserve cubes/SwiftPieces/Glass/GlassSurface.swift. Parent owns Xcode project/network config/docs and backend avatars.

- [ ] Replace DTO/API/store with exact shared contract. Keep byte-based SSE framing (not AsyncBytes.lines); strict decoding, early EOF errors, stale generation guards, long streaming timeout, no retries. Start observation only AFTER POST /runs succeeds; initial snapshot covers early tokens. Authoritative stop reply invalidates old observer before adoption.
- [ ] Make native team chat UI: conversation list with pinned supervisor and groups, supervisor chat empty state inviting a goal, team list and editable agent detail, group detail with member editing, action cards opening real entity targets. No old hero/round selector/fixed-role template. Default opens supervisor conversation once loaded; list is reachable with native back navigation.
- [ ] Use compact native TabView/NavigationStack for conversations/team, visually prioritize actual Blobatar faces and identity; system sans typography and readable whitespace, subdued cool-neutral adaptive surfaces, no repeated topic header + user bubble. Continue SwiftPieces GlassSurface for controls, not entire long-form content.
- [ ] Render Blobatar via backend PNG endpoint with cached async native images. Show honest loading/error and tap retry; do not substitute symbol/random shape faces. Group images compose actual member avatars; stable seeds from API. No per-avatar WebView or added JS runtime.
- [ ] Show one global run status/current_step and stop affordance from any conversation; the supervisor may be working in another group. Sending while global run active disabled. Member/group edits disabled during task with backend errors still handled. Preserve user draft on failed send and accurate local alert ownership (no @Published mutation from alert binding setter).
- [ ] Keep backend settings/health + persisted AppStorage appearance key `appearance` (system/light/dark) and URL key `roundtable.backendURL` so existing user settings survive; apply preferredColorScheme inside Settings presentation too. Do not keep old API aliases.
- [ ] Accessibility IDs: `messageInput`, `sendMessage`, `stopRun`, `teamTab`, `conversationsTab`, `supervisorChat`, `backendSettings`, `backendURL`, `saveBackend`, `closeSettings`, `appearancePicker`, `agentRow_<id>`, `groupRow_<id>`, `action_<id>`, `memberName`, `memberInstructions`, `saveAgent`, `conversationList`. Provide exact routing/edit selectors in report.
- [ ] Skip checks until parent integration. Write report `local://team-ios-report.md` with files, selectors, lifecycle design and risks; no commits.

## Task 3: Avatar integration and exercised delivery

**Owner/files:** parent `backend/app/avatars.py`, `backend/tests/test_avatars.py`, root notices/README, integration fixes, temporary smoke scripts outside repo.

- [ ] Install resvg-py >=0.5,<0.6 (available Python3.12 arm64 wheel); real API `resvg_py.svg_to_bytes(svg_string=...)` returns PNG bytes. Keep dependency in requirements. Fetch fixed Blobatar gen2 URLs with seed only, validate seed allowlist and SVG/size bounds, cache atomically, serialize same-seed fetches to avoid duplicate work; explicit failure rather than a made-up image.
- [ ] Verify actual SVG rasterizes, source PNG stable across rename/system appearance, cached fetch works after source unavailable; include MIT attribution. Python backend stays Python only.
- [ ] Before migration of user data, run new suite on temporary databases, including old-schema fixture backup/migration/reopen. Check native tool support on current real model without exposing key.
- [ ] Run actual API + isolated streamed tool fixture for supervisor creation/group/consultation/finalization and all failure boundaries. Build actual Xcode target and launch simulator; native smoke for bootstrap-only supervisor, member/group creation updates, action navigation, member edit, group membership, stop, history and both themes.
- [ ] Start user-configured real backend after backup-safe cutover and run a real goal requiring dynamic agents/group/consultation, then inspect actual entities and native conversation. Verify a subsequent simple prompt does not force new agents.
- [ ] Review backend/client independently, fix concrete findings, run targeted proof. After smoke, update README, preserve user .env, remove throwaway scripts and stop fixtures. No git commits or branch/worktree changes.
