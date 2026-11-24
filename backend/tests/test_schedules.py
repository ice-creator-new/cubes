"""Scheduled tasks: rule maths, HTTP CRUD, firing, queueing, recovery and tools."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import schedules
from app.config import Settings
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    client_for,
    collect_events,
    get_conversation,
    get_run,
    get_workspace,
    payloads,
    runtime_of,
    start_run,
    store_of,
    system_prompt,
    tool_names,
    wait_for_message,
    wait_for_run,
)
from test_computer import FakeAgent, wait_connected


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return schedules.utc_iso(moment)


async def get_schedules(client) -> dict:
    response = await client.get("/schedules")
    assert response.status_code == 200, response.text
    return response.json()


def schedule_named(state: dict, title: str) -> dict:
    return next(item for item in state["schedules"] if item["title"] == title)


async def create_schedule(client, **overrides) -> dict:
    body = {
        "title": "每天看一眼",
        "prompt": "帮我看一眼今天的新闻",
        "conversation_id": (await get_workspace(client))["direct_conversation_id"],
        "kind": "interval",
        "every_minutes": 5,
    }
    body.update(overrides)
    response = await client.post("/schedules", json=body)
    return response


async def make_due(backend, schedule_id: str, *, seconds: int = 2) -> dict:
    """Move a stored schedule's next fire into the past, as if the tick had just arrived."""
    schedule = store_of(backend).update_schedule(
        schedule_id, next_run_at=iso(utc_now() - timedelta(seconds=seconds))
    )
    assert schedule is not None
    return schedule


async def wait_for_schedule(client, title: str, predicate, *, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = schedule_named(await get_schedules(client), title)
        if predicate(item):
            return item
        await asyncio.sleep(0.05)
    raise AssertionError(f"schedule {title} never matched: {json.dumps(item, ensure_ascii=False)}")


async def wait_for_awaiting(client, conversation_id: str, *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for message in action_messages(await get_conversation(client, conversation_id)):
            if message["action"]["status"] == "awaiting":
                return message
        await asyncio.sleep(0.05)
    raise AssertionError("no awaiting action appeared")


async def confirm(client, action_id: str, approve: bool = True) -> dict:
    response = await client.post(f"/actions/{action_id}/confirm", json={"approve": approve})
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------- rule maths


def test_rules_are_validated_and_described() -> None:
    now = datetime(2026, 10, 23, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    once = schedules.normalize_rule(
        kind="once",
        at="2026-10-24T09:00",
        every_minutes=None,
        cron=None,
        timezone_name="Asia/Shanghai",
        now=now,
    )
    assert once["at"] == "2026-10-24T09:00"
    assert once["every_minutes"] is None and once["cron"] is None
    assert once["next_run_at"] == "2026-10-24T01:00:00+00:00"
    assert schedules.describe(**{k: once[k] for k in ("kind", "at", "every_minutes", "cron")}) == (
        "2026-10-24 09:00 一次"
    )

    interval = schedules.normalize_rule(
        kind="interval",
        at=None,
        every_minutes=30,
        cron=None,
        timezone_name="Asia/Shanghai",
        now=now,
    )
    assert interval["next_run_at"] == "2026-10-23T00:30:00+00:00"
    assert schedules.describe(
        kind="interval", at=None, every_minutes=30, cron=None
    ) == "每 30 分钟"
    assert schedules.describe(
        kind="interval", at=None, every_minutes=120, cron=None
    ) == "每 2 小时"

    daily = schedules.normalize_rule(
        kind="cron",
        at=None,
        every_minutes=None,
        cron="0 9 * * *",
        timezone_name="Asia/Shanghai",
        now=now,
    )
    assert daily["next_run_at"] == "2026-10-23T01:00:00+00:00"
    assert schedules.describe(
        kind="cron", at=None, every_minutes=None, cron="0 9 * * *"
    ) == "每天 09:00"
    assert schedules.describe(
        kind="cron", at=None, every_minutes=None, cron="0 9 * * 1-5"
    ) == "cron 0 9 * * 1-5"


def test_rules_refuse_what_the_contract_forbids() -> None:
    now = datetime(2026, 10, 23, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def rule(**overrides):
        body = {
            "kind": "interval",
            "at": None,
            "every_minutes": 5,
            "cron": None,
            "timezone_name": "Asia/Shanghai",
            "now": now,
        }
        body.update(overrides)
        return schedules.normalize_rule(**body)

    with pytest.raises(schedules.ScheduleError, match="5 分钟"):
        rule(every_minutes=4)
    with pytest.raises(schedules.ScheduleError, match="触发时间必须晚于现在"):
        rule(kind="once", every_minutes=None, at="2026-10-23T07:59")
    with pytest.raises(schedules.ScheduleError, match="格式"):
        rule(kind="once", every_minutes=None, at="2026/10/24 09:00")
    with pytest.raises(schedules.ScheduleError, match="5 段"):
        rule(kind="cron", every_minutes=None, cron="bad")
    with pytest.raises(schedules.ScheduleError, match="至少相隔 5 分钟"):
        rule(kind="cron", every_minutes=None, cron="* * * * *")
    with pytest.raises(schedules.ScheduleError, match="时区"):
        rule(timezone_name="Mars/Olympus")
    with pytest.raises(schedules.ScheduleError, match="触发方式"):
        rule(kind="monthly")


def test_the_timezone_decides_what_0900_means() -> None:
    # 00:30 UTC is 09:30 in Tokyo (already past 09:00) and 08:30 in Shanghai.
    now = datetime(2026, 10, 23, 0, 30, tzinfo=timezone.utc)
    tokyo = schedules.normalize_rule(
        kind="cron",
        at=None,
        every_minutes=None,
        cron="0 9 * * *",
        timezone_name="Asia/Tokyo",
        now=now,
    )
    assert tokyo["next_run_at"] == "2026-10-24T00:00:00+00:00"  # 09:00 in Tokyo, next day
    assert tokyo["timezone"] == "Asia/Tokyo"
    shanghai = schedules.normalize_rule(
        kind="cron",
        at=None,
        every_minutes=None,
        cron="0 9 * * *",
        timezone_name="Asia/Shanghai",
        now=now,
    )
    assert shanghai["next_run_at"] == "2026-10-23T01:00:00+00:00"  # 09:00 in Shanghai


def test_missed_fires_are_skipped_to_the_next_future_one() -> None:
    now = datetime(2026, 10, 23, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    rule = {"kind": "interval", "at": None, "every_minutes": 5, "cron": None, "timezone": "Asia/Shanghai"}
    assert schedules.next_future(rule, now) == now + timedelta(minutes=5)
    spent = {"kind": "once", "at": "2026-10-23T07:00", "every_minutes": None, "cron": None, "timezone": "Asia/Shanghai"}
    assert schedules.next_future(spent, now) is None


def test_an_invalid_timezone_stops_startup(monkeypatch) -> None:
    with pytest.raises(ValueError, match="TIMEZONE"):
        Settings(timezone="Mars/Olympus")
    assert Settings().timezone == "Asia/Shanghai"
    assert Settings().schedule_tick_seconds == 30

    from app.config import load_settings

    monkeypatch.setenv("SCHEDULE_TICK_SECONDS", "0.25")
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    loaded = load_settings()
    assert loaded.timezone == "Europe/Berlin"
    assert loaded.schedule_tick_seconds == 0.25


# ---------------------------------------------------------------------- HTTP CRUD


def test_schedule_crud_over_http(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            state = await get_schedules(client)
            assert state == {
                "schedules": [],
                "timezone": backend.settings.timezone,
                "limits": {"max_enabled": 20, "min_interval_minutes": 5},
            }

            created = await create_schedule(client, conversation_id=direct_id)
            assert created.status_code == 201, created.text
            schedule = created.json()
            assert schedule["kind"] == "interval"
            assert schedule["every_minutes"] == 5
            assert schedule["timezone"] == "Asia/Shanghai"
            assert schedule["enabled"] is True
            assert schedule["queued"] is False
            assert schedule["run_count"] == 0
            assert schedule["last_status"] is None
            assert schedule["created_by"] == "你"
            assert schedule["describe"] == "每 5 分钟"
            assert schedule["conversation_id"] == direct_id
            assert schedule["conversation_title"] == workspace["conversations"][0]["title"]
            assert schedules.parse_iso(schedule["next_run_at"]) > utc_now()

            renamed = await client.patch(
                f"/schedules/{schedule['id']}", json={"title": "改个名字"}
            )
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["title"] == "改个名字"
            paused = await client.patch(f"/schedules/{schedule['id']}", json={"enabled": False})
            assert paused.status_code == 200, paused.text
            assert paused.json()["enabled"] is False
            resumed = await client.patch(f"/schedules/{schedule['id']}", json={"enabled": True})
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["enabled"] is True
            assert schedules.parse_iso(resumed.json()["next_run_at"]) > utc_now()

            for broken in (
                {"kind": "monthly"},
                {"kind": "interval", "every_minutes": 4},
                {"kind": "cron", "cron": "* * * * *"},
                {"kind": "once", "at": "2020-01-01T09:00"},
                {"kind": "once"},
                {"timezone": "Nowhere/Land"},
            ):
                rejected = await client.patch(f"/schedules/{schedule['id']}", json=broken)
                assert rejected.status_code == 422, (broken, rejected.text)
                assert rejected.json()["detail"].endswith("。")

            bad = await create_schedule(client, kind="interval", every_minutes=4)
            assert bad.status_code == 422, bad.text
            assert "5 分钟" in bad.json()["detail"]
            missing = await create_schedule(client, conversation_id="c_missing")
            assert missing.status_code == 404, missing.text
            assert (await client.patch("/schedules/s_missing", json={"title": "x"})).status_code == 404
            assert (await client.post("/schedules/s_missing/run")).status_code == 404
            assert (await client.delete("/schedules/s_missing")).status_code == 404
            assert (await client.patch(f"/schedules/{schedule['id']}", json={})).status_code == 422

            started = await client.post(f"/schedules/{schedule['id']}/run")
            assert started.status_code == 202, started.text
            running = started.json()
            assert running["run_count"] == 1
            assert running["last_run_id"]
            finished = await wait_for_run(client, running["last_run_id"])
            assert finished["status"] == "completed", finished
            assert finished["schedule_id"] == schedule["id"]
            settled = await wait_for_schedule(
                client, "改个名字", lambda item: item["last_status"] == "completed"
            )
            assert settled["run_count"] == 1

            deleted = await client.delete(f"/schedules/{schedule['id']}")
            assert deleted.status_code == 204, deleted.text
            assert (await get_schedules(client))["schedules"] == []

    asyncio.run(body())


def test_a_private_conversation_cannot_be_a_target(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    member = store.create_agent(name="甲", instructions="出方案", kind="member")
    private = store.ensure_private_conversation(supervisor["id"], member["id"])

    async def body() -> None:
        async with client_for(backend) as client:
            response = await create_schedule(client, conversation_id=private["id"])
            assert response.status_code == 422, response.text
            assert response.json()["detail"].endswith("。")

    asyncio.run(body())


def test_at_most_twenty_schedules_may_be_enabled(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            for number in range(20):
                created = await create_schedule(client, title=f"任务 {number}")
                assert created.status_code == 201, created.text
            rejected = await create_schedule(client, title="第二十一个")
            assert rejected.status_code == 409, rejected.text
            assert "20" in rejected.json()["detail"]
            paused = await create_schedule(client, title="暂停的先建", enabled=False)
            assert paused.status_code == 201, paused.text
            # Re-enabling it meets the same cap.
            blocked = await client.patch(
                f"/schedules/{paused.json()['id']}", json={"enabled": True}
            )
            assert blocked.status_code == 409, blocked.text

    asyncio.run(body())


def test_deleting_the_conversation_deletes_its_schedules(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)
    supervisor = store.supervisor()
    group = store.create_conversation(
        kind="group", title="方案小组", purpose="把方案做出来", member_ids=[supervisor["id"]]
    )

    async def body() -> None:
        async with client_for(backend) as client:
            created = await create_schedule(client, conversation_id=group["id"], title="群里的")
            assert created.status_code == 201, created.text
            assert [item["title"] for item in (await get_schedules(client))["schedules"]] == [
                "群里的"
            ]
            assert (await client.delete(f"/conversations/{group['id']}")).status_code == 204
            assert (await get_schedules(client))["schedules"] == []

    asyncio.run(body())


# -------------------------------------------------------------------------- firing


def hidden_workspace(store, schedule_id: str) -> dict:
    """The schedule's hidden conversation, asserting it is linked and really hidden."""
    schedule = store.get_schedule(schedule_id)
    assert schedule is not None and schedule["workspace_conversation_id"]
    conversation = store.get_conversation(schedule["workspace_conversation_id"])
    assert conversation is not None
    assert conversation["kind"] == "background"
    return conversation


async def wait_for_hidden_streaming(
    store, conversation_id: str, *, timeout: float = 10.0
) -> None:
    """Wait until the hidden workspace already streams a reply, i.e. its model call ran."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(
            message["status"] == "streaming"
            for message in store.list_messages(conversation_id)
        ):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the hidden workspace never started streaming")


async def wait_for_hidden_run(store, schedule_id: str, *, timeout: float = 10.0) -> dict:
    """Wait until the schedule's hidden workspace is busy; return the schedule row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        schedule = store.get_schedule(schedule_id)
        if schedule is not None and schedule["workspace_conversation_id"]:
            if store.active_run(schedule["workspace_conversation_id"]) is not None:
                return schedule
        await asyncio.sleep(0.02)
    raise AssertionError("the hidden workspace never started a run")


def test_a_due_schedule_works_in_a_hidden_workspace_and_posts_one_summary(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.script = [
        {"content": "我先看看现在几点。", "tool_calls": [call("c1", "current_time", {})]},
        {"content": "今天的新闻是这些"},
    ]

    async def body() -> None:
        assert backend.app.state.scheduler.running  # the loop owns the app's lifetime
        async with client_for(backend) as client:
            store = store_of(backend)
            supervisor = store.supervisor()
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            created = (await create_schedule(client, conversation_id=direct_id)).json()
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(
                        name == "message_end"
                        and (payload.get("schedule") or {}).get("id") == created["id"]
                        for name, payload in events
                    ),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.2)
            await make_due(backend, created["id"])
            fired = await wait_for_schedule(
                client, created["title"], lambda item: item["run_count"] == 1
            )
            events = await reader

            # The target conversation gets exactly one message: the owner's summary.
            detail = await get_conversation(client, direct_id)
            [answer] = detail["messages"]
            assert answer["kind"] == "agent"
            assert answer["author_id"] == supervisor["id"]
            assert answer["content"] == "今天的新闻是这些"
            assert answer["schedule"] == {"id": created["id"], "title": created["title"]}
            # No run of its own either: the work happened somewhere else.
            assert detail["latest_run"] is None
            assert (await get_workspace(client))["active_runs"] == []

            # The hidden work published nothing: one whole message, no stream, no run.
            assert payloads(events, "delta") == []
            assert payloads(events, "run") == []
            shown = payloads(events, "message_start") + payloads(events, "message_end")
            assert {message["id"] for message in shown} == {answer["id"]}

            # The work really happened, in a conversation no client can reach.
            hidden = hidden_workspace(store, created["id"])
            assert hidden["title"] == f"定时任务「{created['title']}」"
            assert hidden["member_ids"] == [supervisor["id"]]
            worked = store.list_messages(hidden["id"])
            assert worked[0]["kind"] == "user" and worked[0]["content"] == created["prompt"]
            assert "action" in [message["kind"] for message in worked]
            assert worked[-1]["kind"] == "agent"
            assert worked[-1]["content"] == "今天的新闻是这些"
            run = store.get_run(fired["last_run_id"])
            assert run is not None and run["conversation_id"] == hidden["id"]

            # The model was told it is unattended, and where its summary goes.
            system = system_prompt(upstream.requests[-1])
            assert "后台工作区" in system
            assert "会话「主管」" in system
            users = [
                message["content"]
                for message in upstream.requests[-1]["messages"]
                if message["role"] == "user"
            ]
            assert any(
                created["prompt"] in text and "到点后自动发起" in text for text in users
            )

            settled = await wait_for_schedule(
                client, created["title"], lambda item: item["last_status"] is not None
            )
            assert settled["last_status"] == "completed"
            assert settled["last_error"] is None
            assert settled["queued"] is False

    asyncio.run(body())


def test_the_hidden_workspace_is_invisible_everywhere(make_backend, upstream) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.script = [{"content": "悄悄做完了"}]

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = (
                await create_schedule(client, conversation_id=direct_id, title="看不见的")
            ).json()
            await make_due(backend, created["id"])
            fired = await wait_for_schedule(
                client, "看不见的", lambda item: item["run_count"] == 1
            )
            await wait_for_run(client, fired["last_run_id"])
            hidden = hidden_workspace(store, created["id"])

            assert (await client.get(f"/conversations/{hidden['id']}")).status_code == 404
            assert (
                await client.post(
                    f"/conversations/{hidden['id']}/runs", json={"content": "在吗"}
                )
            ).status_code == 404
            assert (
                await client.patch(f"/conversations/{hidden['id']}", json={"title": "改名"})
            ).status_code == 404
            assert (
                await create_schedule(client, conversation_id=hidden["id"], title="后台当目标")
            ).status_code == 404

            listed = await get_workspace(client)
            assert hidden["id"] not in {item["id"] for item in listed["conversations"]}
            assert hidden["id"] not in {
                item["id"] for item in store.list_conversations()
            }
            sidebar = (await client.get("/sidebar")).json()
            assert hidden["id"] not in {item["conversation_id"] for item in sidebar["items"]}
            # The workspace is not searchable: its trigger only exists in there.
            assert store.search_messages(
                created["prompt"],
                agent_id=store.supervisor()["id"],
                is_supervisor=True,
            ) == []

            # Deleting the schedule takes the workspace and everything it held.
            assert (await client.delete(f"/schedules/{created['id']}")).status_code == 204
            assert store.get_conversation(hidden["id"]) is None

    asyncio.run(body())


def test_the_user_can_chat_while_a_scheduled_run_works(make_backend, upstream) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 4
    upstream.script = [
        {"slow": True},
        {"content": "先忙你的，这边也在做"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = (
                await create_schedule(client, conversation_id=direct_id, title="同时在做的")
            ).json()
            started = await client.post(f"/schedules/{created['id']}/run")
            assert started.status_code == 202, started.text
            await wait_for_hidden_run(store, created["id"])
            hidden = hidden_workspace(store, created["id"])
            await wait_for_hidden_streaming(store, hidden["id"])
            scheduled_run = store.active_run(hidden["id"])
            assert scheduled_run is not None

            # The user keeps talking in the target conversation while that is running.
            user_run = await start_run(client, direct_id, "先忙你的")
            assert (await wait_for_run(client, user_run["id"]))["status"] == "completed"
            # Neither run was cut off by the other.
            still_scheduled = store.get_run(scheduled_run["id"])
            assert still_scheduled["status"] in ("running", "completed"), still_scheduled
            assert still_scheduled["error"] is None

            done = await wait_for_schedule(
                client, "同时在做的", lambda item: item["last_status"] is not None, timeout=15
            )
            assert done["last_status"] == "completed"

            detail = await get_conversation(client, direct_id)
            user_messages = [m for m in detail["messages"] if m["kind"] == "user"]
            assert [m["content"] for m in user_messages] == ["先忙你的"]
            assert user_messages[0]["interrupts"] is False
            labelled = [m for m in detail["messages"] if m["schedule"]]
            assert len(labelled) == 1
            assert labelled[0]["content"] == "[1][2][3][4]"
            assert [m["content"] for m in detail["messages"]] == [
                "先忙你的",
                "先忙你的，这边也在做",
                "[1][2][3][4]",
            ]

    asyncio.run(body())


def test_a_second_fire_queues_behind_the_first_and_starts_fresh(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 4
    upstream.script = [
        {"slow": True},
        {"content": "第二次做完了"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = (
                await create_schedule(client, conversation_id=direct_id, title="排队的")
            ).json()
            first = (await client.post(f"/schedules/{created['id']}/run")).json()
            await wait_for_hidden_run(store, created["id"])
            second = (await client.post(f"/schedules/{created['id']}/run")).json()
            assert second["queued"] is True
            assert second["run_count"] == 1
            assert second["last_run_id"] == first["last_run_id"]

            done = await wait_for_schedule(
                client, "排队的", lambda item: item["run_count"] == 2, timeout=15
            )
            assert done["queued"] is False
            run = await wait_for_run(client, done["last_run_id"])
            assert run["status"] == "completed"
            assert run["schedule_id"] == created["id"]

            # The queued fire started from a blank workspace: no trace of the first one.
            hidden = hidden_workspace(store, created["id"])
            assert "[1][2][3][4]" not in [
                message["content"] for message in store.list_messages(hidden["id"])
            ]
            assert store.get_run(first["last_run_id"]) is None
            # One summary per finished fire, both labelled.
            detail = await get_conversation(client, direct_id)
            labelled = [m["content"] for m in detail["messages"] if m["schedule"]]
            assert labelled == ["[1][2][3][4]", "第二次做完了"]

    asyncio.run(body())


def test_a_working_schedule_shows_its_progress_and_its_run_can_be_read(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    store = store_of(backend)
    upstream.chunk_delay = 0.3
    upstream.chunk_count = 6
    upstream.script = [
        {"content": "先在沙盒里看看。", "tool_calls": [call("c1", "computer_shell", {"command": "uptime"})]},
        {"slow": True},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            # The sandbox answers slowly, so the run sits on that step long enough to be seen.
            agent = await FakeAgent(backend.url, store.computer_token(), delay=2.5).start(client)
            try:
                await watch(client)
            finally:
                await agent.stop()

    async def watch(client) -> None:
        direct_id = (await get_workspace(client))["direct_conversation_id"]
        created = (
            await create_schedule(
                client, conversation_id=direct_id, title="看得见进度", use_computer=True
            )
        ).json()
        assert created["running"] is None

        def tool_step_seen(events) -> bool:
            return any(
                item["running"] and "uptime" in item["running"]["step"]
                for state in payloads(events, "schedules")
                for item in state["schedules"]
            )

        reader = asyncio.create_task(
            collect_events(client, "/events", until=tool_step_seen, timeout=10)
        )
        await asyncio.sleep(0.2)
        await client.post(f"/schedules/{created['id']}/run")

        # While it works the row says so, with the step and the run it belongs to.
        working = await wait_for_schedule(
            client, "看得见进度", lambda item: item["running"] is not None
        )
        live = working["running"]
        assert live["run_id"] == working["last_run_id"]
        assert live["step"] and live["started_at"]
        events = await reader
        # Progress travels as the schedules state; the hidden run itself never does.
        rows = [
            item
            for state in payloads(events, "schedules")
            for item in state["schedules"]
            if item["id"] == created["id"] and item["running"]
        ]
        assert rows, "the panel never heard the run working"
        assert payloads(events, "run") == [] and payloads(events, "delta") == []
        # A tool call moves the step on, so the row says what the run is doing.
        assert any("uptime" in item["running"]["step"] for item in rows), [
            item["running"]["step"] for item in rows
        ]

        # The detail is read-only and holds the whole fire: prompt, steps, live answer.
        detail = (await client.get(f"/schedules/{created['id']}/run")).json()
        assert detail["run"]["id"] == live["run_id"]
        assert detail["run"]["status"] == "running"
        kinds = [message["kind"] for message in detail["messages"]]
        assert kinds[0] == "user" and detail["messages"][0]["content"] == created["prompt"]
        assert "action" in kinds

        # Stopping it from the panel ends it without a summary in the conversation.
        stopped = (await client.post(f"/schedules/{created['id']}/stop")).json()
        assert stopped["running"] is None
        settled = await wait_for_schedule(
            client, "看得见进度", lambda item: item["last_status"] is not None
        )
        assert settled["last_status"] == "stopped"
        after = (await client.get(f"/schedules/{created['id']}/run")).json()
        assert after["run"]["status"] == "stopped"
        assert (await get_conversation(client, direct_id))["messages"] == []

    asyncio.run(body())


def test_a_schedule_that_never_ran_has_no_run_to_show(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = (await create_schedule(client, title="还没跑过")).json()
            detail = (await client.get(f"/schedules/{created['id']}/run")).json()
            assert detail == {"schedule_id": created["id"], "run": None, "messages": []}
            assert (await client.get("/schedules/s_missing/run")).status_code == 404
            assert (await client.post("/schedules/s_missing/stop")).status_code == 404

    asyncio.run(body())


def test_pausing_a_queued_schedule_drops_its_pending_fire(make_backend, upstream) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 4
    upstream.script = [{"slow": True}]

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = (
                await create_schedule(client, conversation_id=direct_id, title="暂停的")
            ).json()
            await client.post(f"/schedules/{created['id']}/run")
            await wait_for_hidden_run(store, created["id"])
            queued = (await client.post(f"/schedules/{created['id']}/run")).json()
            assert queued["queued"] is True
            paused = await client.patch(
                f"/schedules/{created['id']}", json={"enabled": False}
            )
            assert paused.status_code == 200, paused.text
            assert paused.json()["queued"] is False

            await wait_for_schedule(
                client, "暂停的", lambda item: item["last_status"] is not None, timeout=15
            )
            await asyncio.sleep(0.5)
            after = schedule_named(await get_schedules(client), "暂停的")
            assert after["run_count"] == 1

    asyncio.run(body())
    assert upstream.request_count == 1


def test_a_queued_fire_starts_when_the_run_ends_not_on_the_next_tick(
    make_backend, upstream
) -> None:
    # A 30 s tick: only the run-finished hook can start the queued fire this quickly.
    backend = make_backend(schedule_tick_seconds=30)
    upstream.chunk_delay = 0.2
    upstream.chunk_count = 4
    upstream.script = [
        {"slow": True},
        {"content": "排队后的回复"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = (
                await create_schedule(
                    client, conversation_id=direct_id, title="等着的", every_minutes=30
                )
            ).json()
            await client.post(f"/schedules/{created['id']}/run")
            await wait_for_hidden_run(store, created["id"])
            # Exactly the state the scheduler leaves behind a busy workspace.
            await client.post(f"/schedules/{created['id']}/run")
            started = await wait_for_schedule(
                client, "等着的", lambda item: item["run_count"] == 2, timeout=8
            )
            second = await wait_for_run(client, started["last_run_id"], timeout=15)
            assert second["status"] == "completed", second

    asyncio.run(body())


def test_missed_work_never_runs_on_startup(tmp_path, make_backend) -> None:
    from app.store import Store

    path = tmp_path / "cubes.sqlite3"
    offline = Store(path)
    offline.initialize()
    direct = offline.direct_conversation()
    assert direct is not None
    interval = offline.create_schedule(
        title="错过的间隔",
        prompt="这条不该跑",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="interval",
        at=None,
        every_minutes=30,
        cron=None,
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=iso(utc_now() - timedelta(hours=3)),
        created_by="你",
    )
    once = offline.create_schedule(
        title="错过的一次",
        prompt="这条也不该跑",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="once",
        at="2026-10-01T09:00",
        every_minutes=None,
        cron=None,
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=iso(utc_now() - timedelta(hours=3)),
        created_by="你",
    )
    offline.close()

    # The backend starts against that file: nothing due may run just because time passed.
    backend = make_backend(database_path=path, schedule_tick_seconds=0.05)

    async def body() -> None:
        async with client_for(backend) as client:
            await asyncio.sleep(0.3)
            state = await get_schedules(client)
            recovered = schedule_named(state, "错过的间隔")
            assert recovered["enabled"] is True
            assert recovered["run_count"] == 0
            assert schedules.parse_iso(recovered["next_run_at"]) > utc_now()
            missed = schedule_named(state, "错过的一次")
            assert missed["enabled"] is False
            assert missed["last_status"] == "missed"
            assert missed["next_run_at"] is None
            detail = await get_conversation(client, direct["id"])
            assert detail["messages"] == []
            assert detail["latest_run"] is None

    asyncio.run(body())
    store = store_of(backend)
    assert store.get_schedule(interval["id"]) is not None
    assert store.get_schedule(once["id"]) is not None
    assert store.get_schedule(interval["id"])["workspace_conversation_id"] is None


def test_a_once_fires_and_then_disables_itself(make_backend, upstream) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.script = [{"content": "一次性的活干完了"}]
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    store.create_schedule(
        title="只跑一次",
        prompt="跑一次就好",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="once",
        at="2026-10-23T09:00",
        every_minutes=None,
        cron=None,
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=iso(utc_now() - timedelta(seconds=2)),
        created_by="你",
    )

    async def body() -> None:
        async with client_for(backend) as client:
            fired = await wait_for_schedule(
                client, "只跑一次", lambda item: item["run_count"] == 1
            )
            assert fired["enabled"] is False
            assert fired["next_run_at"] is None
            run = await wait_for_run(client, fired["last_run_id"])
            assert run["status"] == "completed", run
            done = await wait_for_schedule(
                client, "只跑一次", lambda item: item["last_status"] is not None
            )
            assert done["last_status"] == "completed"
            # One more tick changes nothing.
            await asyncio.sleep(0.3)
            still = schedule_named(await get_schedules(client), "只跑一次")
            assert still["run_count"] == 1

    asyncio.run(body())


def test_layout_events_carry_the_schedule_list(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(name == "schedules" for name, _ in events),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.2)
            created = await create_schedule(client, title="推送我")
            assert created.status_code == 201, created.text
            events = await reader
            pushed = payloads(events, "schedules")
            assert pushed
            assert pushed[-1]["schedules"][0]["title"] == "推送我"
            assert pushed[-1]["timezone"] == backend.settings.timezone

    asyncio.run(body())


# ------------------------------------------------------------------ computer use


def test_a_scheduled_run_may_use_the_computer_without_a_consent_card(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            state = await wait_connected(client, False)
            assert state["connected"] is False
            agent = FakeAgent(backend.url, store.computer_token())
            await agent.start(client)
            try:
                workspace = await get_workspace(client)
                direct_id = workspace["direct_conversation_id"]
                created = (
                    await create_schedule(
                        client,
                        conversation_id=direct_id,
                        title="用电脑的",
                        prompt="在沙盒里看一下磁盘",
                        use_computer=True,
                    )
                ).json()
                upstream.script = [
                    tool_calls(
                        call("call_shell", "computer_shell", {"command": "df -h"})
                    ),
                    {"content": "看完了，磁盘很空"},
                ]
                await make_due(backend, created["id"])
                fired = await wait_for_schedule(
                    client, "用电脑的", lambda item: item["run_count"] == 1
                )
                run = await wait_for_run(client, fired["last_run_id"], timeout=20)
                assert run["status"] == "completed", run
                assert [op for op, _ in agent.calls] == ["shell"]
                # The step ran with no card, out of sight; the target gets the summary only.
                detail = await get_conversation(client, direct_id)
                assert action_messages(detail) == []
                assert [message["content"] for message in detail["messages"]] == [
                    "看完了，磁盘很空"
                ]
                hidden = hidden_workspace(store, created["id"])
                kinds = [message["kind"] for message in store.list_messages(hidden["id"])]
                assert kinds[0] == "user", kinds
                assert kinds[-1] == "agent", kinds
                assert "action" in kinds, kinds
            finally:
                await agent.stop()

    asyncio.run(body())


def test_a_scheduled_run_asks_for_the_sandbox_in_its_target_conversation(
    make_backend, upstream
) -> None:
    # `use_computer` was not pre-approved: the schedule's owner asks where the user reads its
    # results, the run waits for the answer, and a yes holds for later fires too.
    backend = make_backend(schedule_tick_seconds=0.05)
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            agent = FakeAgent(backend.url, store.computer_token())
            await agent.start(client)
            try:
                direct_id = (await get_workspace(client))["direct_conversation_id"]
                created = (
                    await create_schedule(client, conversation_id=direct_id, title="要问的")
                ).json()
                assert created["use_computer"] is False
                upstream.script = [
                    tool_calls(call("call_shell", "computer_shell", {"command": "df -h"})),
                    {"content": "看完了，磁盘很空"},
                ]
                await make_due(backend, created["id"])

                asking = await wait_for_awaiting(client, direct_id)
                # The schedule's owner asks: one the user made belongs to the supervisor.
                assert asking["author_name"] == "主管"
                confirmation = asking["action"]["confirmation"]
                assert confirmation["confirm_label"] == "允许"
                assert "要问的" in confirmation["prompt"]
                waiting = await wait_for_schedule(
                    client, "要问的", lambda item: item["running"] is not None
                )
                assert "允许使用沙盒电脑" in waiting["running"]["step"]
                assert agent.calls == []

                answered = await confirm(client, asking["action"]["id"], True)
                assert answered["status"] == "completed"
                done = await wait_for_schedule(
                    client, "要问的", lambda item: item["last_status"] is not None, timeout=15
                )
                assert done["last_status"] == "completed"
                assert done["use_computer"] is True
                assert [op for op, _ in agent.calls] == ["shell"]
                # The card stays as a settled row; the summary follows it.
                detail = await get_conversation(client, direct_id)
                rows = [
                    (message["kind"], message["action"]["status"] if message["action"] else None)
                    for message in detail["messages"]
                ]
                assert rows == [("action", "completed"), ("agent", None)]
                assert detail["messages"][-1]["content"] == "看完了，磁盘很空"
            finally:
                await agent.stop()

    asyncio.run(body())


def test_a_refused_sandbox_request_lets_the_scheduled_run_go_on_without_it(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            agent = FakeAgent(backend.url, store.computer_token())
            await agent.start(client)
            try:
                direct_id = (await get_workspace(client))["direct_conversation_id"]
                created = (
                    await create_schedule(client, conversation_id=direct_id, title="被拒的")
                ).json()
                upstream.script = [
                    tool_calls(call("call_shell", "computer_shell", {"command": "df -h"})),
                    {"content": "那就先不用电脑，我直接说结论"},
                ]
                await make_due(backend, created["id"])
                asking = await wait_for_awaiting(client, direct_id)
                await confirm(client, asking["action"]["id"], False)
                done = await wait_for_schedule(
                    client, "被拒的", lambda item: item["last_status"] is not None, timeout=15
                )
                assert done["last_status"] == "completed"
                assert done["use_computer"] is False
                assert agent.calls == []
                results = [
                    message["content"]
                    for message in upstream.requests[1]["messages"]
                    if message.get("role") == "tool"
                ]
                assert results and '"denied": true' in results[0]
                detail = await get_conversation(client, direct_id)
                assert [message["content"] for message in detail["messages"]] == [
                    "你拒绝了定时任务「被拒的」使用沙盒电脑",
                    "那就先不用电脑，我直接说结论",
                ]
            finally:
                await agent.stop()

    asyncio.run(body())


def test_stopping_a_schedule_waiting_for_the_sandbox_settles_its_card(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            agent = FakeAgent(backend.url, store.computer_token())
            await agent.start(client)
            try:
                direct_id = (await get_workspace(client))["direct_conversation_id"]
                created = (
                    await create_schedule(client, conversation_id=direct_id, title="等着的")
                ).json()
                upstream.script = [
                    tool_calls(call("call_shell", "computer_shell", {"command": "df -h"})),
                ]
                await make_due(backend, created["id"])
                asking = await wait_for_awaiting(client, direct_id)
                stopped = await client.post(f"/schedules/{created['id']}/stop")
                assert stopped.status_code == 200, stopped.text
                done = await wait_for_schedule(
                    client, "等着的", lambda item: item["last_status"] is not None
                )
                assert done["last_status"] == "stopped"
                card = store.get_action(asking["action"]["id"])
                assert card["status"] == "completed" and card["confirmation"] is None
                late = await client.post(
                    f"/actions/{asking['action']['id']}/confirm", json={"approve": True}
                )
                assert late.status_code == 409
                assert agent.calls == []
                assert (await get_schedules(client))["schedules"][0]["use_computer"] is False
            finally:
                await agent.stop()

    asyncio.run(body())


def test_a_stopped_scheduled_run_says_nothing(make_backend, upstream) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.chunk_delay = 0.3

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = (
                await create_schedule(client, conversation_id=direct_id, title="被停掉的")
            ).json()
            await client.post(f"/schedules/{created['id']}/run")
            await wait_for_hidden_run(store, created["id"])
            hidden = hidden_workspace(store, created["id"])
            await wait_for_hidden_streaming(store, hidden["id"])
            running = store.active_run(hidden["id"])
            assert running is not None
            stopped = await client.post(f"/runs/{running['id']}/stop")
            assert stopped.status_code == 200, stopped.text
            settled = await wait_for_schedule(
                client, "被停掉的", lambda item: item["last_status"] is not None
            )
            assert settled["last_status"] == "stopped"
            # A stopped run leaves the user with nothing at all.
            detail = await get_conversation(client, direct_id)
            assert detail["messages"] == []

    asyncio.run(body())


def test_a_failed_scheduled_run_leaves_one_note_from_its_owner(
    make_backend, upstream
) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    upstream.script = [{"content": "做到一半", "finish_reason": "length"}]

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            supervisor = store.supervisor()
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = (await create_schedule(client, conversation_id=direct_id)).json()
            await make_due(backend, created["id"])
            settled = await wait_for_schedule(
                client, created["title"], lambda item: item["last_status"] is not None
            )
            assert settled["last_status"] == "error"
            detail = await get_conversation(client, direct_id)
            [note] = detail["messages"]
            assert note["kind"] == "agent"
            assert note["author_id"] == supervisor["id"]
            assert note["schedule"] == {"id": created["id"], "title": created["title"]}
            assert note["content"].startswith(
                f"定时任务「{created['title']}」这次没有做完："
            )
            assert settled["last_error"] in note["content"]
            assert note["mood"] == "sorry"

    asyncio.run(body())

# ------------------------------------------------------------------- model tools


def test_schedule_tools_are_not_offered_inside_a_scheduled_run(make_backend, upstream) -> None:
    backend = make_backend(schedule_tick_seconds=0.05)
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    schedule = store.create_schedule(
        title="定时里不能建定时",
        prompt="试试管理定时任务",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="interval",
        at=None,
        every_minutes=30,
        cron=None,
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=iso(utc_now() - timedelta(seconds=2)),
        created_by="主管",
    )
    upstream.script = [
        tool_calls(
            call(
                "call_sched",
                "schedule_create",
                {
                    "title": "嵌套的",
                    "prompt": "不该建",
                    "kind": "interval",
                    "every_minutes": 30,
                },
            )
        ),
        {"content": "没法建，就这样"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            fired = await wait_for_schedule(
                client, "定时里不能建定时", lambda item: item["run_count"] == 1
            )
            run = await wait_for_run(client, fired["last_run_id"])
            assert run["status"] == "completed", run

    asyncio.run(body())

    offered = [tool_names(request) for request in upstream.requests]
    assert offered and all("schedule_create" not in names for names in offered)
    assert "schedule_list" not in offered[0]
    assert "create_agent" in offered[0]  # everything else stays available
    assert len(upstream.requests) == 2
    refused = [
        message["content"]
        for message in upstream.requests[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert refused and "不能再管理定时任务" in refused[0]
    assert schedule["title"] in refused[0] or "ok" in refused[0]
    assert store.get_schedule(schedule["id"]) is not None
    assert [item["title"] for item in store.list_schedules()] == ["定时里不能建定时"]


def test_schedule_create_tool_waits_for_approval(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            upstream.script = [
                tool_calls(
                    call(
                        "call_sched",
                        "schedule_create",
                        {
                            "title": "每天早上看新闻",
                            "prompt": "整理今天的新闻",
                            "kind": "cron",
                            "cron": "0 9 * * *",
                        },
                    )
                ),
                {"content": "已经安排好了"},
            ]
            run = await start_run(client, direct_id, "每天早上帮我看新闻")
            waiting = await wait_for_awaiting(client, direct_id)
            action = waiting["action"]
            assert action["status"] == "awaiting"
            confirmation = action["confirmation"]
            assert confirmation["confirm_label"] == "创建"
            assert confirmation["deny_label"] == "取消"
            prompt = confirmation["prompt"]
            assert "每天 09:00" in prompt
            assert "Asia/Shanghai" not in prompt  # the next fire is shown in local time
            assert "下次" in prompt
            assert "会话" in prompt
            assert "整理今天的新闻" in prompt
            assert "不会使用沙盒电脑" in prompt
            # Nothing is stored until the user says yes.
            assert (await get_schedules(client))["schedules"] == []

            await confirm(client, action["id"], True)
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            state = await get_schedules(client)
            assert [item["title"] for item in state["schedules"]] == ["每天早上看新闻"]
            created = state["schedules"][0]
            assert created["created_by"] == "主管"
            assert created["describe"] == "每天 09:00"
            assert created["conversation_id"] == direct_id
            assert created["enabled"] is True

        results = [
            message["content"]
            for message in upstream.requests[1]["messages"]
            if message.get("role") == "tool"
        ]
        assert results and "已建立定时任务「每天早上看新闻」" in results[0]
        assert created["id"] in results[0]

    asyncio.run(body())


def test_schedule_create_tool_respects_a_refusal(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            upstream.script = [
                tool_calls(
                    call(
                        "call_sched",
                        "schedule_create",
                        {
                            "title": "不要的",
                            "prompt": "别建",
                            "kind": "interval",
                            "every_minutes": 30,
                        },
                    )
                ),
                {"content": "好的，那就不建"},
            ]
            run = await start_run(client, direct_id, "建个定时任务")
            waiting = await wait_for_awaiting(client, direct_id)
            await confirm(client, waiting["action"]["id"], False)
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert (await get_schedules(client))["schedules"] == []

        results = [
            message["content"]
            for message in upstream.requests[1]["messages"]
            if message.get("role") == "tool"
        ]
        assert results and "取消了" in results[0]

    asyncio.run(body())


def test_schedule_list_and_delete_tools(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    seeded = store.create_schedule(
        title="每周整理",
        prompt="整理一下这周的事",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="cron",
        at=None,
        every_minutes=None,
        cron="0 9 * * 1",
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=iso(utc_now() + timedelta(days=3)),
        created_by="主管",
    )

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            direct_id = workspace["direct_conversation_id"]
            upstream.script = [
                tool_calls(call("call_list", "schedule_list", {})),
                tool_calls(
                    call(
                        "call_delete",
                        "schedule_delete",
                        {"schedule_id": seeded["id"], "reason": "不需要了"},
                    )
                ),
                {"content": "删掉了"},
            ]
            run = await start_run(client, direct_id, "查一下定时任务")
            waiting = await wait_for_awaiting(client, direct_id)
            confirmation = waiting["action"]["confirmation"]
            assert confirmation["confirm_label"] == "删除"
            assert "每周整理" in confirmation["prompt"]
            assert "不需要了" in confirmation["prompt"]
            await confirm(client, waiting["action"]["id"], True)
            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished
            assert (await get_schedules(client))["schedules"] == []

        list_result = [
            message["content"]
            for message in upstream.requests[1]["messages"]
            if message.get("role") == "tool"
        ][0]
        assert "每周整理" in list_result
        assert seeded["id"] in list_result
        assert "下次" in list_result
        delete_result = [
            message["content"]
            for message in upstream.requests[2]["messages"]
            if message.get("role") == "tool"
        ]
        assert "已删除定时任务「每周整理」" in delete_result[-1]

    asyncio.run(body())


def test_the_supervisor_prompt_explains_schedules_and_the_current_time(
    make_backend, upstream
) -> None:
    backend = make_backend()
    upstream.script = [{"content": "收到"}]

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            run = await start_run(
                client, workspace["direct_conversation_id"], "说句话"
            )
            await wait_for_run(client, run["id"])

    asyncio.run(body())

    prompt = system_prompt(upstream.requests[0])
    assert "定时任务" in prompt
    assert "schedule_create" in prompt
    # The clock is no longer in the system prompt: it rides in the newest user turn, so the
    # whole prefix before it stays identical between turns and can be cached.
    assert "现在的本地时间" not in prompt
    runtime = runtime_of(upstream.requests[0])
    assert runtime.startswith("<runtime time=")
    assert "Asia/Shanghai" in runtime
    assert "2026-" in runtime or "2027-" in runtime


# ------------------------------------------------------------------- real format


def test_the_run_payload_exposes_its_schedule(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    upstream.script = [{"content": "开始干活"}]

    async def body() -> None:
        async with client_for(backend) as client:
            run = await start_run(client, direct["id"], "普通的一条")
            await wait_for_run(client, run["id"])
            detail = await get_conversation(client, direct["id"])
            assert detail["messages"][0]["schedule"] is None
            assert (await get_run(client, run["id"]))["schedule_id"] is None

    asyncio.run(body())


def test_a_pending_fire_is_coalesced_and_starts_exactly_once(make_backend) -> None:
    from app.scheduler import Scheduler

    backend = make_backend()
    store = store_of(backend)
    direct = store.direct_conversation()
    assert direct is not None
    schedule = store.create_schedule(
        title="只排一次",
        prompt="排队测试",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="interval",
        at=None,
        every_minutes=5,
        cron=None,
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=iso(utc_now() - timedelta(minutes=1)),
        created_by="你",
    )
    attempts: list[str] = []
    state = {"busy": True}

    async def fire(row: dict, text: str = "") -> dict | None:
        attempts.append(row["id"])
        if state["busy"]:
            return None
        return {"id": "r_fake"}

    scheduler = Scheduler(
        store,
        tick_seconds=60,
        default_timezone="Asia/Shanghai",
        fire=fire,
        publish=lambda event, payload: None,
    )

    async def body() -> None:
        await scheduler.tick()  # due, but the conversation is busy -> one pending fire
        queued = store.get_schedule(schedule["id"])
        assert queued is not None and queued["queued"] is True
        first_next = queued["next_run_at"]
        # A second tick does not create a second pending fire; it only advances the rule.
        await scheduler.tick()
        again = store.get_schedule(schedule["id"])
        assert again is not None and again["queued"] is True
        assert again["run_count"] == 0
        # The rule advances from "now", which is the same second, so it never regresses.
        assert schedules.parse_iso(again["next_run_at"]) >= schedules.parse_iso(first_next)
        assert schedules.parse_iso(again["next_run_at"]) > utc_now()
        assert len(attempts) == 2

        state["busy"] = False
        await scheduler.tick()
        started = store.get_schedule(schedule["id"])
        assert started is not None
        assert started["queued"] is False
        assert started["run_count"] == 1
        assert started["last_run_id"] == "r_fake"
        # Nothing is pending any more, so a further tick starts nothing.
        await scheduler.tick()
        assert store.get_schedule(schedule["id"])["run_count"] == 1

    asyncio.run(body())


# -------------------------------------------------------------------- migration


def test_the_v10_migration_snapshots_then_drops_the_hidden_column(tmp_path) -> None:
    from app.store import SCHEMA_VERSION, Store

    path = tmp_path / "cubes.sqlite3"
    offline = Store(path)
    offline.initialize()
    direct = offline.direct_conversation()
    assert direct is not None
    kept = offline.append_message(
        conversation_id=direct["id"],
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="留下的一条",
        status="complete",
    )
    internal = offline.append_message(
        conversation_id=direct["id"],
        author_id=None,
        author_name="你",
        avatar_seed=None,
        kind="user",
        content="定时任务的中间步骤",
        status="complete",
    )
    offline.close()

    # Rebuild the shape a schema-9 database has: a hidden column with hidden rows.
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE messages SET hidden = 1 WHERE id = ?", (internal["id"],))
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    migrated = Store(path)
    migrated.initialize()
    backups = sorted(tmp_path.glob("cubes.sqlite3.pre-v10-*.sqlite3"))
    assert len(backups) == 1, backups
    # The snapshot still holds what the migration is about to delete.
    snapshot = sqlite3.connect(str(backups[0]))
    try:
        columns = {row[1] for row in snapshot.execute("PRAGMA table_info(messages)")}
        assert "hidden" in columns
        assert snapshot.execute(
            "SELECT COUNT(*) FROM messages WHERE hidden = 1"
        ).fetchone()[0] == 1
    finally:
        snapshot.close()

    assert [message["content"] for message in migrated.list_messages(direct["id"])] == [
        "留下的一条"
    ]
    assert migrated.get_message(kept["id"]) is not None
    assert migrated.get_message(internal["id"]) is None
    migrated.close()

    conn = sqlite3.connect(str(path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        assert "hidden" not in columns
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
    finally:
        conn.close()

    # A second start is a no-op: one snapshot per database, never one per boot.
    again = Store(path)
    again.initialize()
    again.close()
    assert len(sorted(tmp_path.glob("cubes.sqlite3.pre-v10-*.sqlite3"))) == 1


def test_a_startup_sweep_removes_a_workspace_whose_schedule_is_gone(tmp_path) -> None:
    # A conversation deleted with its schedules cascades the rule but not the workspace:
    # the next start sweeps what no schedule points at any more.
    from app.store import Store

    path = tmp_path / "cubes.sqlite3"
    store = Store(path)
    store.initialize()
    direct = store.direct_conversation()
    supervisor = store.supervisor()
    assert direct is not None and supervisor is not None
    schedule = store.create_schedule(
        title="会被扫掉的",
        prompt="做点事",
        conversation_id=direct["id"],
        conversation_title=direct["title"],
        kind="interval",
        at=None,
        every_minutes=30,
        cron=None,
        timezone_name="Asia/Shanghai",
        use_computer=False,
        next_run_at=None,
        created_by="你",
        owner_agent_id=supervisor["id"],
    )
    workspace = store.ensure_background_conversation(
        schedule, owner_id=supervisor["id"], purpose=direct["title"]
    )
    store.close()

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (schedule["id"],))
        conn.commit()
    finally:
        conn.close()

    swept = Store(path)
    swept.initialize()
    assert swept.get_conversation(workspace["id"]) is None
    swept.close()
