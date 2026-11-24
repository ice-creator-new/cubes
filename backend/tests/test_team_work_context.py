"""What a group member's request now carries about the work, not just the talk.

Three failures made a team of bots re-read the same files: a teammate's tool work was
invisible, the replay allowance was the same 3 turns / 8K chars on a 1M-token model as on a
32K one, and a compaction threw every path and command away. These tests pin each of them,
then the whole chain once through the real HTTP run loop.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.agent import budget as budget_rules
from app.agent import history
from app.cli_engines import new_messages, render_messages
from fake_upstream import call, tool_calls
from helpers import client_for, get_conversation, last_user_text, run_goal, store_of


def message_row(
    message_id: str,
    seq: int,
    *,
    kind: str = "agent",
    content: str,
    author_id: str | None,
    author_name: str,
    status: str = "complete",
) -> dict:
    return {
        "id": message_id,
        "kind": kind,
        "content": content,
        "status": status,
        "author_id": author_id,
        "author_name": author_name,
        "seq": seq,
        "reply_to_id": None,
        "reactions": [],
        "interrupts": False,
        "schedule": None,
        "attachments": [],
        "mood": None,
    }


def trace_row(
    seq: int, *, author_id: str, call_id: str, name: str, arguments: dict, result: str
) -> dict:
    return {
        "seq": seq,
        "author_id": author_id,
        "tool_call_id": call_id,
        "tool_name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
        "status": "completed",
        "result": result,
    }


def build_history(self_id: str, *, turns: int = 0) -> tuple[list[dict], list[dict]]:
    """A group where one teammate writes a file, then this bot works ``turns`` times."""
    rows = [
        message_row("m_user", 1, kind="user", content="把首页做出来", author_id=None, author_name="你"),
        message_row("m_a1", 2, content="首页写好了", author_id="a1", author_name="甲"),
        message_row("m_a1_action", 3, kind="action", content="写入文件 lib/home.dart", author_id="a1", author_name="甲"),
        message_row("m_a2", 4, content="收到，我来跑测试", author_id="a2", author_name="乙"),
    ]
    traces = [
        trace_row(
            3,
            author_id="a1",
            call_id="call_a1",
            name="file_write",
            arguments={"path": "/ws/lib/home.dart", "content": "…"},
            result="已写入「lib/home.dart」（312 个字符，+13 -7）。",
        )
    ]
    seq = 5
    for index in range(turns):
        rows.append(
            message_row(f"m_own_{index}", seq, content=f"第{index}步", author_id=self_id, author_name="乙")
        )
        rows.append(
            message_row(f"m_own_action_{index}", seq + 1, kind="action", content="运行测试", author_id=self_id, author_name="乙")
        )
        traces.append(
            trace_row(
                seq + 1,
                author_id=self_id,
                call_id=f"call_own_{index}",
                name="computer_shell",
                arguments={"command": f"flutter test {index}"},
                result=f"第{index}轮测试通过",
            )
        )
        seq += 2
    return rows, traces


def test_a_teammates_tool_work_shows_up_under_its_reply() -> None:
    rows, traces = build_history("a2")
    seen = history.to_chat_history(
        rows,
        self_id="a2",
        vision=False,
        pdf=False,
        attachments_dir=Path("/tmp"),
        traces=traces,
    )
    notes = [m["content"] for m in seen if m["role"] == "user" and isinstance(m["content"], str)]
    team_note = next(text for text in notes if "甲" in text and "工具动作" in text)
    assert "file_write" in team_note
    assert "lib/home.dart" in team_note
    assert "+13 -7" in team_note
    # The note follows that reply, so it cannot be read as the user saying it.
    reply_index = next(
        index for index, item in enumerate(seen) if item.get("role") == "user" and "首页写好了" in str(item.get("content"))
    )
    assert seen[reply_index + 1]["content"] == team_note


def test_the_replay_allowance_grows_with_the_window() -> None:
    rows, traces = build_history("a2", turns=8)

    def replayed(window: int) -> int:
        seen = history.to_chat_history(
            rows,
            self_id="a2",
            vision=False,
            pdf=False,
            attachments_dir=Path("/tmp"),
            traces=traces,
            plan=budget_rules.replay_plan(window),
        )
        return sum(1 for item in seen if item.get("tool_calls"))

    assert budget_rules.replay_plan(128_000).turns == 3
    assert budget_rules.replay_plan(1_048_576).turns == 12
    assert replayed(32_000) == 3
    assert replayed(1_048_576) == 8
    # The wide window keeps the actual results, not only their count.
    wide = history.to_chat_history(
        rows,
        self_id="a2",
        vision=False,
        pdf=False,
        attachments_dir=Path("/tmp"),
        traces=traces,
        plan=budget_rules.replay_plan(1_048_576),
    )
    results = [m["content"] for m in wide if m["role"] == "tool"]
    assert any("第0轮测试通过" in str(text) for text in results)


def test_the_summary_transcript_keeps_every_authors_tool_work() -> None:
    rows, traces = build_history("a2", turns=2)
    transcript = history.summary_transcript(rows, traces=traces)
    # The teammate's write and this bot's own commands are both there, with paths.
    assert "lib/home.dart" in transcript
    assert "computer_shell" in transcript
    assert "+13 -7" in transcript


def test_a_cli_turn_still_receives_the_newest_user_turn_with_its_hints() -> None:
    """A resumed Claude Code session is sent only the new part, hints included."""
    hint = "工作集（这个会话里最近动过的文件…）"
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": f"第一轮\n\n{hint}\n\n<runtime time=\"t\" timezone=\"UTC\"/>"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": f"第二轮\n\n{hint}\n\n<runtime time=\"t2\" timezone=\"UTC\"/>"},
    ]
    tail = new_messages(messages, 2)
    assert [item["content"].splitlines()[0] for item in tail] == ["第二轮"]
    assert hint in tail[-1]["content"]
    assert hint in render_messages(tail, "claude-code").blocks[0]["text"]


def test_a_teammates_write_reaches_the_next_members_request(make_backend, upstream, tmp_path) -> None:
    """The whole chain once, on the real run loop: write a file, then hand the turn over."""
    backend = make_backend(workspace_dir=tmp_path / "ws")
    store = store_of(backend)
    store.set_permission_settings(mode="auto")
    supervisor = store.supervisor()
    writer = store.create_agent(name="甲", instructions="写代码", kind="member")
    reader = store.create_agent(name="乙", instructions="跑测试", kind="member")
    group = store.create_conversation(
        kind="group",
        title="修首页",
        purpose="把首页改好",
        member_ids=[supervisor["id"], writer["id"], reader["id"]],
    )

    def script(requests, index):
        if index == 1:
            return tool_calls(
                call("call_1", "file_write", {"path": "lib/home.dart", "content": "// 新首页\n// 第二行\n"})
            )
        if index == 2:
            return {"content": "首页写好了"}
        if index == 3:
            return {"content": "我看到工作集了"}
        return {"content": "收尾"}

    upstream.script = [script]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@甲 @乙 把首页改好")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])

    asyncio.run(body())
    second = upstream.requests[2]
    texts = [str(item.get("content")) for item in second["messages"]]
    # The reader is told the file exists, who wrote it and what is in it now.
    hint = last_user_text(second)
    assert "工作集" in hint
    assert "lib/home.dart" in hint
    assert "写 甲" in hint
    assert "// 新首页" in hint
    # And it sees the writer's result in the group view.
    assert any("[甲] 首页写好了" in text for text in texts)
    # The write really happened on disk, where the hint read it back from.
    assert (tmp_path / "ws" / "lib" / "home.dart").read_text(encoding="utf-8") == "// 新首页\n// 第二行\n"
