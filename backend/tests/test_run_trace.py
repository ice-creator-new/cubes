"""Run traces (§3): every message a run writes carries its ``run_id``, the hand-off graph
answers ask/consult/dm/mention edges, and a conversation lists its runs newest-first."""

from __future__ import annotations

import asyncio

from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    find_result_key,
    get_conversation,
    get_workspace,
    run_goal,
    store_of,
)


def seed_member(backend, name: str) -> dict:
    return store_of(backend).create_agent(name=name, instructions="做研究", kind="member")


def test_run_id_lands_on_every_message_including_consult_and_dm(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    a = seed_member(backend, "甲")
    b = seed_member(backend, "乙")
    b_direct = store.ensure_direct_conversation(b["id"])["id"]
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "create_group",
                {"title": "小组", "purpose": "出结论", "member_ids": [a["id"], b["id"]]},
            )
        ),
        lambda requests, index: tool_calls(
            call(
                "call_2",
                "ask_agent",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "agent_id": a["id"],
                    "task": "给出结论",
                },
            )
        ),
        {"content": "甲的结论"},
        lambda requests, index: tool_calls(
            call("call_3", "direct_message", {"agent_id": b["id"], "content": "帮我确认"})
        ),
        {"content": "乙的答复"},
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "帮我出个结论")
            assert run["status"] == "completed", run

            workspace = await get_workspace(client)
            group = next(c for c in workspace["conversations"] if c["kind"] == "group")
            private = next(c for c in workspace["conversations"] if c["kind"] == "private")

            # The run's own thread, the group it consulted, the private DM thread and the
            # target's relay markers all carry the run id.
            for conversation_id in (direct_id, group["id"], private["id"], b_direct):
                detail = await get_conversation(client, conversation_id)
                assert detail["messages"], conversation_id
                assert all(
                    message["run_id"] == run["id"] for message in detail["messages"]
                ), conversation_id

    asyncio.run(body())


def test_trace_nodes_and_edges_for_ask_consult_dm(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    a = seed_member(backend, "甲")
    b = seed_member(backend, "乙")
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "create_group",
                {"title": "小组", "purpose": "出结论", "member_ids": [a["id"], b["id"]]},
            )
        ),
        lambda requests, index: tool_calls(
            call(
                "call_2",
                "ask_agent",
                {
                    "group_id": find_result_key(requests, "conversation_id"),
                    "agent_id": a["id"],
                    "task": "给出结论",
                },
            )
        ),
        {"content": "甲的结论"},
        lambda requests, index: tool_calls(
            call("call_3", "direct_message", {"agent_id": b["id"], "content": "帮我确认"})
        ),
        {"content": "乙的答复"},
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "帮我出个结论")
            assert run["status"] == "completed", run

            response = await client.get(f"/runs/{run['id']}/trace")
            assert response.status_code == 200, response.text
            trace = response.json()

            assert trace["run"]["id"] == run["id"]
            assert trace["conversation_id"] == direct_id
            assert trace["messages"]
            assert all(message["run_id"] == run["id"] for message in trace["messages"])

            nodes = {node["id"]: node for node in trace["nodes"]}
            assert set(nodes) >= {"user", supervisor["id"], a["id"], b["id"]}
            assert nodes["user"]["kind"] == "user"
            assert nodes[supervisor["id"]]["kind"] == "supervisor"
            assert nodes[a["id"]]["kind"] == "member"
            assert nodes[b["id"]]["kind"] == "member"

            kinds = {(edge["kind"], edge["from"], edge["to"]) for edge in trace["edges"]}
            assert ("ask", "user", supervisor["id"]) in kinds
            assert ("consult", supervisor["id"], a["id"]) in kinds
            assert ("dm", supervisor["id"], b["id"]) in kinds
            # Each hand-off carries what was handed over, so the replay can show it.
            briefs = {edge["kind"]: edge.get("brief") for edge in trace["edges"]}
            assert briefs["consult"] == "给出结论"
            assert briefs["dm"] == "帮我确认"
            assert briefs["ask"] is None

    asyncio.run(body())


def test_trace_mention_edge_from_a_group_handoff(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    a = seed_member(backend, "甲")
    b = seed_member(backend, "乙")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="出方案",
        member_ids=[supervisor["id"], a["id"], b["id"]],
    )
    # 甲 answers first, its reply names 乙, which queues 乙 for the next reply.
    upstream.script = [
        {"content": "方案在这里，@乙 请评估风险"},
        {"content": "风险可控"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@甲 先出方案")
            assert run["status"] == "completed", run

            trace = (await client.get(f"/runs/{run['id']}/trace")).json()
            kinds = {(edge["kind"], edge["from"], edge["to"]) for edge in trace["edges"]}
            assert ("ask", "user", a["id"]) in kinds
            assert ("mention", a["id"], b["id"]) in kinds

    asyncio.run(body())


def test_the_user_hands_a_group_run_to_the_supervisor_even_when_a_member_speaks_first(
    make_backend, upstream
) -> None:
    # Seen live: "@主管 问下别的成员" in a group. The consulted member's answer is the first
    # agent reply in the group, but the run was handed to the supervisor, not to that member.
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    a = seed_member(backend, "甲")
    group = store.create_conversation(
        kind="group", title="小组", purpose="评估", member_ids=[supervisor["id"], a["id"]]
    )
    upstream.script = [
        tool_calls(call("c1", "ask_agent", {"group_id": group["id"], "agent_id": a["id"], "task": "评价一下"})),
        {"content": "甲的评价"},
        {"content": "汇总完成"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            for goal in ("@主管 问下甲怎么看", "问下甲怎么看"):
                run = await run_goal(client, group["id"], goal)
                assert run["status"] == "completed", run
                trace = (await client.get(f"/runs/{run['id']}/trace")).json()
                asks = [(e["from"], e["to"]) for e in trace["edges"] if e["kind"] == "ask"]
                assert asks == [("user", supervisor["id"])], goal
                upstream.script = [
                    tool_calls(call("c2", "ask_agent", {"group_id": group["id"], "agent_id": a["id"], "task": "评价一下"})),
                    {"content": "甲的评价"},
                    {"content": "汇总完成"},
                ]

    asyncio.run(body())


def test_conversation_runs_newest_first_and_404(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]

            upstream.script = [{"content": "一"}]
            first = await run_goal(client, direct_id, "第一次")
            upstream.script = [{"content": "二"}]
            second = await run_goal(client, direct_id, "第二次")

            response = await client.get(f"/conversations/{direct_id}/runs")
            assert response.status_code == 200, response.text
            runs = response.json()["runs"]
            assert [run["id"] for run in runs] == [second["id"], first["id"]]

            limited = await client.get(f"/conversations/{direct_id}/runs?limit=1")
            assert [run["id"] for run in limited.json()["runs"]] == [second["id"]]

            assert (await client.get("/conversations/nope/runs")).status_code == 404
            assert (await client.get("/runs/nope/trace")).status_code == 404

    asyncio.run(body())


def test_a_run_without_a_user_message_still_traces(tmp_path) -> None:
    from app.store import Store

    store = Store(tmp_path / "cubes.sqlite3")
    store.initialize()
    supervisor = store.supervisor()
    direct = store.direct_conversation()
    assert supervisor is not None and direct is not None
    run = store.begin_bare_run(direct["id"], "工作流")
    store.append_message(
        conversation_id=direct["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content="第一步做完了",
        status="complete",
        run_id=run["id"],
    )
    trace = store.run_trace(run["id"])
    assert trace is not None
    assert trace["run"]["id"] == run["id"]
    assert trace["messages"][0]["content"] == "第一步做完了"
