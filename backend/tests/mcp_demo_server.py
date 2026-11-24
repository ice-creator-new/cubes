"""A tiny real MCP server for connector tests: stdio by default, ``crash`` to fail at start.

:func:`http_app` serves the same tools over streamable HTTP; every request must carry
``Authorization: Bearer good-token``, anything else gets 401, which is how a revoked or
mistyped token looks to a connector.
"""

from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

app = MCPServer("demo")
CREATED: list[str] = []


@app.tool(annotations=ToolAnnotations(read_only_hint=True), title="列出问题")
def list_issues(repo: str) -> str:
    """List the open issues of one repository."""
    marker = os.environ.get("DEMO_MARKER", "")
    return f"{repo} 的问题：#1 登录失败，#2 深色模式{marker}" + "".join(
        f"，#{3 + i} {title}" for i, title in enumerate(CREATED)
    )


@app.tool(title="创建问题")
def create_issue(repo: str, title: str) -> str:
    """Create one issue in a repository."""
    CREATED.append(title)
    return f"已在 {repo} 创建问题「{title}」"


@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def explode() -> str:
    """Always fails, the way a server reports a failure it means the caller to read."""
    raise ToolError("仓库不存在")


def http_app():
    inner = app.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if auth != b"Bearer good-token":
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await inner(scope, receive, send)

    return guarded


if __name__ == "__main__":
    if sys.argv[1:2] == ["crash"]:
        print("缺少配置文件 demo.json", file=sys.stderr)
        sys.exit(1)
    app.run()
