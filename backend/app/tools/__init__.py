"""The tool layer: one handler interface for every family, one table to route a call.

Families: the supervisor's management tools, the schedule tools, the toolkit pack, the MCP
connectors, the sandbox computer and the two chat tools. The engine builds a registry per
model call and per tool call and reads schemas, labels and routing from it alone.
"""

from __future__ import annotations

from .registry import ToolRegistry
from .types import (
    ToolHandler,
    ToolOffer,
    ToolOutcome,
    ToolRunContext,
    cap_tool_text,
    error_result,
)

__all__ = [
    "ToolHandler",
    "ToolOffer",
    "ToolOutcome",
    "ToolRegistry",
    "ToolRunContext",
    "cap_tool_text",
    "error_result",
]
