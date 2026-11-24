"""A new run names the bot that will answer from its very first payload.

Clients show "<name> 正在思考" as soon as a run starts. Before this, a fresh run carried no
``current_agent_id`` until the engine took its first turn, so a member's private chat showed the
supervisor thinking first.
"""

from __future__ import annotations

import asyncio

from helpers import client_for, start_run, store_of, wait_for_run


def test_a_direct_chat_run_names_its_member_at_once(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="设计", instructions="做设计", kind="member")
    chat = store.ensure_direct_conversation(member["id"])
    upstream.chunk_delay = 0.2  # keep the run open long enough to look at it

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, chat["id"], "帮我看看这个")
            assert run["current_agent_id"] == member["id"]
            await wait_for_run(client, run["id"])

    asyncio.run(body())


def test_a_group_run_names_the_mentioned_member_or_the_last_speaker(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="出方案", kind="member")
    group = store.create_conversation(
        kind="group", title="方案组", purpose="出方案", member_ids=[supervisor["id"], member["id"]]
    )
    upstream.chunk_delay = 0.2

    async def body() -> None:
        async with client_for(backend) as client:
            named = await start_run(client, group["id"], "@甲 出一版")
            assert named["current_agent_id"] == member["id"]
            await wait_for_run(client, named["id"])
            # Nobody is named now: the member who just spoke gets it, not the supervisor.
            plain = await start_run(client, group["id"], "大家怎么看")
            assert plain["current_agent_id"] == member["id"]
            await wait_for_run(client, plain["id"])

    asyncio.run(body())
