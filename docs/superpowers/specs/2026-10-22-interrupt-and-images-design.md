# 回复中插话与图片输入

两个子项目，按顺序交付：先做 A（插话打断），再做 B（图片）。A 不碰数据格式以外的模型调用，B 依赖 A 里改过的上下文构建。

## 已定的选择

- 回复进行中发送新消息：立即打断当前任务，再按新消息重新回答。
- 模型能否看图：在模型服务设置里按模型手动标记「支持图片」。
- 历史图片：只把最近 4 张原样发给模型，更早的换成文字占位。
- 机器人之间可以互发图片：私信、群消息和普通回复都能附上它看得到的图片。

## A. 回复中插话

### 现状

- `POST /conversations/{id}/runs` 遇到运行中的任务直接返回 409（`main.py:545`）。
- 输入框在任务运行时整体禁用，发送按钮变成停止按钮（`Composer.tsx:47,179,182`）。
- 停止是协作式的：`engine.stop()` 置 `stop_requested`，流式读取每 0.25 秒检查一次，超过 `STOP_TIMEOUT`（15 秒）才硬取消。
- 构建模型上下文时，`_to_chat_history` 丢弃所有 `status != "complete"` 的消息（`engine.py:1588`）。被停掉的半截回复，模型下一轮完全看不到。

### 行为

1. 任务运行中，输入框可以输入、可以发送。占位文字改为「发送会打断当前回复」。
2. 按钮：有草稿时是发送，草稿为空时是停止。Enter 发送。
3. 发送时，后端先打断当前任务，再用新消息开始新任务。前端不再预先拦截「正在回复」。
4. 被打断的回复保留已输出的文字，状态是 `interrupted`，作者行显示「已停止」，和点停止按钮一样。
5. 打断会停掉整个任务，包括正在运行的工具、嵌套的私聊和等待确认的操作。等待确认的操作按现有逻辑失败，原因是「任务已停止。」。

### 后端

- `start_run` 在冲突检查处不再返回 409，改为：
  1. 按会话取一把 `asyncio.Lock`，锁住「打断旧任务 + 创建新任务」整段，避免两次连发互相穿插。
  2. 有运行中的任务时，调用 `engine.stop(run_id, timeout=INTERRUPT_STOP_TIMEOUT)`，其中 `INTERRUPT_STOP_TIMEOUT = 3` 秒。超时后硬取消，和现有兜底一样。
  3. 如果没有进程内任务认领这个 run（`stop()` 返回 `False`），沿用 `stop_run` 路由里那段收尾：标记流式消息为中断，操作失败，run 置为 `stopped`。这段收尾抽成一个函数，两处共用。
  4. 调用 `store.begin_run`，用户消息带上 `interrupts=True`。
- `engine.stop` 新增可选参数 `timeout: float | None`，缺省仍用 `settings.stop_timeout`。
- `messages` 表新增列 `interrupts INTEGER NOT NULL DEFAULT 0`，走 `COLUMN_UPGRADES`。`MessageOut` 新增 `interrupts: bool = False`。
- 原来的 `EngineBusy` 兜底保留：如果加锁后仍撞上，说明有 bug，照旧返回 409。

### 让模型知道自己被打断

`_to_chat_history` 改两处：

- 被打断的消息（`interrupted`，且内容非空）不再丢弃。
  - 作者自己的消息：作为 assistant 内容，末尾追加「（这条回复还没说完就被打断了。）」。
  - 其他成员的消息：照现有格式 `[名字] 内容`，末尾追加同一句。
- 带 `interrupts` 的用户消息：在 `_user_turn_content` 的结果前加一行「（这条消息发出时上一轮回复还没结束，已被它打断。）」。即使被打断时还没输出任何文字（还在思考或调用工具），模型也能知道。

`streaming` 状态的消息照旧丢弃。

### 前端

- `Composer.tsx`：`busy` 只控制占位文字和按钮形态，不再禁用输入框。`canSend = (草稿非空 || 有图片) && !sending && !uploading`。
- `store.send()`：删掉 `runFor` 的拦截，409 的处理不变。
- 新任务的 `run` 事件和旧回复的 `message_end`（interrupted）都通过 SSE 到达，现有的 `applyRun`、`applyMessage` 已经能处理。

### 测试

- 运行中发送：旧 run 变成 `stopped`，旧消息 `interrupted` 并保留文字，新 run 正常完成，接口返回 202。
- 新一轮发给上游的请求里，包含带「还没说完就被打断了」的 assistant 内容，以及带打断提示的用户消息。
- 被打断时旧回复还没有任何文字：新请求里只有用户消息上的打断提示。
- 两个请求几乎同时发送：两者都返回 202，最后只有一个 run 在运行，没有 409。
- 现有断言「运行中发送返回 409」的测试（`test_team_workspace.py:341-355`）改成上面的新行为。

## B. 图片

用户能发图片给机器人，机器人之间也能互相转发图片。机器人不能生成新图片，只能引用它在对话里看到过的图片。

### 现状

- 用户消息只有文字，`RunStartRequest.content` 最多 2000 字（`models.py:656`）。
- 发给模型的历史只有字符串。OpenAI 适配器把消息原样发出；Anthropic 适配器的 `_text_of` 只取文字部分，图片会被丢掉（`anthropic_stream.py:34,139`）。
- 机器人之间传话有两种方式：`direct_message` 私信，主管还可以用 `post_group_message` 发群消息。两者都只有 `content` 文本。
- 回复末尾的 `<mood>值</mood>` 标签由 `MoodStream` 在流式输出时扣下，不会显示给用户。这是给回复附加结构化信息的现成做法。
- 没有任何附件存储。头像缓存放在数据库旁边的 `avatars/` 目录，这是现成的先例。
- 所有请求都要带 `Authorization` 头，`<img src>` 带不了这个头。CSP 的 `img-src` 允许 `data:`，不允许 `blob:`。
- 依赖里没有 `python-multipart`，也没有 Pillow。按约束不加新的运行时依赖。

### 交互

- 加图片的三种方式：在输入框里粘贴，把图片拖到聊天区或输入框，点输入框左侧的图片按钮选择文件。
- 缩略图显示在输入框胶囊内部、文字上方，56px 一张，和引用条同一个位置规则。每张右上角有移除按钮，上传中显示进度遮罩。
- 每条消息最多 4 张。只有图片、没有文字也可以发送。
- 用户气泡里，图片在文字上方。机器人消息里，图片在文字下方。两者用同一个网格：1 张按原始比例显示，最大 320px；2 到 4 张是两列方格。点击图片全屏查看，Esc 或点背景关闭。
- 当前回答的机器人所用模型没有标记支持图片时，图片照样可以发，输入框下方显示一行提示：「「名字」用的模型看不到图片内容，它只能把图片转给能看图的成员。」。
  - 这样主管即使不能看图，也能把你的图片转给能看图的成员。
  - 「当前回答的机器人」按第一个回答者判断：私聊是对方，群里点名了谁就是谁，否则是主管。复用 `firstResponder`。

### 存储

一张图片可以出现在多条消息里（用户发的原图、主管转发的那条私信、成员回复里引用的那条），所以图片和消息是多对多关系。

- 新表 `attachments`，一行是一张上传过的图片：

| 列 | 说明 |
| --- | --- |
| `id` | `att_` 前缀的 12 位随机 ID |
| `mime` | `image/png`、`image/jpeg`、`image/webp`、`image/gif` 之一 |
| `size` | 字节数 |
| `sha256` | 文件内容哈希，也是文件名 |
| `created_at` | ISO 时间 |

- 新表 `message_attachments`，一行是「某条消息带了某张图」：

| 列 | 说明 |
| --- | --- |
| `message_id` | 引用 `messages(id)`，`ON DELETE CASCADE` |
| `attachment_id` | 引用 `attachments(id)` |
| `position` | 在这条消息里的顺序，从 0 开始 |

  主键是 `(message_id, attachment_id)`，同一条消息里同一张图只出现一次。
- 文件存在 `database_path.parent / "attachments" / <sha256>`，同样内容只存一份。
- 启动时清理：删除创建超过 24 小时、但没有任何消息引用的 `attachments` 行，再删除没有任何行引用的文件。删除成员或解散群组时，消息被级联删除，图片在下次启动时一并清理。

### 机器人能引用哪些图片

一个机器人只能转发它看得到的图片：图片挂在某条消息上，而这条消息所在的会话里有这个机器人。主管是每个群的成员，也是主管私聊的成员，所以它能转发你发给它的图片，也能转发群里的图片。

不满足时：

- 工具调用直接失败，错误是「图片 att_xxx 不在你能看到的对话里，本次调用未执行。」，模型可以改正后重试。
- 回复里引用了不存在或看不到的图片，这个 ID 会被忽略，其余正常附上。

### 机器人怎么附图

两种场景，各用现有的做法：

- 工具：`direct_message` 和 `post_group_message` 新增可选参数 `image_ids: string[]`，最多 4 个。工具描述写明：「要附带的图片 ID，只能用对话里出现过的 att_ 开头的 ID」。
  - 新建的私信或群消息绑定这些图片。
  - 接收方的模型没有标记支持图片时，工具照常执行，结果里多一句「「名字」用的模型看不到图片内容，只能看到图片 ID。」，调用方可以改用文字描述。
  - `direct_message` 的结果里，除了对方回复的文字，再加上 `image_ids`：对方回复里附带的图片。调用方据此知道对方发回了哪些图。
- 调用方看图：工具结果只能是文字。如果对方回复带了图片，而调用方的模型支持图片，引擎在这批工具结果后面追加一条用户消息，内容是「[私聊里收到的图片]」，后面跟这些图片的标签和图片块。这条消息只在本次调用的上下文里出现，不写进数据库。它占用最近 4 张的名额。
- 普通回复：回复末尾可以在心情标签前单独一行写 `<images>att_a att_b</images>`。
  - `MoodStream` 改成识别末尾的这两个标签，流式输出时一起扣下，不显示给用户。
  - 回复完成时，解析出的有效 ID 绑定到这条回复。
  - 系统提示里，`MOOD_RULE` 前面加一段说明这个格式，并说明只在确实需要把图片发给对方时才写。

`ask_agent` 不加参数：主管要让群成员看图，先用 `post_group_message` 带图发到群里，成员回答时会在群历史里看到。

### 典型流程：主管在私聊里收发图片

1. 你在主管私聊里发一张图。主管看到 `[图片 att_a]`，模型支持图片时还能看到图片内容。
2. 主管调用 `direct_message`，把图发给设计助手：`image_ids: ["att_a"]`。私聊里这条消息带着图，设计助手能看到。
3. 设计助手回复时在末尾写 `<images>att_b</images>`，附上它想发回的图。`att_b` 必须是它看得到的图，比如它在别的会话里见过的。
4. 主管收到工具结果 `image_ids: ["att_b"]`。模型支持图片时，紧接着还能看到 `att_b` 的内容。
5. 主管回复你时写 `<images>att_b</images>`，这张图就出现在主管私聊里。主管是那条私聊的成员，所以有权转发。

### 接口

- `POST /attachments`：请求体就是图片原始字节，`Content-Type` 写图片类型。不用 multipart，所以不需要新依赖。
  - 用文件头字节判断类型，只认上面 4 种。和声明的 `Content-Type` 不一致时，以文件头为准。
  - 大小上限 5 MB，这是 Anthropic 单张图片的上限。超过返回 413「图片超过 5 MB，请压缩后再发。」。
  - 类型不对返回 415「只支持 PNG、JPEG、WebP 和 GIF 图片。」。
  - 返回 `{id, mime, size}`。
- `GET /attachments/{id}`：返回文件，带 `Cache-Control: private, max-age=31536000, immutable`。
- `RunStartRequest` 新增 `attachment_ids: list[str]`，最多 4 个。有附件时 `content` 可以为空。
  - 附件不存在，或已经挂在别的消息上，返回 422「图片已失效，请重新添加。」。用户只能发自己刚上传的图片，转发是机器人的事。
  - 超过 4 个返回 422「一条消息最多带 4 张图片。」。
- `store.begin_run` 在同一个事务里插入用户消息并写入 `message_attachments`。
- `MessageOut` 新增 `attachments: list[AttachmentOut]`，即 `{id, mime, size}`，按 `position` 排序。用户消息和机器人消息都有。

### 前端上传与显示

- 加入图片时先在本地处理：长边超过 2048px，或文件超过 4 MB 时，用 canvas 缩到长边 2048px。有透明通道的重新编码成 PNG，其余编码成 JPEG，质量 0.9。GIF 原样上传。
- `ApiClient.uploadAttachment(blob)` 发 `POST /attachments`。`ApiClient.attachmentBlob(id)` 带鉴权头取回文件。
- 显示用 `URL.createObjectURL`，按 id 缓存在一个模块级 Map 里。`tauri.conf.json` 的 `img-src` 加上 `blob:`。
- 窗口配置加 `"dragDropEnabled": false`。否则 Tauri 会截获系统的文件拖放，网页收不到 `drop` 事件。侧栏宽度调整和标题栏拖动窗口用的是指针事件和 `data-tauri-drag-region`，不受影响；改完在真实 App 里确认一次。
- 机器人消息的图片网格放在正文下方、复制回复按钮上方。私聊里同样显示。

### 模型是否支持图片

- `providers` 表新增列 `vision_models TEXT NOT NULL DEFAULT '[]'`：存支持图片的模型名，是 `models` 的子集。保存时自动去掉不在 `models` 里的名字。
- `ProviderIn`/`ProviderOut` 新增 `vision_models: list[str]`。`ResolvedModel` 新增 `vision: bool`。
- 设置里模型服务的表单中，每个模型标签多一个「图片」开关（图标按钮，`aria-pressed`），开启后高亮。
- 前端判断用 `agentModelLabel` 同一套解析，新增 `agentSupportsImages(agent, providers, health)`。

### 发给模型

`_to_chat_history(messages, self_id, vision)`，`vision` 来自当前说话机器人的 `ResolvedModel`。

每张图片前面都有一个文字标签 `[图片 att_xxx]`，这样不管模型能不能看图，都知道图片的 ID，可以拿来转发。

- 别人发的消息（用户，或以 `[名字]` 开头的其他机器人）：`content` 变成内容块。

```json
[
  {"type": "text", "text": "[设计助手] 这是改好的版本"},
  {"type": "text", "text": "[图片 att_3f9c2a1b7d04]"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}
]
```

- 机器人自己发过的消息：作为 assistant 文本，末尾带上 `<images>att_a att_b</images>`，和现在补上 `<mood>` 一样，让模型看到自己用过的格式。assistant 消息里不放图片块，因为多数接口不接受。
- 只有最近 4 张图片放图片块，从最新往前数，包括转发来的。更早的图片，或者当前模型不支持图片时，只留文字标签，改写成 `[图片 att_xxx，这里看不到内容]`。
- OpenAI 适配器不用改。Anthropic 适配器：用户消息里的 `image_url` 块，把 data URL 转成 `{"type": "image", "source": {"type": "base64", "media_type", "data"}}`，文字块照旧。
- `_enforce_context_limit` 只统计文字长度，图片块不计入。

### 测试

后端：

- 上传：4 种类型都能成功；文件头和 `Content-Type` 不一致时以文件头为准；超过 5 MB 返回 413；非图片返回 415；同样内容只存一个文件。
- 用户发送：图片挂到用户消息上；复用已发出的图片、数量超限、图片不存在都返回 422；只有图片没有文字也能发送；当前模型不支持图片时也能发送。
- 转发：
  - 主管用 `direct_message` 带上用户的图片：私聊里的消息带同一张图，接收方的上游请求里有这张图的图片块。
  - 引用不在自己会话里的图片：工具返回错误，不创建消息。
  - 接收方模型不支持图片：工具成功，结果里有提示，接收方请求里只有文字标签。
  - 成员回复末尾写 `<images>`：标签不出现在正文和流式增量里，图片挂到回复上，无效 ID 被忽略。
  - 私聊里对方回复带图：`direct_message` 的结果里有 `image_ids`；调用方模型支持图片时，下一次请求在工具结果后面有一条带图片块的用户消息，数据库里没有多出这条消息；不支持时没有这条消息。
  - 主管把私聊里收到的图用 `<images>` 发回给你：图片挂到主管私聊的回复上。
- 上游请求：OpenAI 协议有 `image_url` 块；Anthropic 协议有 `image` 块；第 5 张及更早的图片只剩文字标签；自己发过的消息带 `<images>` 文本而不是图片块。
- 启动清理：过期且没被引用的图片和孤立文件被删除，被引用的保留，包括只被转发消息引用的。

前端：

- 取模型图片能力的逻辑（默认模型、自定义模型、标记失效后回退）。
- 图片压缩的尺寸决策：什么情况下缩放、编码成什么格式。只测纯函数，不测 canvas。

## 不做

- 模型生成新图片。
- 除图片以外的文件。
- 自动探测模型是否支持图片。
