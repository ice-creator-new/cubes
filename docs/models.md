# 模型服务与引擎

回到 [安装与部署](setup.md)。

## 模型服务与按 bot 选模型

「设置 → 模型」管理全部模型服务：每个服务存协议（OpenAI 兼容、Anthropic Messages，或本机的 `claude-code` / `codex`）、地址、API key 和模型列表。key 只留在后端，界面上只看得到 `sk-…abcd` 这样的提示。

- 「添加模型服务」先选预设：OpenAI、Anthropic、DeepSeek、Kimi（Moonshot）、通义千问、智谱 GLM、OpenRouter、Gemini（OpenAI 兼容）和本机 Ollama 会预填协议和地址，「Claude Code」「Codex」用本机已登录的订阅（见下一节），「自定义」则留空自己填。预设只是填表单，不入库。地址按协议填写：OpenAI 兼容填到 `/v1` 为止（程序接上 `/chat/completions`），Anthropic 填到域名即可（程序接上 `/v1/messages`），Ollama 这类本机服务不需要 key。
- 表单里的「获取模型列表」用这份表单的值向上游读模型：编辑已有服务时不填 key 就复用已存的那把。读回来后勾选要用的模型，也可以手动输入模型名。「测试连接」发一次最小请求，报出耗时和是否支持工具调用；主管和成员都依赖工具，不支持工具的模型只适合不需要工具的场景。
- 「默认模型」按服务分组列出全部模型，没有单独选过模型的 bot 都用它。删除服务前会说明有几个 bot 正在用它，这些 bot 删除后回到默认模型。
- 「备用模型」在默认模型下面，默认是「不设置备用模型」。当前模型连不上、超时、被限流（429）、服务端 5xx，或者返回额度/余额不足时，这次调用会在还没有出字之前改用备用模型重试一次，聊天里留一条灰色的系统提示「原模型暂时不可用（原因），这次改用备用模型」。鉴权失败、请求本身有错这类 4xx 不切换；已经出字的回复也不会中途换模型。备用只影响这一次调用，下一次仍然先用原模型；备用模型所属的服务被删除时自动关闭。
- 成员编辑器（主管也一样）的「模型」那一栏：默认是「跟随默认（当前：服务 / 模型）」，也可以选某个服务的某个模型。原来的服务或模型被删掉时，编辑器里提示「原来的模型已被删除，正在使用默认模型。」，回复照常使用默认模型，直到重新选一次。
- 聊天头部在名字下面显示当前用的模型名，太长会截断，悬停看完整名称。
- 改动从下一次回复开始生效，正在进行的回复不受影响。没有配置默认模型时 `/health` 返回 `configured: false`，启动任务返回 HTTP 503。

### 用本机订阅当引擎：Claude Code / Codex

不想再填 API key 时，可以让 bot 直接跑在这台电脑上已经登录的 Claude Code 或 Codex 上，用你自己的订阅。

- 预设里选「Claude Code（用你的 Claude 订阅）」或「Codex（用你的 ChatGPT 订阅）」：协议是 `claude-code` 或 `codex`，没有地址和 key。新建时后端会读这台机器上的模型清单，把「默认」和它报告的全部模型一起存进服务里。
- 预设卡片和表单里会显示这两个命令的状态，由后端实际跑 `claude --version` / `claude auth status`、`codex --version` / `codex login status` 得到：是否安装、是否登录。没就绪时按提示在终端运行 `claude`（或 `codex login`）登录，然后重新打开「模型服务」。
- 模型清单读本机、不花模型额度，缓存 60 秒，读不到也不影响打开设置页：
  - Codex：`codex debug models` 的 JSON，只留 `visibility: "list"` 的条目（名字用 `slug`）；读不到时退回 `~/.codex/models_cache.json`，再退到 `codex debug models --bundled`。`~/.codex/config.toml` 里的 `model` 是它当前的默认，界面上会标「CLI 当前使用」。
  - Claude Code：`claude --model` 接受的别名（`opus`、`sonnet`、`haiku`、`fable`）、`~/.claude/settings.json` 里 `modelPicker.options` 的每一项，以及顶层 `model`（当前默认）；`env.ANTHROPIC_DEFAULT_OPUS_MODEL / SONNET / HAIKU` 是各别名实际指向的模型，写在别名的说明里。`[1m]` 这类后缀是模型名的一部分，原样保留。
- 每个 bot 选模型时，如果是这两个协议，还能选「思考强度」：Claude Code 传 `--effort`（`low`/`medium`/`high`/`xhigh`/`max`），Codex 传 `-c model_reasoning_effort="…"`。可选档位来自该模型在清单里的 `supported_reasoning_levels`；清单里没有这个模型时用 CLI 自己认识的档位。不合法的档位在保存时 422。
- 模型名留「默认」不传模型参数，CLI 用它自己配置的模型；其他名字会原样传给 CLI（`--model` / `-m`）。
- 每个 bot 在自己的目录里运行：设置了 `WORKSPACE_DIR` 就用它，否则是数据库所在目录下的 `bots/<bot id>/`（桌面版就是 `…/data/bots/<bot id>/`）。文件类操作都落在这里。
- CLI 用它自己的工具（Bash、Read、Edit 等），Cubes 的团队工具则以 MCP 服务器「`cubes`」的形式交给它，所以跑在 Claude Code / Codex 上的 bot 也能建成员、问同事、跑工作流、定时任务、记笔记：每轮后端在同一个回环端口上开一个只属于这一轮的临时端点 `/cli-tools/<token>`，令牌和这次运行、这个 bot、这个会话绑定，只用常量时间比对，轮末即失效（最长 1 小时）。列出的正是这一轮该看到的工具，规则和 API 模型完全一样（主管的管理工具、成员的权限、群组和私聊的差别）。Claude Code 用 `--mcp-config <json>`（含地址和 `Authorization` 头）加 `--strict-mcp-config`，并用 `--allowedTools mcp__cubes__<工具名>` 放行这些工具；Codex 用 `-c mcp_servers.cubes.url=…` 和 `-c mcp_servers.cubes.http_headers=…`。给 CLI 的提示词里会说明这些工具可用。
- 每次 MCP 调用都走和 API 模型同一条路：会话里出现一条行动行（「正在创建成员」→「创建了成员「X」」），计入这一轮和这次任务的工具预算，需要确认的工具（如 `delete_agent`）会停下来等你在卡片上点，停止任务会连同这一步一起结束；`runs.tool_calls` 照常累加。权限仍在 CLI 自己手里：以 `acceptEdits` 权限模式运行，能改自己目录里的文件，需要人确认的操作在非交互模式下直接拒绝，bot 不会拿到比你给这个 CLI 更大的权限。
- 会话按 (bot, 会话) 续用同一个 CLI 会话：第一轮 Claude Code 用 `--session-id <uuid>`（Codex 从 `thread.started` 事件里拿 id），之后用 `claude --resume <id>` / `codex exec resume <id>`，只把「上次之后新增的内容」发过去（bot 自己说过的回复不再重发；别人说的话都发）。会话 id 和已发送历史的摘要在数据库 `cli_sessions` 表里。
- 这些情况会重新开一个会话，并把完整历史一次性交给它：会话被回退 / 编辑（历史开头不再是上次那份）、历史被压缩、bot 换了模型或思考强度、CLI 说找不到要 resume 的会话。第一条消息的 system 提示词通过 `--append-system-prompt` 追加（Codex 没有这个参数，写在提示词最前面），历史按「用户：」「助手：」的转录交给它。
- 附件也交给 CLI，不是只留占位：Claude Code 用 `--input-format stream-json`，stdin 是**一行** stream-json 用户消息，内容是转录的文本块加上按原位置排好的图片块（`image`）和 PDF 块（`document`），所以它真的能看图、读 PDF；超过上下文预算的老图片 / 老 PDF 只留带 id 的标签。Codex 没有 stdin 富内容，图片在每一轮写进一个临时目录（按类型带 `.png`/`.jpg`/`.gif`/`.webp` 后缀，轮末连同失败和停止一起删掉）并以 `--image=<路径>` 传进去，提示词里原位置保留 `[图片 att_…]` 标签；PDF 按 `附件/<原文件名>`（重名自动加「 (2)」）拷进这个 bot 的工作目录，提示词里写明这个相对路径，让它自己打开读。文本文件对两者都已经是转录里的正文，不用另传。
- 模型能力按协议本身算：Claude Code 标「图片 + PDF」；Codex 的图片看它模型清单里的 `input_modalities`（没写就当能看），PDF 也算能读（所以 PDF 会进 `附件/`，而不是只留一句「读不到」）。
- 取消（停止任务）会把整个进程组杀掉（先 SIGTERM，5 秒后 SIGKILL），不会留下跑着的 CLI。单次回复最多跑 30 分钟。
- 失败会换成中文说明：没装命令、没登录、额度用完/限流、非零退出（带 CLI 自己的报错）都直接显示在这次回复里。用量（输入/输出/缓存 token）按 CLI 报告的数字记账；Claude Code 的输入 token 包含它自己那段很长的系统提示。
- 找不到命令时可以用 `CUBES_CLAUDE_BIN` / `CUBES_CODEX_BIN` 指定可执行文件路径。
