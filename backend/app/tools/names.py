"""Tool names as constants, in a module that imports nothing.

Labels, schemas, handlers and the approval policy all key on these names; keeping them in
a leaf module lets any of those modules import a name without importing a family.
"""

from __future__ import annotations

# Team management, the supervisor's own tools.
CREATE_AGENT = "create_agent"
CREATE_GROUP = "create_group"
SET_GROUP_MEMBERS = "set_group_members"
POST_GROUP_MESSAGE = "post_group_message"
ASK_AGENT = "ask_agent"
DELETE_AGENT = "delete_agent"
DISSOLVE_GROUP = "dissolve_group"

# Schedules: the four tools a scheduled run may never call (no recursion).
SCHEDULE_CREATE = "schedule_create"
SCHEDULE_UPDATE = "schedule_update"
SCHEDULE_LIST = "schedule_list"
SCHEDULE_DELETE = "schedule_delete"
SCHEDULE_TOOLS: tuple[str, ...] = (
    SCHEDULE_CREATE,
    SCHEDULE_UPDATE,
    SCHEDULE_LIST,
    SCHEDULE_DELETE,
)

# Self-reminders: any bot may set or cancel one, in the chat it is speaking in.
REMIND_ME = "remind_me"
CANCEL_REMINDER = "cancel_reminder"

# Bot-to-bot chats, and taking a question to a group the asker belongs to.
DIRECT_MESSAGE = "direct_message"
ASK_GROUP = "ask_group"
# Group tasks: hand a part of yours to a member, or hand the whole task over.
GROUP_ASSIGN = "group_assign"
GROUP_TRANSFER = "group_transfer"
START_TASK = "start_task"
NOTIFY_USER = "notify_user"
# The card a scheduled run posts in its target conversation to ask for the sandbox.
SCHEDULE_COMPUTER = "schedule_computer"

# The team digest: the supervisor reads one period's facts to write 晨报/周报.
TEAM_REPORT = "team_report"

# Agent skills: read a reusable procedure, create one, read its files, or copy it into the
# sandbox computer so its scripts can run there.
USE_SKILL = "use_skill"
READ_SKILL_FILE = "read_skill_file"
SKILL_CREATE = "skill_create"
SKILL_TO_COMPUTER = "skill_to_computer"

# Workflows: a plan a bot writes and owns, which the user can edit and run.
WORKFLOW_LIST = "workflow_list"
WORKFLOW_READ = "workflow_read"
WORKFLOW_CREATE = "workflow_create"
WORKFLOW_UPDATE = "workflow_update"
WORKFLOW_DELETE = "workflow_delete"
WORKFLOW_RUN = "workflow_run"
WORKFLOW_TOOLS: tuple[str, ...] = (
    WORKFLOW_LIST,
    WORKFLOW_READ,
    WORKFLOW_CREATE,
    WORKFLOW_UPDATE,
    WORKFLOW_DELETE,
    WORKFLOW_RUN,
)

# Talking to the user mid-run, changing one's own look, and splitting off small parallel jobs.
ASK_USER = "ask_user"
UPDATE_SELF = "update_self"
UPDATE_AGENT = "update_agent"
RUN_SUBTASKS = "run_subtasks"

MANAGEMENT_TOOLS: tuple[str, ...] = (
    CREATE_AGENT,
    CREATE_GROUP,
    SET_GROUP_MEMBERS,
    POST_GROUP_MESSAGE,
    ASK_AGENT,
    DELETE_AGENT,
    DISSOLVE_GROUP,
)
