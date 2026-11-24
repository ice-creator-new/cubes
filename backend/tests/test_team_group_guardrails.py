"""Group guardrails: a coordinating supervisor, and replies that stop short.

Two behaviours the engine owes the user, tested over real HTTP against the fake upstream:

* a group's supervisor delegates by default, and gets its work tools back only when the user
  opens by @-mentioning it or the team setting says so;
* a reply that stops on 「需要你决定：」 without ``ask_user`` gets exactly one nudge, and a
  reply that ends normally gets none.

A progress question is no longer a text heuristic: the dispatcher posts a structured status
card (see ``test_dispatch.py``), so the old fact-sheet answer is gone.
"""

from __future__ import annotations

import asyncio
import time

from app.agent.dangling import is_dangling
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    last_user_text,
    run_goal,
    start_run,
    store_of,
    tool_names,
    wait_for_run,
)


def build_team(backend) -> dict:
    store = store_of(backend)
    supervisor = store.supervisor()
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"]],
    )
    return {"supervisor": supervisor, "designer": designer, "group": group}


# ------------------------------------------------------------ supervisor coordination


def work_tools(names: list[str]) -> set[str]:
    return set(names) & {"run_code", "file_write", "note_create", "task_create"}


def test_a_group_supervisor_gets_no_work_tools_unless_named_or_allowed(
    make_backend, upstream
) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [
        {"content": "我来安排"},
        {"content": "我来安排"},
        {"content": "我亲自查一下"},
        {"content": "我记下来了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            # Nobody has spoken in the group yet: the supervisor coordinates, so no work tools.
            run = await run_goal(client, team["group"]["id"], "帮我看看文件")
            assert run["status"] == "completed", run
            coordinating = tool_names(upstream.requests[0])
            assert work_tools(coordinating) == set()
            assert "file_read" in coordinating and "web_search" in coordinating
            assert "ask_user" in coordinating

            # Naming it mid-sentence refers to it; it still only coordinates.
            started = await start_run(
                client,
                team["group"]["id"],
                "请你按照 @主管 的方案继续吧",
                mentions=[team["supervisor"]["id"]],
            )
            run = await wait_for_run(client, started["id"])
            assert run["status"] == "completed", run
            assert work_tools(tool_names(upstream.requests[-1])) == set()
            upstream.requests.clear()

            # The user opens by addressing it: that turn gets its full tools back.
            started = await start_run(
                client,
                team["group"]["id"],
                "@主管 你亲自查一下",
                mentions=[team["supervisor"]["id"]],
            )
            run = await wait_for_run(client, started["id"])
            assert run["status"] == "completed", run
            named = tool_names(upstream.requests[-1])
            assert {"run_code", "file_write"} <= set(named)

            # The team setting turns the restriction off for the whole workspace.
            patched = await client.patch(
                "/settings/team", json={"supervisor_works_in_groups": True}
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["supervisor_works_in_groups"] is True

            run = await run_goal(client, team["group"]["id"], "再看看文件")
            assert run["status"] == "completed", run
            allowed = tool_names(upstream.requests[2])
            assert {"run_code", "file_write"} <= set(allowed)

    asyncio.run(body())


# ---------------------------------------------------------------- replies that stop short


QUESTION = {
    "questions": [
        {
            "header": "方案",
            "question": "先做哪一块？",
            "options": [{"label": "后端"}, {"label": "前端"}],
            "recommended": "后端",
        }
    ]
}


async def wait_for_question(client, conversation_id: str, *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await get_conversation(client, conversation_id)
        for message in action_messages(detail):
            action = message["action"]
            if action["status"] == "awaiting" and (
                (action.get("confirmation") or {}).get("kind") == "question"
            ):
                return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no question appeared in {conversation_id}")


def test_a_dangling_reply_gets_one_nudge_that_opens_the_ask_card(make_backend, upstream) -> None:
    upstream.script = [
        {"content": "有一件事需要你现在决定，因为它在影响我们反复返工："},
        tool_calls(call("call_1", "ask_user", QUESTION)),
        {"content": "好，先做后端。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "帮我把这个做完")
            waiting = await wait_for_question(client, direct_id)

            # Exactly one follow-up call, with the note telling it to ask or finish.
            assert upstream.request_count == 2
            nudge = upstream.requests[1]
            assert "你的回复停在半句" in last_user_text(nudge)
            # The card is open and the run is parked, not completed.
            parked = await get_run(client, run["id"])
            assert parked["status"] == "running"

            answer = await client.post(
                f"/actions/{waiting['action']['id']}/answer",
                json={"answers": [{"question_index": 0, "labels": ["后端"]}], "skip": False},
            )
            assert answer.status_code == 200, answer.text
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            # The follow-up continued the same reply instead of starting a second one.
            replies = agent_messages(await get_conversation(client, direct_id))
            assert replies[0]["content"] == "有一件事需要你现在决定，因为它在影响我们反复返工："
            # The normal, complete answer after the user replied was not nudged again.
            assert upstream.request_count == 3
            assert "你的回复停在半句" not in last_user_text(upstream.requests[2])

    asyncio.run(body())


def test_a_reply_that_ends_normally_gets_no_nudge(make_backend, upstream) -> None:
    upstream.script = [{"content": "已经做完了。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await run_goal(client, direct_id, "做完了吗")
            assert run["status"] == "completed", run
        assert upstream.request_count == 1

    asyncio.run(body())


def test_the_matcher_stays_small() -> None:
    assert is_dangling("有一件事需要你现在决定：")
    assert is_dangling("另外需要你确认一下。")
    assert is_dangling("请你决定用哪个方案")
    assert not is_dangling("已经做完了。")
    assert not is_dangling("表格里还缺一列说明，明天补上。")
    assert not is_dangling("")
