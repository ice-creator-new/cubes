"""Team settings (§1): defaults, partial PATCH, validation and the SSE event."""

from __future__ import annotations

import asyncio

from helpers import client_for, collect_events, first_payload, payloads, store_of


def test_team_settings_defaults(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.get("/settings/team")
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["send_mode"] == "parallel"
            assert data["supervisor_works_in_groups"] is False
            assert data["review"] == {"enabled": False, "max_rounds": 2}
            assert data["digest"] == {
                "enabled": False,
                "frequency": "daily",
                "time": "09:00",
                "weekday": 0,
                "schedule_id": None,
            }
            # New workspaces start unlimited, with every preset's numbers resolved.
            assert data["step_budget"] == "unlimited"
            assert data["step_budgets"] == {
                "unlimited": {
                    "preset": "unlimited",
                    "unlimited": True,
                    "turn_model_calls": 0,
                    "turn_tool_calls": 0,
                    "run_model_calls": 0,
                    "run_tool_calls": 0,
                    "workflow_model_calls": 0,
                    "workflow_tool_calls": 0,
                    "computer_steps": 0,
                    "computer_shell_steps": 0,
                    "computer_browser_steps": 0,
                    "computer_screen_steps": 0,
                },
                "standard": {
                    "preset": "standard",
                    "unlimited": False,
                    "turn_model_calls": 24,
                    "turn_tool_calls": 32,
                    "run_model_calls": 64,
                    "run_tool_calls": 128,
                    "workflow_model_calls": 160,
                    "workflow_tool_calls": 256,
                    "computer_steps": 40,
                    "computer_shell_steps": 30,
                    "computer_browser_steps": 25,
                    "computer_screen_steps": 8,
                },
                "long": {
                    "preset": "long",
                    "unlimited": False,
                    "turn_model_calls": 64,
                    "turn_tool_calls": 96,
                    "run_model_calls": 192,
                    "run_tool_calls": 384,
                    "workflow_model_calls": 480,
                    "workflow_tool_calls": 768,
                    "computer_steps": 120,
                    "computer_shell_steps": 90,
                    "computer_browser_steps": 75,
                    "computer_screen_steps": 24,
                },
                "marathon": {
                    "preset": "marathon",
                    "unlimited": False,
                    "turn_model_calls": 160,
                    "turn_tool_calls": 256,
                    "run_model_calls": 400,
                    "run_tool_calls": 800,
                    "workflow_model_calls": 1000,
                    "workflow_tool_calls": 1600,
                    "computer_steps": 300,
                    "computer_shell_steps": 225,
                    "computer_browser_steps": 190,
                    "computer_screen_steps": 60,
                },
            }

    asyncio.run(body())


def test_team_settings_partial_patch_merges(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            # A nested partial: only one field of one slot.
            response = await client.patch(
                "/settings/team", json={"review": {"enabled": True}}
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["review"] == {"enabled": True, "max_rounds": 2}
            assert data["digest"]["enabled"] is False

            # Both slots at once, another nested partial.
            response = await client.patch(
                "/settings/team",
                json={
                    "review": {"max_rounds": 3},
                    "digest": {"frequency": "weekly", "time": "18:30", "weekday": 3},
                },
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["review"] == {"enabled": True, "max_rounds": 3}
            assert data["digest"]["frequency"] == "weekly"
            assert data["digest"]["time"] == "18:30"
            assert data["digest"]["weekday"] == 3

            # A later GET reads the same state from the store.
            again = (await client.get("/settings/team")).json()
            assert again == data

    asyncio.run(body())


def test_team_settings_rejects_bad_values(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            bad_bodies = [
                {"review": {"max_rounds": 0}},
                {"review": {"max_rounds": 4}},
                {"digest": {"frequency": "hourly"}},
                {"digest": {"time": "25:00"}},
                {"digest": {"time": "9:00"}},
                {"digest": {"time": "09:60"}},
                {"digest": {"weekday": 7}},
                {"digest": {"weekday": -1}},
                {"send_mode": "later"},
                {"step_budget": "endless"},
                {"bogus": True},
                {"review": {"bogus": True}},
            ]
            for bad in bad_bodies:
                response = await client.patch("/settings/team", json=bad)
                assert response.status_code == 422, (bad, response.text)

    asyncio.run(body())


def test_team_settings_publishes_sse_event(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            collected = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(
                        name == "team_settings" for name, _ in events
                    ),
                )
            )
            await asyncio.sleep(0.3)
            response = await client.patch(
                "/settings/team", json={"review": {"enabled": True}}
            )
            assert response.status_code == 200, response.text
            events = await collected
            states = payloads(events, "team_settings")
            assert states, [name for name, _ in events]
            assert states[-1]["review"]["enabled"] is True
            # The event carries the same payload the GET returns.
            assert states[-1] == response.json()

    asyncio.run(body())
