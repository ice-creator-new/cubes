"""Shared route dependencies and the two payload-publishing helpers.

Every router reads the app's own services off ``app.state`` — set once by :func:`create_app`
— so a route never has to be closed over global state and a test can build the app around
any store or engine.
"""

from __future__ import annotations

from fastapi import Request

from .. import models
from ..agent.run_state import RunLauncher
from ..config import Settings
from ..engine import SupervisorEngine
from ..scheduler import Scheduler
from ..store import Store
from ..task_scheduler import TaskScheduler


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_engine(request: Request) -> SupervisorEngine:
    return request.app.state.engine


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_launcher(request: Request) -> RunLauncher:
    return request.app.state.launcher


def get_scheduler(request: Request) -> Scheduler:
    return request.app.state.scheduler


def get_task_scheduler(request: Request) -> TaskScheduler:
    return request.app.state.task_scheduler


def publish_workspace(store: Store, engine: SupervisorEngine) -> dict:
    """Shape the workspace, push it to every window and return it."""
    payload = models.workspace_payload(
        store, confirm_timeout_seconds=engine._settings.confirm_timeout_seconds
    )
    engine.publish("workspace", payload)
    return payload


def publish_sidebar(store: Store, engine: SupervisorEngine) -> dict:
    """Shape the stored layout, push it to every window and return it."""
    layout = models.sidebar_payload(store.sidebar_layout())
    engine.publish("sidebar", layout)
    return layout
