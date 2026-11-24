# 侧栏机器人与自研头像实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把侧栏做成可置顶、可分组的机器人列表；用自研头像引擎替换 Blobatar，让表情随运行状态和模型标注的心情变化，并支持自定义外观。

**Architecture:** 后端在 SQLite 上加列、加表，存放外观、心情和侧栏布局，并提供 `/sidebar` 接口和 `sidebar` SSE 事件；流式输出时截掉末尾的 `<mood>` 标签。Web 端新增 `web/src/bot/`：一个不依赖框架的纯函数引擎，`sample(t)` 输出 SVG 路径。`Avatar` 用共享的 rAF 循环驱动所有头像。侧栏重写为置顶区 + 不分组 + 分组，用自绘右键菜单操作。

**Tech Stack:** FastAPI、SQLite、pytest（真实 uvicorn + FakeUpstream）；React 19.3.1、Vite 8.3.4、TS 7.0.2；vitest（新增，版本精确锁定）。

Spec：`docs/superpowers/specs/2026-10-20-sidebar-bots-design.md`

## Global Constraints

- 界面文案用中文；后端错误 detail 用中文句子、以「。」结尾，沿用 `_validation_detail` 的格式。
- 不用全大写标签，不用中点（·）拼接元信息，不加边框或描线，用色调和间距分区。
- 依赖精确锁定版本。不碰 `backend/.env`；不得以破坏性方式改动 `backend/data/cubes.sqlite3`。
- 迁移只加不改：`SCHEMA_VERSION` 从 3 升到 4，新列写进 `COLUMN_UPGRADES`，新表写进 `SCHEMA_STATEMENTS`。
- 外观目录（前后端各一份，内容必须一致）：
  - 形状：`round pebble squircle capsule bean hexagon cloud drop`
  - 颜色：`ink cream clay coral tangerine honey moss jade sky iris plum slate`
  - 表情：`neutral happy proud curious confused sorry surprised focused sleepy wink`
- 心情（模型可以标注的值）：`neutral happy proud curious confused sorry surprised focused`
- 不复制 bloub 的代码和测量常数，只借鉴方法：径向轮廓、参数化表情、`sample(t)` 纯函数、预先算好的眼睛贴合表。
- iOS 不改。Blobatar 路由（`/avatars/*`）保留。
- 不提交 git（用户没有要求）。

---

### Task 1：后端——外观、心情标签、侧栏布局

**Files:**
- Create: `backend/app/appearance.py`
  - 外观目录常量 `SHAPES`、`COLORS`、`EXPRESSIONS`、`MOODS`（元组）。
  - `parse_appearance(raw: str | None) -> dict | None`：读取 JSON。损坏或含未知值时返回 None 并记一条日志。
  - `class MoodStream`：
    - `push(chunk: str) -> str`：返回此刻可以安全推送给客户端的文本。
    - `visible: str`：截至目前已确认可见的全部文本。
    - `finish() -> tuple[str, str | None]`：返回去掉标签后的完整正文，以及合法的心情值或 None。
- Create: `backend/app/sidebar.py`：布局的校验和整形，被 store 与 main 共用。
- Modify: `backend/app/store.py`
  - `SCHEMA_VERSION = 4`。
  - 新增列 `agents.appearance TEXT`、`messages.mood TEXT`，同时写进 DDL 和 `COLUMN_UPGRADES`。
  - 新增表 `sidebar_folders`、`sidebar_items`，外键见 spec。
  - `update_agent(..., appearance=_UNSET)`；`update_message(..., mood=_UNSET)`。
  - `_agent_row` 增加 `appearance`，`_message_row` 增加 `mood`。
  - 新增方法：
    - `sidebar_layout() -> dict`
    - `replace_sidebar(items: list[dict], folder_ids: list[str]) -> dict`
    - `create_folder(name) -> dict`
    - `rename_folder(id, name) -> dict | None`
    - `delete_folder(id) -> dict | None`
  - 在 `_bootstrap` 末尾执行一次性置顶：`meta` 表里没有 `sidebar_seeded` 键时，把主管私聊写成置顶，再写入这个键。
- Modify: `backend/app/models.py`
  - `Appearance` 模型：三个字段都必填，取值必须在目录内，否则返回中文 422。
  - `AgentOut.appearance: Appearance | None`；`MessageOut.mood: str | None`。
  - `AgentPatchRequest.appearance`：判断 `"appearance" in model_fields_set`。显式传 null 表示重置为 NULL。`_at_least_one` 把 appearance 也算作有效字段。
  - `SidebarFolderOut`、`SidebarItemOut`、`SidebarOut`。
  - `SidebarPutRequest {items: list[SidebarItemIn], folder_ids: list[str]}`。
  - `FolderNameRequest {name}`：去掉首尾空白，长度 1–24。
  - `SnapshotOut.sidebar: SidebarOut`。
- Modify: `backend/app/engine.py`
  - 三段系统提示词末尾都加上心情规则，统一使用常量 `MOOD_RULE`。
  - 合并 `_supervisor_reply` 与 `_member_call` 里重复的 delta 循环。每条流式消息用一个 `MoodStream`，`delta` 事件只推送 `push()` 的返回值，定期刷新和异常时都持久化 `visible`。
  - `_complete_message` 用 `finish()` 得到正文和心情，按去掉标签后的正文解析提及，再写入 `mood`。
  - 中断的消息只保存 `visible`，`mood` 为 NULL。
- Modify: `backend/app/main.py`
  - 路由 `GET /sidebar`、`PUT /sidebar`、`POST /sidebar/folders`（201）、`PATCH /sidebar/folders/{id}`、`DELETE /sidebar/folders/{id}`。都返回 `SidebarOut`，并通过 `engine._publish("sidebar", layout)` 推送。如果 engine 已有公开的发布方法就用它；没有就新增 `engine.publish`，内部委托 `_publish`。
  - `patch_agent` 支持 appearance。
  - CORS 的 `allow_methods` 加上 `PUT`、`DELETE`。
- Test: `backend/tests/test_sidebar.py`、`backend/tests/test_appearance_mood.py`

**Interfaces（后续任务依赖的 HTTP 契约）:**
- `GET /sidebar` → `{"folders":[{"id","name","position"}], "items":[{"conversation_id","pinned","pin_position","folder_id","position"}]}`
  - folders 按 `position` 升序。
  - items 只包含已写入的条目。
- `PUT /sidebar` 请求体 `{"items":[{"conversation_id","pinned","folder_id"}], "folder_ids":[...]}`：
  - 服务端按数组顺序重新编号：置顶条目依次得到 `pin_position`，同一分组内的条目依次得到 `position`。
  - `folder_ids` 必须是现有分组 id 的一个排列。
  - 出现未知会话、未知分组或重复会话时返回 422，detail 用中文。
- `POST /sidebar/folders {"name"}` → 201 + 布局。同名分组（比较时忽略大小写、去掉首尾空白）返回 409「已有同名分组。」
- `PATCH /sidebar/folders/{id} {"name"}` → 布局。分组不存在返回 404「分组不存在。」
- `DELETE /sidebar/folders/{id}` → 布局。组内条目的 `folder_id` 置为 null。
- SSE 事件 `sidebar`，payload 为完整布局；`snapshot` 的 payload 增加 `sidebar` 字段。
- `Agent.appearance: {"shape","color","expression"} | null`；`Message.mood: string | null`。

- [ ] **Step 1：写失败的测试**。测试套件里用真实的 `make_backend` 和 `upstream`，模仿 `test_team_mentions.py::test_a_mentioned_member_replies_instead_of_the_supervisor`。需要覆盖：
  - 新库启动后，`GET /sidebar` 里只有主管私聊一条，且已置顶。重启后不会重复置顶。取消置顶后再重启，仍保持未置顶。
  - 老库升级（用 `test_team_migration.py` 的写法造一个 v3 库）：现有行保留，新列为 NULL，主管被置顶。
  - PUT 往返：置顶顺序和分组内顺序按数组顺序返回。PUT 引用未知会话、未知分组、重复会话，或者 `folder_ids` 不是现有分组的排列，都返回 422。
  - 新建分组；重名返回 409；重命名分组；分组不存在返回 404；删除分组后条目的 `folder_id` 为 null，会话仍然存在。
  - 删除会话后，对应的 `sidebar_items` 被级联删除。如果后端没有删除会话的接口，就直接在 store 里删一行来验证。
  - 每次布局写入都推送一条 SSE `sidebar` 事件。`snapshot` 里包含 `sidebar`。
  - `PATCH /agents/{id}` 设置合法外观后，`GET /workspace` 能读回。未知形状或颜色返回 422；显式传 `appearance: null` 重置为 null；appearance 为空对象时同样受「至少提供一个字段」规则约束。
  - 心情：`upstream.script = [{"content": "好的，方案如下。\n<mood>happy</mood>"}]`，并用 `chunk_count` 让标签被拆进多个分片。验证：所有 `delta` 文本拼起来不含 `<` 也不含 `mood`；保存的 content 是「好的，方案如下。」；mood 是 `happy`；同一段文本中的 @ 提及仍能正确解析。
  - 非法心情 `<mood>angry</mood>`：标签同样被截掉，mood 为 null。
  - 不带标签：正文原样保留，mood 为 null。
  - 正文中间出现的 `<` 字符（例如 `a < b`）最终会完整推送给客户端。
  - 三段系统提示词里都包含 `MOOD_RULE`，用 `helpers.system_prompt` 检查。
  - `MoodStream` 的单元测试：分片落在标签任意位置时，`push` 都不会泄漏标签字符，`finish` 的结果一致。
- [ ] **Step 2：运行测试，确认失败**。命令：`cd backend && uv run --python 3.12 --with-requirements requirements.txt --with pytest pytest -q tests/test_sidebar.py tests/test_appearance_mood.py`。
- [ ] **Step 3：实现上面列出的文件改动**。
- [ ] **Step 4：跑全量测试**，命令同上但不加文件参数。原有的 100 个测试和新增测试全部通过。

### Task 2：Web——头像引擎、Avatar、外观编辑器

**Files:**
- Create: `web/src/bot/`
  - `math.ts`
  - `shapes.ts`：64 个采样点、`radiusAtAngle`、`profilePath`（Catmull-Rom 转三次贝塞尔曲线）。
  - `palette.ts`：根据主色亮度选 `eyeFill`，对比度至少 4.5:1。
  - `expressions.ts`
  - `face.ts`：眼睛投影到球面。`EYE_FIT` 表在模块加载时一次算好。
  - `engine.ts`
  - `catalog.ts`：外观 id 列表，以及 `appearanceFromSeed(seed)`。
  - 测试 `*.test.ts`。
- Modify: `web/package.json`
  - devDependency `vitest`：选与 Vite 8.3.4 兼容的版本并精确锁定，用 `npm view vitest peerDependencies` 核实。
  - 新增脚本 `"test": "vitest run"`。
- Modify: `web/src/api/types.ts`
  - `Agent.appearance: Appearance | null`、`Message.mood: string | null`。
  - 删除 `AvatarFace`。
- Modify: `web/src/api/client.ts`
  - `patchAgent` 的参数增加 `appearance?: Appearance | null`。
  - 删除 `face()` 和 `avatarPNG()`。
- Modify: `web/src/lib/mood.ts`
  - `export type Mood = "idle" | "thinking" | "speaking" | "error" | ExpressionId`。
- Modify: `web/src/state/store.ts`
  - `moodFor` 的优先级：短时持有的心情（held）→ speaking → thinking → 该成员最近一条消息的 `mood`（消息完成 30 秒内有效）→ idle。
  - `applyMessage`：消息完成且带 mood 时持有该心情 30 秒；消息被中断时持有 `error` 4 秒；被 @ 时仍持有 `surprised` 1.4 秒。
  - 删除旧值 `sad`。
- Rewrite: `web/src/components/Avatar.tsx`
  - `Avatar({ seed, appearance, size, name, mood, frozen })`。
  - 所有头像共用一个 rAF 循环，每帧直接改 `d` 属性，不触发 React 重渲染。
  - 用 IntersectionObserver 暂停屏幕外的头像。
  - 尺寸 ≤24 时关闭视线漂移；开启 `prefers-reduced-motion` 时冻结。
  - 保留 `role="img"` 和 `aria-label`。
  - `GroupAvatar` 的槽位布局不变。
- Modify: 所有调用 Avatar 的地方传入 `appearance={agent.appearance}`：
  - `ChatView.tsx`、`Composer.tsx`、`Panels.tsx`、`Sidebar.tsx`
  - 历史消息只有 `avatar_seed`，用 `agentByID(message.author_id)?.appearance` 补上。
- Modify: `web/src/components/Panels.tsx`，在 MemberEditor 里加「外观」区：
  - 大尺寸实时预览。
  - 形状 8 个、颜色 12 个、表情 10 个，都是冻结帧按钮。
  - 每组是 `role="radiogroup"`，方向键切换，`aria-checked` 标出当前选中项。
  - 「恢复默认」发送 `appearance: null`。
  - 保存沿用现有的保存按钮流程。
- Modify: `web/src/styles.css`
  - 删除旧的 `.avatar-*` 规则，以及只服务于这些规则的 keyframes（blink、breathe、bob、glance、hop、pop）。删除前先用 grep 确认没有其他地方引用。
  - 新增外观编辑器的样式。

**Interfaces:**
- `import { createBot, type Frame } from "../bot/engine"`
  - `createBot({ appearance, seed, detail: "full" | "low" }) → { set(patch: { appearance?: Appearance; mood?: Mood }, t: number): void; sample(t: number): Frame }`
- `Frame = { body: string; eyes: [string, string]; eyeAlpha: number; dots: { cx: number; cy: number; r: number; opacity: number }[]; fill: string; eyeFill: string }`，坐标系是 viewBox `0 0 100 100`。
- `appearanceFromSeed(seed: string): Appearance`：确定性派生，不会选到 `ink`。
- `resolveAppearance(appearance: Appearance | null | undefined, seed: string | null | undefined): Appearance`

**引擎要求：**
- 轮廓：64 个角度 θ_i = i/64·2π，屏幕坐标 y 轴向下，球的半径 R = 38，球心在 (50, 52)。
  - 每种形状都用解析式生成，归一化到视觉重量与圆相同。
  - 轮廓必须是从中心可见的星形，否则 r(θ) 无法表示。
- 表情 = `gaze {yaw, pitch, roll}` + `split` + 两只眼各自的 `{w, h, tilt, open}`。
  - `sorry`：两眼外侧上端向外下垂。`focused`：两眼上端向内收，眼睛眯起。`wink`：一只眼睁开，另一只是横向的短线。
  - 带倾角的眼睛，宽高比必须明显偏离 1，否则倾角看不出来。
- 眼睛画成胶囊形，用 `eyeFill` 填充，并用身体轮廓 clipPath 裁剪。不画白色描边。
- `set` 记下切换发生的时刻 t0，以及当时屏幕上的合成姿态。之后的帧从这个姿态出发，用 easeOutQuint 在 0.45 秒内过渡到目标，不回弹。
- `sample(t)` 不能修改任何状态，同一个 t 必须得到同样的输出。
- 静止时不漂浮，只做视线漂移（几条按 seed 生成的低频正弦叠加）和眨眼。眨眼时间表按 seed 预先生成：间隔 2.5–6 秒，每次 0.16 秒，偶尔连眨两下。
- `thinking`：身体收缩成中间的点（r 约 0.3R），两侧在 ±0.78R 处长出两个点，三个点从左到右依次跳动，眼睛隐去。
- `speaking`：每秒约 3 次、幅度 ±4° 的点头，眨眼频率提高到 1.8 倍。
- `error`：眼睛眯起，视线向下。
- 贴合：对每个形状 × 表情组合，在模块加载时求出一个眼睛平移量（只平移、不缩放），保证在视线漂移的整个范围内，眼睛的边界点都留在轮廓以内。运行时只查表，查表在过渡的两端进行，过渡期间按同一条缓动曲线插值。

- [ ] **Step 1：写失败的 vitest 测试**：
  - `sample` 是纯函数：同一个 t 调用两次，结果深相等；在 t1 采样后再采样 t0，结果不变。
  - 每种形状都恰好 64 个采样点，且全部为正的有限数。
  - 形状 × 表情 × 时间网格（10 秒内每隔 0.05 秒）上，每只眼的边界采样点都落在轮廓半径以内（容差 0.5 个 viewBox 单位）。
  - 在过渡中途切换表情时，相邻两帧之间的眼睛中心位移不超过 3 个单位。
  - `appearanceFromSeed` 是确定性的，且不会选出 `ink`。
  - 每种颜色的 `eyeFill` 与主色的对比度都至少 4.5。
  - `catalog.ts` 的三个列表与 Global Constraints 中的目录逐项一致。
- [ ] **Step 2：运行 `cd web && npm test`，确认失败**。
- [ ] **Step 3：实现引擎、Avatar、编辑器、调用方和样式**。
- [ ] **Step 4：`npm test` 与 `npm run build` 都通过**。

### Task 3：Web——侧栏置顶、分组、右键菜单

**Files:**
- Create: `web/src/components/ContextMenu.tsx`
  - `ContextMenu({ x, y, items, onClose })`，`items` 是 `{ label, onSelect?, submenu?, danger?, disabled? }[]`。
  - `role="menu"`，菜单项为 `menuitem`。上下方向键移动，Enter 或空格选择，右方向键展开子菜单、左方向键收回，Esc 关闭并把焦点还给触发元素。
  - 点击菜单外部时关闭。菜单位置限制在窗口内。
- Rewrite: `web/src/components/Sidebar.tsx`，从上到下：
  - 标题行、搜索框。
  - 置顶区：44px 头像横排，名字在下方，满一行换行。
  - 不分组区：按 `updated_at` 倒序。
  - 分组：按 `position` 排列；组内条目按 `position` 排列；标题可折叠，折叠状态存在 `localStorage` 的 `cubes.folders.collapsed`。
  - 底部保留「新对话」和「设置」，原有的「团队」入口也保留。
  - 条目行：30px 头像、名字、一行最后消息摘要、右侧显示时间或运行中的小点。
  - 打开右键菜单：鼠标右键、Shift+F10，或 ContextMenu 键。
  - 新建和重命名分组用行内输入框：Enter 确认，Esc 取消，空名不提交。
  - 搜索时只保留匹配的条目；没有匹配条目的分组隐藏。
  - 收起状态：只显示置顶区和不分组区的头像。
  - 新增 prop `onEditAppearance(agentId)`，由 `App.tsx` 接上，打开该成员的编辑面板。
- Modify: `web/src/lib/time.ts`
  - 新增 `shortTime(iso)`：今天显示 `HH:mm`，其他日期显示 `M/D`。
  - 如果 `historyBucket` 已经没有调用方，删除它。
- Modify: `web/src/api/types.ts`：新增 `SidebarFolder`、`SidebarItem`、`SidebarLayout`；`Snapshot.sidebar`。
- Modify: `web/src/api/client.ts`：
  - `sidebar()`
  - `putSidebar({ items, folder_ids })`
  - `createFolder(name)`、`renameFolder(id, name)`、`deleteFolder(id)`
  - SSE 事件名列表里加上 `sidebar`。
- Modify: `web/src/state/store.ts`：
  - 新增 `sidebar: SidebarLayout | null` 状态。从 snapshot 和 `sidebar` 事件中更新，`boot` 时也调用 `client.sidebar()` 加载。
  - 动作 `pin(conversationID)`、`unpin`、`moveToFolder(conversationID, folderID | null)`：先乐观更新本地状态，再整份 PUT；失败时回滚，并用现有的 `notice` 横幅显示错误。
  - 新置顶的条目排在置顶区末尾；移入分组的条目排在该组末尾。
  - 动作 `createFolder(name): Promise<SidebarFolder | null>`、`renameFolder`、`deleteFolder`。
- Modify: `web/src/App.tsx`：给 `Sidebar` 传入 `onEditAppearance`。
- Modify: `web/src/styles.css`：置顶区、条目行、分组标题、右键菜单的样式。
  - 不用边框。菜单是 `var(--raised)` 加 `var(--lift-strong)` 的浮层，圆角 12px。
  - 当前选中项用 `var(--tint-strong)` 作底色。
  - 侧栏背景继续用 `--rail-glass`；之前加的 `@media (min-width: 761px)` 毛玻璃规则保持不变。

**Interfaces:**
- 使用 Task 1 的 HTTP 契约，以及 Task 2 的 `Avatar({ seed, appearance, size, name, mood })` 和 `GroupAvatar({ members, size })`。
- 与 Task 2 共用 `types.ts`、`client.ts`、`store.ts`、`styles.css`。每次编辑前都要重新 read，只做局部 edit，不整文件重写。新增内容追加为独立区块。

- [ ] **Step 1：实现**。这一块的验证方式是在浏览器里实际操作，不写永久测试。
- [ ] **Step 2：验证**。`npm run build` 通过。用 Playwright 连真实后端，逐项实际操作：
  - 右键置顶、取消置顶
  - 新建分组、把条目移入分组、重命名分组、删除分组
  - 用键盘打开菜单并完成选择
  - 刷新页面后布局保持
  - 每项都截图，并核对 DOM。

### Task 4：整合与实机验证

- [ ] 后端全量 pytest、`npm test`、`npm run build`、`npx tauri build --bundles app` 全部通过。
- [ ] 重启后端服务，启动新打包的 `.app`。在条纹背景窗口前，分别在深色和浅色模式下截图，检查：
  - 侧栏毛玻璃仍然生效
  - 置顶区换行
  - 分组折叠
  - 右键菜单
  - 编辑外观后，各处头像同步更新
  - 发一条消息后，头像依次经过 thinking、speaking，最后停在模型标注的心情上
- [ ] README 的 Web 小节补充置顶、分组、外观和心情的说明。删除临时脚本。
