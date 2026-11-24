"""Pack A — web: a sequential search chain plus a guarded URL reader."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .base import (
    MAX_RESULT_CHARS,
    SEARCH_RESULT_CHARS,
    ToolContext,
    ToolInputError,
    ToolResult,
    ToolSpec,
    cap_text,
    page_detail,
    search_detail,
)
from .net import read_url

PROVIDER_TIMEOUT = 20.0
DEFAULT_RESULTS = 5
MAX_RESULTS = 10
SNIPPET_CHARS = 300
MAX_TITLE_CHARS = 200
RECENCY_VALUES = ("day", "week", "month", "year")
RECENCY_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
# Recency hints understood by each provider; ``None`` means the provider cannot express it.
BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
DUCKDUCKGO_DF = {"day": "d", "week": "w", "month": "m", "year": "y"}

SEARCH_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "description": "搜索词，用最关键的几个词。"},
        "recency": {
            "type": "string",
            "enum": list(RECENCY_VALUES),
            "description": "只看最近一段时间的内容，可省略。",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_RESULTS,
            "description": f"返回条数，默认 {DEFAULT_RESULTS}，最多 {MAX_RESULTS}。",
        },
    },
    "required": ["query"],
}

FETCH_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "url": {"type": "string", "description": "要读取的 http 或 https 地址。"},
        "max_chars": {
            "type": "integer",
            "minimum": 500,
            "maximum": 20_000,
            "description": "返回正文字符上限，默认 8000。",
        },
    },
    "required": ["url"],
}


class _ProviderError(Exception):
    """One provider failed; the chain records the reason and moves on."""


# ------------------------------------------------------------- argument checks


def _string_arg(args: dict, key: str, *, limit: int, field: str) -> str:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ToolInputError(f"{field}不能为空。")
    text = raw.strip()
    if len(text) > limit:
        raise ToolInputError(f"{field}不能超过 {limit} 个字符。")
    return text


def _int_arg(args: dict, key: str, *, default: int, minimum: int, maximum: int, field: str) -> int:
    if key not in args or args[key] is None:
        return default
    raw = args[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolInputError(f"{field}必须是整数。")
    value = int(raw)
    if value < minimum or value > maximum:
        raise ToolInputError(f"{field}必须在 {minimum} 到 {maximum} 之间。")
    return value


def _clamped_int(
    args: dict, key: str, *, default: int, minimum: int, maximum: int, field: str
) -> int:
    """Accept any number and clamp it into ``[minimum, maximum]`` instead of failing."""
    if key not in args or args[key] is None:
        return default
    raw = args[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolInputError(f"{field}必须是整数。")
    return max(minimum, min(maximum, int(raw)))


def _recency_arg(args: dict) -> str | None:
    raw = args.get("recency")
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or raw not in RECENCY_VALUES:
        raise ToolInputError("recency 只能是 day、week、month 或 year。")
    return raw


# ------------------------------------------------------------------- the chain


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _first_string(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)):
            pieces: list[str] = []
            for entry in value:
                if isinstance(entry, str) and entry.strip():
                    pieces.append(entry.strip())
                elif isinstance(entry, dict):
                    for sub in ("text", "snippet", "content", "excerpt", "summary"):
                        nested = entry.get(sub)
                        if isinstance(nested, str) and nested.strip():
                            pieces.append(nested.strip())
                            break
            if pieces:
                return " ".join(pieces)
        if isinstance(value, dict):
            for sub in ("text", "snippet", "content", "excerpt"):
                nested = value.get(sub)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _normalise(item: dict) -> dict | None:
    url = item.get("url") or item.get("link") or item.get("href")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    title = _collapse(_first_string(item, ("title", "name", "heading", "text")))[:MAX_TITLE_CHARS]
    snippet = _collapse(
        _first_string(item, ("snippet", "excerpt", "excerpts", "summary", "description", "content", "text", "highlights"))
    )[:SNIPPET_CHARS]
    published = _collapse(
        _first_string(item, ("published", "published_date", "publishedDate", "published_at", "date", "age"))
    )[:40]
    return {
        "title": title or url,
        "url": url,
        "snippet": snippet,
        "published": published or None,
    }


def _walk_url_dicts(value: Any, out: list[dict]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("url"), str) and value["url"].startswith(("http://", "https://")):
            out.append(value)
            return
        for item in value.values():
            _walk_url_dicts(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_url_dicts(item, out)


def _collect_texts(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and key in ("text", "content", "output"):
                if item.strip():
                    out.append(item)
            else:
                _collect_texts(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_texts(item, out)


_TEXT_PAIR = re.compile(
    r"(?:^|\n)\s*(?:title|标题)[:：]\s*(?P<title>[^\n]+)\n\s*(?:url|link|网址|链接)[:：]\s*(?P<url>https?://\S+)",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")


def _records_from_text(text: str) -> list[dict]:
    """Exa-style ``Title:/URL:`` blocks first, then markdown links."""
    records: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        title = url = published = None
        snippet: list[str] = []
        for line in lines:
            key, sep, value = line.partition(":")
            lowered = key.strip().lower()
            value = value.strip()
            if sep and lowered in ("title", "标题"):
                title = value
                continue
            if sep and lowered in ("url", "link", "网址", "链接") and value.startswith("http"):
                url = value
                continue
            if sep and lowered in ("published", "published date", "publisheddate", "date", "时间"):
                published = value
                continue
            if sep and lowered in ("highlights", "highlight", "摘要", "内容"):
                continue
            if url and line.startswith("http"):
                continue
            snippet.append(line)
        if not url:
            match = _TEXT_PAIR.search(block)
            if match:
                title, url = match.group("title").strip(), match.group("url").strip()
        if url:
            records.append(
                {
                    "title": title or url,
                    "url": url,
                    "snippet": " ".join(snippet),
                    "published": published,
                }
            )
    if not records:
        for match in _MARKDOWN_LINK.finditer(text):
            records.append(
                {
                    "title": match.group(1).strip(),
                    "url": match.group(2).strip(),
                    "snippet": "",
                    "published": None,
                }
            )
    return records


def _results_from_payload(payload: Any) -> list[dict]:
    """Normalised sources from any provider shape, including MCP text blobs."""
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        detail = error.get("message") if isinstance(error, dict) else str(error)
        raise _ProviderError(_collapse(str(detail or "服务返回错误"))[:120])
    raw: list[dict] = []
    _walk_url_dicts(payload, raw)
    results = [item for item in (_normalise(entry) for entry in raw) if item]
    if results:
        return results
    texts: list[str] = []
    _collect_texts(payload, texts)
    for text in texts:
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                nested = json.loads(stripped)
            except json.JSONDecodeError:
                nested = None
            if nested is not None:
                found: list[dict] = []
                _walk_url_dicts(nested, found)
                if found:
                    results.extend(item for item in (_normalise(e) for e in found) if item)
                    continue
        results.extend(_records_from_text(text))
    return results


def _json_or_sse(text: str, content_type: str) -> Any:
    stripped = text.lstrip()
    if "event-stream" in content_type or stripped.startswith(("event:", "data:")):
        events: list[Any] = []
        for frame in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(
                line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
            )
            if not data or data.strip() == "[DONE]":
                continue
            try:
                events.append(json.loads(data))
            except json.JSONDecodeError:
                continue
        if not events:
            raise _ProviderError("返回的内容无法解析")
        return events
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _ProviderError("返回的内容无法解析") from exc


async def _read_json(response) -> Any:
    if response.status_code >= 400:
        raise _ProviderError(f"HTTP {response.status_code}")
    return _json_or_sse(response.text, response.headers.get("content-type", ""))


async def _mcp_call(ctx: ToolContext, url: str, tool: str, arguments: dict) -> list[dict]:
    response = await ctx.http.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
        timeout=PROVIDER_TIMEOUT,
    )
    payload = await _read_json(response)
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), payload)
    results = _results_from_payload(payload)
    if not results:
        raise _ProviderError("没有返回可用结果")
    return results


async def _parallel(ctx: ToolContext, query: str, limit: int, recency: str | None) -> list[dict]:
    settings = ctx.settings
    if settings.parallel_api_key:
        response = await ctx.http.post(
            settings.parallel_api_url,
            json={"objective": query, "search_queries": [query], "max_results": limit},
            headers={"x-api-key": settings.parallel_api_key, "content-type": "application/json"},
            timeout=PROVIDER_TIMEOUT,
        )
        results = _results_from_payload(await _read_json(response))
        if not results:
            raise _ProviderError("没有返回可用结果")
        return results
    return await _mcp_call(
        ctx,
        settings.parallel_mcp_url,
        "web_search",
        {"objective": query, "search_queries": [query]},
    )


async def _exa(ctx: ToolContext, query: str, limit: int, recency: str | None) -> list[dict]:
    settings = ctx.settings
    if settings.exa_api_key:
        body: dict[str, Any] = {
            "query": query,
            "numResults": limit,
            "contents": {"text": {"maxCharacters": SNIPPET_CHARS}},
        }
        if recency:
            start = datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS[recency])
            body["startPublishedDate"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        response = await ctx.http.post(
            settings.exa_api_url,
            json=body,
            headers={"x-api-key": settings.exa_api_key, "content-type": "application/json"},
            timeout=PROVIDER_TIMEOUT,
        )
        results = _results_from_payload(await _read_json(response))
        if not results:
            raise _ProviderError("没有返回可用结果")
        return results
    return await _mcp_call(
        ctx, settings.exa_mcp_url, "web_search_exa", {"query": query, "numResults": limit}
    )


async def _tavily(ctx: ToolContext, query: str, limit: int, recency: str | None) -> list[dict]:
    body: dict[str, Any] = {"query": query, "max_results": limit, "search_depth": "basic"}
    if recency:
        body["topic"] = "news"
        body["days"] = RECENCY_DAYS[recency]
    response = await ctx.http.post(
        ctx.settings.tavily_api_url,
        json=body,
        headers={
            "authorization": f"Bearer {ctx.settings.tavily_api_key}",
            "content-type": "application/json",
        },
        timeout=PROVIDER_TIMEOUT,
    )
    results = _results_from_payload(await _read_json(response))
    if not results:
        raise _ProviderError("没有返回可用结果")
    return results


async def _brave(ctx: ToolContext, query: str, limit: int, recency: str | None) -> list[dict]:
    params: dict[str, Any] = {"q": query, "count": limit}
    if recency:
        params["freshness"] = BRAVE_FRESHNESS[recency]
    response = await ctx.http.get(
        ctx.settings.brave_api_url,
        params=params,
        headers={
            "x-subscription-token": ctx.settings.brave_api_key,
            "accept": "application/json",
        },
        timeout=PROVIDER_TIMEOUT,
    )
    results = _results_from_payload(await _read_json(response))
    if not results:
        raise _ProviderError("没有返回可用结果")
    return results


class _DuckDuckGoParser(HTMLParser):
    """The no-JavaScript result page: title links plus snippet blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "result__a" in classes and attributes.get("href"):
            self._flush()
            self._capture = "title"
            self._buffer = []
            self._href = attributes["href"] or ""
        elif "result__snippet" in classes and self.results:
            self._flush()
            self._capture = "snippet"
            self._buffer = []
        elif self._capture is not None and tag in ("br", "p"):
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None and tag in ("a", "div", "td", "span"):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)

    def _flush(self) -> None:
        text = _collapse("".join(self._buffer))
        if self._capture == "title" and text:
            self.results.append({"title": text, "url": _unwrap_duckduckgo(self._href), "snippet": ""})
        elif self._capture == "snippet" and text and self.results:
            self.results[-1]["snippet"] = text
        self._capture = None
        self._buffer = []


def _unwrap_duckduckgo(href: str) -> str:
    url = href.strip()
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return url


async def _duckduckgo(ctx: ToolContext, query: str, limit: int, recency: str | None) -> list[dict]:
    data = {"q": query}
    if recency:
        data["df"] = DUCKDUCKGO_DF[recency]
    response = await ctx.http.post(
        ctx.settings.duckduckgo_url,
        data=data,
        headers={
            "accept": "text/html",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        },
        timeout=PROVIDER_TIMEOUT,
    )
    if response.status_code >= 400:
        raise _ProviderError(f"HTTP {response.status_code}")
    html = response.text
    lowered = html.lower()
    if "anomaly" in lowered or "bots use duckduckgo" in lowered or "captcha" in lowered:
        raise _ProviderError("被搜索引擎拦截，需要验证")
    parser = _DuckDuckGoParser()
    parser.feed(html)
    parser.close()
    results = [item for item in (_normalise(entry) for entry in parser.results) if item]
    if not results:
        raise _ProviderError("没有返回可用结果")
    return results


Provider = Callable[[ToolContext, str, int, str | None], Awaitable[list[dict]]]
PROVIDERS: dict[str, tuple[Provider, Callable[[Any], bool]]] = {
    "parallel": (_parallel, lambda settings: True),
    "exa": (_exa, lambda settings: True),
    "tavily": (_tavily, lambda settings: bool(settings.tavily_api_key)),
    "brave": (_brave, lambda settings: bool(settings.brave_api_key)),
    "duckduckgo": (_duckduckgo, lambda settings: True),
}


async def search_chain(
    ctx: ToolContext, query: str, *, limit: int, recency: str | None
) -> tuple[str, list[dict]]:
    """First provider with usable sources wins; every failure is remembered."""
    failures: list[str] = []
    attempted = 0
    settings = ctx.settings
    for name in settings.search_providers:
        entry = PROVIDERS.get(name)
        if entry is None:  # pragma: no cover - settings already filters unknown names
            continue
        provider, usable = entry
        if not usable(settings):
            continue
        attempted += 1
        try:
            results = await provider(ctx, query, limit, recency)
        except _ProviderError as exc:
            failures.append(f"{name}（{exc}）")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad provider must not end the chain
            failures.append(f"{name}（{type(exc).__name__}: {_collapse(str(exc))[:100]}）")
            continue
        results = _unique_by_url(results)
        if results:
            return name, results[:limit]
        failures.append(f"{name}（没有返回可用结果）")
    if not attempted:  # pragma: no cover - parallel/exa/duckduckgo are always available
        raise ToolInputError("没有可用的搜索服务，请在 backend/.env 里配置 SEARCH_PROVIDERS。")
    raise ToolInputError(f"搜索失败：{'；'.join(failures)}。")


def _unique_by_url(results: list[dict]) -> list[dict]:
    """Drop repeats of the same page: providers often return one URL several times."""
    seen: set[str] = set()
    unique: list[dict] = []
    for item in results:
        key = item["url"].split("#", 1)[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def format_results(provider: str, query: str, results: list[dict]) -> str:
    lines = [f"找到 {len(results)} 条结果："]
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item['title']}")
        lines.append(f" {item['url']}")
        snippet = item.get("snippet") or ""
        published = item.get("published")
        if snippet or published:
            suffix = f"（{published}）" if published else ""
            lines.append(f" {snippet}{suffix}".rstrip())
    lines.append(f"来源：{provider}，查询「{query}」")
    return "\n".join(lines)


# ---------------------------------------------------------------- tool entries


async def run_web_search(ctx: ToolContext, args: dict) -> ToolResult:
    query = _string_arg(args, "query", limit=300, field="搜索词")
    recency = _recency_arg(args)
    limit = _int_arg(args, "limit", default=DEFAULT_RESULTS, minimum=1, maximum=MAX_RESULTS, field="返回条数")
    provider, results = await search_chain(ctx, query, limit=limit, recency=recency)
    return ToolResult(
        cap_text(format_results(provider, query, results), SEARCH_RESULT_CHARS),
        search_detail(query, provider, results),
    )


async def run_fetch_url(ctx: ToolContext, args: dict) -> ToolResult:
    url = _string_arg(args, "url", limit=2_000, field="地址")
    max_chars = _clamped_int(
        args, "max_chars", default=8_000, minimum=500, maximum=20_000, field="正文字符上限"
    )
    title, final_url, text = await read_url(ctx.http, url, max_chars=max_chars)
    body = f"标题：{title}\n地址：{final_url}\n\n{text}" if title else f"地址：{final_url}\n\n{text}"
    return ToolResult(
        cap_text(body, MAX_RESULT_CHARS),
        page_detail(final_url, title, text),
    )


def _summary_text(value: Any, limit: int) -> str:
    text = _collapse(str(value or ""))
    return text if len(text) <= limit else text[: limit - 1] + "…"


WEB_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="web_search",
        label="搜索网页",
        description=(
            "在互联网上搜索。需要最新信息、事实核查或你不确定的事情时先搜索，不要凭记忆猜测。"
            "返回标题、地址和摘要，需要全文再调用 fetch_url。"
        ),
        parameters=SEARCH_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_web_search,
        summary=lambda args: f"搜索「{_summary_text(args.get('query'), 40)}」",
    ),
    ToolSpec(
        name="fetch_url",
        label="读取网页",
        description=(
            "读取一个网页并转成可读正文。用于展开搜索结果的细节，或读取用户给出的链接。"
            "只支持公开的 http 和 https 地址，内网地址会被拒绝。"
        ),
        parameters=FETCH_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_fetch_url,
        summary=lambda args: f"读取 {_summary_text(args.get('url'), 60)}",
    ),
)
