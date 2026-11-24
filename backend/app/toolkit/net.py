"""Network helpers: the SSRF guard, the HTML-to-text reader and the injectable resolver.

The resolver is a module-level seam so tests can answer DNS without touching the real
network; the HTTP client itself is always supplied by the caller.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import contextmanager
from html.parser import HTMLParser
from typing import Callable, Iterator, Sequence
from urllib.parse import urljoin, urlsplit

from .base import ToolInputError

Resolver = Callable[[str], Sequence[str]]

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 20.0
ALLOWED_SCHEMES = ("http", "https")
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def _system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ToolInputError(f"无法解析地址「{host}」。") from exc
    addresses: list[str] = []
    for info in infos:
        value = str(info[4][0])
        if value not in addresses:
            addresses.append(value)
    return addresses


_resolver: Resolver = _system_resolver


def resolve_host(host: str) -> list[str]:
    return list(_resolver(host))


@contextmanager
def override_resolver(resolver: Resolver | None) -> Iterator[None]:
    """Swap the resolver for the duration of the block (tests only)."""
    global _resolver
    previous = _resolver
    _resolver = resolver or _system_resolver
    try:
        yield
    finally:
        _resolver = previous


def is_public_address(value: str) -> bool:
    """False for loopback, private, link-local, multicast, reserved and unspecified IPs."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def ensure_public_url(url: str) -> str:
    """Reject anything that is not a plain http(s) URL pointing at a public address."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ToolInputError(f"只支持 http 和 https 地址，收到「{parts.scheme or url}」。")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ToolInputError(f"地址「{url}」缺少主机名。")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise ToolInputError(f"地址「{host}」指向本机或内网，已拒绝访问。")
    try:
        parts.port
    except ValueError as exc:
        raise ToolInputError(f"地址「{url}」的端口无效。") from exc
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal is not None else resolve_host(host)
    if not addresses:
        raise ToolInputError(f"无法解析地址「{host}」。")
    for address in addresses:
        if not is_public_address(address):
            raise ToolInputError(f"地址「{host}」解析到内网或保留地址，已拒绝访问。")
    return url


class _TextExtractor(HTMLParser):
    """Readable text from HTML: drops script/style/nav/footer, keeps structure."""

    SKIP = frozenset({"script", "style", "nav", "footer", "noscript", "template", "svg", "iframe"})
    BLOCK = frozenset(
        {
            "p", "div", "br", "li", "ul", "ol", "table", "tr", "td", "th", "section",
            "article", "header", "main", "aside", "blockquote", "pre", "hr", "form",
            "h1", "h2", "h3", "h4", "h5", "h6", "figure", "figcaption", "dl", "dt", "dd",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.lines: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._line: list[str] = []
        self._link: str | None = None
        self._link_text: list[str] = []

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def _flush(self) -> None:
        line = "".join(self._line).strip()
        if line:
            self.lines.append(line)
        self._line = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._link = href if href.startswith(("http://", "https://")) else None
            self._link_text = []
            return
        if tag == "li":
            self._flush()
            self._line.append("- ")
        elif tag == "br":
            self._flush()
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in self.SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            text = " ".join("".join(self._link_text).split())
            if text:
                suffix = f" ({self._link})" if self._link else ""
                if self._line and not self._line[-1].endswith(" "):
                    self._line.append(" ")
                self._line.append(f"{text}{suffix} ")
            self._link = None
            return
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._link is not None:
            self._link_text.append(text)
        else:
            self._line.append(f"{text} ")

    def text(self) -> str:
        self._flush()
        collapsed: list[str] = []
        for line in self.lines:
            if line and (not collapsed or collapsed[-1] != line):
                collapsed.append(line)
        return "\n".join(collapsed)


def html_to_text(markup: str) -> tuple[str, str]:
    """``(title, text)`` for an HTML document; never raises on malformed markup."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is lenient; keep the partial text
        pass
    return parser.title, parser.text()


def _looks_like_html(body: bytes, content_type: str) -> bool:
    if "html" in content_type:
        return True
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def read_url(client, url: str, *, max_chars: int) -> tuple[str, str, str]:
    """Fetch ``url`` with the SSRF guard, returning ``(title, final_url, text)``."""
    current = ensure_public_url(url)
    body = b""
    truncated_body = False
    content_type = ""
    final_url = current
    for _ in range(MAX_REDIRECTS + 1):
        next_url: str | None = None
        async with client.stream(
            "GET",
            current,
            follow_redirects=False,
            timeout=FETCH_TIMEOUT,
            headers={
                "accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "user-agent": "Cubes/2.0 (+toolkit)",
            },
        ) as response:
            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ToolInputError("重定向缺少目标地址，已停止。")
                next_url = urljoin(current, location)
            elif status >= 400:
                raise ToolInputError(f"目标地址返回 HTTP {status}。")
            else:
                final_url = str(response.url)
                content_type = response.headers.get("content-type", "")
                collected = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(collected) >= MAX_BODY_BYTES:
                        truncated_body = True
                        break
                    room = MAX_BODY_BYTES - len(collected)
                    if len(chunk) > room:
                        collected.extend(chunk[:room])
                        truncated_body = True
                        break
                    collected.extend(chunk)
                body = bytes(collected)
        if next_url is None:
            break
        current = ensure_public_url(next_url)
    else:
        raise ToolInputError("重定向次数过多，已停止。")

    raw = _decode(body, content_type)
    if _looks_like_html(body, content_type):
        title, text = html_to_text(raw)
        if not title:
            title = "（无标题）"
    else:
        title, text = "", raw.strip()

    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（已截断）"
    elif truncated_body:
        text = text + "\n…（内容过大，已截断）"
    return title, final_url, text
