"""The connector market: every preset service and how its credentials become a connection.

A preset knows where the vendor's official MCP server lives, which credential fields the
user pastes, and how those values turn into request headers (remote) or environment
variables (local). Two custom presets let the user point at any MCP server instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode

HTTP = "http"
STDIO = "stdio"
CUSTOM_HTTP = "custom-http"
CUSTOM_STDIO = "custom-stdio"

# Local presets run a pinned npm package, so an upgrade is always a deliberate change.
NOTION_PACKAGE = "@notionhq/notion-mcp-server@2.5.2"
BRAVE_PACKAGE = "@brave/brave-search-mcp-server@2.1.4"

FEISHU_MCP_URL = "https://mcp.feishu.cn/mcp"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
# Tools the remote Feishu server offers to an app (tenant) token. It lists nothing that is
# not named here, so the allow-list is also what decides whether writes are possible.
FEISHU_READ_TOOLS = ("fetch-doc", "fetch-file", "get-comments", "list-docs", "get-user")
FEISHU_WRITE_TOOLS = ("create-doc", "update-doc", "add-comments")


@dataclass(frozen=True)
class Field:
    """One credential input the form shows."""

    id: str
    label: str
    secret: bool = True
    required: bool = True
    placeholder: str = ""


@dataclass(frozen=True)
class Connection:
    """Everything needed to open one MCP session."""

    transport: str
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # Feishu: the app credentials are exchanged for a short-lived token per connection.
    feishu: tuple[str, str] | None = None


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    transport: str
    fields: tuple[Field, ...] = ()
    token_url: str | None = None
    token_label: str | None = None
    token_hint: str | None = None
    docs_url: str | None = None
    # The vendor has a read-only mode; the form shows an "allow writes" switch for it.
    writes_toggle: bool = False
    # In read-only mode the vendor itself removes every write tool, so each listed tool is
    # a read even when the server does not annotate it.
    read_only_mode_is_enforced: bool = False
    requires: str | None = None
    build: Callable[[dict[str, str], bool], Connection] | None = None

    @property
    def custom(self) -> bool:
        return self.id in (CUSTOM_HTTP, CUSTOM_STDIO)


def _bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"} if value else {}


def _with_query(url: str, query: dict[str, str]) -> str:
    present = {key: value for key, value in query.items() if value}
    return f"{url}?{urlencode(present)}" if present else url


def _token(label: str = "Token", placeholder: str = "") -> Field:
    return Field("token", label, placeholder=placeholder)


def _optional_key(placeholder: str = "") -> Field:
    return Field("api_key", "API key", required=False, placeholder=placeholder)


def _github(values: dict[str, str], writes: bool) -> Connection:
    headers = _bearer(values["token"])
    if not writes:
        headers["X-MCP-Readonly"] = "true"
    return Connection(HTTP, url="https://api.githubcopilot.com/mcp/", headers=headers)


def _gitlab(values: dict[str, str], writes: bool) -> Connection:
    host = (values.get("host") or "https://gitlab.com").rstrip("/")
    return Connection(HTTP, url=f"{host}/api/v4/mcp", headers=_bearer(values["token"]))


def _linear(values: dict[str, str], writes: bool) -> Connection:
    url = "https://mcp.linear.app/mcp" if writes else "https://mcp.linear.app/mcp/readonly"
    return Connection(HTTP, url=url, headers=_bearer(values["token"]))


def _sentry(values: dict[str, str], writes: bool) -> Connection:
    # Sentry keeps plain ``Bearer`` for its own OAuth; pasted tokens use this scheme.
    return Connection(
        HTTP,
        url="https://mcp.sentry.dev/mcp",
        headers={"Authorization": f"Sentry-Bearer {values['token']}"},
    )


def _supabase(values: dict[str, str], writes: bool) -> Connection:
    query = {"project_ref": values.get("project_ref", ""), "read_only": "" if writes else "true"}
    return Connection(
        HTTP,
        url=_with_query("https://mcp.supabase.com/mcp", query),
        headers=_bearer(values["token"]),
    )


def _stripe(values: dict[str, str], writes: bool) -> Connection:
    return Connection(HTTP, url="https://mcp.stripe.com", headers=_bearer(values["token"]))


def _cloudflare(values: dict[str, str], writes: bool) -> Connection:
    return Connection(
        HTTP, url="https://mcp.cloudflare.com/mcp", headers=_bearer(values["token"])
    )


def _context7(values: dict[str, str], writes: bool) -> Connection:
    return Connection(
        HTTP, url="https://mcp.context7.com/mcp", headers=_bearer(values.get("api_key", ""))
    )


def _firecrawl(values: dict[str, str], writes: bool) -> Connection:
    return Connection(
        HTTP, url="https://mcp.firecrawl.dev/v2/mcp", headers=_bearer(values["api_key"])
    )


def _exa(values: dict[str, str], writes: bool) -> Connection:
    key = values.get("api_key", "")
    return Connection(
        HTTP, url="https://mcp.exa.ai/mcp", headers={"x-api-key": key} if key else {}
    )


def _feishu(values: dict[str, str], writes: bool) -> Connection:
    tools = FEISHU_READ_TOOLS + (FEISHU_WRITE_TOOLS if writes else ())
    return Connection(
        HTTP,
        url=FEISHU_MCP_URL,
        headers={"X-Lark-MCP-Allowed-Tools": ",".join(tools)},
        feishu=(values["app_id"], values["app_secret"]),
    )


def _notion(values: dict[str, str], writes: bool) -> Connection:
    return Connection(
        STDIO, command="npx", args=("-y", NOTION_PACKAGE), env={"NOTION_TOKEN": values["token"]}
    )


def _brave(values: dict[str, str], writes: bool) -> Connection:
    return Connection(
        STDIO, command="npx", args=("-y", BRAVE_PACKAGE), env={"BRAVE_API_KEY": values["api_key"]}
    )


PRESETS: tuple[Preset, ...] = (
    Preset(
        id="github",
        name="GitHub",
        description="读取仓库、Issue 和 Pull Request，也可以评论和提交。",
        transport=HTTP,
        fields=(_token(placeholder="github_pat_… 或 ghp_…"),),
        token_url="https://github.com/settings/personal-access-tokens/new",
        token_label="去 GitHub 创建 token",
        token_hint="建议用细粒度 token，只选需要的仓库；只读时给 Contents、Issues、Pull requests 的读权限即可。",
        docs_url="https://github.com/github/github-mcp-server",
        writes_toggle=True,
        read_only_mode_is_enforced=True,
        build=_github,
    ),
    Preset(
        id="gitlab",
        name="GitLab",
        description="查看项目、合并请求和工作项，支持自建实例。",
        transport=HTTP,
        fields=(
            _token(placeholder="glpat-…"),
            Field("host", "实例地址", secret=False, required=False, placeholder="https://gitlab.com"),
        ),
        token_url="https://gitlab.com/-/user_settings/personal_access_tokens",
        token_label="去 GitLab 创建 token",
        token_hint="需要细粒度 token，并在权限里加上 MCP 的 Execute；旧式 token 会被拒绝。",
        docs_url="https://docs.gitlab.com/user/model_context_protocol/mcp_server/",
        build=_gitlab,
    ),
    Preset(
        id="linear",
        name="Linear",
        description="查找和整理 Issue、项目与周期。",
        transport=HTTP,
        fields=(_token("API key", "lin_api_…"),),
        token_url="https://linear.app/settings/account/security",
        token_label="去 Linear 创建 API key",
        token_hint="在 Security & access 里创建 API key；只需要查看时权限选 Read。",
        docs_url="https://linear.app/docs/mcp",
        writes_toggle=True,
        read_only_mode_is_enforced=True,
        build=_linear,
    ),
    Preset(
        id="sentry",
        name="Sentry",
        description="查看错误、事件和发布情况，帮你定位线上问题。",
        transport=HTTP,
        fields=(_token("Auth token", "sntryu_…"),),
        token_url="https://sentry.io/settings/account/api/auth-tokens/",
        token_label="去 Sentry 创建 token",
        token_hint="只读使用时权限给 org:read、project:read、team:read 即可。",
        docs_url="https://docs.sentry.io/product/sentry-mcp/",
        build=_sentry,
    ),
    Preset(
        id="supabase",
        name="Supabase",
        description="查询数据库表结构和数据，查看项目日志与文档。",
        transport=HTTP,
        fields=(
            _token("Access token", "sbp_…"),
            Field("project_ref", "项目 ID", secret=False, required=False, placeholder="不填则可访问所有项目"),
        ),
        token_url="https://supabase.com/dashboard/account/tokens",
        token_label="去 Supabase 创建 token",
        token_hint="填写项目 ID 可以把访问限制在一个项目里；只读模式下查询以只读数据库角色执行。",
        docs_url="https://supabase.com/docs/guides/ai-tools/mcp",
        writes_toggle=True,
        build=_supabase,
    ),
    Preset(
        id="stripe",
        name="Stripe",
        description="查看客户、订单、订阅和账单。",
        transport=HTTP,
        fields=(_token("受限密钥", "rk_live_…"),),
        token_url="https://dashboard.stripe.com/apikeys",
        token_label="去 Stripe 创建受限密钥",
        token_hint="创建受限密钥时选择授权给智能体，资源权限尽量设为只读。",
        docs_url="https://docs.stripe.com/mcp",
        build=_stripe,
    ),
    Preset(
        id="cloudflare",
        name="Cloudflare",
        description="查看和管理 Workers、DNS、R2 等 Cloudflare 资源。",
        transport=HTTP,
        fields=(_token("API token"),),
        token_url="https://dash.cloudflare.com/profile/api-tokens",
        token_label="去 Cloudflare 创建 token",
        token_hint="按需要的产品授予权限；账户 token 还需要 Account Resources 的读权限。",
        docs_url="https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/",
        build=_cloudflare,
    ),
    Preset(
        id="context7",
        name="Context7",
        description="查询各类编程库和框架的最新文档与示例。",
        transport=HTTP,
        fields=(_optional_key("ctx7sk-…"),),
        token_url="https://context7.com/dashboard",
        token_label="去 Context7 获取 API key",
        token_hint="不填也能用，填写后额度更高。",
        docs_url="https://context7.com/docs/resources/all-clients",
        build=_context7,
    ),
    Preset(
        id="firecrawl",
        name="Firecrawl",
        description="搜索网页并把页面抓取成干净的正文。",
        transport=HTTP,
        fields=(Field("api_key", "API key", placeholder="fc-…"),),
        token_url="https://www.firecrawl.dev/app/api-keys",
        token_label="去 Firecrawl 获取 API key",
        docs_url="https://docs.firecrawl.dev/mcp-server",
        build=_firecrawl,
    ),
    Preset(
        id="exa",
        name="Exa",
        description="面向 AI 的网页搜索，适合查找资料和代码示例。",
        transport=HTTP,
        fields=(_optional_key(),),
        token_url="https://dashboard.exa.ai/api-keys",
        token_label="去 Exa 获取 API key",
        token_hint="不填也能用，但有频率限制。",
        docs_url="https://docs.exa.ai/reference/exa-mcp",
        build=_exa,
    ),
    Preset(
        id="feishu",
        name="飞书",
        description="读取飞书云文档和知识库，允许写操作后还能创建和编辑文档。",
        transport=HTTP,
        fields=(
            Field("app_id", "App ID", secret=False, placeholder="cli_…"),
            Field("app_secret", "App Secret"),
        ),
        token_url="https://open.feishu.cn/app",
        token_label="去飞书开发者后台",
        token_hint="创建企业自建应用，开通云文档和知识库的读取权限，再把应用添加到要读取的文档或知识库。",
        docs_url="https://open.feishu.cn/document/mcp_open_tools/developers-call-remote-mcp-server",
        writes_toggle=True,
        read_only_mode_is_enforced=True,
        build=_feishu,
    ),
    Preset(
        id="notion",
        name="Notion",
        description="搜索和读取 Notion 页面与数据库，在本机运行。",
        transport=STDIO,
        fields=(_token("Integration token", "ntn_…"),),
        token_url="https://www.notion.so/profile/integrations",
        token_label="去 Notion 创建集成",
        token_hint="创建内部集成，能力只勾选读取内容，再在页面的「连接」里添加这个集成。",
        docs_url="https://github.com/makenotion/notion-mcp-server",
        requires="Node.js",
        build=_notion,
    ),
    Preset(
        id="brave",
        name="Brave Search",
        description="用 Brave 搜索网页、新闻和图片，在本机运行。",
        transport=STDIO,
        fields=(Field("api_key", "API key", placeholder="BSA…"),),
        token_url="https://api-dashboard.search.brave.com/app/keys",
        token_label="去 Brave 获取 API key",
        docs_url="https://github.com/brave/brave-search-mcp-server",
        requires="Node.js",
        build=_brave,
    ),
    Preset(
        id=CUSTOM_HTTP,
        name="自定义远程 MCP",
        description="填写任意 MCP 服务的地址和请求头。",
        transport=HTTP,
    ),
    Preset(
        id=CUSTOM_STDIO,
        name="自定义本地 MCP",
        description="在本机运行一条命令，通过标准输入输出连接。",
        transport=STDIO,
    ),
)

PRESETS_BY_ID: dict[str, Preset] = {preset.id: preset for preset in PRESETS}


def connection_for(preset_id: str, config: dict, allow_writes: bool) -> Connection:
    """The connection one connector opens, credentials filled in.

    ``config`` is what the store keeps: ``values`` for a preset, ``url``/``headers`` or
    ``command``/``args``/``env`` for a custom server.
    """
    preset = PRESETS_BY_ID[preset_id]
    if preset.id == CUSTOM_HTTP:
        return Connection(HTTP, url=config.get("url", ""), headers=dict(config.get("headers") or {}))
    if preset.id == CUSTOM_STDIO:
        return Connection(
            STDIO,
            command=config.get("command", ""),
            args=tuple(config.get("args") or ()),
            env=dict(config.get("env") or {}),
        )
    assert preset.build is not None
    stored = config.get("values") or {}
    values = {f.id: str(stored.get(f.id) or "") for f in preset.fields}
    return preset.build(values, allow_writes)


def preset_payload(preset: Preset) -> dict:
    return {
        "id": preset.id,
        "name": preset.name,
        "description": preset.description,
        "transport": preset.transport,
        "custom": preset.custom,
        "fields": [
            {
                "id": f.id,
                "label": f.label,
                "secret": f.secret,
                "required": f.required,
                "placeholder": f.placeholder,
            }
            for f in preset.fields
        ],
        "token_url": preset.token_url,
        "token_label": preset.token_label,
        "token_hint": preset.token_hint,
        "docs_url": preset.docs_url,
        "writes_toggle": preset.writes_toggle,
        "requires": preset.requires,
    }
