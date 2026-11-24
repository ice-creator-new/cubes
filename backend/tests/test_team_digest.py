"""Team digest: the hidden system schedule, its cron, run-now and the team_report facts.

The digest backing is a ``system`` schedule, hidden from GET /schedules and from the
MAX_ENABLED count, but still fired by the scheduler. Enabling the digest creates it (daily
or weekly cron), disabling disables it, and run-now fires a real run whose supervisor
offers ``team_report`` and posts the digest to the supervisor's direct chat.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    store_of,
    tool_names,
    tool_results,
    wait_for_run,
)


async def get_team_settings(client) -> dict:
    response = await client.get("/settings/team")
    assert response.status_code == 200, response.text
    return response.json()


async def patch_team_settings(client, body: dict) -> dict:
    response = await client.patch("/settings/team", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ schedule


def test_enabling_creates_a_hidden_system_schedule_with_the_right_cron(
    make_backend, upstream
) -> None:
    backend = make_backend()
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            defaults = await get_team_settings(client)
            assert defaults["digest"] == {
                "enabled": False,
                "frequency": "daily",
                "time": "09:00",
                "weekday": 0,
                "schedule_id": None,
            }

            # Enabling daily creates the hidden schedule with the daily cron and title.
            state = await patch_team_settings(client, {"digest": {"enabled": True}})
            schedule_id = state["digest"]["schedule_id"]
            assert schedule_id is not None

            schedule = store.digest_schedule()
            assert schedule is not None
            assert schedule["id"] == schedule_id
            assert schedule["cron"] == "0 9 * * *"
            assert schedule["title"] == "团队晨报"
            assert schedule["enabled"] is True
            assert schedule["conversation_id"] == store.direct_conversation()["id"]

            # It is hidden from the panel and does not count against MAX_ENABLED.
            listing = await client.get("/schedules")
            assert listing.status_code == 200, listing.text
            assert listing.json()["schedules"] == []
            assert store.enabled_schedule_count() == 0

            # Weekly carries the weekday into the cron and switches the title.
            state = await patch_team_settings(
                client, {"digest": {"frequency": "weekly", "weekday": 1}}
            )
            schedule = store.digest_schedule()
            assert schedule is not None
            assert schedule["cron"] == "0 9 * * 2"
            assert schedule["title"] == "团队周报"

    asyncio.run(body())


def test_disabling_disables_the_schedule(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            await patch_team_settings(client, {"digest": {"enabled": True}})
            assert store.digest_schedule()["enabled"] is True

            state = await patch_team_settings(client, {"digest": {"enabled": False}})
            assert state["digest"]["schedule_id"] is not None
            assert store.digest_schedule()["enabled"] is False

    asyncio.run(body())


# -------------------------------------------------------------------- run-now


def test_run_now_fires_and_delivers_to_the_supervisor_direct_chat(
    make_backend, upstream
) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    direct = store.direct_conversation()

    async def body() -> None:
        async with client_for(backend) as client:
            await patch_team_settings(client, {"digest": {"enabled": True}})

            upstream.script = [
                tool_calls(call("call_report", "team_report", {"period": "day"})),
                {"content": "今日摘要：做了三件事，没人卡住，建议明天继续。"},
            ]

            response = await client.post("/settings/team/digest/run")
            assert response.status_code == 202, response.text
            fired = response.json()
            run = await wait_for_run(client, fired["run_id"])
            assert run["status"] == "completed", run

    asyncio.run(body())

    # The supervisor's first request offers team_report (and no schedule tools).
    assert "team_report" in tool_names(upstream.request(0))
    assert "schedule_create" not in tool_names(upstream.request(0))
    assert len(upstream.requests) == 2

    # The team_report result carries the facts JSON shape.
    facts = dict(tool_results(upstream.request(1)))["call_report"]
    assert facts["ok"] is True
    assert facts["period"] == "day"
    assert "agents" in facts and "open_tasks" in facts and "failed_runs" in facts

    # The digest is posted to the supervisor's direct chat, by the supervisor.
    messages = store.list_messages(direct["id"])
    assert messages[-1]["kind"] == "agent"
    assert messages[-1]["author_id"] == supervisor["id"]
    assert messages[-1]["content"] == "今日摘要：做了三件事，没人卡住，建议明天继续。"
    assert messages[-1]["schedule"] == {
        "id": store.digest_schedule()["id"],
        "title": "团队晨报",
    }


# ------------------------------------------------------------------- facts


def test_team_report_facts_shape(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    store.create_agent(name="甲", instructions="出方案", kind="member")

    facts = store.team_report(
        period="week", now=datetime.now(timezone.utc), zone=ZoneInfo("Asia/Shanghai")
    )
    assert facts["period"] == "week"
    assert facts["open_tasks"] == []
    assert facts["failed_runs"] == []
    assert {agent["name"] for agent in facts["agents"]} == {"主管", "甲"}

    entry = next(agent for agent in facts["agents"] if agent["name"] == "甲")
    assert set(entry) == {
        "agent_id",
        "name",
        "kind",
        "replies",
        "runs",
        "runs_failed",
        "review_passed",
        "review_revised",
        "total_tokens",
        "model_calls",
        "tasks_done",
        "tasks_open",
        "tasks_blocked",
    }
    assert entry["replies"] == 0
    assert entry["runs"] == 0
    assert entry["tasks_open"] == 0
