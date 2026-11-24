"""Versioned SQLite persistence for agents, conversations, messages, runs and actions.

Nothing touches the database at import time. :meth:`Store.initialize` — called by the app
lifespan, or lazily on the first store call — opens the file, migrates legacy roundtable
data in a single transaction *after* taking a one-time ``<database>.pre-team-backup.sqlite3``
backup, bootstraps the default supervisor and fails any run left ``running`` by a previous
process. Obsolete execution tables are dropped only inside a successful migration, so a
failed migration leaves the original data untouched and recoverable.

:class:`Store` is the one flat surface every caller uses; it is composed from one mixin per
aggregate, all of them over the single connection :class:`~app.store.db.Database` owns.
"""

from __future__ import annotations

from .actions import Actions
from .agents import Agents
from .approval_rules import ApprovalRules
from .attachments import Attachments
from .cli_sessions import CliSessions
from .connectors import Connectors
from .conversations import Conversations
from .db import (
    BACKUP_SUFFIX,
    SCHEMA_VERSION,
    Database,
    legacy_avatar_seed,
    new_id,
    now_iso,
)
from .group_tasks import GroupTasks
from .memories import Memories
from .messages import Messages
from .notes import Notes
from .providers import Providers
from .reactions import Reactions
from .runs import Runs
from .schedules import Schedules
from .skills import Skills
from .workflows import Workflows
from .summaries import Summaries
from .tasks import Tasks
from .team import Team
from .usage import Usage
from .webhooks import Webhooks
from .working_files import WorkingFiles

__all__ = [
    "BACKUP_SUFFIX",
    "SCHEMA_VERSION",
    "Store",
    "legacy_avatar_seed",
    "new_id",
    "now_iso",
]


class Store(
    Database,
    Agents,
    ApprovalRules,
    Conversations,
    Messages,
    Runs,
    Actions,
    Schedules,
    Providers,
    Connectors,
    Notes,
    Tasks,
    Attachments,
    Memories,
    Skills,
    Summaries,
    Team,
    Usage,
    Reactions,
    Workflows,
    Webhooks,
    CliSessions,
    GroupTasks,
    WorkingFiles,
):
    """Small synchronous SQLite store.

    One connection per process, used from the event loop thread only, guarded by a
    reentrant lock; initialization is deferred so importing :mod:`app.main` never touches
    a live database.
    """
