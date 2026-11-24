# 安装与部署

从一台空机器到能用的桌面版或服务器；模型怎么配见 [models.md](models.md)。

## 启动后端

需要 Python 3.11+。在项目根目录执行：

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env
```

编辑 `backend/.env`，填写你自己的 `MODEL_API_KEY` 和准确的 `MODEL_NAME`。它们只在首次启动、数据库里还没有模型服务时导入成一个名为 `mify` 的模型服务（OpenAI 兼容协议）并设为默认模型；之后所有模型设置都在「设置 → 模型」里，改 `.env` 不再生效，删掉 `mify` 也不会再导回来。没有默认模型时 `/health` 返回 `configured: false`，启动任务返回 HTTP 503。环境变量优先于 `.env`。`.env`、虚拟环境和数据库已加入 `.gitignore`。

```bash
.venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 \
  --timeout-graceful-shutdown 5
```

- 健康检查：`http://127.0.0.1:8000/health`
- 工作区：`http://127.0.0.1:8000/workspace`
- `.env` 和相对 `DATABASE_PATH` 都相对于 `backend/`。默认数据库是 `backend/data/cubes.sqlite3`。
- 使用单个 worker。每个会话最多一个任务，不同会话可以并行；任务的注册、队列与事件订阅都在这个进程内管理。
- 用 Ctrl+C 退出，并保留 `--timeout-graceful-shutdown 5`。不要直接杀进程。

## 部署到服务器

后端要一直开着，定时任务才会到点执行；放在服务器上，电脑关掉也照跑。桌面版可以连过去，团队、对话、数据库都在服务器上，这台电脑不再启动自带的后端。

服务器上装 Python 3.11+，拉下仓库后按「启动后端」建虚拟环境、装依赖。服务器上不写 `backend/.env`，设置都用环境变量给：

```bash
cd /srv/cubes/backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export CUBES_AUTH_TOKEN="$(openssl rand -hex 32)"
export CORS_ORIGINS="app://cubes"
export TIMEZONE="Asia/Shanghai"
export DATABASE_PATH=/srv/cubes/data/cubes.sqlite3
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- `CUBES_AUTH_TOKEN`：之后每个请求都要带 `Authorization: Bearer <这串>`，`/health` 的 `auth` 会变成 `true`。桌面端连接时填同一串。不设就等于谁都能调这个后端，只适合本机。
- `CORS_ORIGINS`：桌面窗口的来源是 `app://cubes`；浏览器版还要加上它自己的来源。不接受 `*`。
- `TIMEZONE`：定时任务的「每天 09:00」按这个时区算，写 IANA 名字。
- `CONTEXT_WINDOW_DEFAULT` / `CONTEXT_OUTPUT_DEFAULT`：自动探测（你的设置 → 服务商说明 → models.dev）都说不出某个模型的窗口和输出上限时用的兜底值（token，默认 128000 / 8192）。一次请求可用的额度是「窗口 − min(输出上限, 20000) − 8000」，用到 70% 就会把较早的对话折成摘要。
- `DATABASE_PATH`：数据库和头像缓存都在这里。备份就是停掉服务后拷这个文件。`models-dev.json` 也在同一目录：第一次启动从随包快照复制一份，之后最多每天在后台换一次新的；下载失败就继续用旧的。
- 只绑 `127.0.0.1`，由反向代理对外，后端端口不对公网开；用单个 worker。
- Webhook 要能被外面的服务调用，反向代理必须把 `POST /hooks/` 转到后端：只有这一段不用带 `CUBES_AUTH_TOKEN`，它用 Webhook 自己的密钥鉴权。上面 Caddy 的配置默认就放行；用 nginx 时不要在这一段加 basic auth 或校验启动令牌。真正要保密的是密钥本身：`POST /hooks/{id}` 里 `Authorization: Bearer <密钥>`（或 `?token=`）就是全部凭据，谁拿到谁就能触发那个定时任务或工作流，所以别把带密钥的命令贴进聊天记录或日志；怀疑泄露就在面板里「轮换密钥」。调用方请求体最大 64 KB；同一个 Webhook 每 5 秒只接受一次调用，超过就返回 429。整段后端（`/events`、附件、所有接口）仍然只适合放在反向代理后面，不要直接把 8000 端口开到公网。

用 systemd 让它一直跑（`/etc/systemd/system/cubes.service`）：

```ini
[Unit]
Description=Cubes backend
After=network.target

[Service]
User=cubes
WorkingDirectory=/srv/cubes/backend
Environment=CUBES_AUTH_TOKEN=换成你的那一串
Environment=CORS_ORIGINS=app://cubes
Environment=TIMEZONE=Asia/Shanghai
Environment=DATABASE_PATH=/srv/cubes/data/cubes.sqlite3
ExecStart=/srv/cubes/backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cubes
journalctl -u cubes -f
```

HTTPS 用 Caddy 反代，证书自动申请：

```
cubes.example.com {
  reverse_proxy 127.0.0.1:8000
}
```

- 实时更新走 SSE，不能被缓冲。Caddy 看到 `text/event-stream` 就立即转发，不用另加 `flush_interval`；换成 nginx 则要在这一段加 `proxy_buffering off;`，否则界面要等很久才刷新。
- 桌面端只接受 `https://` 的远端地址，所以别省掉这一层。

然后在桌面版「设置 → 连接」里选「远端服务器」，填 `https://cubes.example.com` 和那串访问令牌，点「测试并连接」：先试一次 `/health`，地址或令牌不对会当场说明，通了才切换，窗口随即重新加载。设置存在 `~/Library/Application Support/com.ice.cubes.desktop/remote.json`（权限 0600，里面是令牌），之后每次启动都直接连服务器，本机不再起后端；「改回本机」删掉它，重新启动自带的后端。远端只接受 `https://`，`http://` 只留给 `127.0.0.1` 和 `localhost`。

- 沙盒电脑还是在 Mac 上跑，因为它要用 Apple container。后端在服务器上时，「电脑」面板里的启动命令会把 `--server` 写成服务器地址（面板里的命令始终是当前连着的后端），`https://` 自动换成 `wss://` 连 `/computer/agent`，配对令牌照旧从面板复制。实时画面是沙盒这台 Mac 的 `http://127.0.0.1:6080`，所以只在这台 Mac 上打得开。
- iOS 客户端和浏览器版同样填服务器地址，只允许 HTTPS。

## 运行 iOS

1. 用 Xcode 27 打开 `cubes.xcodeproj`。
2. 选择 `cubes` Scheme 和 iOS 27 模拟器，运行。
3. 客户端默认连接 `http://127.0.0.1:8000`，右上角设置可修改。
4. 在主管私聊里写下目标并发送。成员出现在「团队」页，讨论出现在对应群组。
5. 任务进行中可以停止。点行动记录可打开它创建的成员或群组。

真机：手机和电脑需处于相互可访问的受信任局域网。后端改为 `--host 0.0.0.0`，iOS 设置填写电脑的局域网地址。真机的 `127.0.0.1` 指手机自己。`Config/Info.plist` 只允许本地网络 HTTP；远程后端请使用 HTTPS。

## 运行 Web 和桌面端

需要 Node 22+；打包桌面端还需要 `uv`（冻结后端用）。先按上面启动后端，然后：

```bash
cd web
npm install
npm --prefix electron install
npm run dev            # 浏览器打开 http://127.0.0.1:1420
npm run desktop        # 桌面窗口（Electron）：自动启动开发服务器和自带的后端（见下文）
npm run build:desktop  # 打包，产物在 web/electron/dist/（mac-arm64/Cubes.app 和 .dmg）
```

桌面端是 `web/electron/`：`main.cjs` 起窗口和后端，`preload.cjs` 给页面一个 `window.cubesDesktop`（后端地址、远端切换、主题、缩放、全屏、外链、菜单事件），`menu.cjs` 是菜单栏。`CUBES_GLASS=0 npm run desktop` 关掉透明和毛玻璃。

- 表情贴纸有自己的出场和退场动画，按 emoji 本身分成七类（爱心、彩纸、星光、笑脸、食物、动物、其他）：分类跟着表情在 Unicode 里的分组走，所以任何一个表情都有自己的动法，连肤色、组合表情也认得出：❤️ 冒小爱心往上飘，🎉 撒一圈彩纸往下落，👍 散金色星光，😂 崩出小圆点，🍰 掉碎屑，🐨 跳一下，其它 emoji 走一圈圆点。每次出场还有一圈扩散的光环，贴纸本身也会按类别缩放、弹跳或晃动；粒子飞出气泡时会临时抬到下面的消息之上，不会被下一条盖住；点掉自己的最后一个表情时会先播一段退场再消失。粒子是照着物理走的：每一颗有自己的发射速度、重力、空气阻力和自转，爱心的往上飘、边走边摆，彩纸往下落并翻滚，星光冲出去很快减速，吃食往下掉；飞行中一直在动，不是飞到位就停。每颗还有自己的远近——近的大而清楚、远的小而虚——以及 0～70ms 的先后，所以看起来是一团有纵深的东西，而不是一圈同时出发的复制品；表情本身（🎊✨❤️）和色条、圆点混在一起。粒子位置是确定的（`web/src/lib/reactionFx.ts`），系统开启「减少动态效果」时粒子不出现、直接到位。
- 发送消息后，视图一定会回到对话最底部（无论你之前翻到多上面）；「查看过程」只在这次任务确实用过工具、发生过接力（hops）或不止一次模型调用时出现，普通一问一答不再挂这个链接，出错/被停止/到上限的提示条仍然带它。
- 对话里自己的话是主题色的气泡，机器人回复和工具调用（搜索、读网页、沙盒操作等）都是中性色的气泡：同一位机器人连着说几条时只有第一条带尖角（在左上角，朝头像那侧），后面的圆角一致；你自己的气泡尖角在右下角。每条消息（你的和机器人的都算）右键有「表情回应」，在指针处打开表情面板：一行常用表情、一个搜索框、按 Unicode 分组的所有表情——笑脸、人物、动物、食物、出行、活动、物品、符号、旗帜，共 1900 多个，能按英文名搜（搜 owl 能找到 🦉）也能直接粘贴一个表情；面板给的就是这一条消息；表情贴纸压在气泡下边缘，不描边：底色相差一档、再加一层浅浅的投影（贴边一层 + 稍远一层），把它从气泡上托起来。三种状态是同一色系的三档亮度：未选（抬得最明显：中性气泡上底色里混 20% 主题色，主题色气泡上抬到 70% 白）、悬停（再亮一点、字变深）、你贴过的（最贴近气泡：中性气泡用白底、主题色气泡只抬 12%，数字加重一档）。贴纸按气泡的宽度换行，贴多少都不会把气泡或对话列撑宽，靠气泡尖角那一侧对齐。
- 界面仿 Grok：左侧侧栏列出所有机器人，每个成员私聊和每个群组各算一个。置顶的机器人以大头像横排显示在最上面，下面是不分组的机器人，再往下是你自己建的分组。中间一列是对话，底部是一个大圆角输入框。浅色、深色和跟随系统都支持，在「设置」里切换。
- 右键机器人可以置顶或取消置顶、移到分组（也能在这里新建分组），点「编辑」改名称、职责、模型和外观，成员还能从这里删除；右键分组标题可以重命名或删除分组。用键盘时按 Shift+F10 打开菜单。布局存在后端，多个窗口之间实时同步。
- 群组里输入 `@` 或点「点名」会弹出成员列表，↑↓ 选择，Enter 或 Tab 确认；Enter 发送，Shift+Enter 换行。⌘\ 收起或展开侧栏。消息里的 `@名字` 显示成带头像的胶囊，点它打开这位成员。
- 在任何会话里输入 `/`（开头或空格之后）会弹出已启用的技能，选中后插入 `/技能名`；消息里它显示成绿色的技能胶囊。后端在这一轮的 `<runtime>` 提示里告诉回答的机器人「用户点名了技能 X，先 `use_skill`」；这个技能已停用或对它关掉时，则提示它告诉你用不了。`a/b` 这样的路径和网址不算。
- 输入框里 `@成员` 和 `/技能` 也直接显示成胶囊：从列表里选中，或手打的名字后面接了空格、光标移开，就变成胶囊。在胶囊后面按一下退格就删掉整个胶囊（连同选中时自动加的那个空格），在胶囊前面按 Delete 也一样。草稿本身还是纯文本，发送、保存草稿和点名都和以前一样。
- 「技能」列表里每张卡片的说明只显示一行，多出来的用省略号，完整内容在展开后的 SKILL.md 里；展开后的表格和代码块在卡片里横向滚动，不会把面板撑宽。
- 侧栏可以拖右边缘调宽窄（220–440px，窗口太窄时会让出聊天区），宽度会记住；双击边缘恢复默认，聚焦边缘后用 ← → 微调。收起后宽 96px，macOS 的红绿灯右边还留有空隙；置顶和不分组的机器人、群组和分组里的机器人都留着，分组之间用一道短横线隔开，点横线可以收起或展开这个分组，悬停能看到分组名。
- macOS 全屏时没有红绿灯：标题行从 52px 收窄到 44px，侧栏的搜索框挪进标题行（和收起按钮同一行），收起的侧栏把按钮放回顶上，聊天区和列表都跟着往上提；进出全屏时这些位置平滑过渡。
- 成员回复按 Markdown 渲染（含表格）。头像是 Cubes 自己画的机器人，可以选 21 种形状（含爱心、蛋、菱形、八边形、猫耳、熊耳、幽灵），身体颜色可以用 12 种预设色或在系统取色器里任选，眼睛颜色默认自动挑一个对比清楚的，也可以任选（太接近身体颜色时编辑器会提醒）；表情不由用户挑，而是跟着回复的心情标签和当时的动作走。眼睛、嘴和腮红都长在同一个球面上：转头时靠近边缘的那只眼睛会压缩、转过去再转回来，嘴和腮红跟着一起转，所以头像有体积而不只是平面贴图。嘴跟着心情变：平时是一道小微笑，开心时张开露出舌头，难过时向下撇，惊讶时是个小圆口，说话时会一张一合；开心的眼睛是圆头的「^ ^」。身体有厚度，会在纵深里转：转头时眼睛先到，身体随后跟着转过去，正面朝转向的一侧收窄、侧面露出来；点头是往前俯、摇头是左右转、开心时跳起来在空中扭一下、刚醒时左右转身环顾、叹气时往前塌，待机时按各自的种子慢慢摇晃和呼吸。左上方的光在轮廓上留下一道柔和的亮边，背光一侧的边缘暗下去，醒着的头像（40px 以上）下面有一块接触阴影，跳起来时阴影变小变淡；光会随转头移动。聊天区里 28px 以上的头像会看向鼠标，鼠标离开后回到漂移；侧栏里的头像平时是完全静止的（光影还在，只是不动），只有鼠标进到它所在的那一行（或者这条会话正开着、它正在干活）时才活起来，而且只在自己这一行里跟着鼠标看；空闲两分钟以上的头像会闭眼睡着、往一侧歪着头慢慢呼吸，一有动静就醒来。群组和私聊里的事件会触发一次性动作：被点名或收到私信时睁大眼睛、身体向上轻抬、肩边弹出一颗带缺口的小圆点；开始发言时点两下头、身体跟着前倾；开心或骄傲的回复先蹲一下再跳起来、眼睛弯成弧；出错或被停止时左右抖一下、换上皱眉的表情；抱歉时下沉并侧倾、眼睛垂下来；进入思考时身体收成中间那个点、两侧浮出另外两个点；刚出现的成员会先环视一圈。心情切换是滑动过去的，眼睛形状改变时用一次眨眼盖住；系统打开「减少动态效果」时头像保持静止。动作模型参考自 [bloub](https://github.com/jeremy-prt/bloub)（MIT），见 `web/ThirdPartyNotices.txt`。
- 回复里的数学公式用 KaTeX 排版：行内写 `$…$` 或 `\(…\)`，单独成行写 `$$…$$` 或 `\[…\]`（`remark-math` + `rehype-katex`，字体随前端一起打包）。太宽的公式在自己那一行里横向滚动；写错的公式按原文以红色显示，不影响整条回复；代码里的 `$` 和「$5、$10」这类价格不会被当成公式。流式输出时公式整块出现，不会逐字淡入拆开。
- 消息区的每个页面都是一个标签，像浏览器一样：⌘W 关闭当前标签，⇧⌘T 重新打开最近关闭的标签（最多记 20 个，只在本次运行内），⌃Tab / ⌃⇧Tab 切换，中键点击关闭，可以拖动排序。右键标签（或 Shift+F10）有「关闭」「关闭其他标签」「关闭右侧标签」「置顶 / 取消置顶」「复制标题」「重新打开关闭的标签」。置顶的标签固定在最左边，只显示头像或图标，没有关闭按钮；新标签和拖动都不会插进置顶区，「关闭其他」「关闭右侧」也不会关掉它们，只能用菜单或 ⌘W 单独关。置顶状态会记住，重启后还在。
- 界面上的展开和收起都有一小段过渡：输入框随内容长高、引用条和行动详情折进折出、分组和菜单展开、对话框升起和淡出、标签的选中块滑动；新消息到时自己的气泡轻轻上浮，置顶或移入分组的那一行滑到新位置。开合在 120–320ms 之间，系统打开「减少动态效果」时全部直接跳变。
- 后端地址默认是 `http://127.0.0.1:8000`，可在「设置」里改。桌面版例外：在「设置 → 连接」里选「本机」（App 自己启动，见下面「桌面版：内嵌后端」）或「远端服务器」（见「部署到服务器」）。
- 浏览器和桌面窗口跟后端不同源，后端只放行 `backend/.env.example` 里 `CORS_ORIGINS` 列出的来源（默认是开发服务器和桌面窗口 `app://cubes`），不接受 `*`。用别的地址访问时，在 `.env` 里设 `CORS_ORIGINS`。
- 桌面端的内容安全策略除了本机后端（`127.0.0.1` / `localhost`），还放行任意 `https:` 地址：远端地址在打包时还不知道。代价是任何 HTTPS 地址都能被连，所以只接受 HTTPS、不放开 `http:`；服务器要有证书（见「部署到服务器」）。
- macOS 桌面端的窗口是透明的，侧栏和顶部标签栏后面透出一层玻璃，聊天区保持不透明。「设置 → 常规 → 窗口材质」二选一：「液态玻璃」是系统的 Liquid Glass（`NSGlassEffectView`，macOS 26 起才有），颜色和明暗跟随系统，没有可调的项；「毛玻璃」可以调「透明度」（0–100%，越高透出越多，默认 60%）和「模糊度」（0–80，默认 30）。两种材质都由 `electron-window-glass` 画在窗口底层：毛玻璃用的是 Core Animation 的私有 backdrop 接口，系统更新后可能失效；它加载不了时窗口退回系统自带的 sidebar 材质（`vibrancy`）。选择存在页面的 `localStorage`（`cubes.glass`）和数据目录的 `glass.json` 里，下次启动在窗口显示前就用上。`CUBES_GLASS=0` 关掉整层材质。浏览器和窄窗口下侧栏是实色，也没有这一项。
- macOS 桌面端有完整的中文菜单栏：「Cubes」（关于、设置… ⌘,、服务、隐藏、退出）、「文件」（回到主管 ⌘N、新建成员… ⇧⌘N、关闭窗口 ⌘W）、「编辑」（撤销、重做、剪切、拷贝、粘贴、全选）、「显示」（显示/隐藏侧栏 ⌘\、搜索机器人 ⌘K、实际大小 ⌘0、放大 ⌘=、缩小 ⌘-、进入全屏幕）、「会话」（输入消息 ⌘L、停止回复 ⌘.）、「工具」（团队、连接器、电脑、工作台、记忆、定时任务，依次 ⌘1–⌘6）、「窗口」（最小化、缩放、Cubes、前置全部窗口）和「帮助」（打开后端日志）。关掉窗口（红灯或 ⌘W）只是把它藏起来，后端和定时任务照常运行，点 Dock 图标或「窗口 → Cubes」回来，⌘Q 才真正退出。菜单项的编号在 `web/electron/menu.cjs` 和 `web/src/lib/menu.ts` 里各写一份，`menu.test.ts` 会检查两边对得上。
- 打包产物用一张本地自签证书签名（见下面「桌面版：内嵌后端」），没有 Apple 开发者证书，首次在其他 Mac 上打开会被 Gatekeeper 拦下。

### 桌面版：内嵌后端

`npm run build:desktop` 先把 `backend/app` 用 PyInstaller 打成 `backend/build/desktop/dist/cubes-backend/`，再 `vite build` 出页面，最后由 electron-builder 把两者放进 `Cubes.app/Contents/Resources/`（`web/` 和 `cubes-backend/`）；打出来的 App 自己带后端，打开即用，不需要另开服务。页面从 `app://cubes` 加载，后端只放行这个来源。

打包好的 App 不需要 Python：PyInstaller 把 Python 3.12 解释器（`libpython3.12.dylib`）、标准库和全部依赖（FastAPI、uvicorn、pydantic、httpx、resvg 等）都冻结进 `Contents/Resources/cubes-backend/`，只链接 macOS 系统库。用户机器上要求 Apple 芯片（arm64）和 macOS 11 或更新；Intel Mac 需要另打 x86_64 或通用二进制。只有打包和 `npm run desktop` 需要本机的 Python 环境。

```bash
cd web
npm run build:desktop      # 打包后端和页面，再打包桌面 App
open web/electron/dist/mac-arm64/Cubes.app
```

- 打包时 `web/electron/scripts/sign.cjs` 用一张本地自签证书给整个 App 签名（证书第一次自动生成，放在 `~/Library/Application Support/com.ice.cubes.desktop/signing/` 的独立钥匙串里，不碰登录钥匙串）。macOS 按签名记住屏幕录制和辅助功能授权，同一张证书签出来的新版本会被认作同一个 App，所以更新后不用重新授权。

- 第一次打开是空的：先给负责人起名字、挑样子，团队里只有它，也还没有模型服务。到「设置 → 模型」加一个之后它和成员才能回复。
- 数据在 `~/Library/Application Support/com.ice.cubes.desktop/`：数据库是 `data/cubes.sqlite3`，头像缓存在 `data/avatars/`。因为里面存着明文 API key，目录权限 0700、数据库文件 0600。这个目录和开发用的 `backend/data/` 互不相干，桌面版不会搬用开发数据。
- 日志在 `~/Library/Application Support/com.ice.cubes.desktop/logs/backend.log`，每次启动覆盖。后端 60 秒内没起来时，窗口里会显示原因、日志路径和「重试」。窗口也不会在后端就绪前去连默认端口。
- 后端只绑 `127.0.0.1` 上的随机端口，并带一个每次启动新生成的访问令牌；只有 App 自己的窗口通过 IPC 拿到它。本机别的进程去 `curl` 那个端口只会拿到 401。退出 App 时后端子进程先收到 SIGTERM，两秒内没退就 SIGKILL。
- 「设置 → 连接」默认是「本机」：地址由 App 自己决定，不能填。选「远端服务器」就按「部署到服务器」连接，切换后窗口重新加载。
- `npm run desktop` 也自带后端，但跑的是源码：1420 上没有开发服务器就先起一个，用 `backend/.venv` 执行 `backend/desktop_main.py`，按 `backend/.env` 读设置（数据库、模型、搜索 key 都和手动起的开发后端一样），同样是随机端口加访问令牌，并放行开发服务器 `http://127.0.0.1:1420`。所以不需要另开 uvicorn，改了后端代码重开一次 `npm run desktop` 就生效。日志在 `backend/data/logs/desktop-dev.log`。需要先按「启动后端」建好 `.venv` 和 `.env`；没有 `.venv` 时窗口里会提示。
- 窗口进程崩了或被强制退出时，后端发现自己的父进程没了会在一秒内自己退出，不会留下孤儿进程。
- 浏览器模式（`npm run dev`）没有自带后端，照旧连「设置」里的地址。
