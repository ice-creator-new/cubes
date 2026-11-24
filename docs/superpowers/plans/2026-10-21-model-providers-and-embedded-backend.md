# 模型服务配置与桌面端内嵌后端：实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 用户在设置里管理多个模型服务（OpenAI 兼容、Anthropic），给每个 bot 选模型；桌面 App 启动时自带后端，带一次性令牌鉴权。

**Architecture:** provider 存在 SQLite，引擎每次模型调用前按发言者解析出 `ResolvedModel`，再由 `open_stream` 选协议适配器。桌面端把后端用 PyInstaller 打成 onedir 放进 Tauri resources，Rust 启动子进程，用随机端口和随机令牌；前端经 `invoke` 拿到地址和令牌。

**Tech Stack:** FastAPI、SQLite、httpx、pytest（真实 uvicorn + FakeUpstream）；React 19、vitest；Tauri 2.12.4（Rust std，无新 crate）；PyInstaller 6.22.3（构建期）。

Spec：`docs/superpowers/specs/2026-10-21-model-providers-and-embedded-backend-design.md`（以下简称 spec）。

## Global Constraints

- 界面文案中文；后端错误 detail 是中文句子、以「。」结尾。
- 不加边框描线，不用全大写标签，不用中点拼接元信息。
- 依赖精确锁定版本；不加新的 Python 运行时依赖、不加新的 Rust crate、不加新的 npm 包。PyInstaller 只进 `backend/requirements-build.txt`。
- 不写 `backend/.env`，不写 `backend/data/cubes.sqlite3`；需要真实数据时用 `.backup` 复制到 `/tmp`，并用 `DATABASE_PATH` 指过去。
- API key 永远不出现在任何响应、日志、SSE 和错误详情里。
- 不改 iOS（`cubes/`）。
- 每个任务各自提交，只 stage 自己改的文件，不 push，不 amend，保留 hooks。遇到 `index.lock` 就等一秒重试。
- 后端测试：`cd backend && uv run --python 3.12 --with-requirements requirements.txt --with pytest pytest -q`；前端：`cd web && npm test && npm run build`。

## 依赖关系

```
A（后端 provider）──┐
                    ├──> C（前端 provider 界面）
B（桌面内嵌）───────┘
```

A 和 B 并行。共享文件只有 `backend/app/main.py`（B 只加一行装配鉴权、一处 CORS 头）和 `backend/app/config.py`（A 负责，包括 B 需要的 `auth_token` 字段）。C 在 A、B 都完成后开始。

---

## Task A：后端 provider、协议适配、按 bot 选模型

**Files:**
- Create: `backend/app/providers.py`（provider 校验、`ResolvedModel`、`resolve_model`、`api_key_hint`、`test_provider`、`list_models`）
- Create: `backend/app/anthropic_stream.py`（`AnthropicMessagesStream`）
- Modify: `backend/app/model_stream.py`（`CompletionStream` → `OpenAIChatStream`，加 `ModelStream` 协议和 `open_stream`）
- Modify: `backend/app/store.py`（`providers` 表、`agents.model_ref`、meta 默认模型、`.env` 导入、0600）
- Modify: `backend/app/engine.py`（`_completion` 按 author 解析）
- Modify: `backend/app/models.py`（provider 请求/响应模型、`AgentOut.model_ref/model_status`、`HealthOut`）
- Modify: `backend/app/main.py`（`/providers*` 路由、`/health`、启动任务的守卫、`PATCH /agents` 的 `model_ref`）
- Modify: `backend/app/config.py`（去掉 `chat_completions_url/configured/configuration_error`；新增 `auth_token`）
- Modify: `backend/tests/fake_upstream.py`（加 `/v1/messages` Anthropic SSE、两种 `/models`）
- Create: `backend/tests/test_providers.py`、`backend/tests/test_anthropic_stream.py`
- Modify: 断言 schema 版本为 "5" 的测试改成 "6"：`test_action_detail.py:398`、`test_sidebar.py:285`、`test_team_migration.py:202,602`、`test_team_schema_v5.py:136`；`test_team_workspace.py` 里健康检查的文案断言。
- Modify: `README.md` 接口表、`backend/.env.example` 的 `MODEL_*` 注释（只在首次启动导入为 mify）。

**Interfaces（C 依赖这些，名字不能改）:**

```python
# providers.py
PROTOCOLS = ("openai-chat", "anthropic-messages")

@dataclass(frozen=True)
class ResolvedModel:
    protocol: str
    base_url: str      # 无末尾 /
    api_key: str
    model: str
    provider_id: str
    provider_name: str

def resolve_model(store, agent: dict) -> ResolvedModel   # 无可用模型时 raise UpstreamError(NO_MODEL)
NO_MODEL = "还没有配置模型服务：请在「设置 → 模型服务」里添加一个。"
def api_key_hint(key: str) -> str | None   # "" -> None；len<8 -> "已设置"；否则 f"{key[:3]}…{key[-4:]}"
```

```python
# model_stream.py
class ModelStream(Protocol):
    result: CompletionResult
    @property
    def tool_calls(self) -> list[ToolCallFragment]: ...
    def chunks(self, *, should_stop=None) -> AsyncIterator[str]: ...
    def validate(self, *, allow_tool_calls: bool) -> None: ...
    async def aclose(self) -> None: ...

def open_stream(client, resolved: ResolvedModel, *, messages, tools) -> ModelStream
```

HTTP（spec「接口」一节为准）：

```
GET    /providers                -> {"providers": [Provider], "default": {"provider_id","model"} | null}
POST   /providers                {name, protocol, base_url, api_key?, models[]} -> Provider  (201)
PATCH  /providers/{id}           同上字段均可选；api_key 缺省=保留，""=清空 -> Provider
DELETE /providers/{id}           -> 204
PUT    /providers/default        {provider_id, model} -> {"provider_id","model"}
POST   /providers/test           {id?, protocol, base_url, api_key?, model} -> {ok, latency_ms, supports_tools, detail}
POST   /providers/models         {id?, protocol, base_url, api_key?} -> {models: [str]}  (失败 502)
PATCH  /agents/{id}              新增可选 model_ref: {provider_id, model} | null

Provider = {id, name, protocol, base_url, api_key_hint, models, created_at, updated_at}
Agent 新增 model_ref: {provider_id, model} | null, model_status: "default" | "custom" | "fallback"
Health = {status, configured, model, base_url, provider_name, configuration_error, auth}
```

`/providers/test` 和 `/providers/models` 带 `id` 且没给 `api_key` 时，用已存的 key。`/providers/test` 用非流式请求，强制调用一个名为 `ping` 的工具（OpenAI：`tool_choice={"type":"function","function":{"name":"ping"}}`；Anthropic：`tool_choice={"type":"tool","name":"ping"}`），`max_tokens` 32；返回里有工具调用即 `supports_tools=true`。provider 的增删改、改默认、改 agent 的 `model_ref` 之后都推送 `workspace` 事件（`main.py` 里已有 `publish_workspace()`）。

- [ ] **A1 存储**：`SCHEMA_VERSION` 5→6；`providers` 表进 `SCHEMA_STATEMENTS`，`("model_ref","TEXT")` 进 `COLUMN_UPGRADES["agents"]`；meta 键 `default_provider_id`、`default_model`、`env_providers_imported`。打开数据库后对 db、`-wal`、`-shm` 做 `chmod 0o600`，父目录 `0o700`。`.env` 导入在 `_bootstrap` 里做，需要 `Store` 能拿到 `Settings` 的 `base_url/api_key/model`（构造参数或 `initialize(settings)`，择一）。`conftest.make_backend` 会传 `api_key="test-key"、model="test-model"`，所以现有测试自动得到一个指向 FakeUpstream 的 mify provider——这是现有 299 个测试继续通过的关键，别破坏。
- [ ] **A2 先写测试**（`test_providers.py`，真实 HTTP）：增删改查、重名 409、非法协议/地址/空模型列表 422（中文、「。」结尾）、key 只出现 hint、`api_key` 省略保持/空串清空、第一个 provider 自动成默认、删默认后改选、默认模型移出列表后改选、`.env` 导入只做一次（删掉 mify 重启不再导入）、文件权限 0600、`model_ref` 校验 422、`model_status` 三种状态、`/health` 无默认时的文案、无默认时启动任务 503。
- [ ] **A3 协议适配**：`OpenAIChatStream` 与现在行为完全一致（URL `{base}/chat/completions`，`Authorization: Bearer`，空 key 不带头）。`AnthropicMessagesStream` 按 spec「协议适配」实现：消息转换、工具 schema、SSE 事件、`stop_reason` 映射、`max_tokens=8192`、`x-api-key` + `anthropic-version: 2023-06-01`。两者都用 `resolved.api_key` 打码。
- [ ] **A4 测试 Anthropic**（`test_anthropic_stream.py` + FakeUpstream 新路由，记录 `protocol`、`x-api-key`、请求体）：纯文本、单个和两个工具调用（`input_json_delta` 分片拼接）、`max_tokens` 截断、`refusal`、`error` 事件、没有 `message_stop` 时报截断；消息转换单测（system 合并、连续 tool 合成一条 user、相邻同角色合并、assistant 的 tool_use 块）。
- [ ] **A5 引擎接线**：`_completion(messages, *, tools, author)` → `open_stream(self._client, resolve_model(self._store, author), ...)`；`_stream_reply` 把 `author` 传进去。端到端测试：建两个 provider（一个 openai-chat 指向 FakeUpstream 的 `/v1`，一个 anthropic 指向 FakeUpstream 根地址），两个成员各绑一个，群里 @ 两人，断言两条请求分别打到两个路由、用了各自的模型名和 key，回复都落库；删掉其中一个 provider 后该成员 `model_status=="fallback"` 且仍能回复。
- [ ] **A6 test/models 接口** + 测试（成功、HTTP 401 时 detail 中文且打码、用已存 key）。
- [ ] **A7** 跑全量后端测试，更新 README 接口表和 `.env.example` 注释，提交：`feat(backend): model providers with per-bot models and an Anthropic adapter`。
- [ ] **A8 冒烟**：把真实库 `.backup` 到 `/tmp`，用 `DATABASE_PATH` 起一个 8001 端口的后端（omp 后台 job），确认 mify 被导入、`GET /providers` 只给 hint；用 curl 在一个私聊里发一句话，确认用真实 mify 模型回复。结束后停掉 8001、删临时文件。

## Task B：桌面端内嵌后端与鉴权

**Files:**
- Create: `backend/app/auth.py`：`install_auth(app, token: str)`；token 非空时，除 `OPTIONS` 外所有请求需 `Authorization: Bearer <token>`，`hmac.compare_digest` 比较，失败 401 `{"detail":"缺少或错误的访问令牌。"}`。
- Create: `backend/desktop_main.py`：`--port --token --data-dir`；数据库 `<data-dir>/data/cubes.sqlite3`；CORS 只放 `tauri://localhost,http://tauri.localhost`；**不读** `backend/.env`；uvicorn 绑 `127.0.0.1`，绑定失败时进程以非零码退出。
- Create: `backend/requirements-build.txt`（`pyinstaller==6.22.3`）、`backend/scripts/build-desktop.sh`（onedir，`--collect-all resvg_py`，产物到 `web/src-tauri/resources/cubes-backend/`，加进 `.gitignore`）。
- Modify: `backend/app/main.py`：创建 app 后 `install_auth(app, resolved.auth_token)`；CORS `allow_headers` 加 `Authorization`。（`Settings.auth_token` 与 `HealthOut.auth` 由 Task A 加；B 开始时若 A 还没加，B 在 `config.py` 加 `auth_token: str = ""`、env `CUBES_AUTH_TOKEN`，并告知 A。）
- Modify: `web/src-tauri/src/lib.rs`、`tauri.conf.json`（`bundle.resources`）、`web/package.json`（`build:desktop`）。
- Modify: `web/src/api/client.ts`（`ApiClient(baseURL, token?)`；所有请求带 `Authorization`；`events()` 改成 `fetch` + `ReadableStream` 解析 SSE，保持现有 `onEvent/onClose` 语义：第一次出错就关闭并报告，不自动重连）。
- Modify: `web/src/state/store.ts`（启动时若 `isDesktop` 先 `invoke("backend_info")`；有返回就用其 `baseURL/token`，否则照旧读 `cubes.backendURL`）、`web/src/components/Panels.tsx` 的 `Settings`（内嵌时隐藏「后端地址」一节；「没有鉴权」提示只在 `health.auth === false` 时显示）、`web/src/App.tsx`（`backend_info` 返回错误时显示全屏中文错误、日志路径和「重试」→ `invoke("restart_backend")`）。
- Test: `backend/tests/test_auth.py`（无 token 401、错 token 401、对 token 200、SSE 也要求、OPTIONS 预检通过、token 为空时行为不变）；`web/src/api/sse.test.ts`（SSE 解析：多事件、跨 chunk 切分、`: ping` 心跳、坏 JSON 关闭）。

Rust（只用 std）：
- 只在 `resource_dir()/cubes-backend/cubes-backend` 存在时启动内嵌后端（release 包）；不存在时（`tauri dev`）`backend_info` 返回 `null`，开发流程不变。
- 端口：`TcpListener::bind("127.0.0.1:0")` 取端口后释放；子进程绑定失败（早退、非零码）换端口重试，最多 3 次。
- 令牌：从 `/dev/urandom` 读 32 字节转 hex。
- 健康检查：`TcpStream` 手写 `GET /health`（带 `Authorization`），每 100ms，最多 15s。
- 日志：stdout/stderr 写到 `app_data_dir()/logs/backend.log`（每次启动覆盖）；数据目录 `app_data_dir()`，`data/` 0700。
- 命令：`backend_info -> Option<{baseURL, token} | {error, logPath}>`、`restart_backend`。
- 退出：`RunEvent::Exit` 时 `kill -TERM <pid>`，2 秒后仍在就 `Child::kill()`。

- [ ] **B1** 先写 `test_auth.py` 和 `sse.test.ts`，看它们失败。
- [ ] **B2** 实现 `auth.py`、main 装配、`desktop_main.py`；跑全量后端测试。
- [ ] **B3** 前端：`ApiClient` 带令牌、fetch 版 SSE、store 启动流程、设置页条件显示、错误页；`npm test && npm run build`。
- [ ] **B4** 打包：构建脚本、Rust 启动与回收、resources、`build:desktop`。
- [ ] **B5 冒烟**：`npm run build:desktop` 后打开 `.app`：空团队（只有主管），设置里没有「后端地址」；`lsof` 找到子进程端口，`curl` 不带令牌返回 401；退出 App 后子进程消失；`backend.log` 存在。截图看界面。记下 `.app` 体积和冷启动到界面可用的时间。
- [ ] **B6** README 加「桌面版」一节（数据位置、日志位置、构建命令、没有签名），提交：`feat(desktop): bundle the backend and guard it with a launch token`。

## Task C：前端 provider 界面

开始前读 A 的接口实现（`backend/app/main.py` 的 `/providers*`、`models.py`）以它为准。

**Files:**
- Modify: `web/src/api/types.ts`（`Provider`、`ProviderDefault`、`ModelRef`、`Agent.model_ref/model_status`、`Health.provider_name/auth`）、`web/src/api/client.ts`（provider 方法、`patchAgent` 支持 `model_ref`）、`web/src/state/store.ts`（`providers` 状态，`workspace` 事件和启动时都重新读 `/providers`）。
- Create: `web/src/lib/providerPresets.ts`（spec 预设表；**逐个用 web_search 核对官方文档里的 base_url**，核对不了的在注释里写明）、`web/src/components/ProviderSettings.tsx`（卡片列表、预设选择、表单、获取模型、测试连接、默认模型、删除确认）。
- Modify: `web/src/components/Panels.tsx`（`Settings` 里换掉现在只读的「模型服务」一节，挂 `ProviderSettings`；`MemberEditor` 在「外观」上方加「模型」下拉，fallback 时提示）、`web/src/components/ChatView.tsx`（私聊头部副标题显示该成员当前模型名，长名截断、`title` 给全名）、`web/src/styles.css`。
- Test: `web/src/lib/providerPresets.test.ts`（每个预设协议合法、地址是 https 或本机 http）、模型下拉选项的纯函数测试（`跟随默认（当前：X）` 文案、按 provider 分组、fallback 时当前值）。

- [ ] **C1** 类型、client、store；`npm test && npm run build`。
- [ ] **C2** `ProviderSettings` 与设置页；空状态写「还没有模型服务。添加一个后，主管和成员才能回复。」。
- [ ] **C3** 成员编辑器的模型下拉、聊天头部模型名。
- [ ] **C4 冒烟**（Playwright 装在 `/tmp`，`playwright@1.64.1`；伪装 mac 壳：`window.__TAURI_INTERNALS__={}`、`navigator.platform='MacIntel'`；后端用真实库的 `/tmp` 副本起在 8001，前端用 1420 上已有的 vite，没有就 `vite preview`）：设置里看到 mify 卡片（只有 hint）；用「自定义」新建一个指向 8001 自身不存在路径的 provider，测试连接给出中文失败；给某成员选 mify 的另一个模型名再保存，接口读回 `model_ref`；把它删掉后成员编辑器显示 fallback 提示。浅色、深色各截图。
- [ ] **C5** README 用户说明，提交：`feat(web): model provider settings and per-bot model picker`。

## 收尾（主会话）

- 全量后端、前端测试；`npm run build:desktop`；重启 omp 后端 job；重新打开桌面 App。
- 真实双协议冒烟需要用户提供 Anthropic key（OpenAI 兼容用 mify）：两个成员各用一个，群里 @ 两人，都要真实回复并能调用工具。
