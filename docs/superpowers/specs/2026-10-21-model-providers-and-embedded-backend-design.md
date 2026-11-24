# 模型服务配置与桌面端内嵌后端

两个子项目，按顺序交付：先做 A（模型服务），再做 B（内嵌后端）。A 在现有开发环境里就能完整测试；B 只改打包和启动，不碰引擎。

## 已定的选择

- API key 存在 SQLite 里，数据库文件权限为 0600。
- 桌面版第一次启动时是一个空团队，不迁移 `backend/data/cubes.sqlite3`。
- 第一版协议：OpenAI 兼容（Chat Completions）和 Anthropic（Messages）。

## A. 模型服务

### 数据

新表 `providers`：

| 列 | 说明 |
| --- | --- |
| `id` | `p_` 前缀的随机 ID |
| `name` | 显示名，工作区内唯一（去空格、不区分大小写） |
| `protocol` | `openai-chat` 或 `anthropic-messages` |
| `base_url` | 以 `http://` 或 `https://` 开头；存储时去掉末尾 `/` |
| `api_key` | 明文；本地 Ollama 这类无需 key 的服务允许为空 |
| `models` | JSON 数组，模型名，至少一个，去重保序 |
| `created_at` / `updated_at` | ISO 时间 |

默认模型存在现有的 `meta` 表里：`default_provider_id`、`default_model` 两个键。两者都不存在表示还没配置默认模型。
`agents` 新增 `model_ref TEXT`：JSON `{"provider_id", "model"}`，为 null 表示跟随默认。主管也是 agent，同样适用。

数据库文件在创建和每次启动时都 `chmod 0600`（目录 0700）。

### 从 `.env` 迁移

启动时如果 `providers` 为空，且 `.env` 里 `MODEL_API_KEY`、`MODEL_NAME` 都有值，就插入一个 `name="mify"`、`protocol="openai-chat"` 的 provider，用 `MODEL_BASE_URL`（缺省用现有的默认地址），并设为默认模型。只在表为空、且从未迁移过时做一次（`meta.env_providers_imported=1` 记录）；之后 `.env` 里的 `MODEL_*` 不再生效，用户删掉 mify 也不会再导回来。

### 解析一次调用用哪个模型

`resolve_model(agent) -> ResolvedModel(protocol, base_url, api_key, model, provider_name)`：

1. agent 的 `model_ref` 指向的 provider 存在，且 `model` 在它的 `models` 里：用它。
2. 否则用 `model_defaults`。
3. 都没有：抛出 `UpstreamError("还没有配置模型服务：请在「设置 → 模型服务」里添加一个。")`。

第 1 步不成立但 `model_ref` 非空时，回复照常用默认模型，同时把这个 agent 标为 `model_status="fallback"`，客户端据此提示「原来的模型已被删除，正在使用默认模型」。provider 被删除或模型被移出列表时不改写 `agents` 行；用户在编辑器里重新选择后才覆盖。

每次模型调用前解析一次（`_stream_reply` 已经知道 `author`），所以设置改动从下一次调用起生效，正在进行的流不受影响。

### 协议适配

`model_stream.py` 拆成：

- `ModelStream` 协议：`chunks(should_stop)`、`tool_calls`、`result`、`validate(allow_tool_calls)`、`aclose()`。引擎只依赖它，现有调用点不变。
- `OpenAIChatStream`：现有 `CompletionStream` 改名，`model`、URL、key 改为来自 `ResolvedModel`。
- `AnthropicMessagesStream`：
  - 请求发到 `POST {base_url}/v1/messages`，带 `x-api-key`、`anthropic-version: 2023-06-01`，`stream: true`，`max_tokens` 固定为 8192（Anthropic 要求必填；第一版不开放配置）。
  - 消息转换，入参仍是引擎现有的 OpenAI 形状：
    - `system` 消息合并成顶层 `system`。
    - `assistant` 带 `tool_calls` → `content: [text?, tool_use{id, name, input}]`；`input` 由 arguments JSON 解析。解析失败的调用在引擎里已经以工具错误回给模型，所以历史里该调用的 `input` 记为 `{}`，紧随其后的 `tool_result` 带着那条错误，模型能看到原因。
    - 连续的 `tool` 消息 → 一条 `user` 消息，内容是多个 `tool_result{tool_use_id, content}`。
    - 相邻同角色消息合并，保证 user/assistant 交替。
  - 工具 schema：`{name, description, input_schema: parameters}`，`tool_choice: {"type": "auto"}`。
  - SSE：`content_block_start`（`tool_use` 记下 id、name）、`content_block_delta`（`text_delta` 推给文本流，`input_json_delta` 拼进 arguments）、`message_delta`（`stop_reason`）、`message_stop`（等同 `[DONE]`）、`error`。
  - `stop_reason` 映射：`end_turn`/`tool_use`/`stop_sequence` 正常；`max_tokens` → 「被长度限制截断」；`refusal` → 「因内容策略拒绝」。
- `open_stream(client, resolved, messages, tools)` 按 `protocol` 选实现。
- 两个实现共用现有的报错文案、`_clip` 截断和 key 打码（打码用当前 provider 的 key）。

### 接口

| 接口 | 说明 |
| --- | --- |
| `GET /providers` | 列表与默认模型：`{providers: [Provider], default: {provider_id, model} \| null}`。`Provider.api_key` 永远不返回，只返回 `api_key_hint`（`sk-…abcd`，key 不足 8 位时为 `已设置`，空为 null） |
| `POST /providers` | 新建；重名 409；字段非法 422（中文、以「。」结尾）。表里第一个 provider 自动成为默认，默认模型取它的第一个模型 |
| `PATCH /providers/{id}` | 改名称、协议、地址、模型列表；`api_key` 不传则保留，传空串则清空。默认模型被移出列表时，默认改为该 provider 的第一个模型 |
| `DELETE /providers/{id}` | 删除。它是默认时，默认改为剩下的第一个 provider 的第一个模型，没有剩下的就清空 |
| `PUT /providers/default` | `{provider_id, model}`；模型必须在列表里 |
| `POST /providers/test` | 用表单里的值（可带 `id` 表示用已存的 key）发一次最小请求：一条用户消息加一个简单工具，`max_tokens` 很小。返回 `{ok, latency_ms, supports_tools, detail}`；`detail` 是中文、已打码 |
| `POST /providers/models` | 同样的表单值，拉模型列表：OpenAI 走 `GET {base}/models`，Anthropic 走 `GET {base}/v1/models`。返回 `{models: [str]}`；失败返回 502，中文说明 |
| `PATCH /agents/{id}` | 新增可选字段 `model_ref`：`{provider_id, model}` 或 null（跟随默认）；指向不存在的 provider 或不在列表里的模型时返回 422 |

`Agent` 返回值新增 `model_ref`（可为 null）和 `model_status`（`default`、`custom`、`fallback`）。

`/health` 改为：`configured` 表示有默认模型；`model`、`base_url` 取默认模型的；`configuration_error` 文案改为指向「设置 → 模型服务」。

任何 provider 变动都推送一次 `workspace` 事件，客户端重新读取 `/providers`。

### 预设

前端内置一份预设列表，只用来预填表单，不入库：

| 预设 | 协议 | base_url |
| --- | --- | --- |
| OpenAI | openai-chat | `https://api.openai.com/v1` |
| Anthropic | anthropic-messages | `https://api.anthropic.com` |
| DeepSeek | openai-chat | `https://api.deepseek.com/v1` |
| Moonshot（Kimi） | openai-chat | `https://api.moonshot.cn/v1` |
| 通义千问（DashScope） | openai-chat | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM | openai-chat | `https://open.bigmodel.cn/api/paas/v4` |
| OpenRouter | openai-chat | `https://openrouter.ai/api/v1` |
| Gemini（OpenAI 兼容） | openai-chat | `https://generativelanguage.googleapis.com/v1beta/openai` |
| Ollama（本机） | openai-chat | `http://127.0.0.1:11434/v1` |
| 自定义 | 用户选 | 用户填 |

实现时逐个核对这些地址的当前文档。

### 界面

「设置 → 模型服务」：

- 每个 provider 一张卡片：名称、协议、地址、`api_key_hint`、模型数量；默认的那个标「默认」。卡片只靠色调区分，没有描边。
- 「添加」先选预设，再进入表单：名称、协议（分段控件）、地址、API key（密码框，编辑时显示占位「已设置，留空保持不变」）、模型（可编辑的标签列表，旁边一个「获取模型列表」按钮，拉回来后勾选）。
- 表单底部「测试连接」：显示耗时和是否支持工具调用。不支持工具时给出警告：主管和成员都依赖工具，这个模型只能用于不需要工具的场景。
- 默认模型：一个按 provider 分组的下拉框。
- 删除前确认，并说明有几个 bot 会回到默认模型。

成员编辑器（主管也一样）：在「外观」上方加「模型」一行，下拉框选项为「跟随默认（当前：X）」以及按 provider 分组的模型。`model_status="fallback"` 时，在下拉框下方显示提示。

聊天头部的副标题或成员行里显示当前用的模型名，长名截断，`title` 显示完整名称。

### 不做

- Responses API、Gemini 原生协议、Bedrock、Vertex。
- 按 provider 设置温度、top_p 等采样参数。
- 用量和费用统计。
- 自动故障切换到另一个 provider。

## B. 桌面端内嵌后端

### 打包

- 用 PyInstaller（锁定版本，构建期依赖，记在 `backend/requirements-build.txt`）以 `--onedir` 把 `backend/app` 打成 `cubes-backend/`。入口是新的 `backend/desktop_main.py`：读取命令行参数 `--port`、`--token`、`--data-dir`，再用 uvicorn 启动 `create_app`。
- `web/src-tauri/tauri.conf.json` 的 `bundle.resources` 带上这个目录。`npm run build:desktop` 先打后端，再执行 `tauri build`。
- `resvg-py` 是原生扩展，需要确认 PyInstaller 能收进去；收不进去就在 hook 里显式加入。

### 启动

Rust（`lib.rs` 的 `setup`）：

1. 在 `app_data_dir()`（`~/Library/Application Support/com.ice.cubes.desktop/`）下建 `data/`，权限 0700。
2. 绑定 `127.0.0.1:0` 拿到一个空闲端口后释放，生成 32 字节随机 token（hex）。
3. 用 `std::process::Command` 启动 `resources/cubes-backend/cubes-backend --port P --token T --data-dir D`，stdout/stderr 写到 `D/logs/backend.log`（只保留最近一个文件）。
4. 轮询 `GET /health`（带 token），间隔 100ms，最多 15s。超时就在窗口里显示错误页：中文说明，附日志路径和「重试」按钮。
5. 就绪后，把 `{baseURL, token}` 存进 Tauri state；前端用 `invoke("backend_info")` 读取。
6. 窗口关闭或 App 退出（`RunEvent::Exit`）时先 SIGTERM，2 秒后还没退出就 SIGKILL。
7. 子进程意外退出时，前端会看到事件流断开，并显示现有的「重新加载」入口；重新加载会调用 `invoke("restart_backend")`。

端口在释放到被子进程重新绑定之间有极短的竞争窗口。子进程绑定失败时退出码非零，Rust 换一个端口重试，最多 3 次。

### 鉴权

- 后端新增 `Settings.auth_token`。不为空时，除 `OPTIONS` 外的所有请求都要带 `Authorization: Bearer <token>`，否则返回 401（中文）。SSE（`/events`）和头像接口一样要求。
- `EventSource` 不能带自定义头，所以前端的事件流改用 `fetch` 读流来实现，以便带上头。
- token 为空时（开发模式）行为不变，`/health` 里新增 `auth: false`，前端据此继续显示现有的「没有鉴权」提示。
- 桌面模式下 CORS 只放行 `tauri://localhost` 和 `http://tauri.localhost`。

### 前端

- 启动时，如果运行在 Tauri 里，并且 `invoke("backend_info")` 有返回，就使用它的 `baseURL` 和 `token`，忽略 `cubes.backendURL`。设置里不显示「后端地址」一节。
- 浏览器开发模式不变：照旧读 `cubes.backendURL`，没有 token。
- `ApiClient` 统一加上 `Authorization` 头。

### 数据

桌面版数据库位于 `D/data/cubes.sqlite3`，头像缓存在旁边。第一次启动是空团队：只有主管，没有 provider，界面引导用户去「设置 → 模型服务」添加。开发用的 `backend/data/` 不受影响。

### 不做

- 代码签名和公证，包括后端子进程的签名。
- 自动更新。
- Windows、Linux 打包。

## 安全

- 不加鉴权的话，任何本机进程都能读出或滥用存起来的 key，所以 B 的 token 是必需的，不是可选项。A 单独上线时（开发模式），README 和设置页继续标明「没有鉴权，只在本机使用」。
- key 永远不出现在响应、日志、错误详情和 SSE 里；测试会断言这一点。
- 数据库以 0600 权限存放明文 key：本机同一用户的进程仍然能读到。这是已接受的取舍。

## 验证

后端（pytest，用现有的 `FakeUpstream`，再加一个 Anthropic 形状的假服务）：

- provider 增删改查、重名、校验文案、key 打码、`api_key` 留空保持不变。
- 默认模型的自动选择与删除后的重新选择；`.env` 迁移只做一次。
- 按 bot 解析：custom、跟随默认、fallback；同一群里两个成员各自打到不同的假上游。
- Anthropic 流：文本、单个和多个工具调用、`input_json_delta` 拼接、`max_tokens` 截断、`refusal`、`error` 事件；消息转换（system 合并、tool_result 合并、角色交替）。
- `/providers/test` 和 `/providers/models` 的成功与失败。
- 鉴权：没带 token 时 401，带了正确 token 时一切照常，SSE 同样。
- 0600 权限。

前端（vitest）：预设填表；`model_ref` 下拉框的选项与 fallback 提示；事件流改成 fetch 后照常解析。

冒烟：

- A：浏览器连本地后端，添加一个 OpenAI 兼容 provider 和一个 Anthropic provider（真实 key，由用户提供），让两个成员分别用这两个 provider，在群里 @ 两人，两边都真实回复并能调用工具。
- B：`npm run build:desktop`，打开 `.app`：空团队，添加 provider 后可以对话；退出后子进程消失；用 `curl` 不带 token 访问端口返回 401。
