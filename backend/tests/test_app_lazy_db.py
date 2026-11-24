"""Building the app must not open its database.

A second process that only imports ``app.main`` (a test run, the CLI, a reloader) used to open
the live database while building the engine. Opening it runs restart recovery, which marks
every running run of the process actually serving that database as stopped, so that server
went on replying while every new send was refused as busy.
"""

from __future__ import annotations

from app.config import Settings
from app.main import create_app


def test_create_app_leaves_a_running_run_alone(tmp_path) -> None:
    path = tmp_path / "live.sqlite3"
    app = create_app(Settings(database_path=path, base_url="http://127.0.0.1:9", api_key="k", model="m"))

    assert app.state.store.ready is False
    assert not path.exists()
