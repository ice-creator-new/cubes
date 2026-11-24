# 在电脑上做事

本机、云端沙盒与它们各自的工具。安装与部署见 [setup.md](setup.md)。

## 电脑

「本机」就是后端正在跑的这台电脑。桌面版的后端在这台 Mac 上，所以本机直接操作这台 Mac：终端命令、截屏、点击，不用运行启动命令。本机的网页用 Cubes 自己的 Chrome：第一次用到浏览器时会在 `数据目录/browser-profile` 下开一个独立配置的窗口（桌面版是 `~/Library/Application Support/com.ice.cubes.desktop/browser-profile`，开发时是 `backend/data/browser-profile`，可以用 `CUBES_BROWSER_PROFILE` 改），不动用户平时的 Chrome 配置和登录；窗口看得见，需要登录或过验证码时用户可以接手。后端退出时会把这次启动的这个窗口关掉，用户自己的 Chrome 不受影响。第一次截屏或点击时，macOS 会要求允许「屏幕录制」和「辅助功能」。打包好的 App 在系统设置里就叫 Cubes。`npm run desktop` 的开发版在系统设置里叫 Electron（不是终端）：`scripts/own.py` 让它从终端启动时成为自己的“负责进程”，权限记在它自己名下。屏幕录制授权后要重开一次 App 才生效。云端是另一台连到 `/computer/agent` 的电脑，面板里有要在那边运行的命令。下面的 Apple container 只是在 Mac 上另开一台 Linux 虚拟机、把它当成云端用的办法，不是本机。

- 需要 [Apple container](https://github.com/apple/container)。`computer/sandbox.py`（本机只要 `computer/sandbox-requirements.txt` 里的 websockets）第一次运行会用 `computer/Containerfile` 构建 `cubes-sandbox:3`：Debian 13、XFCE、Chromium（带调试端口，browser-use 通过 CDP 控制的就是屏幕上这个窗口）、Thunar、终端、Mousepad、中文字体、python3、git、curl、jq，以及 browser-use；用户 `cubes` 可以免密 sudo，方便 bot 装软件。默认走 DaoCloud、中科大和清华镜像，换源用 `--build-arg`。
- 每次启动一个新名字的容器（2 核、2 GB），只在容器网络网关上转发 `/computer/agent` 这一个路径到后端，沙盒里访问别的接口都是 404。实时画面默认在 `http://127.0.0.1:6080/vnc.html`（`--view-port` 可改）。按 Ctrl+C 关闭并删除沙盒，里面的文件不保留。
- Apple container 的后台服务开机不会自己起来；`sandbox.py` 发现没开会先启动它。不用时运行 `container system stop` 可以把它也关掉。
- 沙盒操作有自己的一份上限，随「设置 → 团队 → 每次能做多少步」的档位走：默认档没有上限，标准档一次任务最多 40 步（`MAX_COMPUTER_STEPS_PER_RUN`），其中终端 30 步、浏览器 25 步、屏幕 8 步（`MAX_COMPUTER_SHELL_STEPS_PER_RUN`、`MAX_COMPUTER_BROWSER_STEPS_PER_RUN`、`MAX_COMPUTER_SCREEN_STEPS_PER_RUN`），宽松档 120 / 90 / 75 / 24，很长档 300 / 225 / 190 / 60；不占普通工具的次数上限，每一步多给一次模型调用。终端命令默认最多跑 60 秒，可以要到 600 秒；输出超过 12000 字从中间截断。后端按操作给等待时间：终端是命令超时再加 5 秒，浏览器每个动作 30 秒（一次最多 150 秒），屏幕 20 秒。
- 网页结果只给视口里的前 40 个元素并注明还有多少没显示（要看更多用 `search_page`）；打开那条动作还能看到完整的元素快照（最多 6000 字）。屏幕截图在上下文里留 2 张、浏览器截图留 1 张，更早的换成一行「已省略」。
