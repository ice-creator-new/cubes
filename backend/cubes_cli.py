"""Drive a running Cubes backend from a script, a terminal or an MCP client.

``cubes status|bots|send|result|runs`` speaks the backend's own HTTP API. The desktop app
binds a random port with a random token on every launch and writes both to
``cli.json`` next to its data, so the CLI works there with no arguments; a server is reached
with ``CUBES_URL`` and ``CUBES_TOKEN_FILE`` (never a ``--token`` flag: argv is readable by
every other process on the machine). ``cubes mcp`` serves four safe tools over stdio to
Claude Desktop, Cursor and the like — list bots, send work, wait for a run, read its result —
and deliberately offers no approvals, no deletion and no credentials.

Exit codes: 0 ok, 1 the backend refused or the run did not complete, 2 bad usage,
3 the run is parked on a card or question, 4 the wait timed out (work continues),
5 the backend is unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Sequence

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_WAITING = 3
EXIT_TIMEOUT = 4
EXIT_UNREACHABLE = 5

TERMINAL_STATUSES = frozenset({"completed", "stopped", "error", "limited"})
POLL_SECONDS = 0.4
HTTP_TIMEOUT_SECONDS = 30.0
RUNS_PER_CONVERSATION = 5
RUNS_SHOWN = 20

# `~/.cubes/cli.json` is the file a user (or a server deploy) writes by hand; the desktop
# app owns the second one and rewrites it on every launch.
USER_CONFIG = Path.home() / ".cubes" / "cli.json"
DESKTOP_CONFIG = (
    Path.home() / "Library" / "Application Support" / "com.ice.cubes.desktop" / "cli.json"
)


class CliError(Exception):
    """A failure with the exit code the shell should see."""

    def __init__(self, message: str, code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class Unreachable(CliError):
    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_UNREACHABLE)


# ------------------------------------------------------------------ connection


@dataclass
class Connection:
    url: str
    token: str


def _config_path() -> Path | None:
    """The config file to read: an explicit override, then the user's, then the desktop's."""
    override = os.environ.get("CUBES_CLI_CONFIG")
    if override is not None:
        stripped = override.strip()
        return Path(stripped).expanduser() if stripped else None
    if USER_CONFIG.exists():
        return USER_CONFIG
    if DESKTOP_CONFIG.exists():
        return DESKTOP_CONFIG
    return None


def _read_config() -> dict[str, Any]:
    path = _config_path()
    if path is None:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"读不了配置文件 {path}：{exc}", EXIT_USAGE) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"配置文件 {path} 不是合法的 JSON：{exc}", EXIT_USAGE) from exc
    if not isinstance(value, dict):
        raise CliError(f"配置文件 {path} 里应该是一个 JSON 对象。", EXIT_USAGE)
    return value


def _read_token_file(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CliError(f"读不了访问令牌文件 {path}：{exc}", EXIT_USAGE) from exc


def resolve_connection() -> Connection:
    """Base URL and token: environment first, then the config file."""
    config = _read_config()
    url = (
        os.environ.get("CUBES_URL", "").strip()
        or str(config.get("url") or config.get("base_url") or "").strip()
    )
    if not url:
        raise CliError(
            "没有找到后端地址：设置 CUBES_URL，或者打开桌面版让它写 cli.json，"
            "或者自己建一个 ~/.cubes/cli.json（{\"url\": …, \"token\": …}）。",
            EXIT_USAGE,
        )
    token_file = os.environ.get("CUBES_TOKEN_FILE", "").strip()
    token = _read_token_file(token_file) if token_file else str(config.get("token") or "")
    return Connection(url=url.rstrip("/"), token=token.strip())


# ---------------------------------------------------------------------- client


@dataclass
class Target:
    """Where a `send` goes: a bot's direct chat, or a group."""

    conversation_id: str
    kind: str
    id: str
    name: str


@dataclass
class WaitOutcome:
    kind: str  # "done", "waiting" or "timeout"
    run: dict[str, Any]
    reply: dict[str, Any] | None
    decision: dict[str, Any] | None


class CubesClient:
    def __init__(self, connection: Connection) -> None:
        self._url = connection.url
        self._token = connection.token
        # A loopback backend must never be sent through the shell's proxy variables.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_environment(cls) -> "CubesClient":
        return cls(resolve_connection())

    @property
    def url(self) -> str:
        return self._url

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self._url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise CliError(self._http_detail(exc), EXIT_FAILURE) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Unreachable(f"连不上后端 {self._url}：{reason}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise CliError("后端返回的内容不是 JSON。") from exc

    @staticmethod
    def _http_detail(error: urllib.error.HTTPError) -> str:
        if error.code == 401:
            return "后端不认这个访问令牌（401），检查 cli.json 或 CUBES_TOKEN_FILE。"
        try:
            body = json.loads(error.read() or b"{}")
        except (json.JSONDecodeError, OSError):
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail:
            return detail
        return f"后端返回 HTTP {error.code}。"

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def workspace(self) -> dict[str, Any]:
        return self._request("GET", "/workspace")

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/runs/{urllib.parse.quote(run_id)}")

    def run_trace(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/runs/{urllib.parse.quote(run_id)}/trace")

    def conversation_runs(self, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        quoted = urllib.parse.quote(conversation_id)
        body = self._request("GET", f"/conversations/{quoted}/runs?limit={limit}")
        return list(body.get("runs") or [])

    def start_run(self, conversation_id: str, content: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(conversation_id)
        return self._request("POST", f"/conversations/{quoted}/runs", {"content": content})

    def direct_conversation(self, agent_id: str) -> str:
        quoted = urllib.parse.quote(agent_id)
        body = self._request("POST", f"/agents/{quoted}/direct")
        conversation_id = body.get("id")
        if not isinstance(conversation_id, str):
            raise CliError("后端没有返回这位机器人的私聊。")
        return conversation_id


# --------------------------------------------------------------------- targets


def _direct_conversations(workspace: dict[str, Any]) -> dict[str, str]:
    """agent id → its direct conversation id, for the chats that already exist."""
    found: dict[str, str] = {}
    for conversation in workspace.get("conversations") or []:
        if conversation.get("kind") != "direct":
            continue
        for agent_id in conversation.get("member_ids") or []:
            found.setdefault(agent_id, conversation["id"])
    return found


def _match(candidates: list[tuple[str, str]], wanted: str) -> list[tuple[str, str]]:
    """Candidates whose id or name matches, exact first, then case-insensitive, then prefix."""
    by_id = [item for item in candidates if item[0] == wanted]
    if by_id:
        return by_id
    exact = [item for item in candidates if item[1] == wanted]
    if exact:
        return exact
    folded = [item for item in candidates if item[1].casefold() == wanted.casefold()]
    if folded:
        return folded
    trimmed = wanted.strip().casefold()
    return [item for item in candidates if trimmed and item[1].casefold().startswith(trimmed)]


def _refuse_ambiguous(matches: list[tuple[str, str]], wanted: str, kind: str) -> NoReturn:
    """The single match, or a message naming the ambiguity."""
    names = "、".join(f"「{name}」({item_id})" for item_id, name in matches)
    raise CliError(f"「{wanted}」对得上多个{kind}：{names}。请写全名或 ID。", EXIT_USAGE)


def resolve_target(
    client: CubesClient, workspace: dict[str, Any], wanted: str
) -> Target:
    bots = [
        (agent["id"], agent["name"])
        for agent in workspace.get("agents") or []
        if agent.get("id") and agent.get("name")
    ]
    bot_matches = _match(bots, wanted)
    if len(bot_matches) > 1:
        _refuse_ambiguous(bot_matches, wanted, "机器人")
    if bot_matches:
        agent_id, name = bot_matches[0]
        existing = _direct_conversations(workspace).get(agent_id)
        conversation_id = existing or client.direct_conversation(agent_id)
        return Target(conversation_id=conversation_id, kind="bot", id=agent_id, name=name)
    groups = [
        (conversation["id"], conversation.get("title") or "")
        for conversation in workspace.get("conversations") or []
        if conversation.get("kind") == "group"
    ]
    group_matches = _match(groups, wanted)
    if len(group_matches) > 1:
        _refuse_ambiguous(group_matches, wanted, "群组")
    if group_matches:
        group_id, title = group_matches[0]
        return Target(conversation_id=group_id, kind="group", id=group_id, name=title)
    raise CliError(
        f"找不到机器人或群组「{wanted}」。先运行 cubes bots 看看名字。", EXIT_USAGE
    )


# ----------------------------------------------------------------- run results


def _reply_of(trace: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    """The last reply the run wrote in its own conversation.

    Delegated work (a consulted member in a group, a private chat between bots) also lands
    under the run; the answer the user sees is the one in the conversation they wrote to, so
    messages elsewhere are only a fallback when that conversation has none.
    """
    agents = [
        message for message in trace.get("messages") or [] if message.get("kind") == "agent"
    ]
    own = [
        message
        for message in agents
        if message.get("conversation_id") == run.get("conversation_id")
    ]
    if not own:
        own = agents
    if not own:
        return None
    message = own[-1]
    return {
        "id": message.get("id"),
        "author_id": message.get("author_id"),
        "author_name": message.get("author_name") or "",
        "content": message.get("content") or "",
        "mood": message.get("mood"),
        "created_at": message.get("created_at"),
    }


def _waiting_decision(
    client: CubesClient, run: dict[str, Any]
) -> dict[str, Any] | None:
    """The card or question this run is parked on, if any.

    A conversation runs one task at a time, so a waiting row in the run's own conversation
    belongs to this run. The step wording is the fallback for a wait whose row is gone.
    """
    workspace = client.workspace()
    for decision in workspace.get("decisions") or []:
        if decision.get("conversation_id") == run.get("conversation_id"):
            return decision
    step = str(run.get("current_step") or "")
    if run.get("status") == "running" and step.startswith("等待你"):
        return {"prompt": step, "where": "", "who": "", "if_unanswered": ""}
    return None


def wait_for_run(client: CubesClient, run_id: str, timeout: float) -> WaitOutcome:
    """Poll until the run ends, parks on the user, or ``timeout`` runs out."""
    deadline = time.monotonic() + timeout
    while True:
        run = client.get_run(run_id)
        if run.get("status") in TERMINAL_STATUSES:
            trace = client.run_trace(run_id)
            return WaitOutcome("done", run, _reply_of(trace, run), None)
        decision = _waiting_decision(client, run)
        if decision is not None:
            return WaitOutcome("waiting", run, None, decision)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return WaitOutcome("timeout", run, None, None)
        time.sleep(min(POLL_SECONDS, remaining))


# ---------------------------------------------------------------------- output


def _print_json(document: dict[str, Any]) -> None:
    print(json.dumps(document, ensure_ascii=False, indent=2))


def _reply_line(reply: dict[str, Any] | None) -> str:
    if reply is None:
        return "这次任务没有留下回复。"
    name = reply.get("author_name") or "机器人"
    return f"{name}：{reply.get('content') or ''}"


def _time_text(value: str | None) -> str:
    return str(value or "").replace("T", " ")[:19]


# -------------------------------------------------------------------- commands


def _command_status(args: argparse.Namespace) -> int:
    client = CubesClient.from_environment()
    health = client.health()
    workspace = client.workspace()
    supervisor = next(
        (
            agent
            for agent in workspace.get("agents") or []
            if agent.get("kind") == "supervisor"
        ),
        None,
    )
    groups = [
        conversation
        for conversation in workspace.get("conversations") or []
        if conversation.get("kind") == "group"
    ]
    active = list(workspace.get("active_runs") or [])
    waiting = list(workspace.get("decisions") or [])
    if args.json:
        _print_json(
            {
                "url": client.url,
                "health": health,
                "supervisor": (
                    {"id": supervisor["id"], "name": supervisor["name"]}
                    if supervisor is not None
                    else None
                ),
                "bots": len(workspace.get("agents") or []),
                "groups": len(groups),
                "active_runs": active,
                "waiting": waiting,
            }
        )
        return EXIT_OK
    print(f"后端：{client.url}（在线）")
    if health.get("configured"):
        print(
            f"模型：{health.get('model')}"
            f"（{(health.get('provider_name') or '').strip() or '未命名服务'}）"
        )
    else:
        print(f"模型：{health.get('configuration_error') or '还没配置模型服务'}")
    print(f"主管：{(supervisor or {}).get('name') or '还没有'}")
    print(f"机器人 {len(workspace.get('agents') or [])} 个 · 群组 {len(groups)} 个")
    if active:
        print(f"正在运行 {len(active)} 个：")
        for run in active:
            print(f"  {run.get('id')}  {run.get('current_step') or ''}")
    else:
        print("正在运行：无")
    if waiting:
        print(f"等你决定 {len(waiting)} 项：")
        for decision in waiting:
            where = f"「{decision['where']}」" if decision.get("where") else ""
            who = f"{decision['who']} 在" if decision.get("who") else ""
            print(f"  {who}{where}：{decision.get('prompt') or ''}")
    return EXIT_OK


def _command_bots(args: argparse.Namespace) -> int:
    client = CubesClient.from_environment()
    workspace = client.workspace()
    direct = _direct_conversations(workspace)
    bots = [
        {
            "id": agent["id"],
            "name": agent["name"],
            "kind": agent.get("kind"),
            "conversation_id": direct.get(agent["id"]),
        }
        for agent in workspace.get("agents") or []
    ]
    groups = [
        {
            "id": conversation["id"],
            "title": conversation.get("title") or "",
            "member_ids": list(conversation.get("member_ids") or []),
        }
        for conversation in workspace.get("conversations") or []
        if conversation.get("kind") == "group"
    ]
    if args.json:
        _print_json({"bots": bots, "groups": groups})
        return EXIT_OK
    print("机器人：")
    for bot in bots:
        mark = "（主管）" if bot["kind"] == "supervisor" else ""
        print(f"  {bot['name']}{mark}  {bot['id']}")
    if groups:
        print("群组：")
        for group in groups:
            print(f"  {group['title']}  {group['id']}  {len(group['member_ids'])} 位成员")
    return EXIT_OK


def _command_send(args: argparse.Namespace) -> int:
    client = CubesClient.from_environment()
    workspace = client.workspace()
    target = resolve_target(client, workspace, args.target)
    run = client.start_run(target.conversation_id, args.task)
    run_id = str(run["id"])
    document: dict[str, Any] = {
        "target": {"id": target.id, "name": target.name, "kind": target.kind},
        "conversation_id": target.conversation_id,
        "run": run,
        "reply": None,
        "waiting": None,
    }
    code = EXIT_OK
    if args.wait:
        outcome = wait_for_run(client, run_id, args.timeout)
        document["run"] = outcome.run
        document["reply"] = outcome.reply
        document["waiting"] = outcome.decision
        if outcome.kind == "waiting":
            code = EXIT_WAITING
        elif outcome.kind == "timeout":
            code = EXIT_TIMEOUT
        elif outcome.run.get("status") != "completed":
            code = EXIT_FAILURE
    if args.json:
        _print_json(document)
        return code
    print(f"已交给「{target.name}」：任务 {run_id}")
    if not args.wait:
        return code
    if document["waiting"] is not None:
        decision = document["waiting"]
        prompt = decision.get("prompt") or "它在等你的回答。"
        print(f"这次任务停下来等你：{prompt}")
        print("在 Cubes 窗口里回答后会继续；这里不会替你同意。")
    elif code == EXIT_TIMEOUT:
        print(f'{args.timeout:g} 秒内没做完，任务还在跑；用 cubes result {run_id} 再看。')
    elif document["reply"] is not None:
        print(_reply_line(document["reply"]))
    else:
        finished = document["run"]
        print(f"任务以「{finished.get('status')}」结束：{finished.get('error') or ''}".strip())
    return code


def _command_result(args: argparse.Namespace) -> int:
    client = CubesClient.from_environment()
    run = client.get_run(args.run_id)
    trace = client.run_trace(args.run_id)
    reply = _reply_of(trace, run)
    decision = (
        _waiting_decision(client, run)
        if run.get("status") == "running"
        else None
    )
    code = EXIT_OK
    if run.get("status") == "running":
        code = EXIT_WAITING if decision is not None else EXIT_FAILURE
    elif run.get("status") != "completed":
        code = EXIT_FAILURE
    if args.json:
        _print_json(
            {
                "run": run,
                "conversation_id": run.get("conversation_id"),
                "reply": reply,
                "waiting": decision,
            }
        )
        return code
    print(f"任务 {run.get('id')}：{run.get('status')} · {run.get('current_step') or ''}")
    if decision is not None:
        print(f"它在等你：{decision.get('prompt') or ''}")
        return code
    if run.get("status") == "running":
        print("还没做完；等它结束，或者直接打开 Cubes 看。")
        return code
    if run.get("status") != "completed" and run.get("error"):
        print(run["error"])
    print(_reply_line(reply))
    return code


def _command_runs(args: argparse.Namespace) -> int:
    client = CubesClient.from_environment()
    workspace = client.workspace()
    titles = {
        conversation["id"]: conversation.get("title") or ""
        for conversation in workspace.get("conversations") or []
    }
    rows: list[dict[str, Any]] = []
    for conversation_id, title in titles.items():
        for run in client.conversation_runs(conversation_id, RUNS_PER_CONVERSATION):
            rows.append({**run, "conversation_title": title})
    rows.sort(key=lambda row: (row.get("created_at") or "", row.get("id") or ""), reverse=True)
    rows = rows[:RUNS_SHOWN]
    if args.json:
        _print_json({"runs": rows})
        return EXIT_OK
    if not rows:
        print("还没有运行过的任务。")
        return EXIT_OK
    for run in rows:
        print(
            f"{_time_text(run.get('created_at'))}  {run.get('status'):9}"
            f"  {run.get('conversation_title') or ''}  {run.get('current_step') or ''}"
            f"  {run.get('id')}"
        )
    return EXIT_OK


# ------------------------------------------------------------------ MCP server


def build_mcp_server() -> Any:
    """The stdio MCP server: four safe tools, no approvals, deletion or credentials."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        "cubes",
        instructions=(
            "Cubes is a team of AI bots. Use list_bots to see who is available, send_task to "
            "hand a bot or group a task, wait_run to wait for it, and get_result to read the "
            "reply. Runs that stop to ask the user stay waiting until the user answers in the "
            "Cubes app; this server cannot approve anything."
        ),
    )

    def guard(work: Any) -> str:
        try:
            return json.dumps(work(), ensure_ascii=False)
        except CliError as exc:
            return json.dumps({"ok": False, "error": exc.message}, ensure_ascii=False)

    @server.tool(description="List the team's bots and groups, with ids and names.")
    def list_bots() -> str:
        def work() -> dict[str, Any]:
            client = CubesClient.from_environment()
            workspace = client.workspace()
            direct = _direct_conversations(workspace)
            return {
                "bots": [
                    {
                        "id": agent["id"],
                        "name": agent["name"],
                        "kind": agent.get("kind"),
                        "conversation_id": direct.get(agent["id"]),
                    }
                    for agent in workspace.get("agents") or []
                ],
                "groups": [
                    {
                        "id": conversation["id"],
                        "title": conversation.get("title") or "",
                        "member_ids": list(conversation.get("member_ids") or []),
                    }
                    for conversation in workspace.get("conversations") or []
                    if conversation.get("kind") == "group"
                ],
            }

        return guard(work)

    @server.tool(description="Send a task to a bot or group by name or id; returns the run id.")
    def send_task(target: str, task: str) -> str:
        def work() -> dict[str, Any]:
            client = CubesClient.from_environment()
            resolved = resolve_target(client, client.workspace(), target)
            run = client.start_run(resolved.conversation_id, task)
            return {
                "ok": True,
                "run_id": run["id"],
                "conversation_id": resolved.conversation_id,
                "target": {"id": resolved.id, "name": resolved.name, "kind": resolved.kind},
                "status": run.get("status"),
            }

        return guard(work)

    @server.tool(
        description=(
            "Wait until a run finishes, stops to ask the user, or the timeout passes. "
            "Returns the status and, when finished, the final reply."
        )
    )
    def wait_run(run_id: str, timeout: float = 600.0) -> str:
        def work() -> dict[str, Any]:
            if timeout <= 0:
                return {"ok": False, "error": "timeout 需要大于 0 秒。"}
            client = CubesClient.from_environment()
            outcome = wait_for_run(client, run_id, timeout)
            return {
                "ok": True,
                "status": outcome.run.get("status"),
                "waiting_on_user": outcome.kind == "waiting",
                "timed_out": outcome.kind == "timeout",
                "run": outcome.run,
                "reply": outcome.reply,
                "waiting": outcome.decision,
            }

        return guard(work)

    @server.tool(description="Read a run's status and its final reply.")
    def get_result(run_id: str) -> str:
        def work() -> dict[str, Any]:
            client = CubesClient.from_environment()
            run = client.get_run(run_id)
            reply = _reply_of(client.run_trace(run_id), run)
            decision = (
                _waiting_decision(client, run) if run.get("status") == "running" else None
            )
            return {
                "ok": True,
                "run": run,
                "reply": reply,
                "waiting": decision,
            }

        return guard(work)

    return server


# ------------------------------------------------------------------------ main


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("需要一个秒数。") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("秒数需要大于 0。")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cubes",
        description="从命令行或 MCP 驱动 Cubes：发给机器人一句话、等它做完、取回结果。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="后端、模型、正在运行的任务和等你决定的事")
    status.add_argument("--json", action="store_true", help="只输出一个 JSON 文档")
    status.set_defaults(handler=_command_status)

    bots = subparsers.add_parser("bots", help="列出机器人和群组")
    bots.add_argument("--json", action="store_true", help="只输出一个 JSON 文档")
    bots.set_defaults(handler=_command_bots)

    send = subparsers.add_parser("send", help="把一句话交给一个机器人或群组")
    send.add_argument("target", help="机器人或群组的名字（或 ID）")
    send.add_argument("task", help="要它做的事")
    send.add_argument(
        "--wait", action="store_true", help="等这次任务（连同它派出去的活）结束并打印回复"
    )
    send.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=600.0,
        metavar="秒",
        help="最多等多久（默认 600 秒）；超时后任务继续跑",
    )
    send.add_argument("--json", action="store_true", help="只输出一个 JSON 文档")
    send.set_defaults(handler=_command_send)

    result = subparsers.add_parser("result", help="看一次任务的状态和最终回复")
    result.add_argument("run_id", help="任务 ID")
    result.add_argument("--json", action="store_true", help="只输出一个 JSON 文档")
    result.set_defaults(handler=_command_result)

    runs = subparsers.add_parser("runs", help="最近的任务")
    runs.add_argument("--json", action="store_true", help="只输出一个 JSON 文档")
    runs.set_defaults(handler=_command_runs)

    subparsers.add_parser("mcp", help="在 stdio 上提供 MCP 工具（给 Claude Desktop、Cursor 等）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        build_mcp_server().run("stdio")
        return EXIT_OK
    try:
        return int(args.handler(args))
    except CliError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
