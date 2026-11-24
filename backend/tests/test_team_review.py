"""Supervisor review (§2): the consult/DM answer passes a one-call verdict gate."""

from __future__ import annotations

import asyncio
import json

from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    get_conversation,
    get_workspace,
    last_user_text,
    run_goal,
    store_of,
    tool_results,
)


def build_team(backend) -> tuple[dict, dict, dict]:
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="甲", instructions="做研究", kind="member")
    group = store.create_conversation(
        kind="group",
        title="研究小组",
        purpose="把研究做出来",
        member_ids=[supervisor["id"], member["id"]],
    )
    return supervisor, member, group


def verdict(verdict: str, feedback: str) -> dict:
    return {"content": json.dumps({"verdict": verdict, "feedback": feedback}, ensure_ascii=False)}


def review_rows(detail: dict) -> list[dict]:
    return [m for m in detail["messages"] if m["kind"] == "action" and m["action"]["tool_name"] == "review"]


def review_usage_rows(backend) -> list[dict]:
    store = store_of(backend)
    conn = store._ensure()
    rows = conn.execute(
        "SELECT agent_id, purpose, input_tokens, output_tokens FROM model_usage WHERE purpose = 'review'"
    ).fetchall()
    return [dict(row) for row in rows]


def test_review_disabled_makes_no_review_call(make_backend, upstream) -> None:
    backend = make_backend()
    _, member, group = build_team(backend)
    upstream.script = [
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "给出结论"})
        ),
        {"content": "成员答案"},
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "让甲研究一下")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])
            assert review_rows(detail) == []
            assert run["model_calls"] == 3

        assert upstream.request_count == 3
        assert review_usage_rows(backend) == []

    asyncio.run(body())


def test_review_pass_records_verdict_and_row(make_backend, upstream) -> None:
    backend = make_backend()
    supervisor, member, group = build_team(backend)
    store_of(backend).set_team_settings(review={"enabled": True, "max_rounds": 2})
    upstream.usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 30},
    }
    upstream.script = [
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "给出结论"})
        ),
        {"content": "成员答案"},
        verdict("pass", "可以交付"),
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "让甲研究一下")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 4

            detail = await get_conversation(client, group["id"])
            rows = review_rows(detail)
            assert len(rows) == 1
            row = rows[0]
            assert row["author_id"] == supervisor["id"]
            assert row["action"]["summary"] == "验收通过「甲」"
            assert row["action"]["status"] == "completed"
            assert row["action"]["target_agent_id"] == member["id"]

            # The ask_agent tool result carries the review verdict.
            results = dict(tool_results(upstream.requests[-1]))
            assert results["c1"]["review"] == {"rounds": 0, "passed": True, "feedback": []}

            # Review usage is attributed to the supervisor.
            used = review_usage_rows(backend)
            assert len(used) == 1
            assert used[0]["agent_id"] == supervisor["id"]
            assert used[0]["input_tokens"] == 100
            assert used[0]["output_tokens"] == 20

    asyncio.run(body())


def test_review_revise_then_pass_asks_twice(make_backend, upstream) -> None:
    backend = make_backend()
    supervisor, member, group = build_team(backend)
    store_of(backend).set_team_settings(review={"enabled": True, "max_rounds": 2})
    upstream.script = [
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "给出结论"})
        ),
        {"content": "第一版"},
        verdict("revise", "再详细一点"),
        {"content": "第二版"},
        verdict("pass", "可以了"),
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "让甲研究一下")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 6

            detail = await get_conversation(client, group["id"])
            rows = review_rows(detail)
            assert [r["action"]["summary"] for r in rows] == [
                "打回「甲」：再详细一点",
                "验收通过「甲」（打回 1 次后）",
            ]
            assert all(r["author_id"] == supervisor["id"] for r in rows)

            results = dict(tool_results(upstream.requests[-1]))
            assert results["c1"]["review"] == {
                "rounds": 1,
                "passed": True,
                "feedback": ["再详细一点"],
            }

        # The member was asked twice; the second request carries the feedback.
        assert upstream.request_count == 6
        member_second = upstream.requests[3]
        assert "主管的验收意见" in last_user_text(member_second)
        assert "再详细一点" in last_user_text(member_second)
        # The first member request carries no review feedback.
        member_first = upstream.requests[1]
        assert "主管的验收意见" not in last_user_text(member_first)

    asyncio.run(body())


def test_review_max_rounds_then_deliver(make_backend, upstream) -> None:
    backend = make_backend()
    supervisor, member, group = build_team(backend)
    store_of(backend).set_team_settings(review={"enabled": True, "max_rounds": 2})
    upstream.script = [
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "给出结论"})
        ),
        {"content": "第一版"},
        verdict("revise", "不够详细"),
        {"content": "第二版"},
        verdict("revise", "还是不行"),
        {"content": "第三版"},
        verdict("revise", "依然有问题"),
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "让甲研究一下")
            assert run["status"] == "completed", run
            assert run["model_calls"] == 8

            detail = await get_conversation(client, group["id"])
            rows = review_rows(detail)
            assert [r["action"]["summary"] for r in rows] == [
                "打回「甲」：不够详细",
                "打回「甲」：还是不行",
                "「甲」打回 2 次仍未通过，按现有结果交付",
            ]

            results = dict(tool_results(upstream.requests[-1]))
            assert results["c1"]["review"] == {
                "rounds": 2,
                "passed": False,
                "feedback": ["不够详细", "还是不行"],
            }

    asyncio.run(body())


def test_review_malformed_verdict_is_a_pass(make_backend, upstream) -> None:
    backend = make_backend()
    _, member, group = build_team(backend)
    store_of(backend).set_team_settings(review={"enabled": True, "max_rounds": 2})
    upstream.script = [
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "给出结论"})
        ),
        {"content": "成员答案"},
        {"content": "not valid json"},
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "让甲研究一下")
            assert run["status"] == "completed", run

            detail = await get_conversation(client, group["id"])
            rows = review_rows(detail)
            assert [r["action"]["summary"] for r in rows] == ["验收通过「甲」"]

            results = dict(tool_results(upstream.requests[-1]))
            assert results["c1"]["review"]["passed"] is True

    asyncio.run(body())


def test_a_fenced_revise_verdict_still_sends_the_work_back() -> None:
    # Models wrap the object in a ```json fence or a sentence; that is still a revise.
    from app.engine import SupervisorEngine

    fenced = '好的，验收结果如下：\n```json\n{"verdict": "revise", "feedback": "缺少数据来源"}\n```'
    assert SupervisorEngine._parse_review(fenced) == ("revise", "缺少数据来源")
    assert SupervisorEngine._parse_review("not valid json") == ("pass", "")


def test_review_applies_to_a_supervisor_direct_message(make_backend, upstream) -> None:
    backend = make_backend()
    supervisor, member, _ = build_team(backend)
    store_of(backend).set_team_settings(review={"enabled": True, "max_rounds": 2})
    upstream.script = [
        tool_calls(
            call("c1", "direct_message", {"agent_id": member["id"], "content": "给出结论"})
        ),
        {"content": "第一版"},
        verdict("revise", "再详细一点"),
        {"content": "第二版"},
        verdict("pass", "可以了"),
        {"content": "成员已回复"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "私信甲要结论")
            assert run["status"] == "completed", run

            workspace = await get_workspace(client)
            privates = [c for c in workspace["conversations"] if c["kind"] == "private"]
            assert len(privates) == 1
            detail = await get_conversation(client, privates[0]["id"])
            rows = review_rows(detail)
            assert [r["action"]["summary"] for r in rows] == [
                "打回「甲」：再详细一点",
                "验收通过「甲」（打回 1 次后）",
            ]
            assert all(r["author_id"] == supervisor["id"] for r in rows)

            # The direct_message tool result carries the review verdict.
            results = dict(tool_results(upstream.requests[-1]))
            assert results["c1"]["review"] == {
                "rounds": 1,
                "passed": True,
                "feedback": ["再详细一点"],
            }

    asyncio.run(body())
