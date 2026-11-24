"""Web pack: the sequential search chain, result normalisation and the SSRF-guarded fetcher."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.config import Settings
from app.toolkit import net, split_result, tool_specs
from app.toolkit.base import ToolContext, ToolInputError


@dataclass
class FakeStore:
    """The web pack must never need the database."""


class Upstream:
    """A MockTransport router: no DNS, no sockets, every request recorded."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def route(self, url: str, response: Any) -> "Upstream":
        self.routes[url] = response
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        base = str(request.url).split("?")[0]
        found = self.routes.get(base)
        if found is None:
            return httpx.Response(404, json={"error": "no route"})
        if callable(found):
            return found(request)
        return found

    def url_list(self) -> list[str]:
        return [str(request.url) for request in self.requests]


def settings_for(tmp_path: Path, upstream: Upstream, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_path": tmp_path / "cubes.sqlite3",
        "parallel_mcp_url": "http://mock.local/parallel/mcp",
        "parallel_api_url": "http://mock.local/parallel/search",
        "exa_mcp_url": "http://mock.local/exa/mcp",
        "exa_api_url": "http://mock.local/exa/search",
        "tavily_api_url": "http://mock.local/tavily/search",
        "brave_api_url": "http://mock.local/brave/search",
        "duckduckgo_url": "http://mock.local/duckduckgo/html",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def context_for(settings: Settings, upstream: Upstream, published: list[Any] | None = None):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream.handler), trust_env=False, timeout=5.0
    )
    events = published if published is not None else []
    return ToolContext(
        store=FakeStore(),  # type: ignore[arg-type]
        settings=settings,
        run_id="r1",
        conversation_id="c1",
        agent={"id": "a1", "name": "主管", "kind": "supervisor"},
        http=client,
        publish=lambda event, payload: events.append((event, payload)),
    )


def spec_named(settings: Settings, name: str):
    return next(spec for spec in tool_specs(settings, "supervisor") if spec.name == name)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def call(settings: Settings, upstream: Upstream, name: str, args: dict) -> str:
    context = context_for(settings, upstream)
    spec = spec_named(settings, name)
    try:
        text, _ = split_result(await spec.run(context, args))
        return text
    finally:
        await context.http.aclose()


# ------------------------------------------------------------------- payloads

PARALLEL_TEXT = json.dumps(
    {
        "results": [
            {
                "title": "Tauri 毛玻璃",
                "url": "https://a.example/1",
                "excerpts": [{"text": "如何在 Tauri 窗口里使用毛玻璃材质。"}],
            },
            {
                "title": "窗口材质指南",
                "url": "https://a.example/2",
                "excerpts": [{"text": "系统材质与背景模糊的取舍。"}],
            },
        ]
    },
    ensure_ascii=False,
)


def mcp_body(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}]},
    }


def sse_body(payload: dict, event: str = "message") -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


DDG_HTML = """
<html><body>
<div class="result results_links">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fc.example%2F3&amp;rut=abc">鸭子结果</a>
  <a class="result__snippet">来自 DuckDuckGo 的摘要。</a>
</div>
</body></html>
"""


# ------------------------------------------------------------------- the chain


async def _case_parallel_keyless_mcp_wins_first(tmp_path: Path) -> None:
    upstream = Upstream().route("http://mock.local/parallel/mcp", httpx.Response(200, json=mcp_body(PARALLEL_TEXT)))
    upstream.route("http://mock.local/exa/mcp", httpx.Response(500, text="should not be reached"))
    settings = settings_for(tmp_path, upstream)
    text = await call(settings, upstream, "web_search", {"query": "Tauri 毛玻璃"})
    assert "找到 2 条结果：" in text
    assert "1. Tauri 毛玻璃" in text
    assert "https://a.example/1" in text
    assert "如何在 Tauri 窗口里使用毛玻璃材质。" in text
    assert text.strip().endswith("来源：parallel，查询「Tauri 毛玻璃」")
    assert len(upstream.requests) == 1
    body = json.loads(upstream.requests[0].content)
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "web_search"
    assert body["params"]["arguments"]["search_queries"] == ["Tauri 毛玻璃"]
    assert body["params"]["arguments"]["objective"] == "Tauri 毛玻璃"
    assert "application/json, text/event-stream" in upstream.requests[0].headers["accept"]

def test_parallel_keyless_mcp_wins_first(tmp_path: Path) -> None:
    run(_case_parallel_keyless_mcp_wins_first(tmp_path))


async def _case_parallel_mcp_sse_response_is_parsed(tmp_path: Path) -> None:
    upstream = Upstream().route(
        "http://mock.local/parallel/mcp",
        httpx.Response(200, content=sse_body(mcp_body(PARALLEL_TEXT)), headers={"content-type": "text/event-stream"}),
    )
    settings = settings_for(tmp_path, upstream)
    text = await call(settings, upstream, "web_search", {"query": "毛玻璃"})
    assert "https://a.example/1" in text
    assert "来源：parallel" in text

def test_parallel_mcp_sse_response_is_parsed(tmp_path: Path) -> None:
    run(_case_parallel_mcp_sse_response_is_parsed(tmp_path))


async def _case_failure_falls_through_to_exa_sse(tmp_path: Path) -> None:
    exa_text = "Title: Exa 结果\nURL: https://b.example/2\nPublished: 2024-05-01\n一段摘录"
    upstream = Upstream()
    upstream.route("http://mock.local/parallel/mcp", httpx.Response(503, text="down"))
    upstream.route(
        "http://mock.local/exa/mcp",
        httpx.Response(
            200,
            content=sse_body(mcp_body(exa_text)),
            headers={"content-type": "text/event-stream"},
        ),
    )
    settings = settings_for(tmp_path, upstream)
    text = await call(settings, upstream, "web_search", {"query": "毛玻璃"})
    assert "1. Exa 结果" in text
    assert "https://b.example/2" in text
    assert "2024-05-01" in text
    assert "来源：exa，查询「毛玻璃」" in text
    exa_request = json.loads(upstream.requests[1].content)
    assert exa_request["params"]["name"] == "web_search_exa"
    assert exa_request["params"]["arguments"]["query"] == "毛玻璃"
    assert exa_request["params"]["arguments"]["numResults"] == 5

def test_failure_falls_through_to_exa_sse(tmp_path: Path) -> None:
    run(_case_failure_falls_through_to_exa_sse(tmp_path))


async def _case_empty_results_also_fall_through(tmp_path: Path) -> None:
    empty = json.dumps({"results": []})
    upstream = Upstream()
    upstream.route("http://mock.local/parallel/mcp", httpx.Response(200, json=mcp_body(empty)))
    upstream.route(
        "http://mock.local/exa/mcp",
        httpx.Response(
            200,
            content=sse_body(mcp_body("Title: 备用\nURL: https://b.example/9\n替代结果")),
            headers={"content-type": "text/event-stream"},
        ),
    )
    settings = settings_for(tmp_path, upstream)
    text = await call(settings, upstream, "web_search", {"query": "x"})
    assert "https://b.example/9" in text
    assert "来源：exa" in text

def test_empty_results_also_fall_through(tmp_path: Path) -> None:
    run(_case_empty_results_also_fall_through(tmp_path))


async def _case_every_provider_failing_names_each_one(tmp_path: Path) -> None:
    upstream = Upstream()
    for url in (
        "http://mock.local/parallel/mcp",
        "http://mock.local/exa/mcp",
        "http://mock.local/duckduckgo/html",
    ):
        upstream.route(url, httpx.Response(500, text="boom"))
    settings = settings_for(tmp_path, upstream)
    with pytest.raises(ToolInputError) as caught:
        await call(settings, upstream, "web_search", {"query": "x"})
    message = str(caught.value)
    for provider in ("parallel", "exa", "duckduckgo"):
        assert provider in message
    assert "tavily" not in message
    assert message.endswith("。")

def test_every_provider_failing_names_each_one(tmp_path: Path) -> None:
    run(_case_every_provider_failing_names_each_one(tmp_path))


async def _case_keyed_providers_are_skipped_without_keys(tmp_path: Path) -> None:
    upstream = Upstream()
    for url in (
        "http://mock.local/parallel/mcp",
        "http://mock.local/exa/mcp",
        "http://mock.local/duckduckgo/html",
        "http://mock.local/tavily/search",
        "http://mock.local/brave/search",
    ):
        upstream.route(url, httpx.Response(500, text="boom"))
    settings = settings_for(tmp_path, upstream)
    with pytest.raises(ToolInputError):
        await call(settings, upstream, "web_search", {"query": "x"})
    touched = {url.split("/")[3] for url in upstream.url_list()}
    assert "tavily" not in touched
    assert "brave" not in touched

def test_keyed_providers_are_skipped_without_keys(tmp_path: Path) -> None:
    run(_case_keyed_providers_are_skipped_without_keys(tmp_path))


async def _case_tavily_with_key_and_week_recency(tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.route(
        "http://mock.local/tavily/search",
        httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Tavily 结果",
                        "url": "https://e.example/5",
                        "content": "摘要",
                        "published_date": "2024-01-01",
                    }
                ]
            },
        ),
    )
    settings = settings_for(
        tmp_path, upstream, search_providers=("tavily",), tavily_api_key="tk"
    )
    text = await call(settings, upstream, "web_search", {"query": "新闻", "recency": "week"})
    assert "来源：tavily" in text
    assert "2024-01-01" in text
    body = json.loads(upstream.requests[0].content)
    assert body["query"] == "新闻"
    assert body["days"] == 7
    assert body["topic"] == "news"
    assert upstream.requests[0].headers["authorization"] == "Bearer tk"

def test_tavily_with_key_and_week_recency(tmp_path: Path) -> None:
    run(_case_tavily_with_key_and_week_recency(tmp_path))


async def _case_brave_with_key_and_month_recency(tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.route(
        "http://mock.local/brave/search",
        httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Brave 结果",
                            "url": "https://d.example/4",
                            "description": "描述",
                            "age": "2 days ago",
                        }
                    ]
                }
            },
        ),
    )
    settings = settings_for(
        tmp_path, upstream, search_providers=("brave",), brave_api_key="bk"
    )
    text = await call(settings, upstream, "web_search", {"query": "毛玻璃", "recency": "month", "limit": 3})
    assert "来源：brave" in text
    assert "https://d.example/4" in text
    url = str(upstream.requests[0].url)
    assert "freshness=pm" in url
    assert "count=3" in url
    assert upstream.requests[0].headers["x-subscription-token"] == "bk"

def test_brave_with_key_and_month_recency(tmp_path: Path) -> None:
    run(_case_brave_with_key_and_month_recency(tmp_path))


async def _case_exa_api_uses_key_and_year_recency(tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.route(
        "http://mock.local/exa/search",
        httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Exa API",
                        "url": "https://f.example/6",
                        "text": "接口摘要",
                        "publishedDate": "2024-02-02",
                    }
                ]
            },
        ),
    )
    settings = settings_for(tmp_path, upstream, search_providers=("exa",), exa_api_key="ek")
    text = await call(settings, upstream, "web_search", {"query": "毛玻璃", "recency": "year"})
    assert "来源：exa" in text
    assert "接口摘要" in text
    body = json.loads(upstream.requests[0].content)
    assert body["query"] == "毛玻璃"
    assert body["numResults"] == 5
    assert body["startPublishedDate"].startswith("20")
    assert body["startPublishedDate"].endswith("Z")
    assert upstream.requests[0].headers["x-api-key"] == "ek"

def test_exa_api_uses_key_and_year_recency(tmp_path: Path) -> None:
    run(_case_exa_api_uses_key_and_year_recency(tmp_path))


async def _case_duckduckgo_html_parse_unwraps_redirects(tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.route("http://mock.local/duckduckgo/html", httpx.Response(200, text=DDG_HTML))
    settings = settings_for(tmp_path, upstream, search_providers=("duckduckgo",))
    text = await call(settings, upstream, "web_search", {"query": "毛玻璃", "recency": "day"})
    assert "1. 鸭子结果" in text
    assert "https://c.example/3" in text
    assert "duckduckgo.com/l/" not in text
    assert "来自 DuckDuckGo 的摘要。" in text
    assert "来源：duckduckgo" in text
    body = upstream.requests[0].content.decode()
    assert "df=d" in body

def test_duckduckgo_html_parse_unwraps_redirects(tmp_path: Path) -> None:
    run(_case_duckduckgo_html_parse_unwraps_redirects(tmp_path))


async def _case_duckduckgo_anomaly_page_is_a_failure(tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.route(
        "http://mock.local/duckduckgo/html",
        httpx.Response(200, text="<html><body>Unfortunately, bots use DuckDuckGo too.</body></html>"),
    )
    settings = settings_for(tmp_path, upstream, search_providers=("duckduckgo",))
    with pytest.raises(ToolInputError) as caught:
        await call(settings, upstream, "web_search", {"query": "毛玻璃"})
    assert "duckduckgo" in str(caught.value)

def test_duckduckgo_anomaly_page_is_a_failure(tmp_path: Path) -> None:
    run(_case_duckduckgo_anomaly_page_is_a_failure(tmp_path))


async def _case_snippets_are_capped_and_single_line(tmp_path: Path) -> None:
    long_text = "很长的摘要" * 200 + "\n\n第二行"
    payload = json.dumps(
        {"results": [{"title": "长", "url": "https://a.example/long", "excerpts": [{"text": long_text}]}]},
        ensure_ascii=False,
    )
    upstream = Upstream().route("http://mock.local/parallel/mcp", httpx.Response(200, json=mcp_body(payload)))
    settings = settings_for(tmp_path, upstream, search_providers=("parallel",))
    text = await call(settings, upstream, "web_search", {"query": "长"})
    snippet_line = [line for line in text.splitlines() if "很长的摘要" in line][0]
    assert len(snippet_line.strip()) <= 300
    assert "\n\n" not in text

def test_snippets_are_capped_and_single_line(tmp_path: Path) -> None:
    run(_case_snippets_are_capped_and_single_line(tmp_path))


async def _case_repeated_pages_are_listed_once(tmp_path: Path) -> None:
    """Providers repeat one page under several excerpts; the user should see it once."""
    page = {"title": "Announcing Tauri 2.12", "url": "https://v2.tauri.app/blog/tauri-212/"}
    payload = json.dumps(
        {
            "results": [
                {**page, "excerpts": [{"text": "第一段"}]},
                {"title": "tauri – Rust", "url": "https://docs.rs/tauri", "excerpts": [{"text": "文档"}]},
                {**page, "url": "https://v2.tauri.app/blog/tauri-212#notes", "excerpts": [{"text": "第二段"}]},
                {"title": "tauri – Rust", "url": "https://DOCS.rs/tauri/", "excerpts": [{"text": "又一次"}]},
            ]
        },
        ensure_ascii=False,
    )
    upstream = Upstream().route("http://mock.local/parallel/mcp", httpx.Response(200, json=mcp_body(payload)))
    settings = settings_for(tmp_path, upstream, search_providers=("parallel",))
    context = context_for(settings, upstream)
    try:
        text, detail = split_result(await spec_named(settings, "web_search").run(context, {"query": "tauri"}))
    finally:
        await context.http.aclose()
    assert text.startswith("找到 2 条结果")
    assert detail is not None
    assert [item["url"] for item in detail["items"]] == [
        "https://v2.tauri.app/blog/tauri-212/",
        "https://docs.rs/tauri",
    ]


def test_repeated_pages_are_listed_once(tmp_path: Path) -> None:
    run(_case_repeated_pages_are_listed_once(tmp_path))


async def _case_web_search_validates_arguments(tmp_path: Path) -> None:
    upstream = Upstream()
    settings = settings_for(tmp_path, upstream)
    with pytest.raises(ToolInputError):
        await call(settings, upstream, "web_search", {"query": "   "})
    with pytest.raises(ToolInputError):
        await call(settings, upstream, "web_search", {"query": "x", "limit": 50})
    with pytest.raises(ToolInputError):
        await call(settings, upstream, "web_search", {"query": "x", "recency": "decade"})
    assert upstream.requests == []

def test_web_search_validates_arguments(tmp_path: Path) -> None:
    run(_case_web_search_validates_arguments(tmp_path))


# ------------------------------------------------------------------- fetch_url


HTML_PAGE = """<!doctype html>
<html><head><title>示例页面</title>
<style>body { color: red }</style>
<script>alert("脚本内容")</script>
</head>
<body>
<nav>导航内容</nav>
<h1>一级标题</h1>
<p>正文段落。</p>
<ul><li>项目一</li><li>项目二</li></ul>
<p>参考<a href="https://example.com/x">链接文字</a>。</p>
<footer>页脚内容</footer>
</body></html>
"""


@dataclass
class Resolved:
    mapping: dict[str, list[str]]

    def __call__(self, host: str) -> list[str]:
        return self.mapping.get(host, ["93.184.216.34"])


def public_resolver(*unknown: str) -> Resolved:
    return Resolved({})


async def _case_fetch_url_converts_html_to_text(tmp_path: Path) -> None:
    upstream = Upstream().route(
        "http://public.example/page",
        httpx.Response(200, text=HTML_PAGE, headers={"content-type": "text/html; charset=utf-8"}),
    )
    settings = settings_for(tmp_path, upstream)
    with net.override_resolver(public_resolver()):
        text = await call(settings, upstream, "fetch_url", {"url": "http://public.example/page"})
    assert "标题：示例页面" in text
    assert "地址：http://public.example/page" in text
    assert "一级标题" in text
    assert "正文段落。" in text
    assert "- 项目一" in text
    assert "链接文字 (https://example.com/x)" in text
    assert "脚本内容" not in text
    assert "color: red" not in text
    assert "导航内容" not in text
    assert "页脚内容" not in text

def test_fetch_url_converts_html_to_text(tmp_path: Path) -> None:
    run(_case_fetch_url_converts_html_to_text(tmp_path))


async def _case_fetch_url_rejects_private_targets(tmp_path: Path) -> None:
    upstream = Upstream()
    settings = settings_for(tmp_path, upstream)
    resolver = Resolved(
        {
            "internal.example": ["10.0.0.5"],
            "loop.example": ["127.0.0.1"],
            "meta.example": ["169.254.169.254"],
            "v6.example": ["fd00::1"],
            "mapped.example": ["::ffff:192.168.1.4"],
        }
    )
    with net.override_resolver(resolver):
        for url in (
            "http://internal.example/",
            "http://loop.example/",
            "http://meta.example/latest/meta-data",
            "http://v6.example/",
            "http://mapped.example/",
            "http://localhost:8000/workspace",
            "http://printer.local/",
            "file:///etc/passwd",
            "ftp://public.example/x",
        ):
            with pytest.raises(ToolInputError) as caught:
                await call(settings, upstream, "fetch_url", {"url": url})
            assert "地址" in str(caught.value) or "协议" in str(caught.value)
    assert upstream.requests == []

def test_fetch_url_rejects_private_targets(tmp_path: Path) -> None:
    run(_case_fetch_url_rejects_private_targets(tmp_path))


async def _case_fetch_url_refuses_redirect_into_private_network(tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.route(
        "http://public.example/start",
        httpx.Response(302, headers={"location": "http://internal.example/secret"}),
    )
    settings = settings_for(tmp_path, upstream)
    resolver = Resolved({"internal.example": ["10.1.2.3"]})
    with net.override_resolver(resolver):
        with pytest.raises(ToolInputError):
            await call(settings, upstream, "fetch_url", {"url": "http://public.example/start"})
    assert len(upstream.requests) == 1

def test_fetch_url_refuses_redirect_into_private_network(tmp_path: Path) -> None:
    run(_case_fetch_url_refuses_redirect_into_private_network(tmp_path))


async def _case_fetch_url_follows_at_most_five_redirects(tmp_path: Path) -> None:
    upstream = Upstream()
    for index in range(8):
        upstream.route(
            f"http://public.example/hop{index}",
            httpx.Response(302, headers={"location": f"http://public.example/hop{index + 1}"}),
        )
    settings = settings_for(tmp_path, upstream)
    with net.override_resolver(public_resolver()):
        with pytest.raises(ToolInputError) as caught:
            await call(settings, upstream, "fetch_url", {"url": "http://public.example/hop0"})
    assert "重定向" in str(caught.value)
    assert len(upstream.requests) == 6

def test_fetch_url_follows_at_most_five_redirects(tmp_path: Path) -> None:
    run(_case_fetch_url_follows_at_most_five_redirects(tmp_path))


async def _case_fetch_url_reports_the_final_url(tmp_path: Path) -> None:
    upstream = Upstream()
    upstream.route("http://public.example/old", httpx.Response(301, headers={"location": "/new"}))
    upstream.route("http://public.example/new", httpx.Response(200, text="<html><title>新</title><p>到站</p></html>"))
    settings = settings_for(tmp_path, upstream)
    with net.override_resolver(public_resolver()):
        text = await call(settings, upstream, "fetch_url", {"url": "http://public.example/old"})
    assert "地址：http://public.example/new" in text
    assert "到站" in text

def test_fetch_url_reports_the_final_url(tmp_path: Path) -> None:
    run(_case_fetch_url_reports_the_final_url(tmp_path))


async def _case_fetch_url_caps_the_body_and_text(tmp_path: Path) -> None:
    upstream = Upstream().route(
        "http://public.example/big",
        httpx.Response(200, content=b"a" * (3 * 1024 * 1024), headers={"content-type": "text/plain"}),
    )
    settings = settings_for(tmp_path, upstream)
    with net.override_resolver(public_resolver()):
        text = await call(settings, upstream, "fetch_url", {"url": "http://public.example/big", "max_chars": 99999})
    assert "已截断" in text
    assert len(text) < 20_500

def test_fetch_url_caps_the_body_and_text(tmp_path: Path) -> None:
    run(_case_fetch_url_caps_the_body_and_text(tmp_path))


async def _case_fetch_url_plain_text_passthrough(tmp_path: Path) -> None:
    upstream = Upstream().route(
        "http://public.example/raw.txt",
        httpx.Response(200, text="纯文本内容", headers={"content-type": "text/plain"}),
    )
    settings = settings_for(tmp_path, upstream)
    with net.override_resolver(public_resolver()):
        text = await call(settings, upstream, "fetch_url", {"url": "http://public.example/raw.txt"})
    assert "纯文本内容" in text
    assert "地址：http://public.example/raw.txt" in text


def test_fetch_url_plain_text_passthrough(tmp_path: Path) -> None:
    run(_case_fetch_url_plain_text_passthrough(tmp_path))