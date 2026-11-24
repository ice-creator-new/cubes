"""The Linux sandbox VM every bot shares, driven through the agent that runs inside it.

``computer/sandbox.py`` starts the VM in Apple container; the agent inside
(``computer/cubes_computer.py``) opens a WebSocket to ``/computer/agent`` with the pairing token
and introduces itself. From then on the backend sends one call at a time. Bots reach it through
three tools, so each step can take the cheapest way that works:

- ``computer_shell``: a bash command; the answer is its exit code and output.
- ``computer_browser``: one call runs up to five actions by element index over CDP. In the
  sandbox that is browser-use attached to the on-screen Chromium; on the host it is Cubes' own
  Chrome window (``host_browser.py``), which also reads the page's text, evaluates a script and
  lists console errors. The answer carries each action's result, the numbered elements and a
  ``snapshot`` for the action row. A batch that holds a step the backend cannot validate runs
  its valid prefix and reports the rest as skipped.
- ``computer``: the screen by pixels for desktop programs; every answer is a fresh screenshot.
  Only offered to a bot whose model can see images.

The backend guards the tools with what it can know without the agent's help: a run that has
already opened or clicked in the browser is taken to have Chromium in front, so screen clicks,
typing and keys are refused; address-bar keys and typed URLs are refused outright; a verbatim
repeat of a step that changed nothing is refused once and ends the run the second time; browser
answers that politely report a failure (``Element index … not available``) are classified as
failures; and every op gets the deadline the agent needs for it (protocol 4 has no focused
window, DOM fingerprint or stable refs yet).

Only version-4 agents are accepted: version 1 drove the user's own Mac, version 2 had no shell or
browser, version 3 drove the browser one Playwright step at a time.
Only a screen action holds the machine: pointer, keys and pixels cannot be shared, so a run
that wants the screen waits for whoever has it (and for at most one 30 s idle lease). Shell
commands run in their own processes and browser batches in their own window, so those never
wait for the hold — two runs' work proceeds side by side. The pairing token is stored once per
database, so the command that starts the sandbox keeps working across restarts; the desktop
backend still moves to a new port every launch.

Wire format (JSON text frames):

- agent → backend, first: ``{"type": "hello", "version": 4, "name", "os",
  "screen": {"width", "height"}, "view"?}``; ``view`` is the live view URL on the Mac.
- backend → agent: ``{"type": "call", "id", "op", "args"}``; ``op`` is ``shell``, ``browser`` or
  a screen action.
- agent → backend: ``{"type": "result", "id", "ok", "error"?, ...}`` with the shell's
  ``exit_code``/``stdout``/``stderr``, the browser's ``title``/``url``/``tabs``/``steps``/
  ``elements``/``above``/``below`` (and the host's ``snapshot``), or a screenshot's
  ``image``/``mime``/``cursor``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from .errors import ToolError
from .toolkit import ToolInputError

PROTOCOL_VERSION = 4
# One sandbox, three ways in: commands, the browser by its page elements, and the screen.
SHELL_TOOL = "computer_shell"
BROWSER_TOOL = "computer_browser"
SCREEN_TOOL = "computer"
TOOL_LABELS = {SHELL_TOOL: "沙盒终端", BROWSER_TOOL: "沙盒浏览器", SCREEN_TOOL: "沙盒屏幕"}
TOOL_NAMES = tuple(TOOL_LABELS)
# How long the agent may take per op, computed on the backend so both sides agree: a command
# gets its own timeout plus this much grace, a browser step gets BROWSER_STEP_SECONDS (the agent
# runs each of the five actions with a 30 s action timeout), and a screen action gets the settle
# time plus a screenshot. The old single 45 s cut off browser batches the agent was still running.
SHELL_GRACE_SECONDS = 5.0
BROWSER_STEP_SECONDS = 30.0
BROWSER_MAX_SECONDS = 150.0
SCREEN_SECONDS = 20.0
# How long a run's screen hold survives without a screen action. A run that moved on to the
# terminal or the browser must not keep every other run waiting for the pointer.
SCREEN_LEASE_SECONDS = 30.0
HELLO_SECONDS = 10.0
POLL_SECONDS = 0.05
# A refused handshake; a newer agent took over.
CLOSE_UNAUTHORIZED = 4401
CLOSE_OUTDATED = 4426
CLOSE_REPLACED = 4409

MAX_TEXT_CHARS = 2_000
MAX_KEYS_CHARS = 40
MAX_SCROLL = 10
MAX_WAIT_SECONDS = 10
MAX_COMMAND_CHARS = 8_000
DEFAULT_SHELL_SECONDS = 60
MAX_SHELL_SECONDS = 600
MAX_URL_CHARS = 2_000
MAX_VALUE_CHARS = 4_000
MAX_JS_CHARS = 8_000

NOT_CONNECTED = "沙盒电脑没有开：请用户在「电脑」面板里启动沙盒。"
DISCONNECTED = "沙盒电脑断开了连接，这一步没有完成。"
PLACES = ("local", "cloud")


def offline_message(place: str, *, mixed: bool) -> str:
    """Why a call cannot run: the machine the user picked is not connected."""
    if mixed and place == "local":
        return "本机和云端的电脑都没连上。请用户在「电脑」面板里启动至少一台。"
    if place == "cloud":
        return "云端电脑没有连上：请用户在「电脑」面板里复制命令，在服务器上启动。"
    return NOT_CONNECTED


def requested_place(raw: Any) -> str | None:
    """The machine a tool call named, or None when it left the choice to the setting."""
    if raw is None or raw == "":
        return None
    if raw in PLACES:
        return str(raw)
    raise ToolInputError("place 只能是 local 或 cloud，本次操作未执行。")

# What each action does, in the words the action row shows.
ACTIONS: dict[str, str] = {
    "screenshot": "看屏幕",
    "click": "点击",
    "double_click": "双击",
    "right_click": "右键点击",
    "move": "移动鼠标",
    "drag": "拖动",
    "scroll": "滚动",
    "type": "输入文字",
    "key": "按键",
    "wait": "等待",
    "zoom": "放大看",
}
POINT_ACTIONS = ("click", "double_click", "right_click", "move", "drag")
# Screen actions that act on what is in front; ``move`` and ``screenshot`` never touch a control.
SCREEN_DRIVING = ("click", "double_click", "right_click", "drag", "type", "key")
SCROLL_DIRECTIONS = {"up": "向上", "down": "向下", "left": "向左", "right": "向右"}
KEYS_PATTERN = re.compile(r"[A-Za-z0-9+_\- ]+")
# Elements listed in a browser answer; the rest waits for search_page. A full 12 KB DOM every
# step was the audit's J-class waste.
ELEMENTS_IN_OBSERVATION = 40

SCREEN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SCREEN_TOOL,
        "description": (
            "看沙盒的屏幕，用鼠标键盘按像素操作。只给桌面程序用：文件管理器、终端窗口、系统对话框、"
            "装好的 App、要看清画面的时候。网页上的一切（打开网址、点击、输入、滚动、找内容）都用 "
            f"{BROWSER_TOOL}：运行过浏览器之后，这个工具的点击、输入、按键会被后台直接拒绝，"
            "按地址栏（ctrl+l、cmd+l、F6）和输入网址也会被拒绝。每个动作之后返回一张新截图，"
            "红圈是鼠标位置；画面和上一张一样时会明确告诉你。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": (
                        "screenshot 看屏幕；click/double_click/right_click 点击 x,y；move 移动鼠标到 x,y；"
                        "drag 从 x,y 拖到 to_x,to_y；scroll 滚动（可带 x,y）；type 输入 text；"
                        "key 按键 keys；wait 等待 seconds 秒；zoom 把 x,y,width,height 这块区域放大"
                    ),
                },
                "x": {"type": "integer", "description": "截图上的横坐标（像素）"},
                "y": {"type": "integer", "description": "截图上的纵坐标（像素）"},
                "to_x": {"type": "integer", "description": "drag 的终点横坐标"},
                "to_y": {"type": "integer", "description": "drag 的终点纵坐标"},
                "text": {"type": "string", "description": "type 要输入的文字，中文也可以；回车请用 key"},
                "keys": {
                    "type": "string",
                    "description": "key 要按的键，用 + 连接，例如 enter、ctrl+l、ctrl+shift+t、alt+f4",
                },
                "direction": {
                    "type": "string",
                    "enum": list(SCROLL_DIRECTIONS),
                    "description": "scroll 的方向，默认 down",
                },
                "amount": {"type": "integer", "description": "scroll 的格数，1 到 10，默认 3"},
                "seconds": {"type": "integer", "description": "wait 的秒数，1 到 10，默认 2"},
                "width": {"type": "integer", "description": "zoom 区域的宽（截图像素）"},
                "height": {"type": "integer", "description": "zoom 区域的高（截图像素）"},
            },
            "required": ["action"],
        },
    },
}

SHELL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SHELL_TOOL,
        "description": (
            "在沙盒里用 bash 执行一条命令，返回退出码和输出。文件和文件夹、下载、安装软件、跑脚本、"
            "处理文本和数据，都用它。要网页上的数据也先用它：curl、python3 -c、jq 一条命令就能拿到"
            "结构化结果，比用浏览器点很多下快得多、也更稳；只有要登录、要点按钮或者页面靠 JS 才用浏览器。"
            "可以用 sudo；没有交互式输入，需要确认的命令请加 -y。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令，可以用管道和 &&，例如 ls -la ~/Desktop",
                },
                "cwd": {"type": "string", "description": "在哪个目录执行，默认主目录 /home/cubes"},
                "timeout": {
                    "type": "integer",
                    "description": f"最多等几秒，1 到 {MAX_SHELL_SECONDS}，默认 {DEFAULT_SHELL_SECONDS}",
                },
                "place": {
                    "type": "string",
                    "enum": ["local", "cloud"],
                    "description": "哪一台：local 是这台 Mac 上的沙盒，cloud 是云端。用户没选「混合」时不要填。",
                },
            },
            "required": ["command"],
        },
    },
}

# What a browser call may do; each is one browser-use action, and ``label`` is how the action row
# says it.
BROWSER_ACTIONS: dict[str, str] = {
    "open": "打开",
    "back": "返回",
    "click": "点击",
    "input": "填写",
    "select": "选择",
    "keys": "按键",
    "scroll": "滚动",
    "find_text": "找到文字",
    "search_page": "页内搜索",
    "switch_tab": "切换标签页",
    "close_tab": "关闭标签页",
    "wait": "等待",
    # Read and debug without touching pixels. Only the Mac's own Chrome implements these; the
    # sandbox refuses them and says which tool replaces them.
    "read_text": "读正文",
    "run_js": "执行脚本",
    "console": "看控制台",
    # Not a browser-use action: it turns on the answer's screenshot, which is what the model
    # means when it puts it in the list (seen in the audit: [find_text, screenshot]).
    "screenshot": "看一眼页面",
}
# Names models write for the same action; the audit found ``{"action": "key", "text": "Return"}``.
BROWSER_ALIASES = {"key": "keys", "type": "input", "press": "keys", "goto": "open"}
MAX_BROWSER_ACTIONS = 5

BROWSER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": BROWSER_TOOL,
        "description": (
            "操作屏幕上的 Chromium：打开网址、在网页上点击、填表、翻页、登录。按页面元素的编号操作，"
            "不用坐标：先 open，看返回的 [编号]<标签 属性/> 元素清单，再 click / input / select。"
            "一次可以给 1 到 5 个动作，按顺序执行：页面一变就停下，不合法的那一步会被跳过并说明原因，"
            "前面的照常执行。每次返回标题、网址、标签页、每个动作的结果（✓成功、✗没成功、…没执行）、"
            "视口里的元素，以及上下还有几屏。网页上的正文用 read_text，整页找内容用 search_page，"
            "调试页面用 run_js（执行一段 JS 并拿回结果）和 console（最近的错误与警告）"
            "（read_text、run_js、console 只有本机 Chrome 支持，沙盒会说明替代做法）。"
            "不要用 terminal 里的 curl 或手写 DevTools 脚本来点页面。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_BROWSER_ACTIONS,
                    "description": (
                        "按顺序执行的动作。例如填表：[{action: input, index: 3, text: \"张三\"}, "
                        "{action: input, index: 5, text: \"上海\"}, {action: click, index: 8}]。"
                        "会换页的动作（open、click 链接、keys Enter）放在最后。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": list(BROWSER_ACTIONS),
                                "description": (
                                    "open 打开 url；back 返回上一页；click 点击 index；input 往 index 输入 text；"
                                    "select 在下拉框 index 里选 text；keys 按键 keys（Enter、Escape、Control+a）；"
                                    "scroll 向下（up 或 down=false 向上）滚 pages 屏；find_text 滚到 text 所在处；"
                                    "search_page 在整页里找 pattern，不用翻页；switch_tab / close_tab 用 tab_id；"
                                    "wait 等 seconds 秒；read_text 读页面可见正文；run_js 执行 code 并返回结果；"
                                    "console 看最近的错误和警告；screenshot 顺带要一张截图（等于 screenshot: true）"
                                ),
                            },
                            "url": {"type": "string", "description": "open 的网址"},
                            "new_tab": {"type": "boolean", "description": "open 时在新标签页打开"},
                            "index": {"type": "integer", "description": "元素编号，就是方括号里的数字"},
                            "text": {"type": "string", "description": "input 要输入的文字、select 的选项、find_text 要找的文字"},
                            "keys": {"type": "string", "description": "keys 要按的键"},
                            "up": {"type": "boolean", "description": "scroll 的方向：true 向上"},
                            "down": {"type": "boolean", "description": "scroll 的方向，默认 true（向下）"},
                            "pages": {"type": "number", "description": "scroll 几屏，默认 1"},
                            "pattern": {"type": "string", "description": "search_page 要找的文字"},
                            "tab_id": {"type": "string", "description": "switch_tab / close_tab 的标签页编号（4 位）"},
                            "seconds": {"type": "integer", "description": "wait 的秒数，1 到 10"},
                            "code": {
                                "type": "string",
                                "description": "run_js 要执行的 JavaScript 表达式；结果按 JSON 返回",
                            },
                        },
                        "required": ["action"],
                    },
                },
                "screenshot": {
                    "type": "boolean",
                    "description": "需要亲眼看页面时设为 true，最后会附一张屏幕截图",
                },
            },
            "required": ["actions"],
        },
    },
}


def schemas(*, vision: bool) -> list[dict[str, Any]]:
    """Commands and the browser for every bot; the screen only for one that can see it."""
    offered = [SHELL_SCHEMA, BROWSER_SCHEMA]
    if vision:
        offered.append(SCREEN_SCHEMA)
    return [_with_place(item) for item in offered]


def _with_place(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema["function"]["parameters"]["properties"]
    if "place" in properties:
        return schema
    cloned = copy.deepcopy(schema)
    cloned["function"]["parameters"]["properties"]["place"] = {
        "type": "string",
        "enum": ["local", "cloud"],
        "description": "哪一台：local 是用户正在用的这台电脑，cloud 是云端。用户没选「混合」时不要填。",
    }
    return cloned


def prompt_hint(name: str, system: str, screen: dict, *, vision: bool, host: bool = False) -> str:
    """What a bot needs to drive the sandbox: what is there, which tool fits which goal, when
    to stop. The audit of 129 real actions found the prompt's numbered priority ignored (73%
    of actions drove the browser by pixels), so this is written by goal, and the tools say the
    same thing the backend enforces."""
    if host:
        lines = [
            f"\n\n你正在操作用户眼前这台电脑「{name}」（{system}，屏幕 {screen['width']}×{screen['height']}）。"
            "这不是虚拟机，也不是沙盒，不需要用户先运行任何命令：终端命令直接在这台电脑上执行，"
            "点击打在真实屏幕上，能碰到用户的文件。不要删除、移动或覆盖用户没点名的东西。",
            "选工具（先看要做的事，同一个办法失败两次就换工具）：",
            f"- 文件和命令、下载、安装、跑脚本 → {SHELL_TOOL}。这就是这台电脑自己的终端，"
            "主目录是用户的家目录。不要在终端里用 open 打开网址，也不要手写 Chrome DevTools（CDP）"
            f"脚本去点页面：网页上的事都用 {BROWSER_TOOL}。",
            f"- 网页（打开、点击、填表、翻页、登录） → {BROWSER_TOOL}：这是 Cubes 自己的 Chrome 窗口，"
            "独立的配置，不是用户平时的浏览器；用户能看见，也能接手登录或过验证码。先 open，"
            "再看返回的 [编号]<标签 属性/> 元素清单，按编号 click / input / select / keys；"
            "读正文用 read_text，整页找内容用 search_page，调试页面用 run_js 和 console；"
            "要亲眼看页面就在同一个调用里带上 screenshot。",
        ]
        if vision:
            lines.append(
                f"- 看本地图片文件（截图、下载的图片） → view_image：给路径，可带 x,y,width,height "
                "只裁一块。不要用 Preview 之类的程序打开图片再截屏，那既慢又看不清。"
            )
            lines.append(
                f"- 看屏幕、点 App、填真正的窗口 → {SCREEN_TOOL}：先 screenshot，再按截图像素点在控件中间；"
                "控件太小就用 zoom 把那一块放大再点；每次一个动作；输入前先点一下输入框；"
                "回车用 key enter；快捷键用 cmd，不要用 ctrl。"
                "第一次截屏或点击时，macOS 可能会要求允许屏幕录制和辅助功能，那是系统弹窗，不是要用户跑命令。"
            )
        else:
            lines.append("你看不到屏幕截图，所以只用终端；需要看画面的事请告诉用户做不到。")
        lines.append(
            "通用规则：同一个办法失败两次就换工具，不要原样重试——完全一样的步骤会直接被拒绝，第三次会结束任务；"
            "每个任务第一次使用会先请用户允许，用户拒绝或停止后不要再调用；做完后用文字告诉用户结果。"
        )
        return "\n".join(lines)
    lines = [
        f"\n\n你有一台 Linux 沙盒电脑「{name}」（{system}，XFCE 桌面，屏幕 {screen['width']}×{screen['height']}）。"
        "它是隔离的虚拟机，不是用户的电脑：碰不到用户的文件，能上网，可以用 sudo 装软件，"
        "关掉后里面的一切都会消失。主目录 /home/cubes，桌面文件夹 ~/Desktop；"
        "装好了 Chromium、Thunar 文件管理器、xfce4-terminal、Mousepad、python3、git、curl、jq。",
        "选工具（先看要做的事，同一个办法失败两次就换工具）：",
        f"- 文件和命令、下载、安装、跑脚本、处理文本数据 → {SHELL_TOOL}：一条命令就够，"
        "比在屏幕上点很多下快得多，例如 ls、cat、python3 -c、pip install。",
        f"- 网页上的数据（查票、查价格、读文章、抓接口） → 也先用 {SHELL_TOOL}："
        "curl、python3 -c 'import requests'、jq 一次调用就拿到结构化结果，不用一屏屏翻；"
        "只有要登录、要点按钮、页面靠 JS 渲染时才用浏览器。",
        f"- 网页上要点、要填表、要翻页、要登录 → {BROWSER_TOOL}：先 open 网址，再看返回的 [编号]，"
        "再 click / input；填表时把几个 input 和最后的 click 放进同一次调用。长页面里找内容用 search_page，"
        "不要一屏一屏地滚；要看画面就在同一个调用里带上 screenshot。"
        "不要在终端里手写 Chrome DevTools（CDP）脚本来点页面；read_text、run_js、console 只有本机 Chrome "
        f"支持，沙盒里读正文用 search_page，调试用 {SHELL_TOOL} 的 curl。",
    ]
    if vision:
        lines.append(
            "- 看本地图片文件（截图、下载的图片） → view_image：给路径，可带 x,y,width,height "
            "只裁一块；不要把图片打开再截屏，那既慢又看不清。"
        )
        lines.append(
            f"- 桌面程序（文件管理器、终端窗口、系统对话框、装好的 App、需要看清画面布局） → {SCREEN_TOOL}："
            "先 screenshot，再按截图坐标点在控件中间；控件太小就用 zoom 把那一块放大再点；"
            "每次一个动作，确认生效再做下一步；"
            "输入前先点一下输入框；回车用 key enter；快捷键用 ctrl，不要用 cmd。"
            f"网页上不要用它：运行过浏览器之后，后台会拒绝这个工具在屏幕上的点击、输入和按键，"
            "也会拒绝 ctrl+l、cmd+l、F6 和直接输入网址（这些请用 computer_browser 的 open 和 click）。"
            "要切到别的窗口（例如刚用浏览器打开过页面，现在要操作桌面程序）先按 key alt+tab。"
        )
    else:
        lines.append("你看不到屏幕截图，所以只用上面两个工具；需要看画面的事请告诉用户做不到。")
    lines.append(
        "通用规则：先想清楚哪一步用哪个工具；同一个办法失败两次就换工具（换另一个电脑工具，"
        "或者用 shell 的 curl / python），不要原样重试——完全一样的步骤会直接被拒绝，第三次会结束任务；"
        "每个任务第一次使用会先请用户允许，用户拒绝或停止后不要再调用；做完后用文字告诉用户结果。"
    )
    return "\n".join(lines)


def _integer(payload: dict, field: str, *, required: bool = True) -> int | None:
    raw = payload.get(field)
    if raw is None:
        if required:
            raise ToolInputError(f"缺少 {field}，本次操作未执行。")
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolInputError(f"{field} 必须是整数，本次操作未执行。")
    return int(round(raw))


def _point(payload: dict, x_field: str, y_field: str, screen: dict) -> tuple[int, int]:
    x = _integer(payload, x_field)
    y = _integer(payload, y_field)
    assert x is not None and y is not None
    width, height = int(screen["width"]), int(screen["height"])
    if not (0 <= x < width and 0 <= y < height):
        raise ToolInputError(
            f"坐标 ({x}, {y}) 超出截图范围（{width}×{height}），本次操作未执行。"
        )
    return x, y


def _bounded(payload: dict, field: str, default: int, maximum: int) -> int:
    value = _integer(payload, field, required=False)
    if value is None:
        return default
    if value < 1 or value > maximum:
        raise ToolInputError(f"{field} 必须在 1 到 {maximum} 之间，本次操作未执行。")
    return value


@dataclass(frozen=True)
class SandboxCall:
    """One call after the backend's own validation: what the agent gets and what it left out."""

    op: str
    args: dict[str, Any]
    # What the action row and the model see: "运行「ls」", "点击 #3", "输入「你好」".
    done: str
    # Browser steps the backend refused to send, with the reason, in the order they appeared.
    skipped: list[dict[str, str]] = field(default_factory=list)


def parse_call(tool: str, payload: dict, screen: dict, *, vision: bool) -> SandboxCall:
    """The call as the agent takes it; a bad call is the model's to fix."""
    if tool == SHELL_TOOL:
        return _parse_shell(payload)
    if tool == BROWSER_TOOL:
        return _parse_browser(payload, vision=vision)
    op = payload.get("action")
    if not isinstance(op, str) or op not in ACTIONS:
        raise ToolInputError(
            f"action 只能是 {'、'.join(ACTIONS)} 之一，本次操作未执行。"
        )
    if op == "screenshot":
        return SandboxCall(op, {}, "看了一眼屏幕")
    if op == "zoom":
        x, y = _point(payload, "x", "y", screen)
        width = _integer(payload, "width")
        height = _integer(payload, "height")
        assert width is not None and height is not None
        shot_w, shot_h = int(screen["width"]), int(screen["height"])
        if width < 8 or height < 8:
            raise ToolInputError("zoom 的 width、height 至少 8 像素，本次操作未执行。")
        if x + width > shot_w or y + height > shot_h:
            raise ToolInputError(
                f"zoom 区域超出截图范围（{shot_w}×{shot_h}），本次操作未执行。"
            )
        return SandboxCall(
            op, {"x": x, "y": y, "width": width, "height": height}, f"放大 ({x}, {y}) {width}×{height}"
        )
    if op in POINT_ACTIONS:
        x, y = _point(payload, "x", "y", screen)
        if op == "drag":
            to_x, to_y = _point(payload, "to_x", "to_y", screen)
            return SandboxCall(
                op,
                {"x": x, "y": y, "to_x": to_x, "to_y": to_y},
                f"拖动 ({x}, {y}) 到 ({to_x}, {to_y})",
            )
        return SandboxCall(op, {"x": x, "y": y}, f"{ACTIONS[op]} ({x}, {y})")
    if op == "scroll":
        direction = payload.get("direction", "down")
        if direction not in SCROLL_DIRECTIONS:
            raise ToolInputError("direction 只能是 up、down、left 或 right，本次操作未执行。")
        amount = _bounded(payload, "amount", 3, MAX_SCROLL)
        args: dict[str, Any] = {"direction": direction, "amount": amount}
        if payload.get("x") is not None or payload.get("y") is not None:
            args["x"], args["y"] = _point(payload, "x", "y", screen)
        return SandboxCall(op, args, f"{SCROLL_DIRECTIONS[direction]}滚动 {amount} 格")
    if op == "type":
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ToolInputError("type 需要 text，本次操作未执行。")
        if len(text) > MAX_TEXT_CHARS:
            raise ToolInputError(f"一次最多输入 {MAX_TEXT_CHARS} 个字符，本次操作未执行。")
        shown = text if len(text) <= 20 else text[:19] + "…"
        return SandboxCall(op, {"text": text}, f"输入「{shown}」")
    if op == "key":
        # Weak models put the key in ``text``; it can only mean the key here.
        keys = payload.get("keys") if payload.get("keys") is not None else payload.get("text")
        if not isinstance(keys, str) or not keys.strip():
            raise ToolInputError("key 需要 keys，例如 enter 或 ctrl+c，本次操作未执行。")
        keys = keys.strip().lower()
        if len(keys) > MAX_KEYS_CHARS or not KEYS_PATTERN.fullmatch(keys):
            raise ToolInputError("keys 只能是用 + 连接的键名，例如 ctrl+c，本次操作未执行。")
        return SandboxCall(op, {"keys": keys}, f"按 {keys}")
    seconds = _bounded(payload, "seconds", 2, MAX_WAIT_SECONDS)
    return SandboxCall(op, {"seconds": seconds}, f"等待 {seconds} 秒")


def _parse_shell(payload: dict) -> tuple[str, dict, str]:
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolInputError("需要 command，本次操作未执行。")
    if len(command) > MAX_COMMAND_CHARS:
        raise ToolInputError(f"命令不能超过 {MAX_COMMAND_CHARS} 个字符，本次操作未执行。")
    args: dict[str, Any] = {
        "command": command,
        "timeout": _bounded(payload, "timeout", DEFAULT_SHELL_SECONDS, MAX_SHELL_SECONDS),
    }
    cwd = payload.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.strip() or len(cwd) > 500:
            raise ToolInputError("cwd 必须是一个目录路径，本次操作未执行。")
        args["cwd"] = cwd.strip()
    return SandboxCall("shell", args, f"运行「{shorten_middle(' '.join(command.split()), SUMMARY_COMMAND_CHARS)}」")


# The longest command the one-line summary quotes before it shortens. The row, the run's step
# and the model's trace all read this sentence; the whole command lives in the detail.
SUMMARY_COMMAND_CHARS = 60


def shorten_middle(text: str, limit: int) -> str:
    """``text`` if it fits, else its start and its end around ``…``.

    A command's end is often what tells two runs apart (the file it reads, the port it serves,
    the ``| tail`` it pipes into), so the cut goes in the middle, never at the end.
    """
    if len(text) <= limit:
        return text
    room = max(2, limit - 1)
    head = (room + 1) // 2
    tail = room - head
    return f"{text[:head].rstrip()}…{text[-tail:].lstrip()}"


def _parse_browser(payload: dict, *, vision: bool) -> SandboxCall:
    """A browser batch: each step is validated on its own, the valid prefix runs, and the rest
    is reported as skipped with its reason.

    The audit found a whole call thrown away because one step was wrong (``[find_text,
    screenshot]`` lost its find_text); a prefix is what is safe to run, since a later step aims
    at indexes the skipped one may have changed.
    """
    raw = payload.get("actions")
    # A weak model sometimes sends one action flat, the way the earlier tool took it.
    if raw is None and isinstance(payload.get("action"), str):
        raw = [payload]
    if not isinstance(raw, list) or not raw:
        raise ToolInputError("需要 actions：1 到 5 个动作组成的数组，本次操作未执行。")
    if len(raw) > MAX_BROWSER_ACTIONS:
        raise ToolInputError(f"一次最多 {MAX_BROWSER_ACTIONS} 个动作，本次操作未执行。")
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    wants_screenshot = vision and payload.get("screenshot") is True
    # Only a bad step stops the batch: it may have been meant as the one that changes the page,
    # so a later step's index can no longer be trusted. A step that simply cannot run (a
    # screenshot for a bot that cannot see) does not invalidate the ones after it.
    broken = False
    for position, step in enumerate(raw, 1):
        name = _step_name(step)
        if broken:
            skipped.append(
                {"action": name, "reason": f"第 {position} 个动作（{name}）：前面一步无效，没有执行"}
            )
            continue
        try:
            action = _browser_action(step, position)
        except ToolInputError as exc:
            skipped.append({"action": name, "reason": _skip_reason(str(exc))})
            broken = True
            continue
        if action["action"] == "screenshot":
            # A step in the list, a flag on the call: the agent takes it as the top-level switch.
            if not vision:
                skipped.append({"action": name, "reason": "screenshot：你看不到截图，没有执行"})
                continue
            wants_screenshot = True
            continue
        actions.append(action)
    if not actions and not wants_screenshot:
        raise ToolInputError(_no_action(skipped))
    args: dict[str, Any] = {"actions": actions}
    if wants_screenshot:
        args["screenshot"] = True
    words = [_browser_words(step) for step in actions] or ["看一眼页面"]
    shown = "，".join(words[:3]) + (f" 等 {len(words)} 步" if len(words) > 3 else "")
    return SandboxCall("browser", args, shown, skipped)


def _step_name(step: Any) -> str:
    """The action name a skipped step is reported under, whether or not it is a real one."""
    name = step.get("action") if isinstance(step, dict) else None
    return name if isinstance(name, str) and name else "?"


def _skip_reason(raw: str) -> str:
    """A validation message without the "not executed" tail it carries for a whole call."""
    return raw.removesuffix("本次操作未执行。").rstrip("，") or "参数不对"


def _no_action(skipped: list[dict[str, str]]) -> str:
    reasons = "；".join(entry["reason"] for entry in skipped)
    return f"这次调用没有可执行的动作：{reasons}。请改好再发。"


def _browser_action(step: Any, position: int) -> dict[str, Any]:
    """One action as browser-use takes it; a bad one names its place in the list."""
    where = f"第 {position} 个动作"
    if not isinstance(step, dict):
        raise ToolInputError(f"{where}必须是对象，本次操作未执行。")
    name = step.get("action")
    if isinstance(name, str):
        name = BROWSER_ALIASES.get(name.strip().lower(), name)
    if not isinstance(name, str) or name not in BROWSER_ACTIONS:
        raise ToolInputError(f"{where}的 action 只能是 {'、'.join(BROWSER_ACTIONS)} 之一，本次操作未执行。")
    if name == "screenshot":
        return {"action": name}
    if name in ("read_text", "console"):
        return {"action": name}
    if name == "back":
        return {"action": name}

    def text(
        field: str,
        *,
        fallback: str | None = None,
        fallback2: str | None = None,
        limit: int = MAX_VALUE_CHARS,
        blank: bool = False,
    ) -> str:
        """``field`` as a string; the fallbacks name fields weak models put it in instead."""
        value = step.get(field)
        for other in (fallback, fallback2):
            if value is None and other is not None:
                value = step.get(other)
        if not isinstance(value, str) or (not blank and not value.strip()):
            raise ToolInputError(f"{where}（{name}）需要 {field}，本次操作未执行。")
        if len(value) > limit:
            raise ToolInputError(f"{where}的 {field} 不能超过 {limit} 个字符，本次操作未执行。")
        return value

    def index() -> int:
        value = _integer(step, "index")
        assert value is not None
        if value < 0:
            raise ToolInputError(f"{where}的 index 不能是负数，本次操作未执行。")
        return value

    if name == "open":
        # ``goto`` is the alias, ``text`` the field weak models put the address in.
        url = text("url", fallback="text", limit=MAX_URL_CHARS).strip()
        if "://" not in url:
            url = f"https://{url}"
        if not url.startswith(("http://", "https://")):
            raise ToolInputError(f"{where}的 url 只能是 http 或 https 网址，本次操作未执行。")
        return {"action": name, "url": url, "new_tab": step.get("new_tab") is True}
    if name == "click":
        return {"action": name, "index": index()}
    if name == "input":
        # An empty text is allowed: it clears the field; ``keys`` is the field drift, not a key.
        return {
            "action": name,
            "index": index(),
            "text": text("text", fallback="keys", blank=True),
            "clear": True,
        }
    if name == "select":
        return {"action": name, "index": index(), "text": text("text")}
    if name == "keys":
        # ``text`` and ``key`` are how the same field arrives ({"action": "key", "text": "Return"}).
        keys = step.get("keys")
        for field in ("text", "key"):
            if keys is None:
                keys = step.get(field)
        keys = keys or "Enter"
        if not isinstance(keys, str) or not KEYS_PATTERN.fullmatch(keys) or len(keys) > MAX_KEYS_CHARS:
            raise ToolInputError(f"{where}的 keys 只能是键名，例如 Enter 或 Control+a，本次操作未执行。")
        return {"action": name, "keys": _playwright_key(keys)}
    if name == "scroll":
        pages = step.get("pages", 1)
        if isinstance(pages, bool) or not isinstance(pages, (int, float)) or not 0 < pages <= 10:
            raise ToolInputError(f"{where}的 pages 必须在 0 到 10 之间，本次操作未执行。")
        # ``up`` was being dropped and turned a wanted scroll-up into a scroll-down.
        up = step.get("up") is True or step.get("down") is False
        return {"action": name, "down": not up, "pages": float(pages)}
    if name == "find_text":
        return {"action": name, "text": text("text", limit=200)}
    if name == "search_page":
        return {"action": name, "pattern": text("pattern", fallback="text", limit=200)}
    if name in ("switch_tab", "close_tab"):
        tab = step.get("tab_id")
        if not isinstance(tab, str) or len(tab.strip()) != 4:
            raise ToolInputError(f"{where}需要 tab_id（标签页列表里的 4 位编号），本次操作未执行。")
        return {"action": name, "tab_id": tab.strip()}
    if name == "run_js":
        return {"action": name, "code": text("code", fallback="script", fallback2="text", limit=MAX_JS_CHARS)}
    seconds = _integer(step, "seconds", required=False) or 2
    return {"action": name, "seconds": min(max(seconds, 1), MAX_WAIT_SECONDS)}


def _browser_words(step: dict[str, Any]) -> str:
    name = step["action"]
    label = BROWSER_ACTIONS[name]
    if name == "open":
        return f"{label} {step['url']}"
    if "index" in step:
        return f"{label} #{step['index']}"
    if name == "keys":
        return f"{label} {step['keys']}"
    if name in ("find_text", "search_page"):
        found = step.get("text") or step.get("pattern") or ""
        return f"{label}「{found if len(found) <= 12 else found[:11] + '…'}」"
    if name == "run_js":
        code = str(step.get("code") or "")
        return f"{label}「{code if len(code) <= 16 else code[:15] + '…'}」"
    return label


# Key names as browser-use and CDP expect them (Enter, not enter); models write them every way.
PLAYWRIGHT_KEYS = {
    name.lower(): name
    for name in (
        "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "Home", "End", "PageUp", "PageDown",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Control", "Shift", "Alt", "Meta", "Insert",
    )
}
PLAYWRIGHT_KEYS.update(
    {"return": "Enter", "esc": "Escape", "ctrl": "Control", "cmd": "Control", "command": "Control",
     "option": "Alt", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
     "del": "Delete", "pgup": "PageUp", "pgdn": "PageDown"}
)


def _playwright_key(raw: str) -> str:
    """``ctrl+a`` as ``Control+a``: named keys get their canonical casing, single characters stay."""
    parts = [part.strip() for part in raw.split("+") if part.strip()]
    return "+".join(PLAYWRIGHT_KEYS.get(part.lower(), part) for part in parts)


# ---------------------------------------------------------------------- guards
# What the backend can enforce without the agent's help. Protocol 4 reports neither the focused
# window nor a DOM fingerprint, so these rules use what the backend itself knows: which tools
# this run has already driven, the keys and text a screen action carries, and the identity of
# the page or the bytes of the screenshot each answer came back with.

# The keys that put the cursor in a browser's address bar on the sandbox's Chromium.
ADDRESS_BAR_KEYS = frozenset(
    {frozenset({"ctrl", "l"}), frozenset({"f6"}), frozenset({"alt", "d"})}
)
# "baidu.com", "https://www.12306.cn/index/", "bing.com/search?q=hello+world" — but not
# "hello world" and not "你好世界".
URL_PATTERN = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$", re.IGNORECASE)
BROWSER_FRONT_REFUSAL = (
    "前台是浏览器，屏幕工具不能点网页：这次任务已经用 computer_browser 打开或点击过页面，"
    "后台据此认为 Chromium 就在屏幕上。请用 computer_browser 的 click（元素编号）、input 或 open；"
    "确实要操作桌面程序，先按 key alt+tab。本次操作未执行。"
)
ADDRESS_BAR_REFUSAL = (
    "屏幕工具不能操作地址栏：要打开网址用 computer_browser 的 open，要在页面上输入用它的 input，"
    "按键用它的 keys。本次操作未执行。"
)
# A key name as the screen tool sends it, without the shift/ctrl spelling drift.
KEY_ALIASES = {
    "cmd": "ctrl", "command": "ctrl", "control": "ctrl",
    "meta": "super", "win": "super", "return": "enter", "esc": "escape",
}


def _key_parts(keys: str) -> frozenset[str]:
    return frozenset(KEY_ALIASES.get(part, part) for part in keys.split("+") if part)


def switches_windows(keys: str) -> bool:
    """Whether these keys are how a model leaves the browser window for a desktop program."""
    parts = _key_parts(keys)
    return "super" in parts or {"alt", "tab"} <= parts


def looks_like_url(text: str) -> bool:
    """Whether typing this on the screen is really navigating, not filling in a field."""
    value = text.strip()
    if not value or len(value) > MAX_URL_CHARS or any(ch.isspace() for ch in value):
        return False
    return bool(URL_PATTERN.match(value))


def screen_refusal(op: str, args: dict, *, browser_in_front: bool) -> str | None:
    """Why this screen action may not run, or ``None``.

    Deterministic on purpose: the audit found the prompt already forbade pixel-driving the
    browser and 8 of 15 runs did it anyway, so the rule has to be enforced, not asked for.
    ``browser_in_front`` is the run's own history — it drove the browser with open or click —
    because the sandbox cannot yet say which window has focus.
    """
    if op not in SCREEN_DRIVING:
        return None
    if op == "key" and switches_windows(str(args.get("keys") or "")):
        return None
    if op == "key" and _key_parts(str(args.get("keys") or "")) in ADDRESS_BAR_KEYS:
        return ADDRESS_BAR_REFUSAL
    if op == "type" and looks_like_url(str(args.get("text") or "")):
        return ADDRESS_BAR_REFUSAL
    return BROWSER_FRONT_REFUSAL if browser_in_front else None


# A step that changed nothing, sent exactly the same way, is the cheapest mistake to catch.
REPEAT_REFUSAL = (
    "这一步和上一步完全一样，页面没有任何变化：不要原样重试，换一个办法"
    "（新的元素编号、search_page，或者用 computer_shell 一次拿到数据）。本次操作未执行。"
)
REPEAT_LIMIT = "同一步已经重复了两次，任务结束：请换一个做法。"


def call_key(call: SandboxCall) -> str:
    """The same step written the same way: what the repeat guard compares."""
    return f"{call.op} {json.dumps(call.args, ensure_ascii=False, sort_keys=True)}"


def observation(op: str, reply: dict) -> str | None:
    """What the page or the screen looks like after one call, as far as the backend can tell.

    A browser answer is identified by its URL and title, a screenshot by the hash of its bytes.
    A command has no page, and the first step of a run has nothing to compare with: ``None``
    means the backend cannot tell, and nothing ran in between that could have moved the page,
    so an identical repeat still counts as one.
    """
    if op == "browser":
        url, title = str(reply.get("url") or ""), str(reply.get("title") or "")
        return f"page|{url}|{title}" if url or title else None
    if op == "shell":
        return None
    image = reply.get("image")
    if isinstance(image, str) and image:
        return "screen|" + hashlib.sha256(image.encode()).hexdigest()
    return None


def repeat_verdict(last: dict | None, key: str) -> str | None:
    """``None`` to run, ``"refuse"`` once, then ``"limit"``.

    ``last`` is what the previous call left behind: its key, whether the page or screen changed
    across it (``True``, ``False``, or ``None`` when the backend cannot tell), and how many
    times it was refused. An unknown change counts as no change: between two identical calls
    nothing ran that could have moved the page. A step that *did* move the page may be repeated
    — a second scroll down is how a long page is read.
    """
    if last is None or last.get("key") != key or last.get("changed") is True:
        return None
    return "limit" if last.get("refusals") else "refuse"


def call_seconds(op: str, args: dict) -> float:
    """The deadline to enforce on the backend, per op; both sides then wait for the same time.

    The old single 45 s cut off browser batches the agent was still running (30 s per action,
    up to five) and dropped their answer.
    """
    if op == "shell":
        return float(args.get("timeout") or DEFAULT_SHELL_SECONDS) + SHELL_GRACE_SECONDS
    if op == "browser":
        return min(BROWSER_STEP_SECONDS * max(1, len(args.get("actions") or [])), BROWSER_MAX_SECONDS)
    return SCREEN_SECONDS


# browser-use answers a failed step with ok and the complaint as the content, so the audit saw
# "✓ Element index 2 not available"; the backend is the only place these can be classified.
SOFT_FAILURES = (
    (re.compile(r"Element index .* not available", re.IGNORECASE), "stale_index"),
    (re.compile(r"not found or not visible", re.IGNORECASE), "not_found"),
    (re.compile(r"Text [\"'].*?[\"'] not found", re.IGNORECASE), "not_found"),
    (re.compile(r"No matches found", re.IGNORECASE), "not_found"),
    (re.compile(r"Navigation failed", re.IGNORECASE), "network"),
)
ERROR_KINDS = {"stale_index": "编号过期", "not_found": "没找到", "network": "网络不通", "failed": "出错"}
ERROR_HINTS = {
    "stale_index": "页面变了，按下面新的编号再来一次",
    "not_found": "换个说法，或者用 search_page 搜整页",
    "network": "检查网址，或者先用 computer_shell 的 curl 试一下",
    "failed": "读懂原因再换办法",
}


def step_failure(step: dict) -> str | None:
    """The error kind of one browser step, or ``None`` when it really succeeded."""
    message = str(step.get("result") or "")
    for pattern, kind in SOFT_FAILURES:
        if pattern.search(message):
            return kind
    return "failed" if step.get("ok") is False else None


# One element of browser-use's DOM tree, including the "|SHADOW(open)|[12]" decorations.
_ELEMENT_LINE = re.compile(r"^(?:\|[^|\n]*\|)*\*?\[")


def trim_elements(raw: str, limit: int = ELEMENTS_IN_OBSERVATION) -> tuple[str, int]:
    """The first ``limit`` elements of a browser-use DOM, and how many more there were."""
    lines = raw.splitlines()
    starts = [index for index, line in enumerate(lines) if _ELEMENT_LINE.match(line.strip())]
    if len(starts) <= limit:
        return raw, 0
    return "\n".join(lines[: starts[limit]]), len(starts) - limit


class ComputerHub:
    """The connected sandbox, the call in flight and which run holds it."""

    def __init__(
        self,
        publish: Callable[[str, Any], None],
        token: Callable[[], str],
        *,
        target: str = "local",
        host: Any = None,
    ) -> None:
        self._publish = publish
        # Read on use: the store opens its database only after the engine is built.
        self._token = token
        self._target = target if target in ("local", "cloud", "mixed") else "local"
        # The machine this process runs on. It is "local" whenever no agent claimed that place.
        self._host = host
        # One agent per place. A second hello for the same place replaces that one only.
        self._agents: dict[str, dict] = {}
        self._pending: dict[str, tuple[str, asyncio.Future[dict]]] = {}
        self._holder: dict | None = None
        # A sandbox's screen and its Chromium are single, so its non-shell ops take turns
        # there; commands do not, because each runs in its own process. A plain set is used
        # instead of an asyncio.Lock: a cancelled acquire can leave a lock owned by nobody.
        self._desk_busy: set[str] = set()
        self._latest_image_id: str | None = None

    def _uses_host(self, place: str) -> bool:
        return place == "local" and self._host is not None and "local" not in self._agents

    # ------------------------------------------------------------------ state

    @property
    def target(self) -> str:
        return self._target

    def restore_target(self, target: str) -> None:
        """The saved target, read once the database is open; nothing is announced yet."""
        self._target = target if target in ("local", "cloud", "mixed") else "local"

    def set_target(self, target: str) -> None:
        if target not in ("local", "cloud", "mixed"):
            raise ValueError(target)
        self._target = target
        self._announce()

    def choose(self, requested: str | None) -> str:
        """Which connected machine a call uses. A fixed target ignores ``requested``."""
        if self._target != "mixed":
            return self._target
        if requested in PLACES and (requested in self._agents or (requested == "local" and self._host is not None)):
            return str(requested)
        if requested in PLACES:
            return str(requested)
        if "local" in self._agents or self._host is not None:
            return "local"
        if "cloud" in self._agents:
            return "cloud"
        return "local"

    def machine(self, requested: str | None) -> tuple[str, dict | None]:
        place = self.choose(requested)
        if self._uses_host(place):
            return place, self._host.info()
        slot = self._agents.get(place)
        return place, None if slot is None else slot["info"]

    @property
    def connected(self) -> bool:
        return self.info is not None

    @property
    def info(self) -> dict | None:
        _place, info = self.machine(None)
        return info

    @property
    def holder_run_id(self) -> str | None:
        self._expire_holder()
        return None if self._holder is None else self._holder["run_id"]

    def _machine_state(self, place: str) -> dict:
        if self._uses_host(place):
            info = self._host.info()
        else:
            info = None if place not in self._agents else self._agents[place]["info"]
        return {
            "connected": info is not None,
            "name": None if info is None else info["name"],
            "os": None if info is None else info["os"],
            "screen": None if info is None else info["screen"],
            "view": None if info is None else info["view"],
        }

    def state(self) -> dict:
        info = self.info
        self._expire_holder()
        return {
            "connected": info is not None,
            "name": None if info is None else info["name"],
            "os": None if info is None else info["os"],
            "screen": None if info is None else info["screen"],
            "view": None if info is None else info["view"],
            "target": self._target,
            "machines": {place: self._machine_state(place) for place in PLACES},
            # ``screen_at`` is the lease's clock, not something the panel shows.
            "holder": (
                None
                if self._holder is None
                else {k: v for k, v in self._holder.items() if k != "screen_at"}
            ),
            "latest_image_id": self._latest_image_id,
        }
    def _announce(self) -> None:
        self._publish("computer", self.state())

    def authorized(self, header: str | None) -> bool:
        scheme, _, value = (header or "").partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return False
        return hmac.compare_digest(value.strip().encode(), self._token().encode())

    # ------------------------------------------------------------------ connection

    async def serve(self, socket: WebSocket) -> None:
        """Own one accepted agent connection until it closes."""
        try:
            hello = await asyncio.wait_for(socket.receive_json(), timeout=HELLO_SECONDS)
        except (asyncio.TimeoutError, WebSocketDisconnect, json.JSONDecodeError):
            await _close_quietly(socket, 1008)
            return
        if isinstance(hello, dict) and hello.get("version") != PROTOCOL_VERSION:
            # An agent from the build that drove the Mac itself: refuse it outright.
            await _close_quietly(socket, CLOSE_OUTDATED)
            return
        try:
            info = _hello_info(hello)
        except ValueError:
            await _close_quietly(socket, 1008)
            return
        place = str(info["place"])
        previous = self._agents.get(place)
        self._agents[place] = {"socket": socket, "info": info}
        if previous is not None:
            self._fail_place(place, DISCONNECTED)
            await _close_quietly(previous["socket"], CLOSE_REPLACED)
        self._announce()
        try:
            while True:
                message = await socket.receive_json()
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "result":
                    item = self._pending.pop(str(message.get("id")), None)
                    if item is not None and not item[1].done():
                        item[1].set_result(message)
        except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
            pass
        finally:
            current = self._agents.get(place)
            if current is not None and current["socket"] is socket:
                self._agents.pop(place, None)
                self._fail_place(place, DISCONNECTED)
                self._announce()

    async def shutdown(self) -> None:
        agents, self._agents = self._agents, {}
        self._fail_pending(DISCONNECTED)
        for slot in agents.values():
            await _close_quietly(slot["socket"], 1001)
        close = getattr(self._host, "close", None)
        if close is not None:
            # The host browser is a process this backend started; it must not outlive it.
            await asyncio.to_thread(close)

    def _fail_place(self, place: str, reason: str) -> None:
        doomed = [call_id for call_id, (owner, _future) in self._pending.items() if owner == place]
        for call_id in doomed:
            _owner, future = self._pending.pop(call_id)
            if not future.done():
                future.set_exception(ToolError(reason))

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for _place, future in pending.values():
            if not future.done():
                future.set_exception(ToolError(reason))

    # ------------------------------------------------------------------ holding
    #
    # Only screen actions hold the machine. A shell command or a browser batch cannot fight
    # another run for the pointer, so it never asks for the hold; two runs' browser windows
    # are apart anyway. The hold is a lease rather than ownership: a run that has not touched
    # the screen for SCREEN_LEASE_SECONDS is ignored, so one that moved on to the terminal
    # does not keep everybody else waiting.

    def _screen_lease_live(self) -> bool:
        return (
            self._holder is not None
            and time.monotonic() - float(self._holder.get("screen_at") or 0.0)
            < SCREEN_LEASE_SECONDS
        )

    def _expire_holder(self) -> None:
        """Forget a hold whose lease ran out; the run will ask again for its next screen step."""
        if self._holder is not None and not self._screen_lease_live():
            self._holder = None

    def desk_busy(self, place: str) -> bool:
        """Whether another run is using a sandbox's screen or Chromium right now."""
        return place in self._desk_busy

    def take_desk(self, place: str) -> None:
        """Take a sandbox's turn at its screen and Chromium; commands never take it."""
        self._desk_busy.add(place)

    def free_desk(self, place: str) -> None:
        self._desk_busy.discard(place)

    async def acquire(
        self, holder: dict, *, stopped: Callable[[], bool], timeout: float
    ) -> None:
        """Wait until no other run holds the screen, then hold it for ``holder['run_id']``."""
        deadline = time.monotonic() + timeout
        while True:
            self._expire_holder()
            current = self._holder
            if current is None or current["run_id"] == holder["run_id"]:
                break
            if stopped():
                return
            if time.monotonic() >= deadline:
                raise ToolError(f"屏幕一直被「{current['agent_name']}」占用，这一步没有执行。")
            await asyncio.sleep(POLL_SECONDS)
        holder["screen_at"] = time.monotonic()
        if self._holder is not holder:
            self._holder = holder
            self._announce()

    def touch_screen(self, run_id: str) -> None:
        """This run is still using the screen: keep its lease alive."""
        if self._holder is not None and self._holder["run_id"] == run_id:
            self._holder["screen_at"] = time.monotonic()

    def holder_name(self, run_id: str) -> str | None:
        """Who else holds the screen, when it is not ``run_id``."""
        self._expire_holder()
        if self._holder is None or self._holder["run_id"] == run_id:
            return None
        return str(self._holder["agent_name"])

    def release(self, run_id: str) -> None:
        """The run ended: give back its screen lease and close the browser window it opened."""
        if self._holder is not None and self._holder["run_id"] == run_id:
            self._holder = None
            self._announce()
        self._close_browser(run_id)

    def _close_browser(self, run_id: str) -> None:
        host = self._host
        if host is None or not host.has_browser(run_id):
            return
        # Closing a window talks CDP; a run's finalize path runs on the event loop.
        threading.Thread(
            target=host.release_browser, args=(run_id,), daemon=True, name=f"browser-{run_id[:8]}"
        ).start()

    def show(self, attachment_id: str) -> None:
        self._latest_image_id = attachment_id
        self._announce()

    # ------------------------------------------------------------------ calls

    async def call(
        self,
        op: str,
        args: dict,
        *,
        stopped: Callable[[], bool],
        place: str | None = None,
        run_id: str = "",
    ) -> dict | None:
        """Run one action on the chosen machine; ``None`` when the run stopped while it was out."""
        chosen = self.choose(place)
        if op not in ("shell", "browser"):
            # Keep the hold alive across a slow screen step; the tools layer took the hold.
            self.touch_screen(run_id)
        if self._uses_host(chosen):
            return await self._call_host(op, args, stopped, run_id)
        slot = self._agents.get(chosen)
        if slot is None:
            raise ToolError(offline_message(chosen, mixed=self._target == "mixed" and not self._agents and self._host is None))
        socket = slot["socket"]
        call_id = uuid.uuid4().hex
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = (chosen, future)
        try:
            await socket.send_json({"type": "call", "id": call_id, "op": op, "args": args})
        except (WebSocketDisconnect, RuntimeError) as exc:
            self._pending.pop(call_id, None)
            raise ToolError(DISCONNECTED) from exc
        loop = asyncio.get_running_loop()
        limit = call_seconds(op, args)
        deadline = loop.time() + limit
        try:
            while not future.done():
                if stopped():
                    return None
                if loop.time() >= deadline:
                    raise ToolError(f"沙盒电脑在 {int(limit)} 秒内没有完成这一步。")
                await asyncio.wait({future}, timeout=POLL_SECONDS)
            return future.result()
        finally:
            self._pending.pop(call_id, None)

    async def _call_host(
        self, op: str, args: dict, stopped: Callable[[], bool], run_id: str = ""
    ) -> dict | None:
        if stopped():
            return None
        # Only this run's own stop signal and processes: another run's command keeps running.
        self._host.arm(run_id)
        loop = asyncio.get_running_loop()
        future = asyncio.wrap_future(
            loop.run_in_executor(None, lambda: self._host.call(op, args, run_id=run_id))
        )
        limit = call_seconds(op, args)
        deadline = loop.time() + limit
        while not future.done():
            if stopped():
                self._host.cancel(run_id)
                return None
            if loop.time() >= deadline:
                self._host.cancel(run_id)
                raise ToolError(f"这台电脑在 {int(limit)} 秒内没有完成这一步。")
            await asyncio.wait({future}, timeout=POLL_SECONDS)
        return future.result()



def _hello_info(message: Any) -> dict:
    if not isinstance(message, dict) or message.get("type") != "hello":
        raise ValueError("expected hello")
    screen = message.get("screen")
    if not isinstance(screen, dict):
        raise ValueError("screen missing")
    width, height = screen.get("width"), screen.get("height")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (width, height)):
        raise ValueError("bad screen")
    name = message.get("name")
    system = message.get("os")
    place = message.get("place") or "local"
    if place not in PLACES:
        raise ValueError("bad place")
    return {
        "place": place,
        "name": name.strip()[:60] if isinstance(name, str) and name.strip() else "Cubes 沙盒",
        "os": system.strip()[:60] if isinstance(system, str) and system.strip() else "Linux",
        "screen": {"width": width, "height": height},
        "view": _local_view(message.get("view")) if place == "local" else _remote_view(message.get("view")),
    }


def _local_view(raw: Any) -> str | None:
    """The live view link, only when it points at this Mac's loopback over http."""
    if not isinstance(raw, str) or len(raw) > 300:
        return None
    parts = urlsplit(raw)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
        return None
    return raw


def _remote_view(raw: Any) -> str | None:
    """A cloud machine's live view: http or https, with no password in the address."""
    if not isinstance(raw, str) or len(raw) > 300:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    return raw



async def _close_quietly(socket: WebSocket, code: int) -> None:
    try:
        await socket.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        pass
