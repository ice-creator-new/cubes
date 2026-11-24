"""Pack E — code: a short-lived subprocess with a minimal environment and a hard timeout.

This is a convenience boundary, not a sandbox: the child runs as the current user, in the
working folder, and never inherits the backend's secrets. Whether a run needs the user's yes
first is the approval mode's call (see :mod:`app.permissions`).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .. import permissions
from .base import (
    MAX_RESULT_CHARS,
    Gate,
    ToolContext,
    ToolInputError,
    ToolResult,
    ToolSpec,
    cap_text,
    text_detail,
)

DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 60
OUTPUT_CAP_BYTES = 16 * 1024
MAX_CODE_CHARS = 50_000

CODE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "language": {
            "type": "string",
            "enum": ["python", "shell"],
            "description": "python 或 shell。",
        },
        "code": {"type": "string", "description": "要执行的代码或命令。"},
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TIMEOUT,
            "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，最多 {MAX_TIMEOUT}。",
        },
    },
    "required": ["language", "code"],
}

SHELL = "/bin/sh"


def _workspace(ctx: ToolContext) -> Path:
    return permissions.working_folder(ctx.settings)


def _code_gate(settings: Any, store: Any, args: dict) -> Gate:
    code = str(args.get("code") or "")
    first = next((line.strip() for line in code.splitlines() if line.strip()), "")
    shown = first if len(first) <= 80 else first[:79] + "…"
    lines = code.count("\n") + 1 if code else 0
    word = "Python" if args.get("language") == "python" else "shell"
    more = f"，共 {lines} 行" if lines > 1 else ""
    return Gate(
        grant=permissions.CODE,
        rule="run_code",
        prompt=f"想在这台电脑上运行一段 {word}：{shown}{more}。它以你的身份运行，能读写你能读写的文件。",
        summary=f"想运行 {word}",
        resume=f"正在运行 {word}",
    )


def _child_env(workspace: Path) -> dict[str, str]:
    """Only the handful of variables a program needs; never the backend's secrets."""
    temporary = workspace / ".tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(workspace),
        "LANG": os.environ.get("LANG") or "en_US.UTF-8",
        "TMPDIR": str(temporary),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }


async def _read_capped(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
    if stream is None:  # pragma: no cover - both pipes are always piped
        return b"", False
    collected = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        room = OUTPUT_CAP_BYTES - len(collected)
        if room <= 0:
            truncated = True
            continue
        if len(chunk) > room:
            collected.extend(chunk[:room])
            truncated = True
            continue
        collected.extend(chunk)
    return bytes(collected), truncated


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover - already gone
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _render(
    stdout: bytes, stderr: bytes, stdout_cut: bool, stderr_cut: bool
) -> tuple[str, str]:
    out = stdout.decode("utf-8", errors="replace").rstrip()
    err = stderr.decode("utf-8", errors="replace").rstrip()
    if stdout_cut:
        out += "\n…（标准输出已截断）"
    if stderr_cut:
        err += "\n…（标准错误已截断）"
    return out, err


async def run_code(ctx: ToolContext, args: dict) -> str:
    language = args.get("language")
    if language not in ("python", "shell"):
        raise ToolInputError("语言只能是 python 或 shell。")
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ToolInputError("代码不能为空。")
    if len(code) > MAX_CODE_CHARS:
        raise ToolInputError(f"代码不能超过 {MAX_CODE_CHARS} 个字符。")
    raw_timeout = args.get("timeout_seconds")
    if raw_timeout is None:
        timeout = DEFAULT_TIMEOUT
    elif isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise ToolInputError("超时秒数必须是整数。")
    else:
        timeout = int(raw_timeout)
        if timeout < 1 or timeout > MAX_TIMEOUT:
            raise ToolInputError(f"超时秒数必须在 1 到 {MAX_TIMEOUT} 之间。")

    workspace = _workspace(ctx)
    command = (
        [sys.executable, "-I", "-c", code] if language == "python" else [SHELL, "-c", code]
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace),
            env=_child_env(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ToolInputError(f"无法启动小程序：{exc}。") from exc

    stdout_task = asyncio.create_task(_read_capped(process.stdout))
    stderr_task = asyncio.create_task(_read_capped(process.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_group(process)
        await process.wait()
    stdout, stdout_cut = await stdout_task
    stderr, stderr_cut = await stderr_task
    out, err = _render(stdout, stderr, stdout_cut, stderr_cut)

    lines: list[str] = []
    if timed_out:
        lines.append(f"运行超时（{timeout} 秒），已终止。")
    lines.append(f"退出码：{process.returncode}")
    lines.append("标准输出：")
    lines.append(out or "（无输出）")
    if err:
        lines.append("标准错误：")
        lines.append(err)
    text = cap_text("\n".join(lines), MAX_RESULT_CHARS)
    return ToolResult(text, text_detail(text))


def _summary(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


CODE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="run_code",
        label="运行代码",
        description=(
            "在工作文件夹里运行一段 Python 或 shell 代码，用来做数据处理、验证思路或生成文件。"
            "环境是隔离的：没有网络密钥，超时会被终止，输出有长度上限。"
        ),
        parameters=CODE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_code,
        summary=lambda args: "运行 Python 代码" if args.get("language") == "python" else "运行 shell 命令",
        gate=_code_gate,
    ),
)
