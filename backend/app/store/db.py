"""The connection, the write transaction, the schema and every versioned migration.

Every aggregate below runs on this one connection, under one reentrant lock."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..appearance import split_trailing_tags, strip_echoed_digest
from ..constants import STEP_STOPPED
from ..providers import DEFAULT_ENV_PROTOCOL, DEFAULT_ENV_PROVIDER_NAME, models_json

_UNSET = object()

SCHEMA_VERSION = 33
LEGACY_SCHEMA_VERSION = 2
BACKUP_SUFFIX = ".pre-team-backup.sqlite3"

# One snapshot per database, taken by the v10 migration that drops ``messages.hidden``.
V10_BACKUP_PREFIX = ".pre-v10-"
# One snapshot, taken by the v22 migration that folds workflow chats into their owners' chats.
V22_BACKUP_PREFIX = ".pre-v22-"
RESTART_ERROR = "服务重启导致任务中断，未完成的部分已保留。"
DEFAULT_PROVIDER_ID_KEY = "default_provider_id"
COMPUTER_TOKEN_KEY = "computer_token"
COMPUTER_TARGET_KEY = "computer_target"
DEFAULT_MODEL_KEY = "default_model"
# The workspace-wide backup model a failed call retries on (provider id and model).
FALLBACK_PROVIDER_ID_KEY = "fallback_provider_id"
FALLBACK_MODEL_KEY = "fallback_model"
ENV_PROVIDERS_IMPORTED_KEY = "env_providers_imported"
ONBOARDED_KEY = "onboarded"
DEFAULT_SUPERVISOR_NAME = "主管"
DEFAULT_SUPERVISOR_INSTRUCTIONS = "帮助用户完成目标，自主组建团队"
DIRECT_PURPOSE = "主管私聊"

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        instructions TEXT NOT NULL,
        kind TEXT NOT NULL,
        avatar_seed TEXT NOT NULL,
        appearance TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        purpose TEXT NOT NULL,
        legacy_roles TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_order INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_members (
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (conversation_id, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        author_id TEXT,
        author_name TEXT NOT NULL,
        avatar_seed TEXT,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT NOT NULL,
        action_id TEXT,
        mentions TEXT NOT NULL DEFAULT '[]',
        reply_to_id TEXT,
        mood TEXT,
        relay_conversation_id TEXT,
        relay_direction TEXT,
        relay_peer_id TEXT,
        relay_peer_name TEXT,
        origin_message_id TEXT,
        interrupts INTEGER NOT NULL DEFAULT 0,
        schedule_id TEXT,
        schedule_title TEXT,
        seq INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        goal TEXT NOT NULL,
        status TEXT NOT NULL,
        current_step TEXT NOT NULL,
        error TEXT,
        model_calls INTEGER NOT NULL DEFAULT 0,
        tool_calls INTEGER NOT NULL DEFAULT 0,
        current_agent_id TEXT,
        pending_agent_ids TEXT NOT NULL DEFAULT '[]',
        hops INTEGER NOT NULL DEFAULT 0,
        schedule_id TEXT,
        wrapped INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        conversation_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        result TEXT,
        error TEXT,
        target_agent_id TEXT,
        target_conversation_id TEXT,
        confirmation TEXT,
        message_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_invocations (
        run_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        result TEXT,
        action_id TEXT NOT NULL,
        PRIMARY KEY (run_id, tool_call_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_usage (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        agent_id TEXT NOT NULL,
        conversation_id TEXT,
        purpose TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cached_tokens INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sidebar_folders (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        position INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sidebar_items (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        pinned INTEGER NOT NULL DEFAULT 0,
        pin_position INTEGER NOT NULL DEFAULT 0,
        folder_id TEXT REFERENCES sidebar_folders(id) ON DELETE SET NULL,
        position INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notes (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        author_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        assignee_id TEXT,
        author_id TEXT,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        protocol TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key TEXT NOT NULL DEFAULT '',
        models TEXT NOT NULL DEFAULT '[]',
        vision_models TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id TEXT PRIMARY KEY,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS message_attachments (
        message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        PRIMARY KEY (message_id, attachment_id)
    )
    """,
    # A scheduled task: at its due time the backend starts a normal run in one
    # conversation with ``prompt`` as the user's message. Rule fields (``at``/``every_minutes``/
    # ``cron``) are mutually exclusive and interpreted in ``timezone``; ``next_run_at`` is
    # always canonical UTC. ``queued`` means "due but the conversation was busy".
    """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        conversation_title TEXT NOT NULL,
        kind TEXT NOT NULL,
        at TEXT,
        every_minutes INTEGER,
        cron TEXT,
        timezone TEXT NOT NULL,
        use_computer INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        next_run_at TEXT,
        queued INTEGER NOT NULL DEFAULT 0,
        last_run_at TEXT,
        last_run_id TEXT,
        last_status TEXT,
        last_error TEXT,
        run_count INTEGER NOT NULL DEFAULT 0,
        owner_agent_id TEXT,
        workspace_conversation_id TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # An account-wide external service (an MCP server) every bot may use. ``config`` holds
    # the preset's field values or a custom server's address/command, secrets included; the
    # API only ever returns hints. ``tools`` caches the last successful tool listing.
    """
    CREATE TABLE IF NOT EXISTS connectors (
        id TEXT PRIMARY KEY,
        preset TEXT NOT NULL,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        config TEXT NOT NULL DEFAULT '{}',
        allow_writes INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        tools TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # An agent skill (https://agentskills.io/specification): a folder ``<name>/`` holding a
    # ``SKILL.md`` (YAML frontmatter + Markdown body) plus optional scripts/references/assets.
    # The file is the source of truth; the row mirrors the frontmatter and is rewritten when
    # the file is. A skill is available to every bot unless its name sits in that bot's
    # ``tool_permissions.skills_disabled``.
    """
    CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL,
        source TEXT NOT NULL,
        source_url TEXT,
        author_id TEXT,
        enabled INTEGER NOT NULL,
        has_scripts INTEGER NOT NULL,
        file_count INTEGER NOT NULL,
        use_count INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # Long-term memory: one row per fact, kept with a timeline. A replaced row stays with
    # ``status`` retired and ``superseded_by`` pointing at its replacement; ``status``
    # retired with no replacement means it was forgotten. ``source_message_ids`` is a JSON
    # array. Retired rows reach the panel but never a search or a prompt.
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        agent_id TEXT,
        topic TEXT NOT NULL,
        content TEXT NOT NULL,
        pinned INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        superseded_by TEXT,
        origin TEXT NOT NULL,
        author_id TEXT,
        source_conversation_id TEXT,
        source_message_ids TEXT NOT NULL DEFAULT '[]',
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # The search index: ``body`` holds the bigram shadow text (see ``memories.bigrams``), so
    # a two-character Chinese query matches inside a sentence. Every write above indexes
    # its own row in the same transaction, so no trigger is needed.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        memory_id UNINDEXED,
        body,
        tokenize='unicode61'
    )
    """,
    # One row per run a memory extraction was claimed for: a restart must not repeat it.
    """
    CREATE TABLE IF NOT EXISTS memory_runs (
        run_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
    # Finished turns waiting for their conversation's next batched extraction. Kept in the
    # database so a restart extracts them later instead of dropping them; ``reply_ids`` is a
    # JSON array. A row leaves once its batch was sent (or memory was switched off).
    """
    CREATE TABLE IF NOT EXISTS memory_pending (
        run_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        trigger_message_id TEXT NOT NULL,
        reply_ids TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # One rolling summary per conversation step: everything at or before ``upto_seq`` is
    # replaced by ``summary`` in the model's context. Rows only ever move forward.
    """
    CREATE TABLE IF NOT EXISTS conversation_summaries (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        upto_seq INTEGER NOT NULL,
        summary TEXT NOT NULL,
        tokens INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (conversation_id, upto_seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_message_attachments_attachment ON message_attachments(attachment_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_content ON messages(content)",
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_runs_conversation ON runs(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_actions_identity ON actions(run_id, tool_call_id)",
    "CREATE INDEX IF NOT EXISTS idx_conversations_order ON conversations(updated_order DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sidebar_items_folder ON sidebar_items(folder_id)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due ON scheduled_tasks(enabled, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_conversation ON scheduled_tasks(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_model_usage_agent ON model_usage(agent_id, created_at)",
    # Which agents produced a reply in a run, recorded once per run/agent.
    """
    CREATE TABLE IF NOT EXISTS run_participants (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        first_at TEXT NOT NULL,
        PRIMARY KEY (run_id, agent_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_participants_run ON run_participants(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_participants_agent ON run_participants(agent_id)",
    # Emoji reactions on a message. ``actor_id`` is ``"user"`` for the user, else the agent
    # id; ``actor_name`` is the snapshot of whoever reacted (``"你"`` for the user).
    """
    CREATE TABLE IF NOT EXISTS reactions (
        message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        emoji TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (message_id, emoji, actor_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id)",
    # A plan a bot wrote and the user can edit. ``steps`` is the ordered JSON list
    # (title/kind/instruction/gate/on_fail); its row is the definition, not a run.
    """
    CREATE TABLE IF NOT EXISTS workflows (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        owner_agent_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        steps TEXT NOT NULL DEFAULT '[]',
        version INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 1,
        source TEXT NOT NULL DEFAULT 'agent',
        run_count INTEGER NOT NULL DEFAULT 0,
        last_run_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_workflows_owner_name ON workflows(owner_agent_id, name)",
    # One walk through one workflow: its cursor, the engine run serving it, and where it
    # stands. ``workflow_version`` is the definition the run started with, so a later edit
    # never rewrites a run in progress.
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
        workflow_version INTEGER NOT NULL,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        run_id TEXT,
        status TEXT NOT NULL,
        cursor INTEGER NOT NULL DEFAULT 0,
        input TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        steps TEXT NOT NULL DEFAULT '[]',
        started_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON workflow_runs(workflow_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_conversation ON workflow_runs(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)",
    # One step of one run: its own status, how many attempts it took, and what it produced.
    """
    CREATE TABLE IF NOT EXISTS workflow_steps (
        workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
        step_index INTEGER NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        -- A ``confirm`` gate the user already approved: a pause after the card must not
        -- make the walk ask the same question twice.
        approved INTEGER NOT NULL DEFAULT 0,
        output TEXT NOT NULL DEFAULT '',
        message_id TEXT,
        started_at TEXT,
        finished_at TEXT,
        PRIMARY KEY (workflow_run_id, step_index)
    )
    """,
    # One secret URL that starts a schedule or a workflow now. Neither the secret nor even a
    # hint of it is stored: ``secret_hash`` is the only trace, and the secret itself is shown
    # once, when it is created or rotated. One hook per target.
    """
    CREATE TABLE IF NOT EXISTS webhooks (
        id TEXT PRIMARY KEY,
        target_kind TEXT NOT NULL,
        target_id TEXT NOT NULL,
        secret_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_called_at TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhooks_target ON webhooks(target_kind, target_id)",
    # One local CLI conversation per (bot, chat): the id to resume, and a digest of the
    # history it has already been given, so the next turn can send only what is new.
    """
    CREATE TABLE IF NOT EXISTS cli_sessions (
        agent_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        -- ``provider_id|model|effort``: a model change starts a fresh session.
        signature TEXT NOT NULL,
        session_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, conversation_id)
    )
    """,
    # How far the user has read one conversation: the newest message sequence they saw. A
    # bot reply with a higher ``seq`` is unread, and a run whose last message is above it has
    # not been opened yet. Cleared when the conversation is deleted.
    """
    CREATE TABLE IF NOT EXISTS conversation_reads (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        last_read_seq INTEGER NOT NULL DEFAULT 0,
        read_at TEXT NOT NULL
    )
    """,
    # One "always allow" rule: a tool a bot may call without a confirmation card. A NULL
    # ``agent_id`` means any bot. The settings page lists these rows and deletes them.
    """
    CREATE TABLE IF NOT EXISTS approval_rules (
        id TEXT PRIMARY KEY,
        tool_name TEXT NOT NULL,
        agent_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_approval_rules_tool ON approval_rules(tool_name)",
    # v26: work a group took on. A root task is one member answering one user request; a
    # child (``parent_id``) is a part a member handed to another with ``group_assign``.
    # ``status`` is running/done/failed/stopped; ``result_message_id`` is the reply that
    # answered it, so a reply to that message continues the same task.
    """
    CREATE TABLE IF NOT EXISTS group_tasks (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        request_message_id TEXT,
        parent_id TEXT,
        owner_id TEXT NOT NULL,
        assigned_by TEXT,
        title TEXT NOT NULL,
        instruction TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        run_id TEXT,
        result_message_id TEXT,
        rounds INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_tasks_conversation ON group_tasks(conversation_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_group_tasks_run ON group_tasks(run_id)",
    # v29: the files a team conversation touched. One row per path (the newest touch wins),
    # so every member can be told what state an artifact is in without re-reading it. No
    # content is stored: the hint reads the file it inlines from disk, so it is never stale.
    """
    CREATE TABLE IF NOT EXISTS working_files (
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        author_id TEXT NOT NULL,
        author_name TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        lines INTEGER,
        size INTEGER,
        sha TEXT,
        added INTEGER,
        removed INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_working_files_recent ON working_files(conversation_id, updated_at DESC)",
    # v33: one bot's piece of work in one conversation — a turn from its first word to its
    # answer, stop or failure. The steps are its own messages (its tool calls and the
    # narration leading into them), a group hides them behind the turn's card, and
    # ``parent_id`` names the activity that caused this one (an ``ask_agent`` answer, a task
    # thread, a bot-to-bot hand-off). ``task_id`` is set for a task-thread turn.
    """
    CREATE TABLE IF NOT EXISTS activities (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        agent_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        run_id TEXT,
        task_id TEXT,
        parent_id TEXT,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        current_step TEXT NOT NULL DEFAULT '',
        latest_text TEXT NOT NULL DEFAULT '',
        request_message_id TEXT,
        answer_message_id TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activities_agent ON activities(agent_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_activities_conversation ON activities(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_activities_parent ON activities(parent_id, started_at)",
)

# Additive columns introduced after a table first shipped: a plain ``ALTER TABLE`` keeps
# every existing row, so a forward migration never rewrites history. Missing columns are
# added once per table inside the migration transaction; present ones are skipped.
COLUMN_UPGRADES: dict[str, tuple[tuple[str, str], ...]] = {
    # ``model_ref`` holds ``{"provider_id", "model"}``; NULL means "follow the default".
    # ``token_budget`` holds ``{"limit", "period"}``; NULL means no budget.
    # ``tool_permissions`` holds the four per-member tool switches; NULL means everything allowed.
    # ``send_mode`` overrides the team default for sends that meet this bot mid-reply;
    # NULL follows the team setting.
    "agents": (
        ("appearance", "TEXT"),
        ("model_ref", "TEXT"),
        ("token_budget", "TEXT"),
        ("tool_permissions", "TEXT"),
        ("send_mode", "TEXT"),
        # v27: this bot's own approval mode (ask/auto/full); NULL follows the workspace.
        ("permission_mode", "TEXT"),
    ),
    "messages": (
        ("mentions", "TEXT NOT NULL DEFAULT '[]'"),
        ("reply_to_id", "TEXT"),
        ("mood", "TEXT"),
        ("relay_conversation_id", "TEXT"),
        ("relay_direction", "TEXT"),
        ("relay_peer_id", "TEXT"),
        ("relay_peer_name", "TEXT"),
        # The user message a bot-to-bot exchange was started for, in another conversation.
        ("origin_message_id", "TEXT"),
        # A user message sent while the previous reply was still running, and cut it off.
        ("interrupts", "INTEGER NOT NULL DEFAULT 0"),
        # The schedule that sent this message, and its title at that moment.
        ("schedule_id", "TEXT"),
        ("schedule_title", "TEXT"),
        # The run that produced this message, so a run trace can join across conversations.
        ("run_id", "TEXT"),
        # A self-reminder's trigger message: the user sees it as a quiet system row.
        ("reminder", "INTEGER NOT NULL DEFAULT 0"),
        # v30: the group task a ``kind="task"`` card message stands for.
        ("task_id", "TEXT"),
        # v30: the model response behind this text row also emitted tool calls, so the
        # text is narration leading into them, not an answer. NULL on old and non-agent rows.
        ("leads_to_tools", "INTEGER"),
        # v32: who answered this user message, why, and by which rule (see ``app.dispatch``).
        ("route", "TEXT"),
        # v32: the members the sender addressed when it was sent — the @ picker's picks, the
        # opening @ names and the author of the quoted message. A mid-sentence @ is a
        # reference and stays out, so the dispatcher decides about it.
        ("addresses", "TEXT NOT NULL DEFAULT '[]'"),
        # v32: a ``kind="status"`` row's structured progress card (JSON).
        ("status_card", "TEXT"),
        # v33: the bot turn (an activity) that wrote this row; NULL on old and non-turn rows.
        ("activity_id", "TEXT"),
        # v34: a notice that offers to pull one agent into the group (JSON, see models.InviteOut).
        ("invite", "TEXT"),
    ),
    "runs": (
        ("current_agent_id", "TEXT"),
        ("pending_agent_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ("hops", "INTEGER NOT NULL DEFAULT 0"),
        ("schedule_id", "TEXT"),
        # Tokens the provider reported for this run, summed over its model calls, and how
        # many times its conversation was compacted while it ran.
        ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("compactions", "INTEGER NOT NULL DEFAULT 0"),
        # v28: some bot's turn in this run stopped at its step budget, so 「继续」 resumes.
        ("wrapped", "INTEGER NOT NULL DEFAULT 0"),
    ),
    # v25: a group's own standing rules, read into every prompt in that group.
    "conversations": (("rules", "TEXT NOT NULL DEFAULT ''"),),
    # v30: a task that needs more than one answer works in a hidden ``thread`` conversation of
    # its own (``thread_id``), shows one card in the group (``card_message_id``), and reports
    # its progress there (``steps`` tool calls, and the step it is on now). Existing rows keep
    # NULL/0/'' and the next turn opens the thread.
    "group_tasks": (
        ("thread_id", "TEXT"),
        ("card_message_id", "TEXT"),
        ("steps", "INTEGER NOT NULL DEFAULT 0"),
        ("current_step", "TEXT NOT NULL DEFAULT ''"),
        # v31: a queued task has no run yet. The scheduler records when it really started,
        # and the thread's newest prose line so the group card can show it live.
        ("started_at", "TEXT"),
        ("latest_text", "TEXT NOT NULL DEFAULT ''"),
        # v32: why it failed or stopped, who stopped it, and whether the user put it down or
        # has seen a finished one. A plain group reply no longer makes a task at all, so old
        # rows without a thread are archived by the migration rather than resumed.
        ("end_reason", "TEXT"),
        ("stopped_by", "TEXT"),
        ("dismissed_at", "TEXT"),
        ("seen_at", "TEXT"),
    ),
    "scheduled_tasks": (
        # The bot that reports this task's results, and the hidden conversation it works in.
        ("owner_agent_id", "TEXT"),
        ("workspace_conversation_id", "TEXT"),
        # A system schedule (the team digest) is hidden from the panel and the MAX_ENABLED count.
        ("system", "INTEGER NOT NULL DEFAULT 0"),
    ),
    # A graph: ``steps`` holds the nodes (each with a stable id), ``edges`` the connections,
    # including the branch choice each one belongs to.
    "workflows": (("edges", "TEXT NOT NULL DEFAULT '[]'"),),
    # ``loops`` counts the rounds each loop edge has sent this walk back: ``{edge_key: n}``.
    "workflow_runs": (
        ("edges", "TEXT NOT NULL DEFAULT '[]'"),
        ("loops", "TEXT NOT NULL DEFAULT '{}'"),
    ),
    # Every node state names the node it belongs to, so an edit never renumbers a walk.
    # ``rounds`` is how many times a loop rewound it; ``history`` keeps what each earlier
    # round produced (status, output, choice, message), newest last.
    "workflow_steps": (
        ("node_id", "TEXT NOT NULL DEFAULT ''"),
        ("choice", "TEXT"),
        ("rounds", "INTEGER NOT NULL DEFAULT 0"),
        ("history", "TEXT NOT NULL DEFAULT '[]'"),
    ),
    # ``detail`` holds the user-facing summary of a finished call (JSON), added after v5.
    "actions": (("confirmation", "TEXT"), ("detail", "TEXT")),
    # v24: the original filename a client sent, for PDFs and text files (images had none).
    "attachments": (("name", "TEXT NOT NULL DEFAULT ''"),),
    # Legacy per-model vision / limit columns: only the v23 migration reads them (it turns
    # them into the user layer of ``model_overrides``), so they stay for old databases.
    "providers": (
        ("vision_models", "TEXT NOT NULL DEFAULT '[]'"),
        ("model_limits", "TEXT NOT NULL DEFAULT '{}'"),
        # What the provider's own /models answer declared per model (provider layer).
        ("model_caps", "TEXT NOT NULL DEFAULT '{}'"),
        # The user's own per-model answers (or ``*``): {model: {images, context, output}}.
        ("model_overrides", "TEXT NOT NULL DEFAULT '{}'"),
    ),
}


def now_iso() -> str:
    """Second-precision ISO-8601 UTC timestamp, e.g. ``2026-10-16T00:00:00Z``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _like_escape(text: str) -> str:
    """Make a user string a literal LIKE pattern (``%`` and ``_`` stop being wildcards)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def legacy_avatar_seed(session_id: str, agent_id: str) -> str:
    """Stable avatar seed for a migrated historical role (never derived from its name)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"cubes:legacy:{session_id}:{agent_id}").hex


def _copy_database(source: sqlite3.Connection, target: Path) -> None:
    """Copy ``source`` into a fresh, complete database file at ``target``."""
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()


def _remove_file(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def _json_list(raw: Any) -> list[dict]:
    """A stored JSON list of objects, or ``[]`` when the column holds anything else."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _json_object(raw: str | None) -> dict:
    """A stored JSON object, or ``{}`` when the column holds anything else."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}

class Database:
    """One shared connection, its lock, the schema and every migration that versioned it."""

    def __init__(
        self,
        path: Path,
        *,
        env_base_url: str = "",
        env_api_key: str = "",
        env_model: str = "",
    ) -> None:
        self.path = Path(path)
        # The only thing ``backend/.env`` still provides: the provider imported on first
        # start. Every later model setting lives in the database.
        self._env_base_url = env_base_url
        self._env_api_key = env_api_key
        self._env_model = env_model
        self._lock = threading.RLock()
        self._init_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
    @property
    def attachments_dir(self) -> Path:
        """Image files, stored once under their SHA-256, next to the database."""
        return self.path.parent / "attachments"
    @property
    def skills_dir(self) -> Path:
        """Skill folders, one directory per skill, next to the database."""
        return self.path.parent / "skills"
    @property
    def ready(self) -> bool:
        """True once the connection is open (tests use this to assert laziness)."""
        return self._conn is not None
    def initialize(self) -> None:
        """Open the database, migrate, bootstrap the supervisor and fail stale runs."""
        with self._init_lock:
            if self._conn is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=NORMAL")
            except BaseException:
                conn.close()
                raise
            self._conn = conn
            try:
                self._migrate()
                self._bootstrap()
                self.sweep_background_conversations()
                self._recover_interrupted()
                # A workflow's cursor lives in its own rows: a restart pauses the walk
                # instead of failing it, so the user can continue from the same step.
                self.pause_live_workflow_runs()
                self._drop_orphan_attachments()
                self._secure_permissions()
            except BaseException:
                self._conn = None
                conn.close()
                raise
    def close(self) -> None:
        with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                conn.close()
    def _secure_permissions(self) -> None:
        """Keep plaintext keys readable only by this user: 0600 files, 0700 directory."""
        with suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        for path in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            with suppress(OSError):
                os.chmod(path, 0o600)
    def _ensure(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            self.initialize()
            conn = self._conn
        if conn is None:  # pragma: no cover - initialize always sets a connection
            raise RuntimeError("数据库未初始化。")
        return conn
    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._ensure()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None
    def _schema_version(self, conn: sqlite3.Connection) -> int:
        if not self._table_exists(conn, "meta"):
            return 0
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0
    def _migrate(self) -> None:
        conn = self._ensure()
        with self._lock:
            version = self._schema_version(conn)
            legacy = version < LEGACY_SCHEMA_VERSION and (
                self._table_exists(conn, "sessions") or self._table_exists(conn, "messages")
            )
            if legacy:
                self._backup(conn)
            if version < SCHEMA_VERSION:
                self._drop_hidden_column(conn)
            if version < 22 and self._has_workflow_chats(conn):
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self._snapshot(conn, f"{V22_BACKUP_PREFIX}{stamp}.sqlite3")
            with self._transaction() as tx:
                if legacy:
                    self._rename_legacy_tables(tx)
                for statement in SCHEMA_STATEMENTS:
                    tx.execute(statement)
                self._ensure_columns(tx)
                # ``messages.run_id`` is added by ``_ensure_columns``, so its index must
                # follow it (a plain ``CREATE INDEX`` in the schema would miss the column).
                tx.execute("CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id)")
                # v30 added ``group_tasks.thread_id`` above, so its index follows as well.
                tx.execute(
                    "CREATE INDEX IF NOT EXISTS idx_group_tasks_thread ON group_tasks(thread_id)"
                )
                # v33: ``messages.activity_id`` is added above, so its index follows too.
                tx.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_activity ON messages(activity_id)"
                )
                # v14 runs it again: replies ending in a lone unclosed ``<focused>`` slipped past.
                if version < 14:
                    self._strip_reply_tags(tx)
                # v17: a plain-text ``@mood focused`` last line slipped past the tag parser.
                if version < 17:
                    self._strip_reply_tags(tx, "%mood%")
                # v19: a ``<react>…</react>`` tag written before the parser peeled it anywhere.
                if version < 19:
                    self._strip_reply_tags(tx, "%react>%")
                # v21: a straight list of steps becomes a graph — one chain, with node ids.
                if version < 21:
                    self._graph_workflows(tx)
                # v22: a walk happens in its owner's own chat, not in a chat made for it.
                if version < 22:
                    self._fold_workflow_chats(tx)
                # v23: the old vision list and limit map become the user layer of overrides.
                if version < 23:
                    self._migrate_model_overrides(tx)
                # v32: only ``start_task``/``group_assign`` make a task now. Old rows with no
                # thread came from plain group replies; they are archived (kept for the fact
                # history, hidden from the sidebar and the chip list), never resumed.
                if version < 32:
                    self._archive_reply_tasks(tx)
                if version < 12:
                    self._strip_echoed_digests(tx)
                if legacy:
                    self._copy_legacy(tx)
                    self._drop_legacy_tables(tx)
                tx.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
    @staticmethod
    def _archive_reply_tasks(tx: sqlite3.Connection) -> None:
        """v32: plain-reply tasks become archived rows; every status is left as it was.

        Archiving only sets ``dismissed_at``, so the timeline's card stays and the fact
        history keeps reading. A row whose thread exists is real work and is untouched.
        """
        tx.execute(
            "UPDATE group_tasks SET dismissed_at = ?, updated_at = ? "
            "WHERE thread_id IS NULL AND dismissed_at IS NULL",
            (now_iso(), now_iso()),
        )

    def _has_workflow_chats(self, conn: sqlite3.Connection) -> bool:
        return self._table_exists(conn, "conversations") and (
            conn.execute("SELECT 1 FROM conversations WHERE kind = 'workflow' LIMIT 1").fetchone()
            is not None
        )

    @staticmethod
    def _migrate_model_overrides(conn: sqlite3.Connection) -> None:
        """v23: the old vision list and limit map become the user layer of overrides.

        Both were the user's own choices, so they map onto the ``user`` layer of the new
        resolution: a listed vision model gets ``images: true`` and a limit gets its
        ``context``/``output``. The old columns are blanked in the same statement, so this
        migration can run once per database.
        """
        rows = conn.execute(
            "SELECT id, vision_models, model_limits, model_overrides FROM providers"
        ).fetchall()
        for row in rows:
            try:
                vision = json.loads(row["vision_models"] or "[]")
            except ValueError:
                vision = []
            vision = [str(model) for model in vision] if isinstance(vision, list) else []
            try:
                limits = json.loads(row["model_limits"] or "{}")
            except ValueError:
                limits = {}
            limits = limits if isinstance(limits, dict) else {}
            if not vision and not limits:
                continue
            merged: dict[str, dict] = {
                str(model): dict(entry)
                for model, entry in _json_object(row["model_overrides"]).items()
                if isinstance(entry, dict)
            }
            for model in vision:
                merged.setdefault(model, {})["images"] = True
            for model, entry in limits.items():
                if not isinstance(entry, dict):
                    continue
                kept = merged.setdefault(str(model), {})
                for key in ("context", "output"):
                    amount = entry.get(key)
                    if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
                        kept[key] = amount
            conn.execute(
                "UPDATE providers SET model_overrides = ?, vision_models = '[]', "
                "model_limits = '{}' WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), row["id"]),
            )

    @staticmethod
    def _fold_workflow_chats(conn: sqlite3.Connection) -> None:
        """v22: move every per-walk conversation into the chat with the bot that owned the walk.

        Messages, runs, actions and usage follow the walk, appended after what the chat already
        holds so nothing already numbered is renumbered (summaries point at those numbers). A
        bot with no chat of its own gets the walk's conversation as its chat instead, so no
        history is lost either way.
        """
        chats = conn.execute("SELECT id FROM conversations WHERE kind = 'workflow'").fetchall()
        for chat in chats:
            chat_id = str(chat["id"])
            owner = conn.execute(
                """
                SELECT w.owner_agent_id AS owner
                  FROM workflow_runs r JOIN workflows w ON w.id = r.workflow_id
                 WHERE r.conversation_id = ? ORDER BY r.rowid DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            owner_id = str(owner["owner"]) if owner is not None and owner["owner"] else None
            if owner_id is None:
                member = conn.execute(
                    "SELECT agent_id FROM conversation_members WHERE conversation_id = ? "
                    "ORDER BY position LIMIT 1",
                    (chat_id,),
                ).fetchone()
                owner_id = str(member["agent_id"]) if member is not None else None
            target = None
            if owner_id is not None:
                is_supervisor = conn.execute(
                    "SELECT 1 FROM agents WHERE id = ? AND kind = 'supervisor'", (owner_id,)
                ).fetchone()
                target = conn.execute(
                    (
                        "SELECT id FROM conversations WHERE kind = 'direct' "
                        "ORDER BY rowid LIMIT 1"
                    )
                    if is_supervisor is not None
                    else (
                        "SELECT c.id FROM conversations c JOIN conversation_members m "
                        "ON m.conversation_id = c.id WHERE c.kind = 'direct' "
                        "AND m.agent_id = ? ORDER BY c.rowid LIMIT 1"
                    ),
                    () if is_supervisor is not None else (owner_id,),
                ).fetchone()
            if target is None:
                title = conn.execute(
                    "SELECT name FROM agents WHERE id = ?", (owner_id,)
                ).fetchone()
                name = str(title["name"]) if title is not None else "工作流"
                conn.execute(
                    "UPDATE conversations SET kind = 'direct', title = ?, purpose = ? WHERE id = ?",
                    (name, f"与「{name}」的私聊", chat_id),
                )
                continue
            target_id = str(target["id"])
            offset = int(
                conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS top FROM messages WHERE conversation_id = ?",
                    (target_id,),
                ).fetchone()["top"]
            )
            conn.execute(
                "UPDATE messages SET conversation_id = ?, seq = seq + ? WHERE conversation_id = ?",
                (target_id, offset, chat_id),
            )
            for table, column in (
                ("runs", "conversation_id"),
                ("actions", "conversation_id"),
                ("actions", "target_conversation_id"),
                ("model_usage", "conversation_id"),
                ("memory_pending", "conversation_id"),
                ("workflow_runs", "conversation_id"),
                ("memories", "source_conversation_id"),
            ):
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (target_id, chat_id)
                )
            conn.execute("DELETE FROM conversations WHERE id = ?", (chat_id,))

    @staticmethod
    def _graph_workflows(conn: sqlite3.Connection) -> None:
        """Give every stored plan one chain of edges and every node state its node id.

        A workflow written before graphs existed was a list of steps; reading it as a chain is
        exactly what it meant, so the migration changes no behaviour and loses no order.
        """
        for table in ("workflows", "workflow_runs"):
            for row in conn.execute(f"SELECT id, steps FROM {table}").fetchall():
                nodes = _json_list(row["steps"])
                edges = [
                    {"from": node.get("id") or f"n{position + 1}", "to": nodes[position + 1].get("id") or f"n{position + 2}", "when": None}
                    for position, node in enumerate(nodes[:-1])
                ]
                conn.execute(
                    f"UPDATE {table} SET edges = ? WHERE id = ?",
                    (json.dumps(edges, ensure_ascii=False), row["id"]),
                )
        for row in conn.execute("SELECT id, steps FROM workflow_runs").fetchall():
            for position, node in enumerate(_json_list(row["steps"])):
                conn.execute(
                    "UPDATE workflow_steps SET node_id = ? "
                    "WHERE workflow_run_id = ? AND step_index = ? AND node_id = ''",
                    (node.get("id") or f"n{position + 1}", row["id"], position),
                )

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        """Add every column a later schema version introduced, keeping existing rows."""
        for table, columns in COLUMN_UPGRADES.items():
            present = {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, declaration in columns:
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    def _backup(self, conn: sqlite3.Connection) -> None:
        """Snapshot the pre-team database before the migration touches anything.

        The copy always goes to a unique temporary file in the same directory and is
        published with :func:`os.replace` only after it completed, so the target is either
        the previous good copy or a fresh complete one — never a torn file. A failed copy
        removes its temporary file and aborts the migration, leaving every existing copy
        untouched. Only a legacy migration calls this, so version-3 startups never
        re-snapshot.
        """
        self._snapshot(conn, BACKUP_SUFFIX)
    def _snapshot(self, conn: sqlite3.Connection, suffix: str) -> Path:
        """Copy the live database to ``<database><suffix>`` atomically; return the target."""
        target = self.path.with_name(self.path.name + suffix)
        temporary = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex}")
        try:
            _copy_database(conn, temporary)
        except BaseException:
            _remove_file(temporary)
            raise
        try:
            os.replace(str(temporary), str(target))
        except OSError:
            _remove_file(temporary)
            raise
        return target
    def _drop_hidden_column(self, conn: sqlite3.Connection) -> None:
        """v10: a scheduled run no longer records its work in the target conversation.

        The rows it left there were never shown to anyone, so they are deleted along with
        the column that marked them. A snapshot is taken first: the rows are unrecoverable
        once gone, and this is the only migration that destroys data.
        """
        present = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "hidden" not in present:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._snapshot(conn, f"{V10_BACKUP_PREFIX}{stamp}.sqlite3")
        with self._transaction() as tx:
            tx.execute("DELETE FROM messages WHERE hidden = 1")
            tx.execute("ALTER TABLE messages DROP COLUMN hidden")
    def _strip_reply_tags(self, tx: sqlite3.Connection, pattern: str = "%>") -> None:
        """v11: replies stored before the parser knew every tag shape lose those tags now.

        Older builds kept ``<neutral></neutral>``, ``<curious>curious</curious>``, a late
        ``<answer>done</answer>`` or an ``<images>`` line that came before the mood in the
        text. They are peeled the same way a live reply is; the mood found fills an empty
        ``mood``, and passed-on image ids that still exist are bound to the reply. ``pattern``
        narrows the scan to replies whose trimmed end could hold the shape being cleaned.
        """
        rows = tx.execute(
            "SELECT id, content, mood FROM messages WHERE kind = 'agent' AND rtrim(content) LIKE ?",
            (pattern,),
        ).fetchall()
        for row in rows:
            body, mood, image_ids, _reaction = split_trailing_tags(str(row["content"]))
            if body == str(row["content"]).rstrip():
                continue
            tx.execute(
                "UPDATE messages SET content = ?, mood = COALESCE(mood, ?) WHERE id = ?",
                (body, mood, row["id"]),
            )
            if not image_ids:
                continue
            placeholders = ", ".join("?" for _ in image_ids)
            present = {
                str(found["id"])
                for found in tx.execute(
                    f"SELECT id FROM attachments WHERE id IN ({placeholders})", image_ids
                ).fetchall()
            }
            self._bind_attachments(
                tx, str(row["id"]), [image for image in image_ids if image in present]
            )
    @staticmethod
    def _strip_echoed_digests(tx: sqlite3.Connection) -> None:
        """v12: replies that copied the replayed ``<此前用过的工具>`` block lose it.

        Older builds appended that block to a bot's own past replies in its history; some
        models then wrote it into new answers, where the user saw it as part of the reply.
        """
        rows = tx.execute(
            "SELECT id, content FROM messages WHERE kind = 'agent' AND content LIKE '%<此前用过的工具>%'"
        ).fetchall()
        for row in rows:
            cleaned = strip_echoed_digest(str(row["content"]))
            if cleaned != row["content"]:
                tx.execute("UPDATE messages SET content = ? WHERE id = ?", (cleaned, row["id"]))
    def _rename_legacy_tables(self, tx: sqlite3.Connection) -> None:
        # The old product used the same table names with different columns, so move the
        # old tables aside before creating the new schema inside the same transaction.
        for name in ("messages", "sessions"):
            if self._table_exists(tx, name):
                tx.execute(f"ALTER TABLE {name} RENAME TO {name}_legacy")
    def _drop_legacy_tables(self, tx: sqlite3.Connection) -> None:
        for name in ("messages_legacy", "sessions_legacy"):
            tx.execute(f"DROP TABLE IF EXISTS {name}")
    def _copy_legacy(self, tx: sqlite3.Connection) -> None:
        """Turn old roundtable sessions into read-only legacy group conversations.

        Topics, message order, author name snapshots, role snapshots and generation status
        are preserved. Old fixed roles never become members of the active team. A
        ``messages`` table without its ``sessions`` table cannot be migrated safely, so the
        whole migration aborts instead of dropping rows that were never copied.
        """
        if not self._table_exists(tx, "sessions_legacy"):
            raise RuntimeError(
                "迁移中止：检测到旧的 messages 表但缺少 sessions 表，无法安全迁移旧数据。"
            )
        has_messages = self._table_exists(tx, "messages_legacy")
        sessions = tx.execute(
            "SELECT * FROM sessions_legacy ORDER BY updated_order ASC, rowid ASC"
        ).fetchall()
        for session in sessions:
            agents = json.loads(session["agents"])
            roles: list[dict[str, str]] = []
            seeds: dict[str, str] = {}
            for agent in agents if isinstance(agents, list) else []:
                if not isinstance(agent, dict):
                    continue
                agent_id = str(agent.get("id") or "")
                seed = legacy_avatar_seed(session["id"], agent_id)
                if agent_id:
                    seeds[agent_id] = seed
                roles.append(
                    {
                        "name": str(agent.get("name") or "未命名成员"),
                        "instructions": str(agent.get("role") or ""),
                        "avatar_seed": seed,
                    }
                )
            self._insert_conversation(
                tx,
                conversation_id=session["id"],
                kind="group",
                title=session["title"],
                purpose=session["topic"],
                member_ids=[],
                legacy_roles=roles,
                created_at=session["created_at"],
                updated_at=session["updated_at"],
                updated_order=session["updated_order"],
            )
            if not has_messages:
                continue
            for message in tx.execute(
                "SELECT * FROM messages_legacy WHERE session_id = ? ORDER BY seq ASC, rowid ASC",
                (session["id"],),
            ).fetchall():
                kind = "user" if message["kind"] == "user" else "agent"
                status = "complete" if message["status"] == "complete" else "interrupted"
                avatar_seed = seeds.get(str(message["agent_id"] or "")) if kind == "agent" else None
                self._insert_message(
                    tx,
                    message_id=message["id"],
                    conversation_id=session["id"],
                    author_id=None,
                    author_name=message["author"],
                    avatar_seed=avatar_seed,
                    kind=kind,
                    content=message["content"],
                    status=status,
                    action_id=None,
                    seq=message["seq"],
                    created_at=message["created_at"],
                )
    def _bootstrap(self) -> None:
        with self._transaction() as tx:
            supervisor = tx.execute(
                "SELECT * FROM agents WHERE kind = 'supervisor' ORDER BY rowid ASC LIMIT 1"
            ).fetchone()
            if supervisor is None:
                timestamp = now_iso()
                tx.execute(
                    """
                    INSERT INTO agents (id, name, instructions, kind, avatar_seed, created_at, updated_at)
                    VALUES (?, ?, ?, 'supervisor', ?, ?, ?)
                    """,
                    (
                        new_id("a"),
                        DEFAULT_SUPERVISOR_NAME,
                        DEFAULT_SUPERVISOR_INSTRUCTIONS,
                        uuid.uuid4().hex,
                        timestamp,
                        timestamp,
                    ),
                )
                supervisor = tx.execute(
                    "SELECT * FROM agents WHERE kind = 'supervisor' ORDER BY rowid ASC LIMIT 1"
                ).fetchone()
            supervisor_id = supervisor["id"]
            direct = tx.execute(
                "SELECT id FROM conversations WHERE kind = 'direct' ORDER BY rowid ASC LIMIT 1"
            ).fetchone()
            if direct is None:
                self._insert_conversation(
                    tx,
                    conversation_id=new_id("c"),
                    kind="direct",
                    title=supervisor["name"],
                    purpose=DIRECT_PURPOSE,
                    member_ids=[supervisor_id],
                    legacy_roles=[],
                )
            # Private bot-to-bot chats hold exactly their two bots, and a schedule's hidden
            # workspace holds only its owner; the supervisor joins neither by a restart.
            for row in tx.execute(
                "SELECT id FROM conversations WHERE kind NOT IN ('private', 'background') "
                "ORDER BY rowid ASC"
            ).fetchall():
                present = tx.execute(
                    "SELECT 1 FROM conversation_members WHERE conversation_id = ? AND agent_id = ?",
                    (row["id"], supervisor_id),
                ).fetchone()
                if present is None:
                    position = tx.execute(
                        """
                        SELECT COALESCE(MAX(position), -1) + 1 AS next
                          FROM conversation_members WHERE conversation_id = ?
                        """,
                        (row["id"],),
                    ).fetchone()["next"]
                    tx.execute(
                        """
                        INSERT OR IGNORE INTO conversation_members (conversation_id, agent_id, position)
                        VALUES (?, ?, ?)
                        """,
                        (row["id"], supervisor_id, position),
                    )
            self._seed_sidebar(tx, supervisor_id)
            # A schedule without an owner reports as the supervisor: it created the task,
            # or the user created it in the client and the supervisor speaks for them.
            tx.execute(
                "UPDATE scheduled_tasks SET owner_agent_id = ? WHERE owner_agent_id IS NULL",
                (supervisor_id,),
            )
            self._import_env_provider(tx)
    def _seed_sidebar(self, tx: sqlite3.Connection, supervisor_id: str) -> None:
        """Pin the supervisor's thread the first time this workspace gets a sidebar.

        Guarded by a ``meta`` key rather than by the row itself, so unpinning the thread
        later is never undone by a restart.
        """
        seeded = tx.execute(
            "SELECT value FROM meta WHERE key = 'sidebar_seeded'"
        ).fetchone()
        if seeded is not None:
            return
        direct = tx.execute(
            """
            SELECT c.id FROM conversations c
              JOIN conversation_members m ON m.conversation_id = c.id
             WHERE c.kind = 'direct' AND m.agent_id = ?
             ORDER BY c.rowid ASC LIMIT 1
            """,
            (supervisor_id,),
        ).fetchone()
        if direct is None:  # pragma: no cover - bootstrap just created it
            direct = tx.execute(
                "SELECT id FROM conversations WHERE kind = 'direct' ORDER BY rowid ASC LIMIT 1"
            ).fetchone()
        if direct is not None:
            tx.execute(
                """
                INSERT OR IGNORE INTO sidebar_items
                    (conversation_id, pinned, pin_position, folder_id, position)
                VALUES (?, 1, 0, NULL, 0)
                """,
                (direct["id"],),
            )
        tx.execute(
            "INSERT INTO meta (key, value) VALUES ('sidebar_seeded', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
    def _import_env_provider(self, tx: sqlite3.Connection) -> None:
        """Import ``.env``'s model as the ``mify`` provider, exactly once per database.

        Guarded by a ``meta`` key as well as by an empty table, so deleting the imported
        provider never brings it back and later ``.env`` edits have no effect.
        """
        if self._meta(tx, ENV_PROVIDERS_IMPORTED_KEY) is not None:
            return
        if tx.execute("SELECT 1 FROM providers LIMIT 1").fetchone() is not None:
            return
        base_url = self._env_base_url.strip().rstrip("/")
        model = self._env_model.strip()
        env_key = self._env_api_key.strip()
        if not model or not env_key or not base_url.lower().startswith(
            ("http://", "https://")
        ):
            return
        timestamp = now_iso()
        provider_id = new_id("p")
        tx.execute(
            """
            INSERT INTO providers
                (id, name, protocol, base_url, api_key, models, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                DEFAULT_ENV_PROVIDER_NAME,
                DEFAULT_ENV_PROTOCOL,
                base_url,
                env_key,
                models_json([model]),
                timestamp,
                timestamp,
            ),
        )
        self._write_default(tx, provider_id, model)
        self._meta_set(tx, ENV_PROVIDERS_IMPORTED_KEY, "1")
    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])
    @staticmethod
    def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    @staticmethod
    def _meta_delete(conn: sqlite3.Connection, key: str) -> None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    def computer_token(self) -> str:
        """The pairing token the computer agent presents; made once, kept across restarts."""
        with self._transaction() as tx:
            token = self._meta(tx, COMPUTER_TOKEN_KEY)
            if token:
                return token
            token = secrets.token_urlsafe(24)
            self._meta_set(tx, COMPUTER_TOKEN_KEY, token)
            return token

    def onboarded(self) -> bool:
        """Whether the workspace passed first-run onboarding (naming the supervisor).

        A database from before this screen has no ``meta`` key; anything the user made — a
        message, another bot, a group — means they were already using the app and must not
        be sent through it. The key itself is only written by the onboarding route, so
        skipping the screen is never undone by a restart.
        """
        conn = self._ensure()
        with self._lock:
            value = self._meta(conn, ONBOARDED_KEY)
            if value is not None:
                return value == "1"
            legacy = conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
            if legacy is None:
                legacy = conn.execute(
                    "SELECT 1 FROM agents WHERE kind != 'supervisor' LIMIT 1"
                ).fetchone()
            if legacy is None:
                legacy = conn.execute(
                    "SELECT 1 FROM conversations WHERE kind != 'direct' LIMIT 1"
                ).fetchone()
        return legacy is not None

    def set_onboarded(self) -> None:
        with self._transaction() as tx:
            self._meta_set(tx, ONBOARDED_KEY, "1")

    def computer_target(self) -> str:
        """Which machine the bots may drive: ``local``, ``cloud`` or ``mixed``."""
        conn = self._ensure()
        with self._lock:
            value = self._meta(conn, COMPUTER_TARGET_KEY)
        return value if value in ("local", "cloud", "mixed") else "local"

    def set_computer_target(self, value: str) -> str:
        if value not in ("local", "cloud", "mixed"):
            raise ValueError(value)
        with self._transaction() as tx:
            self._meta_set(tx, COMPUTER_TARGET_KEY, value)
        return value
    def _recover_interrupted(self) -> int:
        """Fail runs/messages/actions left active by a previous process; return the count."""
        with self._transaction() as tx:
            timestamp = now_iso()
            tx.execute(
                "UPDATE actions SET status = 'error', error = ?, confirmation = NULL, "
                "updated_at = ? WHERE status IN ('running', 'awaiting')",
                (RESTART_ERROR, timestamp),
            )
            interrupted = tx.execute(
                "UPDATE messages SET status = 'interrupted' WHERE status = 'streaming'"
            ).rowcount
            # Group work a dead process was doing stopped with it; 「接着做」 picks it up. A
            # queued task never got a run, so it has no way to start by itself after a restart.
            # Its card stopped too, so no card is left looking live, and the row says who
            # stopped it: nothing the user did.
            tx.execute(
                "UPDATE messages SET status = 'interrupted' WHERE id IN "
                "(SELECT card_message_id FROM group_tasks "
                " WHERE status IN ('running', 'awaiting', 'queued') AND card_message_id IS NOT NULL)"
            )
            tx.execute(
                "UPDATE group_tasks SET status = 'stopped', stopped_by = ?, updated_at = ? "
                "WHERE status IN ('running', 'awaiting', 'queued')",
                ("重启后中断", timestamp),
            )
            # A turn the dead process was writing stopped with it: its card says so instead
            # of promising work that nobody is doing any more.
            tx.execute(
                "UPDATE activities SET status = 'stopped', current_step = '', "
                "summary = '已停下：服务重启导致任务中断', finished_at = ? "
                "WHERE status IN ('running', 'awaiting')",
                (timestamp,),
            )
            runs = tx.execute(
                """
                UPDATE runs
                   SET status = 'stopped', error = ?, current_step = ?, finished_at = ?, updated_at = ?
                 WHERE status = 'running'
                """,
                (RESTART_ERROR, STEP_STOPPED, timestamp, timestamp),
            ).rowcount
        return int(runs) + int(interrupted)
