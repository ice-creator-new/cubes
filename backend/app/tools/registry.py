"""One table of every tool, so schemas, labels and routing never disagree.

Each family module produces :class:`~app.tools.types.ToolHandler` values; the engine builds
one registry per model call and per tool call, asking it for the schemas to offer, the label
an action row shows, and the handler that executes a call. Adding a tool means adding a
handler, not editing a schema list, a label map and two if/elif chains.
"""

from __future__ import annotations

from typing import Sequence

from .. import computer as computer_rules
from .types import ToolHandler, ToolOffer

# What a supervisor coordinating a group may not call: the tools that do the work itself.
# Reading files, searching the web, memory, asking the user and team/group management stay.
EXECUTION_TOOL_NAMES = frozenset(
    {
        *computer_rules.TOOL_NAMES,
        "run_code",
        "file_write",
        # The workbench's writing side; reading notes and tasks is still coordination.
        "note_create",
        "note_update",
        "note_delete",
        "task_create",
        "task_update",
    }
)


def _visible(handler: ToolHandler, offer: ToolOffer) -> bool:
    """Whether this handler belongs in one offer's list at all (role and environment)."""
    return handler.offered(offer) and not (
        offer.coordinating and handler.name in EXECUTION_TOOL_NAMES
    )


class ToolRegistry:
    """Every handler known to one engine, in prompt order."""

    def __init__(self, handlers: Sequence[ToolHandler]) -> None:
        self._handlers = list(handlers)
        # First definition wins: two families may not claim the same name.
        self._by_name: dict[str, ToolHandler] = {}
        for handler in self._handlers:
            self._by_name.setdefault(handler.name, handler)

    def get(self, name: str) -> ToolHandler | None:
        """The handler for ``name``, whether or not this offer may call it."""
        return self._by_name.get(name)

    def resolve(self, offer: ToolOffer, name: str) -> ToolHandler | None:
        """The handler for ``name`` if this offer may call it, else ``None``."""
        handler = self._by_name.get(name)
        if handler is None or not handler.callable(offer):
            return None
        if offer.coordinating and handler.name in EXECUTION_TOOL_NAMES:
            return None
        return handler

    def label(self, name: str) -> str:
        """The Chinese label an action row shows; an unknown name stays as the model wrote it."""
        handler = self._by_name.get(name)
        return handler.label if handler is not None else name

    def schemas(self, offer: ToolOffer) -> list[dict]:
        """The ``tools`` payload for one model call, in prompt order."""
        return [handler.schema for handler in self._handlers if _visible(handler, offer)]
