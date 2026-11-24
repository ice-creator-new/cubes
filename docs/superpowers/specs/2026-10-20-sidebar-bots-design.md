# 侧栏机器人与自研头像设计

日期：2026-10-20
范围：Web 与桌面端（`web/`）和共用后端（`backend/`）。iOS 本轮不改，继续使用 Blobatar。

## 目标

1. 侧栏改成「机器人」列表。每个成员私聊和每个群组都是一个机器人。用户可以右键置顶，可以手动新建分组并把机器人归类，默认不分组。
2. 用自研的头像引擎替换 Blobatar。头像根据回复内容和运行状态变化表情，外观可由用户自定义。

## 已定决策

| 问题 | 决定 |
|---|---|
| 覆盖客户端 | Web 与桌面端。数据存在后端，iOS 以后接入 |
| 头像风格 | 借鉴 bloub 的思路：一个实心形体加两只遮罩挖空的胶囊眼，参数化表情，径向轮廓变形。形状目录、配色、表情参数由 Cubes 自己定义，不使用 bloub 的测量常数，也不复刻 x.ai 的造型 |
| 表情如何响应内容 | 模型在每条回复末尾附一个心情标签 |
| 群组头像 | 沿用成员头像叠放；群组本身不能自定义外观 |
| 置顶区 | 可以置顶多个，横排大头像，满一行自动换行；默认置顶主管 |

## 1. 数据（后端）

SQLite 只加列、加表，不改写旧数据。

| 新增 | 内容 |
|---|---|
| `agents.appearance` | TEXT，存 JSON：`{"shape","color","expression"}`。为 NULL 时，客户端按 `avatar_seed` 确定性地派生 |
| `messages.mood` | TEXT，可为 NULL。取值见第 2 节 |
| 表 `sidebar_folders` | `id TEXT PK`、`name TEXT`（1–24 字）、`position INTEGER` |
| 表 `sidebar_items` | `conversation_id TEXT PK`（外键关联 conversations，级联删除）、`pinned INTEGER`、`pin_position INTEGER`、`folder_id TEXT NULL`（外键关联 sidebar_folders，删除时置 NULL）、`position INTEGER` |

规则：

- 侧栏条目以会话为单位，主管就是主管那条私聊。
- 没有出现在 `sidebar_items` 里的会话视为不置顶、不分组，按 `updated_at` 倒序排在不分组区。
- 数据库首次升级到这个版本时，把主管私聊写成置顶。只执行一次，用 `meta` 表里的一个键记录是否已执行。
- 删除分组时，组内条目的 `folder_id` 置 NULL，会话本身不受影响。

接口：

- `AgentOut.appearance: Appearance | null`。`PATCH /agents/{id}` 接受 `appearance`，取值不在目录内时返回 422。
- `MessageOut.mood: Mood | null`。
- `GET /sidebar` 返回 `{folders:[{id,name,position}], items:[{conversation_id,pinned,pin_position,folder_id,position}]}`。
- `PUT /sidebar` 用整份布局替换现有布局。引用了未知会话或未知分组时返回 422。整份写回，不存在排序冲突。
- `POST /sidebar/folders {name}` 新建分组，`PATCH /sidebar/folders/{id} {name}` 重命名，`DELETE /sidebar/folders/{id}` 删除。
- 布局变化通过现有 SSE 推送一条 `sidebar` 事件，其他窗口据此刷新。

外观目录写在后端常量里，前端保留一份同样的列表，用一个测试保证两边一致：

- 形状（8）：`round pebble squircle capsule bean hexagon cloud drop`
- 颜色（12）：`ink cream clay coral tangerine honey moss jade sky iris plum slate`
- 默认表情：取第 3 节表情目录中的静态表情。

## 2. 心情标注

- 在 `SUPERVISOR_SYSTEM`、`MEMBER_SYSTEM`、`MEMBER_DIRECT_SYSTEM` 末尾追加一条规则：回复的最后一行写 `<mood>值</mood>`，值只能取 `neutral happy proud curious confused sorry surprised focused` 之一。
- 后端的流式解析器在推送前截掉末尾的 `<mood…>`。它会缓冲可能属于这个标签的尾部片段，使客户端永远收不到标签的任何字符。完成时把值存入 `messages.mood`。标签缺失或取值非法时存 NULL，正文保持原样。
- 头像显示优先级：
  1. 运行状态：`thinking`、`speaking`、`error`。
  2. 该成员最新一条消息的心情，从消息完成起保持 30 秒。
  3. 用户设置的默认表情。

## 3. 头像引擎（`web/src/bot/`，不依赖 React）

- `math.ts`：缓动、插值、`TAU`。
- `shapes.ts`：8 种形状，各自是 64 个角度上的半径数组（`PROFILE_SAMPLES = 64`），由解析式生成并归一化到同一视觉重量。提供 `radiusAtAngle` 和 `profilePath`。
- `palette.ts`：12 种颜色。眼睛颜色根据主色亮度自动取深色或浅色，对比度至少 4.5:1。
- `expressions.ts`：表情由以下参数组成：
  - `gaze {yaw, pitch, roll}`
  - `split`（两眼间距）
  - 两只眼各自的 `{w, h, tilt, open}`

  静态表情包括 `neutral happy proud curious confused sorry surprised focused sleepy wink`。愤怒、难过一类的表情靠两只眼的倾角左右镜像来表现。眼睛宽高比接近 1 时看不出倾角，测试会限制这种组合。
- `face.ts`：把眼睛投影到半径为 1 的球面，再按 `radiusAtAngle` 贴合实际轮廓。每个「形状 × 表情」组合的修正偏移在模块加载时一次算好并存表，运行时只查表，避免逐帧求解导致抖动。
- `engine.ts`：`createBot(config)` 返回 `{ setMood, setShape, setColor, sample(t) }`。
  - `sample` 是时间的纯函数，输出 `{ bodyPath, eyePaths, dots, fill, eyeFill }`。
  - 换形状、换表情时，用指数缓出从屏幕上当前的那一帧开始过渡，所以中途再次切换也是连续的。
  - 静止时没有漂浮，只有视线微移和眨眼。眨眼时间表由 seed 预先生成。
  - `thinking`：身体收缩成中间那个点，两侧再长出两个点，三个点依次跳动。
  - `speaking`：眨眼更密，有轻微的点头。
  - `error`：眼睛眯起、视线下垂。
- `Avatar.tsx`：React 外壳。同一屏的所有头像共用一个 `requestAnimationFrame` 循环。不可见的头像暂停（`IntersectionObserver`）。24px 及以下只眨眼、不做视线微移。开启 `prefers-reduced-motion` 时冻结在静态表情。保留 `role="img"` 和 `aria-label`。
- `GroupAvatar.tsx`：保持现在的叠放布局，每个槽位换成新的 `Avatar`。
- 成员编辑面板新增「外观」区：形状、颜色、默认表情各一排冻结帧预览按钮（`role="radiogroup"`），实时预览，保存时调用 `PATCH /agents/{id}`。
- Web 端删除 `face.json` 与 PNG 的加载代码，以及 `AvatarFace` 类型。后端的 Blobatar 路由保留给 iOS。

## 4. 侧栏（`web/src/components/Sidebar.tsx`）

从上到下：

1. 标题行（红绿灯和收起按钮），下面是搜索框。
2. 置顶区：44px 头像横排，名字在头像下方（单行，超长显示省略号），满一行换行。当前打开的条目有底色。
3. 不分组的条目，不显示标题。
4. 各个分组：标题可以折叠，折叠状态存在 `localStorage`。空分组显示「右键机器人可移入」。
5. 底部：「新对话」和「设置」。

条目行：

- 30px 头像、名字、一行最后消息摘要。
- 右侧显示时间（今天显示时:分，其他日期显示月/日），回复中时换成运行中的小点。
- 原来按「今天 / 昨天 / 最近 7 天」的分段去掉。
- 搜索时置顶区和分组保留，只显示匹配的条目；没有匹配条目的分组隐藏。

右键菜单（`ContextMenu.tsx`，自绘）：

- 条目：「置顶 / 取消置顶」；「移到分组」子菜单，列出现有分组，加上「不分组」和「新建分组…」；私聊成员另有「编辑外观」。
- 分组标题：「重命名」「删除分组」。
- 菜单使用 `role="menu"` / `menuitem`。方向键移动，Enter 选择，Esc 关闭并把焦点还给触发元素。键盘用户按 Shift+F10 或 ContextMenu 键打开。
- 新建和重命名分组用行内输入框：Enter 确认，Esc 取消，名字为空时不提交。

收起状态：只显示置顶区和不分组条目的头像，不显示分组标题。

状态：`store` 新增 `sidebar` 切片。操作先在本地乐观更新，再整份 `PUT`。请求失败时回滚，并用现有横幅提示错误。

## 5. 错误处理

- 写外观或布局失败：回滚到旧值，横幅显示后端返回的中文错误。
- 会话被删除：外键级联清理 `sidebar_items`，客户端随下一次 workspace 刷新同步。
- 外观 JSON 损坏或含未知值：后端读取时视为 NULL，按 seed 派生，并记一条日志。

## 6. 验证

- 后端 pytest：
  - 迁移只加不改，老库升级后主管被置顶一次，再次升级不重复置顶。
  - 布局 PUT/GET 往返。
  - PUT 引用未知会话或分组时返回 422。
  - 删除分组后条目回到不分组。
  - 外观校验。
  - 心情标签被截掉：标签被拆在多个流式分片里时也不泄漏任何字符。
  - 非法心情存 NULL。
  - SSE 推送 `sidebar` 事件。
- 引擎 vitest：
  - `sample` 是纯函数，同一个 t 两次得到相同输出。
  - 所有形状的采样点数都是 64。
  - 每个形状 × 表情 × 时间采样下，眼睛都不超出轮廓。
  - 中途切换表情时没有跳变。
  - 前后端的外观目录一致。
- 实机：桌面端截图检查深色和浅色、右键菜单、置顶换行、分组折叠、编辑外观后头像同步、回复后表情变化。
- 浏览器：Playwright 检查键盘操作右键菜单。

## 不做

- iOS 端的侧栏、头像和外观编辑。
- 群组的外观自定义。
- 拖拽排序。顺序由「置顶时间 / 移入分组时间」决定，`position` 字段为以后的拖拽预留。
- 导出 GIF 或视频。
