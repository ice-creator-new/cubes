"""Per-member profile (§4): the all-time tallies, the period usage, and the supervisor-only
``成员档案`` runtime hint that never leaks into the system prompt."""

from __future__ import annotations

import asyncio
import json

from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    get_workspace,
    last_user_text,
    run_goal,
    store_of,
    system_prompt,
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


def test_profile_counts_replies_runs_tasks_reviews_memories(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor, member, group = build_team(backend)

    store.create_task(title="任务一", assignee_id=member["id"], status="todo")
    store.create_task(title="任务二", assignee_id=member["id"], status="done")
    store.create_task(title="任务三", assignee_id=member["id"], status="done")
    for index in range(3):
        store.create_memory(
            content=f"甲的经验 {index}", scope="agent", agent_id=member["id"], origin="manual"
        )

    store.set_team_settings(review={"enabled": True, "max_rounds": 2})
    upstream.usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 30},
    }
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

            response = await client.get(f"/agents/{member['id']}/profile?period=week")
            assert response.status_code == 200, response.text
            profile = response.json()

            assert profile["agent_id"] == member["id"]
            assert profile["period"] == "week"
            # 甲 answered twice during the revise-then-pass consult.
            assert profile["replies"] == 2
            assert profile["runs"] == 1
            assert profile["runs_completed"] == 1
            assert profile["runs_failed"] == 0
            assert profile["tasks_done"] == 2
            assert profile["tasks_open"] == 1
            assert profile["reviews"] == {"passed": 1, "revised": 1}
            assert profile["top_tools"] == []
            assert profile["memories"] == 3
            assert profile["usage"]["model_calls"] == 2
            assert profile["usage"]["total_tokens"] == 240
            assert len(profile["recent_runs"]) == 1
            assert profile["recent_runs"][0]["run_id"] == run["id"]
            assert profile["recent_runs"][0]["status"] == "completed"

    asyncio.run(body())


def test_profile_top_tools_counts_member_tool_use(make_backend, upstream) -> None:
    backend = make_backend()
    supervisor, member, group = build_team(backend)
    upstream.script = [
        tool_calls(call("c1", "calculate", {"expression": "1 + 1"})),
        {"content": "结果是 2"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@甲 算一下")
            assert run["status"] == "completed", run

            profile = (await client.get(f"/agents/{member['id']}/profile?period=day")).json()
            assert profile["top_tools"] == [{"tool_name": "calculate", "count": 1}]

    asyncio.run(body())


def test_profile_404_and_invalid_period(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            assert (await client.get("/agents/nope/profile")).status_code == 404
            member = store_of(backend).create_agent(name="甲", instructions="做研究", kind="member")
            assert (await client.get(f"/agents/{member['id']}/profile?period=year")).status_code == 422

    asyncio.run(body())


def test_profile_hint_only_in_supervisor_requests(make_backend, upstream) -> None:
    backend = make_backend()
    _, member, group = build_team(backend)
    upstream.script = [
        tool_calls(
            call("c1", "ask_agent", {"group_id": group["id"], "agent_id": member["id"], "task": "给出结论"})
        ),
        {"content": "甲的结论"},
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "让甲研究一下")
            assert run["status"] == "completed", run

        # requests: [0] supervisor (ask_agent), [1] member (consult), [2] supervisor (final).
        assert upstream.request_count == 3
        supervisor_requests = [upstream.requests[0], upstream.requests[2]]
        member_request = upstream.requests[1]

        for request in supervisor_requests:
            assert "成员档案：" in last_user_text(request)
            assert "成员档案：" not in system_prompt(request)
        assert "成员档案：" not in last_user_text(member_request)
        assert "成员档案：" not in system_prompt(member_request)

    asyncio.run(body())


def test_profile_hint_keeps_system_prompt_stable_across_stat_changes(make_backend, upstream) -> None:
    backend = make_backend()
    _, member, group = build_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]

            # First supervisor turn: 甲 has no runs yet.
            upstream.script = [{"content": "第一次回答"}]
            await run_goal(client, direct_id, "第一次")
            first_system = system_prompt(upstream.requests[0])
            assert "成员档案：" in last_user_text(upstream.requests[0])

            # 甲 answers a run, changing its stats.
            upstream.script = [{"content": "甲的回复"}]
            await run_goal(client, group["id"], "@甲 请回复")

            # Second supervisor turn sees different stats, but the same system prompt.
            upstream.script = [{"content": "第二次回答"}]
            await run_goal(client, direct_id, "第二次")
            second_system = system_prompt(upstream.requests[2])
            assert "成员档案：" in last_user_text(upstream.requests[2])

            assert first_system == second_system

    asyncio.run(body())
