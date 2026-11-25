# 接口速查

完整字段以代码和 `/docs`（OpenAPI）为准。

## 接口概览

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 本地服务和默认模型的状态：`configured`、`model`、`base_url`、`provider_name`、`configuration_error`、`auth`，不返回密钥 |
| `GET /workspace` | 负责人、成员、会话和正在运行的任务（`active_runs`；同一会话可以同时有 `lane` 为 `work` 和 `reply` 的多条线）；`onboarded` 为 false 时客户端显示首次见面页 |
| `POST /onboarding` | 首次见面 `{name, appearance?}`：给负责人改名、存下外观（和 `PATCH /agents/{id}` 用同一套校验），把负责人私聊标题一起改掉，置 `meta.onboarded` 并推送 `workspace` 事件；重名 409，外观非法 422 |
| `GET /providers` | 全部模型服务、默认模型和备用模型；每个服务只返回 `api_key_hint`（形如 `sk-…abcd`），永远不返回 key。`model_caps` 是每个列出模型的解析结果 `{模型: {images, video, audio, pdf, context_window, max_output, reasoning, reasoning_levels, sources}}`，`sources` 的每个字段是 `user` / `provider` / `catalog` / `default` 之一，说明这个值是从哪一层来的；`model_overrides` 是用户自己的 `{模型 或 "*": {images, context, output}}`。CLI 协议还会带 `model_details`：每个模型在这台机器上的 `label`、`description`、`efforts`、`default_effort` 和 `current`（CLI 当前配置的是不是它），读一次缓存 60 秒，读不到就是空数组。`engines` 是两个本机 CLI 的状态 `{kind, label, installed, ready, detail, command, version}`，读起来不花模型额度 |
| `POST /providers` | 新建模型服务（OpenAI 兼容、Anthropic Messages，或 `claude-code` / `codex`）；重名返回 409，字段非法返回 422（中文）；表里第一个服务自动成为默认。HTTP 协议建好后会顺手读一次它的 `GET /models`，把图像/视频/音频、窗口、输出上限和思考能力存成服务商那一层；读不到不影响创建。CLI 协议不需要地址和 key：模型列表是「默认」加上这台机器上 CLI 报告的全部模型（新建时自动读一次，读不到就只有「默认」） |
| `PATCH /providers/{id}` | 改名称、协议、地址、模型列表和 `model_overrides`（`{模型 或 "*": {images, context, output}}`，字段省略或为 `null` 表示自动）；`api_key` 不传则保留，传空串则清空；地址、key 或模型列表变了会重新探测一次能力；默认模型被移出列表时改用该服务的第一个模型 |
| `DELETE /providers/{id}` | 删除模型服务；它是默认时改选剩下的第一个服务，没有剩下的就清空默认 |
| `PUT /providers/default` | 设置默认模型 `{provider_id, model}`；模型不在列表里返回 422 |
| `PUT /providers/fallback` | 设置备用模型 `{provider_id, model}`；两个字段都为空表示关闭，模型不在列表里返回 422 |
| `POST /providers/test` | 用表单值发一次最小请求，返回 `{ok, latency_ms, supports_tools, detail}`；带 `id` 且不传 `api_key` 时复用已存的 key。CLI 协议不发请求，只报这个命令是否装好、是否登录 |
| `POST /providers/models` | 拉上游模型列表：OpenAI 走 `GET {base}/models`，Anthropic 走 `GET {base}/v1/models`；失败返回 502。返回 `{models, caps}`，`caps` 是同一份答案解析出的能力（已按「你的设置 → 服务商 → models.dev → 默认」解析好，带 `sources`）；带 `id` 时顺手把这些能力存进那个服务。CLI 协议返回 `["默认", …这台机器上的模型]`，不花模型额度 |
| `POST /attachments` | 原始请求体上传一个附件，可选头 `X-Attachment-Name`（`encodeURIComponent` 过的原文件名）；后端按字节判断类型：图片（PNG/JPEG/WebP/GIF，≤ 5 MB）、PDF（`%PDF-` 开头，≤ 20 MB）、UTF-8 文本（无 NUL，≤ 20 MB）。返回 `{id, mime, size, name}`；不支持的类型 415，太大 413 |
| `GET /attachments/{id}` | 取回附件原始字节，`Content-Type` 是它真实的类型；有文件名时带 `Content-Disposition: inline; filename*=UTF-8''` |
| `GET /events?conversation_id=` | 工作区 SSE；先发 `snapshot`，之后推送所有任务的实时事件，连接不会自己结束 |
| `GET /conversations/{id}` | 一个会话的全部消息；带 `limit`（1 到 200）时只返回最新的一页：`before` 给出消息 `seq` 则返回比它更早的，按时间正序，并带 `has_more`（还有更早的）和 `references`（这一页引用的、但不在页内的被回复消息，供引用行显示）。不带 `limit` 时消息全量返回，`has_more` 为 false、`references` 为空。定时任务的后台会话按不存在处理（404） |
| `POST /conversations/{id}/read` | 记住这个会话已经读到哪条消息 `{seq}`；之后新到的机器人回复才算未读，侧栏圆点清掉，并推送 `workspace` |
| `POST /conversations/{id}/runs` | 启动一次发送，可带 `mentions` 和 `reply_to_id`（回复某条成员消息；不存在、不在本会话或不是成员消息时返回 422）。`mode` 决定会话已经在工作时这条消息怎么办：`parallel`（默认）在正在做的工作线旁边另开一条回复线，工作线不停；`queue` 先存成一条 `status: "queued"` 的消息和一次 `status: "queued"` 的运行，等有空位再开始；`interrupt` 停下当前所有线再回答新消息。一个会话最多 4 条线，每位成员最多 2 条，超过就排队。不传时按这位成员的 `send_mode`、再按团队的 `send_mode` 决定（旧的 `steer` 已迁移为 `parallel`）。返回这次消息对应的运行，带 `lane`（`work` 或 `reply`；排队时 `status` 是 `queued`） |
| `DELETE /runs/{id}` | 撤回一条还在排队的发送：删掉那条消息和它的运行，推送 `message_removed`；已经开始或已结束的运行返回 409，不存在返回 404 |
| `POST /conversations/{id}/messages/{mid}/edit` | 改一条自己发的消息 `{content}`：删掉这一条和它后面的全部内容（连带工具行、图片绑定、摘要、只存在于后半段的运行），按新文本重新开始一次任务并把这条消息原来的图片带过去；不是自己的消息或行动行 422，会话正在运行 409，私聊 403，没有这条消息 404。返回新任务 |
| `POST /conversations/{id}/messages/{mid}/rewind` | 删掉这条消息之后的全部内容，这条保留；会话正在运行 409。返回这个会话剩下的全部消息（`has_more` 为 false），其他窗口收到 `message_removed` 与 `workspace` |
| `POST /conversations/{id}/messages/{mid}/reroute` | 重新派发一条自己发的群消息 `{speakers: [成员 id]}`：正在回答它的运行先停下，再用这几位重新回答，不新写用户消息；这条消息的 `route` 改成 `by: "user"`，`intents` 清掉。不是群组 400、不是自己的消息 422、有人不在群里 422、这条还在排队 409、会话正忙 409。返回新运行 |
| `GET /conversations/{id}/tasks` | 这个聊天开的任务（群组和私聊都算），最新在前：负责人、状态、线程、步骤、卡片消息和最近几步；不存在或不是群组/私聊返回 404 |
| `POST /conversations/{id}/tasks/{task_id}/now` | 「现在就做」：让一条排队中的任务不等这位成员手上的活，立刻在自己的线程里开始（放开这一位成员的并行限制；聊天的 4 条线、团队同时任务数仍要放得下）。返回启动后的任务；不在排队、线程正忙、聊天或团队已经满了返回 409，聊天里没有这个任务返回 404 |
| `POST /conversations/{id}/invite` | 确认一条通知上的「拉进群」`{agent_id, message_id?, notice_id?}`：把成员加进群并发一行「已把 X 拉进群」，清掉那条通知的 `invite`；带 `message_id` 时启动一次运行，让这位成员回答那条被 @ 的消息（`route_override` 只有它）。不是群组或成员不存在 404、群正在回复 409。返回这次运行 |
| `GET /webhooks?target_kind=&target_id=` | 一个定时任务或工作流名下的 Webhook（`{webhook: {id, target_kind, target_id, path, created_at, last_called_at}}`，没有时是 `null`）；永远不带密钥 |
| `POST /webhooks` | 给一个目标创建 Webhook `{target_kind: "schedule"\|"workflow", target_id}`；返回里带唯一的 `secret`（只此一次）；目标不存在 404，已经有 Webhook 409 |
| `POST /webhooks/{id}/rotate` | 换一个密钥（网址不变，旧密钥立刻失效），返回同样的带 `secret` 的形状；不存在 404 |
| `DELETE /webhooks/{id}` | 删除 Webhook，之后它的网址 404；不存在 404 |
| `POST /hooks/{id}` | 公开的触发端点：`Authorization: Bearer <密钥>` 或 `?token=<密钥>`；请求体可选（JSON `{"input"}` 或纯文本，≤64 KB）；202 返回本次运行的 `run_id`（定时任务排队时 `status: "queued"`），密钥不对 401，5 秒内重复 429，目标停用 409、已删掉 404 |
| `POST /cli-tools/{token}` | 给本机 CLI 用的 MCP 端点（streamable HTTP，JSON-RPC `initialize` / `tools/list` / `tools/call`）。`token` 是后端为某一轮 CLI 运行临时发的，自带凭证所以不需要启动令牌（和 `/hooks/` 一样）；令牌不对或过期 401。列出的工具正是那一轮该看到的工具，一次 `tools/call` 和 API 模型的工具调用走同一条路（行动行、预算、确认卡、停止） |
| `PUT /messages/{id}/reactions/{emoji}` | 给一条消息贴表情（`emoji` 是 URL 编码的路径段）；幂等。消息不存在 404，表情不合法或消息不能贴（正在生成、行动行、后台会话）422，成为第 21 个不同表情时 409。返回 `{message_id, conversation_id, reactions}`，推送 `reactions` 事件；消息作者是仍存在的机器人时，安排一次防抖的表情回应 |
| `DELETE /messages/{id}/reactions/{emoji}` | 移除用户自己贴的某个表情；幂等（本来就没有也返回当前列表）。消息不存在 404。推送 `reactions` 事件，并取消一个已经无事可回应的待定表情回应 |
| `GET /workflows` | 全部工作流，最新改过的在前；每条带所有者名字、步骤、版本、启用状态、跑过几次 |
| `POST /workflows` | 新建 `{name, description?, steps[], edges?, enabled?, owner_agent_id?}`；`edges` 省略时按节点顺序连成一条链。节点是 `{id?, title?, instruction, kind?, gate?, on_fail?, choices?, x?, y?}`，连线是 `{from, to, when?, max_loops?}`（`when` 只能是分支节点的选项；`max_loops` 1–20，只能写在绕回前面节点的连线上）。`owner_agent_id` 省略时归主管；同一机器人名下重名 409；往前走的连线成环、循环线没有绕回去、有落单节点、分支没连出去、`when` 对不上选项都返回 422 |
| `PATCH /workflows/{id}` | 改 `name`、`description`、`steps`（整套节点）、`edges`（整套连线）、`enabled`、`owner_agent_id`；改名字或图会把 `version` 加一 |
| `DELETE /workflows/{id}` | 删除工作流；它名下的运行记录一起删除，已经跑出来的私聊消息保留 |
| `GET /workflows/{id}` | 定义，外加 `markdown`（这份计划的 `WORKFLOW.md` 渲染）和 `runs`（最近 10 次运行） |
| `POST /workflows/{id}/run` | 开跑 `{input?}`：新建一个工作流页面并开始，返回 `{workflow_run, conversation_id}`（`conversation_id` 就是这个页面）；停用 409，这个工作流已经在跑 409，暂停着的则从原处继续（返回同一个 `workflow_run.id`） |
| `GET /conversations/{id}/workflow-run` | 这个会话最近的一次工作流（页面跑完也还带着它），`{workflow_run}` 或 `{"workflow_run": null}`；页面的流转条用它恢复，重连的 `snapshot` 事件也带着它 |
| `GET /workflow-runs/{id}` | 一次运行：状态（`queued`/`running`/`waiting_user`/`paused`/`done`/`failed`/`stopped`）、游标、这次运行复制下来的 `steps` / `edges`、每条循环线已经绕了几轮（`loops`），以及每个节点的状态、尝试次数、产出、它选的分支选项、第几轮（`round`）和之前各轮的结果（`history`） |
| `POST /workflow-runs/{id}/pause` | 暂停；活着的运行在下一个步骤边界停下（不打断正在做的那一步），停着的直接改状态 |
| `POST /workflow-runs/{id}/resume` | 从游标继续；只有 `paused` 能继续，其他状态 409 |
| `POST /workflow-runs/{id}/stop` | 停止并结束这次运行 |
| `POST /workflow-runs/{id}/answer` | 回答 `ask_user` 那一步 `{input}`，它就成为该步的产出并继续；没在等你回答时 409 |
| `POST /workflow-runs/{id}/nodes/{node_id}/{retry\|skip}` | 重跑或跳过某个节点（路径里也可以写序号，按这次运行的图解析）；活着的运行在节点边界生效，停着时直接改行 |
| `GET /runs/{id}` | 任务详情：状态、当前步骤、模型/工具调用次数、排队和接力位置，以及本次任务的 token 用量 `input_tokens`、`output_tokens`、`cached_tokens`（模型服务报告的所有调用之和，模型不报告时为 0）和压缩次数 `compactions`，以及 `lane`（`work`/`reply`） |
| `POST /runs/{id}/stop` | 停止任务，保留已生成内容 |
| `POST /agents` | 新增成员；重名返回 409 |
| `PATCH /agents/{id}` | 修改成员名称、职责、外观和模型；`appearance` 含 `shape`、`color` 和可选的 `eye`：`color` 是目录里的颜色或任意 `#rrggbb`，`eye` 是任意 `#rrggbb`，不传或传 null 时自动挑一个和身体对比清楚的眼睛颜色；`appearance` 传 null 时恢复默认外观；旧的 `expression` 字段会被接收、校验后丢弃；`model_ref` 为 `{provider_id, model}` 或 null（跟随默认），指向不存在的服务或不在列表里的模型时返回 422；CLI 协议还可带 `effort`（思考强度，必须是该模型支持的档位，否则 422），成员返回值带 `model_status`（`default`/`custom`/`fallback`）；`send_mode` 是这位成员忙时你发的消息怎么处理（`parallel` / `queue` / `interrupt`；旧值 `steer` 迁移为 `parallel`。null 跟随团队的 `send_mode`）；重名返回 409 |
| `POST /agents/{id}/direct` | 幂等返回该成员的私聊会话 |
| `GET /agents/{id}/activities` | 这位成员的工作记录：每次发言（一轮活动）的最新在前，可按 `started_at` 用 `before` 往前翻（`limit` 默认 50），返回 `{activities, has_more}`；每条带标题、结论、状态、当前步骤、步数、起止时间和最终的答复消息 id，以及 `todos`、`todo_done`、`todo_total` 和 `lane` |
| `GET /activities/{id}` | 一条活动的详情：`activity`（同上）、`conversation_title`、`answer`（最终答复消息，可能为 null）、`notes`（别的线给这条工作留下的备注，`{kind,text,source_message_id,created_at}`，`kind` 为 `context`/`request`）和 `sections`——先是这一轮自己，再是它问过的同事或开出的任务线程；每个分组带 `messages`（这一轮写下的全部消息，格式与会话接口相同、从旧到新，含工具详情和附件）和 `steps`（每步带 `kind`（`tool`/`note`/`ask`/`handoff`）、`title`、`subtitle`、`status`、`message_id`、`action_id` 和指向分组的 `target_activity_id`）；不存在返回 404「这条记录不存在。」。运行中靠 `activity` 事件更新（每秒最多一次，状态一变立刻推） |
| `DELETE /agents/{id}` | 删除成员及其私聊；主管（409）、正在回复的成员（409）、不存在的成员（404）会被拒绝。删除后推送 `workspace` 与 `sidebar` 事件 |
| `POST /actions/{id}/confirm` | 回答等待中的行动（如删除成员）：`approve` 为 true 表示同意；再加 `always: true` 表示这次同意并记住一条规则，以后这位成员的同种操作不再出卡。返回处理后的行动；已处理过返回 409 |
| `GET /approval-rules` | 已记住的「总是允许」规则：工具名、中文名 `title`、成员和创建时间 |
| `POST /approval-rules` | 手动加一条规则：`{tool_name, agent_id?}`（不给 `agent_id` 表示所有成员）；成员不存在返回 404 |
| `DELETE /approval-rules/{id}` | 删掉一条规则，这种操作又会需要确认；不存在返回 404 |
| `GET /conversations/{id}/context` | 这个会话的上下文用量，按一次真实请求算：`tokens`、`estimated`、`measured`（模型上次报告的提示词大小）、`window`、`usable`、`threshold` 和 `threshold_percent`（自动压缩点）、`output_reserve`、`buffer`、`free`、`percent`，代表这个会话回答的成员 `agent_id`/`agent_name`、`model`/`provider`，以及 `categories`——`system`（系统提示词）、`memory`、`skills`、`tools`、`user`、`reply`、`calls`、`results`（每种工具一个 `items` 子项）、`summary` 各占多少（合计等于 `tokens`）；后台会话 404，没有模型时 409 |
| `POST /conversations/{id}/compact` | 立刻压缩一次（和自动压缩同一个动作，不受阈值限制，最近 6 轮原样保留），返回压缩后的用量和 `before`/`after`；压缩开始和结束时推 `context` 事件；这一轮回复还在跑时返回 409 |
| `POST /actions/{id}/answer` | 回答 `ask_user` 的问题组：`{answers:[{question_index, labels, other}], skip}`，`labels` 必须是该题给出的选项，`other` 是「其他」里自己写的话；不给 `skip` 时每一题都要有答案。这次任务从原处继续，返回处理后的行动（`summary` 逐题连成「标签：答案」）；不是问题、已经答过返回 409，没有这条行动返回 404 |
| `PATCH /conversations/{id}` | 修改群组标题、目的或成员；会话正在运行时返回 409 |
| `DELETE /conversations/{id}` | 解散群组及其聊天记录；非群组（409）、正在回复的群组（409）、不存在的群组（404）会被拒绝。解散后推送 `workspace` 与 `sidebar` 事件 |
| `GET /sidebar` / `PUT /sidebar` | 读取或整体替换侧栏布局：哪些置顶、哪些在哪个分组、各自顺序。布局变化会推送 `sidebar` 事件 |
| `POST /sidebar/folders`、`PATCH`/`DELETE /sidebar/folders/{id}` | 新建、重命名、删除分组；同名返回 409；删除分组后，组里的机器人回到不分组 |
| `GET /avatars/{seed}.png` | 成员头像 PNG（iOS 端使用） |
| `GET /avatars/{seed}/face.json` | 成员头像的矢量分层（iOS 端使用） |
| `GET /notes` / `GET /notes/{id}` | 团队笔记的列表（标题、摘要、作者、更新时间）与完整正文；变化会推送 `notes` 事件 |
| `DELETE /notes/{id}` | 删除一篇笔记，删除后不可恢复；不存在返回 404。删除后推送 `notes` 事件 |
| `GET /tasks` | 任务清单：标题、说明、状态、负责人、进度说明；变化会推送 `tasks` 事件 |
| `PATCH /tasks/{id}` | 手动改任务状态（`todo`/`doing`/`done`/`blocked`）或写进度说明；不存在返回 404，状态不合法或说明超过 4000 字返回 422。改完推送 `tasks` 事件 |
| `GET /memories` | 全部记忆（含已替换、已忘记的旧条目，最新的在前）与设置 `{paused, auto_extract, core_chars}`（默认 `false`、`true`、`1200`）；变化会推送 `memories` 事件 |
| `POST /memories` | 手动添加一条 `{content, topic?, scope, agent_id?}`：`scope` 只能是 `user`、`agent`（必须带一个存在的 `agent_id`，否则 404）或 `team`；内容为空、范围不对或成员缺 `agent_id` 返回 422。新条目 `origin` 为 `user` |
| `PATCH /memories/{id}` | 改 `content` 会新建一条并退役旧条（旧条 `status` 为 `retired`、`valid_to` 为现在、`superseded_by` 指向新条），新条沿用钉住、范围与所属成员；只改 `topic` 或 `pinned` 仍是同一条；不存在返回 404。改完推送 `memories` 事件 |
| `DELETE /memories/{id}` | 物理删除一条（无论是否已退役），连同全文索引一起消失；不存在返回 404。删完推送 `memories` 事件 |
| `PATCH /memories/settings` | 改 `paused`（暂停后既不注入也不写入）或 `auto_extract`（对话结束后是否自动整理），返回整份状态并推送 `memories` 事件 |
| `GET /team/export` | 团队结构导成一个 Markdown 文件（front matter + 成员/分组/工作流/技能各一节），带 `Content-Disposition` 下载头；不含聊天、记忆正文、API key、令牌或 Webhook 密钥 |
| `POST /team/import` | `{markdown, apply, overwrite_supervisor}`：默认只预览会新建/改名/跳过的成员、分组、工作流和本机缺失的技能；`apply=true` 时写入（冲突名字加「（2）」），默认不改主管 |
| `GET /schedules` | 全部定时任务（触发规则、下次触发时间、所在会话、是否启用、上次结果）加默认时区和上限（最多 20 个启用、最少 5 分钟一次）；变化会推送 `schedules` 事件 |
| `POST /schedules` | 新建定时任务：`kind` 为 `once`（带 `at`，形如 `2026-10-24T09:00`）、`interval`（带 `every_minutes`）或 `cron`（带 5 段表达式），可选 `timezone`、`use_computer`、`enabled`；规则不合法返回 422，会话不存在或后台会话 404，私聊不能作为目标（422），启用数超限 409 |
| `PATCH /schedules/{id}` | 修改规则、名称、内容、目标会话，或用 `enabled` 暂停/恢复；不存在返回 404 |
| `POST /schedules/{id}/run` | 立即触发一次：在任务自己的后台会话里现在开始，上一次还没结束就先排队；返回该定时任务。每条定时任务带 `running`：正在进行时是 `{run_id, step, started_at, model_calls, tool_calls}`，空闲时为 null，进行中每秒最多随 `schedules` 事件更新一次 |
| `GET /schedules/{id}/run` | 最近一次运行的详情（只读）：`run` 和它在后台会话里留下的全部消息（指令、每一步、回复）；还没运行过时 `run` 为 null |
| `POST /schedules/{id}/stop` | 停止正在进行的那次运行；被停止的运行不发总结；返回该定时任务 |
| `DELETE /schedules/{id}` | 删除定时任务和它在后台留下的记录，之后不再触发；不存在返回 404 |
| `GET /skills` | 全部技能（按名称排序）：`id`、`name`、`description`、`source`（`user`/`agent`/`upload`/`github`）、`source_url`、`author_id`、`author_name`、`enabled`、`has_scripts`、`file_count`、`use_count`、`last_used_at`；变化会推送 `skills` 事件 |
| `GET /skills/{id}` | 一个技能的详情：上面的字段加 `body`（SKILL.md 正文，不含 frontmatter）和 `files`（除 SKILL.md 外按路径排序的 `{path, size}`）；不存在返回 404 |
| `POST /skills` | 自己写一个技能 `{name, description, body}`：`source` 为 `user`、`enabled` 为 true；名称非法返回 422，重名返回 409 |
| `POST /skills/upload` | 原始请求体上传一个 zip（`Content-Type: application/zip`）或单个 SKILL.md（`text/markdown`）；`source` 为 `upload`、`enabled` 为 false，超限返回 413 |
| `POST /skills/github` | 从 `{url}` 导入一个 GitHub 技能（`https://github.com/<owner>/<repo>` 或 `…/tree/<分支>/<路径>`）；`source` 为 `github`、`enabled` 为 false，下载失败返回 502 |
| `GET /skills/local` | 本机其他应用的技能文件夹（见上面的目录列表），每个 `{path, app, name, description, installed, error}`；同一个技能被两个应用链到同一处时只列一次 |
| `POST /skills/local` | 拷入 `{path}`（必须是 `GET /skills/local` 列出的文件夹）；`source` 为 `local`、`enabled` 为 false，文件夹里有链接或设备文件返回 422，重名 409，超限 413 |
| `PATCH /skills/{id}` | 改 `enabled`、`description` 或 `body`（会重写 SKILL.md，名称不可改）；不存在返回 404，非法返回 422 |
| `DELETE /skills/{id}` | 删除技能及其文件夹和记录，并把它的名字从每位成员的 `skills_disabled` 里去掉；不存在返回 404 |
| `GET /computer` | 沙盒电脑的状态（是否连接、名字、系统、屏幕尺寸、实时画面地址、谁在用、最新截图）和配对令牌；状态变化推送 `computer` 事件（不含令牌） |
| `POST /computer/stop` | 停止正在用沙盒的任务 |
| `WS /computer/agent` | 沙盒里连接程序的接入点；`Authorization: Bearer <配对令牌>`，错误令牌以 4401 关闭，旧版本（操作 Mac 的第 1 版、只能看屏幕的第 2 版）以 4426 关闭，新连接接管旧连接（旧的以 4409 关闭） |

事件流异常关闭时，客户端显示错误并提供手动重新加载，没有隐藏的自动重试。
