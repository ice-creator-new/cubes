"""When a confirmation card may be skipped.

One thing lets a call through without asking: a remembered 「总是允许」 rule for that tool
and bot.
"""

from __future__ import annotations

from typing import Any

from .tools.names import DELETE_AGENT, DISSOLVE_GROUP

# The card sites' own Chinese names, so the rules page reads 「删除成员」, not ``delete_agent``.
TOOL_TITLES: dict[str, str] = {
    DELETE_AGENT: "删除成员",
    DISSOLVE_GROUP: "解散群组",
    "schedule_create": "创建定时任务",
    "schedule_update": "修改定时任务",
    "schedule_delete": "删除定时任务",
    "schedule_computer": "定时任务使用沙盒电脑",
    "computer_shell": "沙盒终端",
    "computer_browser": "沙盒浏览器",
    "computer": "沙盒屏幕",
    "token_budget": "超出用量预算",
    "files:write_inside": "在允许的文件夹里写文件",
    "files:read_outside": "读取允许范围外的文件",
    "files:write_outside": "在允许范围外写文件",
    "run_code": "运行代码",
}


def tool_title(tool_name: str) -> str:
    """The Chinese name of a card's tool, falling back to its id (connector tools)."""
    return TOOL_TITLES.get(tool_name, tool_name)


def rule_covers(store: Any, *, tool_name: str, agent_id: str) -> bool:
    """Whether a remembered rule covers this tool for this bot."""
    if not tool_name:
        return False
    return store.approval_rule_for(tool_name, agent_id) is not None


