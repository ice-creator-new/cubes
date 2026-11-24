"""Groups the user runs: creating one, @所有人, side-by-side opinions, rules, group memory."""

from __future__ import annotations

import asyncio

from fake_upstream import call, tool_calls
from helpers import agent_messages, client_for, get_conversation, run_goal, store_of, system_prompt


def build_team(backend) -> dict:
    store = store_of(backend)
    supervisor = store.supervisor()
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    return {"supervisor": supervisor, "designer": designer, "reviewer": reviewer}


def speaker_of(request: dict) -> str:
    prompt = system_prompt(request)
    for name in ("甲", "乙"):
        if prompt.startswith(f"你是「{name}」"):
            return name
    return "主管"


def answer_by_speaker(answers: dict[str, str]):
    """A script entry that answers whoever the request is for, whatever order they arrive in."""

    def entry(requests: list[dict], index: int) -> dict:
        return {"content": answers[speaker_of(requests[index - 1])]}

    return entry


def test_the_user_creates_a_group_and_the_supervisor_is_optional(make_backend) -> None:
    backend = make_backend()
    team = build_team(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.post(
                "/conversations",
                json={
                    "title": "评审组",
                    "purpose": "评审方案",
                    "member_ids": [team["designer"]["id"]],
                    "rules": "先给结论",
                },
            )
            assert response.status_code == 201, response.text
            group = response.json()
            assert group["rules"] == "先给结论"
            assert group["kind"] == "group"
            # The supervisor is no longer forced in; it is added only when asked for.
            assert group["member_ids"] == [team["designer"]["id"]]
            with_boss = await client.post(
                "/conversations",
                json={
                    "title": "带主管的组",
                    "purpose": "评审方案",
                    "member_ids": [team["designer"]["id"]],
                    "include_supervisor": True,
                },
            )
            assert with_boss.status_code == 201, with_boss.text
            assert with_boss.json()["member_ids"] == [
                team["supervisor"]["id"],
                team["designer"]["id"],
            ]
            missing = await client.post(
                "/conversations", json={"title": "x", "purpose": "y", "member_ids": ["nobody"]}
            )
            assert missing.status_code == 400
            empty = await client.post(
                "/conversations", json={"title": "x", "purpose": "y", "member_ids": []}
            )
            assert empty.status_code == 422

    asyncio.run(body())


def test_everyone_answers_on_their_own_with_no_closing(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    group = store_of(backend).create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[team["supervisor"]["id"], team["designer"]["id"], team["reviewer"]["id"]],
    )
    upstream.script = [answer_by_speaker({"甲": "用 A 方案", "乙": "风险在工期", "主管": "两条都记下了"})]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "@所有人 说说你们的看法")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])

        replies = agent_messages(detail)
        # Every member answers side by side; there is no separate supervisor closing word.
        assert len(replies) == 3
        assert {reply["author_id"] for reply in replies} == {
            team["designer"]["id"],
            team["reviewer"]["id"],
            team["supervisor"]["id"],
        }
        by_speaker = {speaker_of(request): request for request in upstream.requests}
        member_texts = {
            name: " ".join(str(m.get("content")) for m in by_speaker[name]["messages"])
            for name in ("甲", "乙", "主管")
        }
        # Nobody saw anyone else's opinion: each is its own.
        assert "风险在工期" not in member_texts["甲"]
        assert "用 A 方案" not in member_texts["乙"]
        assert "用 A 方案" not in member_texts["主管"]
        assert "风险在工期" not in member_texts["主管"]
        assert "各自独立地回答" in str(by_speaker["甲"]["messages"][-1]["content"])
        assert "收尾" not in str(by_speaker["主管"]["messages"][-1]["content"])

    asyncio.run(body())


def test_an_un_at_message_with_nobody_else_goes_to_the_supervisor(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    group = store_of(backend).create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[team["supervisor"]["id"], team["designer"]["id"]],
    )
    upstream.script = [{"content": "不客气"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, group["id"], "谢谢大家")
            assert run["status"] == "completed", run
            detail = await get_conversation(client, group["id"])

        assert len(upstream.requests) == 1
        assert agent_messages(detail)[-1]["author_id"] == team["supervisor"]["id"]

    asyncio.run(body())


def test_group_rules_and_group_memory_reach_only_that_group(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    store = store_of(backend)
    members = [team["supervisor"]["id"], team["designer"]["id"]]
    group = store.create_conversation(kind="group", title="方案小组", purpose="做方案", member_ids=members)
    other = store.create_conversation(kind="group", title="闲聊", purpose="随便聊", member_ids=members)
    upstream.script = [
        tool_calls(call("m1", "remember", {"content": "方案小组的结论用表格写", "scope": "group"})),
        {"content": "记下了"},
        {"content": "好"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            patched = await client.patch(f"/conversations/{group['id']}", json={"rules": "回答不超过三句"})
            assert patched.status_code == 200 and patched.json()["rules"] == "回答不超过三句"
            run = await run_goal(client, group["id"], "@甲 记一下：结论用表格写")
            assert run["status"] == "completed", run
            run = await run_goal(client, other["id"], "@甲 在吗")
            assert run["status"] == "completed", run

        first, _, elsewhere = upstream.requests
        assert "回答不超过三句" in system_prompt(first)
        assert "回答不超过三句" not in system_prompt(elsewhere)
        assert "方案小组的结论用表格写" not in system_prompt(elsewhere)
        rows = [m for m in store.list_memories() if m["scope"] == "group"]
        assert [(m["agent_id"], m["content"]) for m in rows] == [(group["id"], "方案小组的结论用表格写")]
        # Dissolving the group takes its own facts with it.
        store.dissolve_group(group["id"])
        assert [m for m in store.list_memories() if m["scope"] == "group"] == []

    asyncio.run(body())
