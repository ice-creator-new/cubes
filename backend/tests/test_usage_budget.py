"""Per-agent token usage and the per-agent token budget gate (real HTTP)."""

from __future__ import annotations

import asyncio
import calendar
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import usage as usage_rules
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    get_conversation,
    get_workspace,
    run_goal,
    start_run,
    store_of,
    wait_for_run,
)


def build_team(backend) -> dict:
    """Supervisor plus two members in one group, seeded straight into the store."""
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    designer = store.create_agent(name="甲", instructions="出方案", kind="member")
    reviewer = store.create_agent(name="乙", instructions="评估风险", kind="member")
    group = store.create_conversation(
        kind="group",
        title="方案小组",
        purpose="把方案做出来",
        member_ids=[supervisor["id"], designer["id"], reviewer["id"]],
    )
    return {
        "supervisor": supervisor,
        "designer": designer,
        "reviewer": reviewer,
        "group": group,
    }


async def wait_for_awaiting(client, conversation_id: str, *, timeout: float = 15.0) -> dict:
    """The first action message the engine put into the awaiting status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await get_conversation(client, conversation_id)
        for message in action_messages(detail):
            if message["action"]["status"] == "awaiting":
                return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no awaiting action appeared in {conversation_id}")


async def task_thread_id(client, group_id: str, *, timeout: float = 15.0) -> str:
    """The thread of the group's first task: where a member's own turn (and its cards) live."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = (await client.get(f"/conversations/{group_id}/tasks")).json()["tasks"]
        for task in tasks:
            if task["thread_id"]:
                return str(task["thread_id"])
        await asyncio.sleep(0.05)
    raise AssertionError(f"no task thread appeared in {group_id}")


async def confirm(client, action_id: str, approve: bool = True) -> dict:
    response = await client.post(f"/actions/{action_id}/confirm", json={"approve": approve})
    assert response.status_code == 200, response.text
    return response.json()


def usage_by_agent(data: dict) -> dict:
    return {entry["agent_id"]: entry for entry in data["agents"]}


# ------------------------------------------------------------------ attribution


def test_usage_attributed_to_each_agent_in_a_group_relay(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    upstream.script = [
        {
            "content": "方案在这里，@乙 请评估风险",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        },
        {
            "content": "风险可控",
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 60},
            },
        },
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await run_goal(client, team["group"]["id"], "@甲 先出方案")
            assert run["status"] == "completed", run

            response = await client.get("/usage")
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["period"] == "week"
            by_id = usage_by_agent(data)

            # The supervisor never spoke: zeros, no budget.
            supervisor = by_id[team["supervisor"]["id"]]
            assert supervisor["input_tokens"] == 0
            assert supervisor["output_tokens"] == 0
            assert supervisor["total_tokens"] == 0
            assert supervisor["model_calls"] == 0
            assert supervisor["budget_used"] is None

            designer = by_id[team["designer"]["id"]]
            assert designer["input_tokens"] == 100
            assert designer["output_tokens"] == 20
            assert designer["cached_tokens"] == 30
            assert designer["total_tokens"] == 120
            assert designer["model_calls"] == 1

            reviewer = by_id[team["reviewer"]["id"]]
            assert reviewer["input_tokens"] == 200
            assert reviewer["output_tokens"] == 50
            assert reviewer["cached_tokens"] == 60
            assert reviewer["total_tokens"] == 250
            assert reviewer["model_calls"] == 1

            # Supervisor first, then members in workspace order.
            assert data["agents"][0]["agent_id"] == team["supervisor"]["id"]
            assert [entry["agent_id"] for entry in data["agents"][1:]] == [
                team["designer"]["id"],
                team["reviewer"]["id"],
            ]

    asyncio.run(body())


# ------------------------------------------------------------------ periods / zeros


def test_usage_periods_zeros_and_validation(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="甲", instructions="做研究", kind="member")

    async def body() -> None:
        async with client_for(backend) as client:
            # Anything but day/week/month is rejected.
            for bad in ("year", "", "WEEK"):
                response = await client.get("/usage", params={"period": bad})
                assert response.status_code == 422, (bad, response.text)

            # Default period is week; an unused agent reads zeros and no mood.
            response = await client.get("/usage")
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["period"] == "week"
            by_id = usage_by_agent(data)
            entry = by_id[member["id"]]
            assert entry["input_tokens"] == 0
            assert entry["output_tokens"] == 0
            assert entry["cached_tokens"] == 0
            assert entry["total_tokens"] == 0
            assert entry["model_calls"] == 0
            assert entry["budget_used"] is None
            assert entry["last_reply_at"] is None
            assert entry["last_mood"] is None

            # ``since`` is the start of the requested period, in the workspace timezone.
            since = datetime.fromisoformat(data["since"])
            assert since.hour == 0 and since.minute == 0 and since.second == 0
            assert since.weekday() == 0  # Monday
            assert since.utcoffset().total_seconds() == 8 * 3600  # Asia/Shanghai

            day = (await client.get("/usage", params={"period": "day"})).json()
            day_since = datetime.fromisoformat(day["since"])
            assert day_since.hour == 0 and day_since.minute == 0

            month = (await client.get("/usage", params={"period": "month"})).json()
            month_since = datetime.fromisoformat(month["since"])
            assert month_since.day == 1

    asyncio.run(body())


def test_budget_used_counts_the_agents_own_period(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    member = store.create_agent(name="甲", instructions="做研究", kind="member")
    store.record_usage(
        agent_id=member["id"],
        conversation_id=None,
        run_id=None,
        purpose="reply",
        input_tokens=3000,
        output_tokens=1000,
        cached_tokens=500,
    )

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.patch(
                f"/agents/{member['id']}",
                json={"token_budget": {"limit": 10000, "period": "week"}},
            )
            assert response.status_code == 200, response.text

            data = (await client.get("/usage")).json()
            by_id = usage_by_agent(data)
            entry = by_id[member["id"]]
            # total excludes cached; budget_used is input + output in the bot's own period.
            assert entry["input_tokens"] == 3000
            assert entry["output_tokens"] == 1000
            assert entry["cached_tokens"] == 500
            assert entry["total_tokens"] == 4000
            assert entry["model_calls"] == 1
            assert entry["budget_used"] == 4000
            # The supervisor has no budget.
            assert by_id[supervisor["id"]]["budget_used"] is None

    asyncio.run(body())


# ------------------------------------------------------------------ PATCH token_budget


def test_patch_token_budget_set_clear_and_invalid(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None

    async def body() -> None:
        async with client_for(backend) as client:
            # Set.
            response = await client.patch(
                f"/agents/{supervisor['id']}",
                json={"token_budget": {"limit": 10000, "period": "week"}},
            )
            assert response.status_code == 200, response.text
            assert response.json()["token_budget"] == {"limit": 10000, "period": "week"}

            workspace = await get_workspace(client)
            sup = next(a for a in workspace["agents"] if a["id"] == supervisor["id"])
            assert sup["token_budget"] == {"limit": 10000, "period": "week"}

            # Clear with an explicit null.
            response = await client.patch(
                f"/agents/{supervisor['id']}", json={"token_budget": None}
            )
            assert response.status_code == 200, response.text
            assert response.json()["token_budget"] is None

            # Out-of-range limit and an unknown period are both rejected.
            assert (
                await client.patch(
                    f"/agents/{supervisor['id']}",
                    json={"token_budget": {"limit": 1, "period": "week"}},
                )
            ).status_code == 422
            assert (
                await client.patch(
                    f"/agents/{supervisor['id']}",
                    json={"token_budget": {"limit": 10000, "period": "year"}},
                )
            ).status_code == 422

            # An empty patch still has nothing to update.
            assert (await client.patch(f"/agents/{supervisor['id']}", json={})).status_code == 422

    asyncio.run(body())


# ------------------------------------------------------------------ budget gate


def test_budget_gate_approve_proceeds_without_a_second_card(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    designer = team["designer"]
    # Pre-spend the designer past a small budget.
    store_of(backend).record_usage(
        agent_id=designer["id"],
        conversation_id=None,
        run_id=None,
        purpose="reply",
        input_tokens=5000,
        output_tokens=0,
        cached_tokens=0,
    )
    # A two-step reply (tool call then final text) means the designer's model is called
    # twice: the gate may prompt only once, before the first call.
    upstream.script = [
        tool_calls(call("call_1", "calculate", {"expression": "1+1"})),
        {"content": "结果是 2。", "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.patch(
                f"/agents/{designer['id']}",
                json={"token_budget": {"limit": 4000, "period": "week"}},
            )
            assert response.status_code == 200, response.text

            run = await start_run(client, team["group"]["id"], "@甲 帮我算 1+1")
            # The gate fires inside the member's task thread; the decisions panel still shows it.
            # No task thread here: the gate fires in the member's group turn.
            thread_id = team["group"]["id"]
            awaiting = await wait_for_awaiting(client, thread_id)

            action = awaiting["action"]
            assert action["tool_name"] == "token_budget"
            assert action["summary"] == "「甲」超出用量预算"
            assert action["confirmation"] == {
                "prompt": "「甲」本周已用 5000 token，超过了设定的 4000。继续这次回复吗？",
                "confirm_label": "继续",
                "deny_label": "停下",
            }

            decided = await confirm(client, action["id"], True)
            assert decided["status"] == "completed"
            assert decided["summary"] == "已同意「甲」超出预算继续"

            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert finished["model_calls"] == 2

            detail = await get_conversation(client, thread_id)
            budgets = [
                message
                for message in action_messages(detail)
                if message["action"]["tool_name"] == "token_budget"
            ]
            assert len(budgets) == 1

    asyncio.run(body())


def test_budget_gate_deny_ends_limited_without_a_reply(make_backend, upstream) -> None:
    backend = make_backend()
    team = build_team(backend)
    designer = team["designer"]
    store_of(backend).record_usage(
        agent_id=designer["id"],
        conversation_id=None,
        run_id=None,
        purpose="reply",
        input_tokens=5000,
        output_tokens=0,
        cached_tokens=0,
    )
    upstream.script = [{"content": "这是我的方案"}]

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.patch(
                f"/agents/{designer['id']}",
                json={"token_budget": {"limit": 4000, "period": "week"}},
            )
            assert response.status_code == 200, response.text

            run = await start_run(client, team["group"]["id"], "@甲 出个方案")
            # No task thread here: the gate fires in the member's group turn.
            thread_id = team["group"]["id"]
            awaiting = await wait_for_awaiting(client, thread_id)
            action = awaiting["action"]
            assert action["tool_name"] == "token_budget"

            await confirm(client, action["id"], False)

            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "limited", finished
            assert finished["error"] == "「甲」已超出本周用量预算"

    asyncio.run(body())

    # No reply model call was made: the gate stopped it before the request.
    assert upstream.request_count == 0


def test_scheduled_run_over_budget_ends_limited_without_a_card(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    store = store_of(backend)
    supervisor = store.supervisor()
    assert supervisor is not None
    store.record_usage(
        agent_id=supervisor["id"],
        conversation_id=None,
        run_id=None,
        purpose="reply",
        input_tokens=5000,
        output_tokens=0,
        cached_tokens=0,
    )
    upstream.script = [{"content": "总结好了"}]

    async def body() -> None:
        async with client_for(backend) as client:
            await client.patch(
                f"/agents/{supervisor['id']}",
                json={"token_budget": {"limit": 4000, "period": "week"}},
            )
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            created = (
                await client.post(
                    "/schedules",
                    json={
                        "title": "预算超了",
                        "prompt": "整理一下",
                        "conversation_id": direct_id,
                        "kind": "interval",
                        "every_minutes": 5,
                    },
                )
            ).json()
            await client.post(f"/schedules/{created['id']}/run")

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                state = (await client.get("/schedules")).json()
                item = next(s for s in state["schedules"] if s["id"] == created["id"])
                if item["last_status"] is not None:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("scheduled run never finished")

            assert item["last_status"] == "limited"

            # The hidden workspace parked no card the user could ever answer.
            schedule = store.get_schedule(created["id"])
            assert schedule is not None and schedule["workspace_conversation_id"]
            hidden = store.get_conversation(schedule["workspace_conversation_id"])
            assert hidden is not None
            assert not [
                message
                for message in store.list_messages(hidden["id"])
                if message["kind"] == "action"
            ]

    asyncio.run(body())


# ------------------------------------------------------------------ dashboard fields


def _set_usage_created_at(store, run_id: str, moment: datetime) -> None:
    """Pin one seeded ``model_usage`` row (tagged by ``run_id``) to a fixed instant."""
    conn = store._ensure()
    with store._lock:
        conn.execute(
            "UPDATE model_usage SET created_at = ? WHERE run_id = ?",
            (usage_rules.utc_key(moment), run_id),
        )


def test_usage_series_shape_and_gap_free(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    for period, expected in (("day", 24), ("week", 7)):
        report = store.usage_report(period=period, now=now, zone=zone)
        starts = [datetime.fromisoformat(bucket["start"]) for bucket in report["series"]]
        assert len(starts) == expected, period
        # Ordered and gap-free: every boundary is the previous plus exactly one bucket.
        assert starts == sorted(starts), period
        if period == "day":
            assert [start.hour for start in starts] == list(range(24)), period
        else:
            assert [start.weekday() for start in starts] == list(range(7)), period
        assert all(start.minute == 0 and start.second == 0 for start in starts), period

    month = store.usage_report(period="month", now=now, zone=zone)
    month_starts = [datetime.fromisoformat(bucket["start"]) for bucket in month["series"]]
    days = calendar.monthrange(2026, 10)[1]
    assert len(month_starts) == days
    assert [start.day for start in month_starts] == list(range(1, days + 1))
    assert all(start.hour == 0 and start.minute == 0 for start in month_starts)


def test_usage_series_seeded_row_lands_in_the_local_bucket(make_backend) -> None:
    backend = make_backend(timezone="America/New_York")
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="做研究", kind="member")
    store.record_usage(
        agent_id=member["id"],
        conversation_id=None,
        run_id="r_local",
        purpose="reply",
        input_tokens=10,
        output_tokens=5,
        cached_tokens=3,
    )

    async def body() -> None:
        async with client_for(backend) as client:
            # Anchor seeding to the route's own period start, then pin the row to 03:00 local.
            since = datetime.fromisoformat(
                (await client.get("/usage", params={"period": "day"})).json()["since"]
            )
            _set_usage_created_at(store, "r_local", since + timedelta(hours=3))

            data = (await client.get("/usage", params={"period": "day"})).json()
            series = data["series"]
            assert len(series) == 24
            # 03:00 local lives in the 4th hourly bucket; total excludes cached tokens.
            assert series[3]["total_tokens"] == 15
            assert series[3]["by_agent"] == {member["id"]: 15}
            assert sum(bucket["total_tokens"] for bucket in series) == 15

    asyncio.run(body())


def test_usage_totals_previous_and_by_purpose_cover_deleted_agents(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    member = store.create_agent(name="甲", instructions="做研究", kind="member")
    since, _ = usage_rules.period_bounds("week", now, zone)
    prev_start = usage_rules.previous_period_start("week", now, zone)
    prev_prev_start = usage_rules.previous_period_start("week", prev_start, zone)

    # Current week: one row per purpose.
    store.record_usage(
        agent_id=member["id"], conversation_id=None, run_id="r_cur_reply",
        purpose="reply", input_tokens=100, output_tokens=10, cached_tokens=5,
    )
    store.record_usage(
        agent_id=member["id"], conversation_id=None, run_id="r_cur_compact",
        purpose="compact", input_tokens=200, output_tokens=20, cached_tokens=7,
    )
    store.record_usage(
        agent_id=member["id"], conversation_id=None, run_id="r_cur_memory",
        purpose="memory-extract", input_tokens=300, output_tokens=30, cached_tokens=9,
    )
    # Last week and the week before: must land only in previous_total / nowhere.
    store.record_usage(
        agent_id=member["id"], conversation_id=None, run_id="r_prev",
        purpose="reply", input_tokens=50, output_tokens=5, cached_tokens=1,
    )
    store.record_usage(
        agent_id=member["id"], conversation_id=None, run_id="r_old",
        purpose="reply", input_tokens=999, output_tokens=1, cached_tokens=0,
    )
    _set_usage_created_at(store, "r_cur_reply", since + timedelta(hours=1))
    _set_usage_created_at(store, "r_cur_compact", since + timedelta(days=2))
    _set_usage_created_at(store, "r_cur_memory", since + timedelta(days=5))
    _set_usage_created_at(store, "r_prev", prev_start + timedelta(hours=2))
    _set_usage_created_at(store, "r_old", prev_prev_start + timedelta(hours=2))

    # The member goes away, but its ledger rows stay: totals must still see them.
    store.delete_agent(member["id"])

    report = store.usage_report(period="week", now=now, zone=zone)

    assert report["totals"] == {
        "input_tokens": 600,
        "output_tokens": 60,
        "cached_tokens": 21,
        "total_tokens": 660,
        "model_calls": 3,
    }
    assert report["previous_total"] == 55
    assert report["by_purpose"] == {
        "reply": 110,
        "compact": 220,
        "memory-extract": 330,
        "review": 0,
        "dispatch": 0,
        "reaction": 0,
    }
    # The deleted member no longer has an entry, yet its tokens still count above.
    assert member["id"] not in {entry["agent_id"] for entry in report["agents"]}


def test_bucket_starts_across_a_dst_change_returns_seven_local_midnights() -> None:
    zone = ZoneInfo("America/New_York")
    # 2026-11-01 is a Sunday; its week runs Monday 2026-10-26 .. Sunday 2026-11-01,
    # straddling the fall-back (2026-11-01 02:00 EDT -> EST).
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=zone)
    starts = usage_rules.bucket_starts("week", now, zone)

    assert len(starts) == 7
    monday = datetime(2026, 10, 26, 0, 0, 0, tzinfo=zone)
    assert [start.date() for start in starts] == [
        (monday + timedelta(days=i)).date() for i in range(7)
    ]
    assert all(start.hour == 0 and start.minute == 0 and start.second == 0 for start in starts)
    # Every boundary is a true local midnight: 24 real hours between consecutive midnights.
    assert all(
        (starts[i + 1] - starts[i]).total_seconds() == 24 * 3600 for i in range(6)
    )
