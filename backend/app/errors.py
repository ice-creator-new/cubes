"""Expected, reportable failures shared by the store, model layer and engine.

Keeping them in one leaf module lets ``model_stream`` raise the same exceptions the
engine classifies (``StreamAborted`` is a ``RunStopped``), without import cycles.
"""

from __future__ import annotations


class BackendError(RuntimeError):
    """Base class for failures that are shown to the client verbatim."""


class UpstreamError(BackendError):
    """The model service failed, refused the request, or returned nothing usable."""


class RunStopped(BackendError):
    """The current run was asked to stop; partial output must be preserved."""


class StreamAborted(RunStopped):
    """Raised inside a streamed completion once a stop has been requested."""


class RunLimited(BackendError):
    """A per-run budget was reached; the run ends as ``limited``, never as success."""


class TurnLimited(RunLimited):
    """One bot's turn used its own budget: that reply ends here, the run does not.

    A nested turn (a consultation, a private message, a workflow step) is the caller's tool
    work: running out of calls inside it must not take the run that asked for it down.
    """


class TurnHandedOff(BackendError):
    """A tool handed this turn's work somewhere else (a workflow's own page): the turn ends.

    Nothing failed: the bot said what it had to say, and doing the same work here as well
    would duplicate what the place it handed off to is already doing.
    """


class WorkflowPaused(BackendError):
    """The user paused a workflow run; it keeps its cursor and continues later."""


class WorkflowWaiting(BackendError):
    """A workflow step is waiting for the user's answer; the walk resumes on their message."""



class WorkflowBusy(BackendError):
    """A walk cannot start or continue right now; the message says why, for the user."""

class ToolError(BackendError):
    """Tool arguments or targets were rejected; the message goes back to the model."""


class TaskBusy(BackendError):
    """A task cannot start right now; the message says why, for the user."""


class EngineBusy(RuntimeError):
    """Raised when this conversation already owns a running run."""


class ActionUnknown(RuntimeError):
    """The action id the client confirmed does not exist."""


class ActionNotAwaiting(RuntimeError):
    """The action is not waiting for an answer any more (decided, failed or stale)."""


class AgentNameTaken(RuntimeError):
    """A member name collides with another agent in the workspace."""


class AgentDeleteRefused(RuntimeError):
    """A member that may not be deleted right now; the message is shown verbatim."""


class GroupDissolveRefused(RuntimeError):
    """A conversation that may not be dissolved right now; the message is shown verbatim."""


AGENT_BUSY = "这个成员正在回复，等它结束后再删除。"
GROUP_BUSY = "这个群组正在回复，等它结束后再解散。"
NOT_A_GROUP = "只能解散群组。"
