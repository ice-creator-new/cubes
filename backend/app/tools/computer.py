"""The sandbox computer as registry handlers: shell, browser and screen steps.

Commands run side by side on the shared VM; the screen and the browser take turns, and only
a screen action holds the machine. The user allows the sandbox once per run, and a run a
schedule started asks for that permission in its target conversation instead of in its
hidden workspace.
"""

from __future__ import annotations

from .. import permissions

import asyncio
import base64
import binascii
import json
import time
from typing import Any

from .. import attachments as attachment_rules
from .. import computer as computer_rules
from .. import models
from ..agent import history
from ..approvals import UNATTENDED_REFUSAL, Card, ask, ask_in
from ..errors import RunLimited, RunStopped, ToolError
from ..toolkit import ToolInputError, page_detail, text_detail
from . import resolve
from .args import json_parser
from .names import SCHEDULE_COMPUTER
from .types import Runner, ToolHandler, ToolOutcome, cap_tool_text

ROLES = frozenset({"supervisor", "member"})

# What the bot is told when it cannot see a screenshot.
NO_VISION = "你看不到截图，不能用 computer；请用 computer_shell 或 computer_browser。"
# Screenshot turns are labelled with the tool that took them, so each keeps its own budget of
# pixels in the context: the screen keeps the previous frame (a click that changed nothing is
# only visible by comparing two), the browser's DOM is the real observation and one is enough.
SCREEN_SHOT_LABEL = history.COMPUTER_SHOT_LABEL
BROWSER_SHOT_LABEL = "[浏览器截图，红圈是鼠标位置]"
SHOT_LABEL = {
    computer_rules.SCREEN_TOOL: SCREEN_SHOT_LABEL,
    computer_rules.BROWSER_TOOL: BROWSER_SHOT_LABEL,
}
SHOT_KEEP: dict[str, int] = {SCREEN_SHOT_LABEL: 2, BROWSER_SHOT_LABEL: 1}
# The user said no to this run: no later step may ask again.
DENIED = "用户拒绝了这次任务使用沙盒电脑，不要再调用沙盒工具。"


def handlers(hub, sees_screen) -> list[ToolHandler]:
    """One handler per sandbox tool; the screen is only offered to a bot that sees images.

    ``sees_screen(offer)`` decides that per model call: the tool list is rebuilt for every
    request, so a settings change applies to the next call.
    """
    offered: list[ToolHandler] = []
    for schema in computer_rules.schemas(vision=True):
        name = schema["function"]["name"]
        offered.append(
            ToolHandler(
                name=name,
                label=computer_rules.TOOL_LABELS[name],
                schema=schema,
                roles=ROLES,
                parse=json_parser(computer_rules.TOOL_LABELS[name]),
                run=_runner(name),
                # The sandbox is offered only while it is up; calling it anyway says why not.
                offered_when=(
                    (lambda offer: hub.connected)
                    if name != computer_rules.SCREEN_TOOL
                    else (lambda offer: hub.connected and sees_screen(offer))
                ),
                computer_step=True,
            )
        )
    return offered


def _runner(tool: str) -> Runner:
    async def run(ctx, payload: dict[str, Any]) -> ToolOutcome:
        return await _step(ctx, tool, payload)

    return run


async def consent_and_acquire(ctx, run, author, info, *, resume: str | None = None, screen: bool = False) -> ToolOutcome | None:
    """The per-run consent card, and the screen hold when a screen action needs it.

    Returns a finished ``handled`` outcome when the user refused (or the ask timed out) and
    the caller must return it verbatim; ``None`` once the caller may run its call. Only a
    screen action takes the hold — a command or a browser batch in another run must not wait
    for the pointer. ``resume`` is the action phrase shown after the user says yes.
    """
    hub = ctx.computer
    noun = "这台电脑" if info.get("host") else "沙盒电脑"
    if not run.computer_allowed:
        if run.schedule_id is not None:
            decision = await _ask_for_scheduled_computer(ctx, info)
        else:
            wall = (
                "这就是用户正在用的电脑，不是虚拟机，能碰到上面的文件。"
                if info.get("host")
                else "它是隔离的 Linux 虚拟机，碰不到这台 Mac。"
            )
            card = Card(
                prompt=(
                    f"「{author['name']}」想使用{noun}「{info['name']}」：运行命令、操作浏览器、看屏幕和点击，可能会上网。"
                    f"{wall}允许后，这次任务里不再逐一询问；随时可以点停止。"
                ),
                confirm_label="允许",
                deny_label="拒绝",
                summary=f"「{author['name']}」想使用{noun}",
                step=f"等待你允许使用{noun}",
                resume_summary=f"正在{resume}" if resume else None,
                grant=permissions.COMPUTER_HOST if info.get("host") else permissions.COMPUTER_CLOUD,
            )
            decision = await ask(ctx, card)
        if decision != "approve":
            denied = decision in ("deny", "stop")
            if decision == "deny":
                run.computer_denied = True
            summary = f"你拒绝了使用{noun}" if denied else f"等待确认超时，没有使用{noun}"
            result = {"ok": False, "denied": True} if denied else {"ok": False, "error": summary}
            text = json.dumps(result, ensure_ascii=False)
            ctx.finish_action(
                ctx.action,
                status="completed" if denied else "error",
                summary=summary,
                error=None if denied else summary,
                result=text,
            )
            return ToolOutcome(summary=summary, result=result, text=text, handled=True)
        run.computer_allowed = True

    if not screen:
        return None
    stopped = lambda: run.stop_requested  # noqa: E731 - read at every poll
    other = hub.holder_name(run.run_id)
    if other is not None:
        ctx.set_step(f"等「{other}」用完屏幕")
    await hub.acquire(
        {
            "run_id": run.run_id,
            "conversation_id": run.conversation_id,
            "agent_id": author["id"],
            "agent_name": author["name"],
        },
        stopped=stopped,
        timeout=ctx.settings.confirm_timeout_seconds,
    )
    if run.stop_requested:
        raise RunStopped("任务已停止。")
    return None


async def _take_desk(ctx, hub, place: str, run) -> bool:
    """Take a sandbox's one-at-a-time desk for a screen or browser op, saying why it waits.

    A shell command runs in its own process and never takes this desk; the sandbox's
    Chromium and its screen are single, so those two kinds of op take turns. The wait is a
    poll rather than a lock: a cancelled lock acquire can leave the desk owned by nobody.
    """
    if hub.desk_busy(place):
        ctx.set_step("等前一条命令跑完")
    deadline = time.monotonic() + computer_rules.BROWSER_MAX_SECONDS + computer_rules.SCREEN_SECONDS + 10
    while hub.desk_busy(place):
        if run.stop_requested:
            raise RunStopped("任务已停止。")
        if time.monotonic() >= deadline:
            raise ToolError("沙盒电脑一直在忙，这一步没有执行。")
        await asyncio.sleep(computer_rules.POLL_SECONDS)
    hub.take_desk(place)
    return True


async def _step(ctx, tool: str, payload: dict[str, Any]) -> ToolOutcome:
    """One step on the shared VM: allowed once per run, the screen held by one run at a time."""
    hub = ctx.computer
    requested = computer_rules.requested_place(payload.get("place"))
    if hub.target != "mixed" and requested not in (None, hub.target):
        word = "本机" if hub.target == "local" else "云端"
        raise ToolError(f"用户现在只允许操作{word}电脑，本次操作未执行。")
    place, info = hub.machine(requested)
    if info is None:
        raise ToolError(
            computer_rules.offline_message(place, mixed=hub.target == "mixed" and requested is None)
        )
    run = ctx.run
    author = ctx.author
    if run.computer_denied:
        raise ToolInputError(DENIED)
    vision = resolve.sees_images(ctx.store, author)
    if tool == computer_rules.SCREEN_TOOL and not vision:
        raise ToolInputError(NO_VISION)
    call = computer_rules.parse_call(tool, payload, info["screen"], vision=vision)
    # Two guards run before the sandbox is touched: a screen action aimed at the browser is
    # refused outright, and a verbatim repeat of a step that changed nothing is refused once.
    _refuse_screen(tool, call, run)
    key = computer_rules.call_key(call)
    verdict = computer_rules.repeat_verdict(run.computer_last, key)
    if verdict == "limit":
        raise RunLimited(computer_rules.REPEAT_LIMIT)
    if verdict == "refuse":
        run.computer_last["refusals"] = 1
        raise ToolInputError(computer_rules.REPEAT_REFUSAL)

    gated = await consent_and_acquire(
        ctx, run, author, info, resume=call.done, screen=tool == computer_rules.SCREEN_TOOL
    )
    if gated is not None:
        return gated
    stopped = lambda: run.stop_requested  # noqa: E731 - read at every poll
    ctx.set_step(f"「{author['name']}」正在{call.done}")
    desk = False
    if call.op != "shell" and not info.get("host"):
        # A sandbox's screen and Chromium are single, so its non-shell ops take turns there.
        desk = await _take_desk(ctx, hub, place, run)
        ctx.set_step(f"「{author['name']}」正在{call.done}")
    try:
        reply = await hub.call(
            call.op, call.args, stopped=stopped, place=place, run_id=run.run_id
        )
    finally:
        if desk:
            hub.free_desk(place)
    if reply is None:
        raise RunStopped("任务已停止。")
    if not reply.get("ok"):
        raise ToolError(_sentence(reply.get("error")) or "沙盒电脑没能完成这一步。")
    unchanged = _remember(run, key, call, reply, host=bool(info.get("host")))
    if call.op == "shell":
        return _shell_outcome(call.done, reply, str(call.args.get("command") or ""))
    image_id = _save_screenshot(ctx.store, hub, reply.get("image"), ctx.action_message_id)
    images = [] if image_id is None else [image_id]
    if call.op == "browser":
        return _browser_outcome(call, reply, images)
    return _screen_outcome(call, reply, images, unchanged, info)


def _refuse_screen(tool: str, call: computer_rules.SandboxCall, run) -> None:
    """Refuse a screen action the backend will not let near the browser."""
    if tool != computer_rules.SCREEN_TOOL:
        return
    refusal = computer_rules.screen_refusal(
        call.op, call.args, browser_in_front=run.computer_browser_front
    )
    if refusal is not None:
        raise ToolInputError(refusal)


def _remember(run, key: str, call: computer_rules.SandboxCall, reply: dict, *, host: bool = False) -> bool | None:
    """Note what this step left behind, for the repeat guard and the screen's own no-change note.

    ``True`` means the screen or page is byte-for-byte (or title-for-title) what it was before
    this call; ``None`` means the backend cannot tell, which still counts an identical repeat
    as one on an unchanged picture.
    """
    page = computer_rules.observation(call.op, reply)
    moved = None if page is None or run.computer_page is None else page != run.computer_page
    if page is not None:
        run.computer_page = page
    run.computer_last = {"key": key, "changed": moved, "refusals": 0}
    if call.op == "browser" and any(
        str(step.get("action")) in ("open", "click") for step in call.args.get("actions") or []
    ):
        # The sandbox's Chromium now owns the screen. On the real Mac the browser is just
        # a window, so a later click still goes through the screen tool.
        if not host:
            run.computer_browser_front = True
    if call.op == "key" and computer_rules.switches_windows(str(call.args.get("keys") or "")):
        run.computer_browser_front = False
    # The caller wants "the picture did not move", which is the absence of a change.
    return None if moved is None else not moved


def _screen_outcome(
    call: computer_rules.SandboxCall,
    reply: dict[str, Any],
    images: list[str],
    unchanged: bool | None,
    info: dict,
) -> ToolOutcome:
    """A screen step: the screenshot speaks, plus the one thing pixels cannot say themselves."""
    result: dict[str, Any] = {"ok": True, "done": call.done, "screen": info["screen"]}
    cursor = reply.get("cursor")
    if isinstance(cursor, dict) and {"x", "y"} <= cursor.keys():
        result["cursor"] = {"x": cursor["x"], "y": cursor["y"]}
    if unchanged is not None:
        result["changed"] = not unchanged
    summary = call.done
    if not images:
        result["note"] = "这一步没有拿到截图。"
    if unchanged:
        # Two byte-identical screenshots: the action did nothing (the audit watched runs
        # click guessed coordinates and photograph the same frame).
        summary = f"{call.done}，画面和上一步一样"
        note = "画面和上一步一样。"
        result["note"] = f"{result['note']} {note}" if result.get("note") else note
    return ToolOutcome(summary=summary, result=result, images=images)


async def _ask_for_scheduled_computer(ctx, info: dict) -> str:
    """Ask in the schedule's target conversation, as its owner, for the sandbox.

    The run's own workspace is hidden, so the card goes where the user reads the
    schedule's results. A yes is stored on the schedule: later fires do not ask again.
    """
    run = ctx.run
    schedule = ctx.store.get_schedule(run.schedule_id or "")
    target = ctx.store.get_conversation(schedule["conversation_id"]) if schedule else None
    if schedule is None or target is None:
        raise ToolError(UNATTENDED_REFUSAL)
    owner = ctx.store.get_agent(schedule["owner_agent_id"] or "") or resolve.supervisor(ctx.store)
    title = schedule["title"]
    noun = "这台电脑" if info.get("host") else "沙盒电脑"
    wall = (
        "这就是用户正在用的电脑，不是虚拟机，能碰到上面的文件。"
        if info.get("host")
        else "它是隔离的 Linux 虚拟机，碰不到这台 Mac。"
    )
    card = Card(
        prompt=(
            f"定时任务「{title}」正在后台运行，「{ctx.author['name']}」想使用{noun}「{info['name']}」："
            f"运行命令、操作浏览器、看屏幕和点击，可能会上网。{wall}"
            "允许后这个定时任务以后到点也直接使用，不再询问；可以在「定时」里随时关掉。"
        ),
        confirm_label="允许",
        deny_label="拒绝",
        summary=f"定时任务「{title}」想使用{noun}",
        step=f"等待你在「{target['title']}」里允许使用{noun}",
        grant=permissions.COMPUTER_HOST if info.get("host") else permissions.COMPUTER_CLOUD,
    )
    ask, decision = await ask_in(
        ctx,
        card,
        conversation_id=target["id"],
        author=owner,
        tool_name=SCHEDULE_COMPUTER,
        tool_call_id=ctx.action["tool_call_id"],
    )
    if decision == "approve":
        # A yes is remembered on the schedule: later fires do not ask again.
        ctx.store.update_schedule(schedule["id"], use_computer=True)
        ctx.publish_schedules()
    summary = {
        "approve": f"你允许了定时任务「{title}」使用{noun}",
        "deny": f"你拒绝了定时任务「{title}」使用{noun}",
        "timeout": f"等待确认超时，定时任务「{title}」这次没有使用{noun}",
    }.get(decision, f"定时任务「{title}」已停止，没有使用{noun}")
    finished = ctx.store.finish_action(
        ask["id"],
        status="error" if decision == "timeout" else "completed",
        summary=summary,
        message_status="complete",
        error=summary if decision == "timeout" else None,
        result=json.dumps({"decision": decision}, ensure_ascii=False),
    )
    if finished is not None:
        ctx.publish("message_end", models.message_payload(finished[1]))
    from ..approvals import _decisions

    ctx.publish("decisions", _decisions(ctx))
    return decision


def _save_screenshot(store, hub, encoded: Any, message_id: str) -> str | None:
    """Keep one screenshot on its action row and as the panel's preview; ``None`` if bad."""
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    mime = attachment_rules.sniff_mime(data)
    if mime not in ("image/jpeg", "image/png") or len(data) > attachment_rules.MAX_IMAGE_BYTES:
        return None
    row = store.save_attachment(data, mime)
    store.attach_to_message(message_id, [row["id"]])
    hub.show(row["id"])
    return str(row["id"])


def _sentence(raw: Any) -> str:
    """A short Chinese reason from the agent, ending like every other error the user reads."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = raw.strip()[:300]
    return text if text.endswith(("。", "！", "？")) else f"{text}。"


# What an opened command row shows of the output. The model reads its own capped copy; this is
# what the user scrolls through, so it is more than the default text detail's short excerpt.
DETAIL_OUTPUT_CHARS = 8_000


def _shell_outcome(done: str, reply: dict[str, Any], command: str) -> ToolOutcome:
    """A command's result as plain text the model reads at a glance: exit code first.

    The row's detail carries the whole command as written (the one-line summary shortens it
    in the middle) and the output, so opening the row shows exactly what ran and what it said.
    """
    code = reply.get("exit_code")
    head = "超时被终止" if reply.get("timed_out") else f"退出码 {code}"
    parts = [f"{head}，用时 {reply.get('seconds', '?')} 秒，目录 {reply.get('cwd', '~')}。"]
    stdout = str(reply.get("stdout") or "").rstrip()
    stderr = str(reply.get("stderr") or "").rstrip()
    parts.append(f"输出：\n{stdout}" if stdout else "没有输出。")
    if stderr:
        parts.append(f"错误输出：\n{stderr}")
    text = cap_tool_text("\n".join(parts))
    shown = "超时" if reply.get("timed_out") else ("完成" if code == 0 else f"退出码 {code}")
    return ToolOutcome(
        summary=f"{done}，{shown}",
        result={},
        text=text,
        detail={**text_detail(stdout or stderr or head, limit=DETAIL_OUTPUT_CHARS), "command": command},
    )


def _browser_outcome(
    call: computer_rules.SandboxCall, reply: dict[str, Any], images: list[str]
) -> ToolOutcome:
    """What each action did — ✓, ✗ or … — then the page trimmed to what a model can act on.

    The full 12 KB DOM on every step was the audit's biggest context cost, and browser-use
    reports a failed click as ok with the complaint as the content, so the marks and the
    classification are the backend's job.
    """
    title = str(reply.get("title") or "")
    url = str(reply.get("url") or "")
    lines: list[str] = []
    failure: tuple[str, str] | None = None  # (label, error kind) of the first failed step
    for step in reply.get("steps") or []:
        name = str(step.get("action") or "")
        if name == "…":
            # The sandbox's own row: the rest of the batch did not run.
            lines.append(f"… {step.get('result') or '没有执行'}")
            continue
        label = computer_rules.BROWSER_ACTIONS.get(name, name)
        kind = computer_rules.step_failure(step)
        if kind is None:
            lines.append(f"✓ {label}：{step.get('result') or ''}".rstrip("："))
            continue
        failure = failure or (label, kind)
        message = str(step.get("result") or "").strip()
        hint = f"{computer_rules.ERROR_KINDS[kind]}：{computer_rules.ERROR_HINTS[kind]}"
        lines.append(f"✗ {label}：{message}（{hint}）" if message else f"✗ {label}：没成功（{hint}）")
    lines += [f"… {entry['reason']}" for entry in call.skipped]
    lines += [f"页面：{title or '无标题'}", f"网址：{url}"]
    tabs = reply.get("tabs") or []
    if len(tabs) > 1:
        lines.append("标签页：" + "；".join(f"{tab.get('id')} {tab.get('title') or tab.get('url')}" for tab in tabs))
    above, below = reply.get("above") or 0, reply.get("below") or 0
    if above or below:
        lines.append(f"上面还有 {above} 屏，下面还有 {below} 屏" + ("（要看更多就 scroll 或 search_page）" if below else ""))
    elements = str(reply.get("elements") or "").strip()
    page = ""
    if not elements:
        lines.append("这个页面是空的，可能还在加载，可以 wait 一下。")
    else:
        page, more = computer_rules.trim_elements(elements)
        lines.append(f"页面内容（[编号] 是可以操作的元素）：\n{page}")
        if more:
            lines.append(
                f"（元素太多，只列出前 {computer_rules.ELEMENTS_IN_OBSERVATION} 个，"
                f"还有 {more} 个没显示；要看更多用 search_page）"
            )
    summary = call.done
    if failure is not None:
        summary = f"{call.done}，{failure[0]}没成功（{computer_rules.ERROR_KINDS[failure[1]]}）"
    detail = page_detail(url, title, page) if url.startswith("http") else None
    # The host sends its own ``snapshot``; the sandbox's browser-use only sends ``elements``,
    # so its rows fall back to the same trimmed list the model was shown.
    snapshot = str(reply.get("snapshot") or page or "").strip()
    if detail is not None and snapshot:
        # The row's opened detail keeps the element list the model acted on, for the user to
        # read while watching the same page; 6000 chars is what the contract caps it at.
        detail = {**detail, "snapshot": snapshot[:6000]}
    return ToolOutcome(
        summary=summary,
        result={},
        text=cap_tool_text("\n".join(lines)),
        detail=detail,
        images=images,
    )
