"""The local computer is the machine the backend runs on, not an Apple container."""

from __future__ import annotations

import asyncio
import threading
import time

from app.computer import ComputerHub
from app.host_computer import HostComputer


def test_a_shell_on_the_host_does_not_need_an_agent() -> None:
    host = HostComputer()
    hub = ComputerHub(lambda *_args: None, lambda: "token", host=host)

    async def body() -> None:
        state = hub.state()
        assert state["connected"] is True
        assert state["machines"]["local"]["connected"] is True
        assert state["machines"]["local"]["name"] == "这台 Mac"
        assert state["machines"]["cloud"]["connected"] is False
        reply = await hub.call(
            "shell",
            {"command": "echo cubes-host", "timeout": 10},
            stopped=lambda: False,
            run_id="run-1",
        )
        assert reply is not None
        assert reply["exit_code"] == 0
        assert "cubes-host" in reply["stdout"]

    asyncio.run(body())
    host.close()


def test_stopping_cancels_a_host_command() -> None:
    host = HostComputer()
    hub = ComputerHub(lambda *_args: None, lambda: "token", host=host)
    stop = False

    async def body() -> None:
        nonlocal stop

        async def flip() -> None:
            await asyncio.sleep(0.4)
            nonlocal stop
            stop = True

        stopper = asyncio.create_task(flip())
        reply = await hub.call(
            "shell",
            {"command": "sleep 30", "timeout": 60},
            stopped=lambda: stop,
            run_id="run-1",
        )
        await stopper
        assert reply is None

    asyncio.run(body())
    host.close()


def test_two_runs_commands_overlap_and_both_finish() -> None:
    host = HostComputer()
    answers: dict[str, dict] = {}

    def shell(run_id: str, command: str) -> None:
        answers[run_id] = host.call(
            "shell", {"command": command, "timeout": 30}, run_id=run_id
        )

    started = time.monotonic()
    first = threading.Thread(target=shell, args=("a", "sleep 0.8; echo a"))
    second = threading.Thread(target=shell, args=("b", "sleep 0.8; echo b"))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    elapsed = time.monotonic() - started

    assert answers["a"]["exit_code"] == 0 and "a" in answers["a"]["stdout"]
    assert answers["b"]["exit_code"] == 0 and "b" in answers["b"]["stdout"]
    # Serial would be 1.6 s; two process groups side by side stay near one command's time.
    assert elapsed < 1.5, elapsed
    host.close()


def test_cancelling_one_run_leaves_the_other_running() -> None:
    host = HostComputer()
    answers: dict[str, dict] = {}

    def shell(run_id: str, command: str) -> None:
        answers[run_id] = host.call(
            "shell", {"command": command, "timeout": 30}, run_id=run_id
        )

    first = threading.Thread(target=shell, args=("a", "sleep 30"))
    second = threading.Thread(target=shell, args=("b", "sleep 0.6; echo done"))
    first.start()
    second.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with host._lock:
            if "a" in host._procs and host._procs["a"]:
                break
        time.sleep(0.02)
    host.cancel("a")
    first.join(timeout=10)
    second.join(timeout=10)

    assert host._stop_for("a").is_set()
    assert answers["a"]["exit_code"] != 0  # killed by its own run's cancel
    assert answers["b"]["exit_code"] == 0 and "done" in answers["b"]["stdout"]
    host.close()


def test_shell_without_a_cwd_starts_in_the_working_folder(tmp_path) -> None:
    host = HostComputer(default_cwd=lambda: tmp_path)
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()

    def run(args: dict) -> dict:
        return host.call("shell", {**args, "timeout": 10}, run_id="run-1")

    default = run({"command": "pwd"})
    assert default["cwd"] == str(tmp_path)
    chosen = run({"command": "pwd", "cwd": str(explicit)})
    assert chosen["cwd"] == str(explicit)
    host.close()


def test_host_permissions_can_be_read_without_prompting() -> None:
    from app.host_computer import host_permissions

    state = host_permissions()
    assert set(state) == {"needed", "screen", "accessibility"}
    assert state["needed"] is True
    assert isinstance(state["screen"], bool)
    assert isinstance(state["accessibility"], bool)
