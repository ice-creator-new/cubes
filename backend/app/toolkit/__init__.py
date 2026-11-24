"""The tool packs the supervisor and every member share.

``backend/app/toolkit`` is deliberately independent of the engine: a tool only needs a
:class:`~app.toolkit.base.ToolContext`, so adding one never touches the run loop.
"""

from __future__ import annotations

from .base import Gate, ToolContext, ToolInputError, ToolResult, ToolSpec, page_detail, split_result, text_detail
from .registry import openai_schemas, tool_specs

__all__ = [
    "Gate",
    "ToolContext",
    "ToolInputError",
    "ToolResult",
    "ToolSpec",
    "openai_schemas",
    "page_detail",
    "split_result",
    "tool_specs",
    "text_detail",
]
