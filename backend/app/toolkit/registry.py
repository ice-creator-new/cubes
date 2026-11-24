"""Registration of every tool pack and the two functions the engine needs."""

from __future__ import annotations

from .base import ToolSpec, schemas
from .code import CODE_TOOLS
from .files import FILE_TOOLS
from .images import IMAGE_TOOLS
from .memory import MEMORY_TOOLS
from .memories import MEMORY_TOOLS as LONG_TERM_TOOLS
from .utility import UTILITY_TOOLS
from .web import WEB_TOOLS
from .workbench import WORKBENCH_TOOLS

# Pack order is prompt order: web, memory/workbench, utility, files, code.
ALL_TOOLS: tuple[ToolSpec, ...] = (
    *WEB_TOOLS,
    *MEMORY_TOOLS,
    *LONG_TERM_TOOLS,
    *WORKBENCH_TOOLS,
    *UTILITY_TOOLS,
    *FILE_TOOLS,
    *IMAGE_TOOLS,
    *CODE_TOOLS,
)

ROLES = frozenset({"supervisor", "member"})


def tool_specs(settings, role: str) -> list[ToolSpec]:
    """Every tool ``role`` may call with these settings, in a stable order."""
    if role not in ROLES:
        return []
    return [spec for spec in ALL_TOOLS if spec.offered_to(settings, role)]


def openai_schemas(specs: list[ToolSpec]) -> list[dict]:
    """The ``tools`` payload for one chat completion request."""
    return schemas(specs)
