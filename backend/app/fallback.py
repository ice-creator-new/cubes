"""Deciding whether a failed model call is worth retrying on the workspace backup model.

The retry only makes sense for a fault that says the primary service is down, overloaded or
out of quota — not for a request the provider rejected (a bad key, a bad payload). The error
classes carry a Chinese sentence, so this reads that sentence: the HTTP status the gateway
printed, the connection wording the adapter chose, or a quota phrase in the upstream body.
"""

from __future__ import annotations

import re
from typing import Any

# Phrases a provider puts in a quota / billing refusal, whatever HTTP status it chose.
QUOTA_HINTS = (
    "insufficient",
    "quota",
    "billing",
    "credit",
    "balance",
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded",
    "capacity",
    "余额",
    "额度",
    "配额",
    "欠费",
    "限流",
)

_HTTP = re.compile(r"HTTP\s+(\d{3})")


def fallback_reason(error: Any) -> str | None:
    """A short Chinese reason when this fault may be retried elsewhere, else ``None``.

    ``None`` means the failure is the request's own (4xx auth / bad request) and another
    model would only hide it, so the turn fails exactly as it did before fallbacks existed.
    """
    text = str(error or "")
    lowered = text.lower()
    if any(hint in lowered for hint in QUOTA_HINTS):
        return "额度不足或被限流"
    match = _HTTP.search(text)
    if match is not None:
        status = int(match.group(1))
        if status == 429:
            return "请求被限流"
        if status >= 500:
            return f"服务返回 HTTP {status}"
        # Any other status is the request's own: do not paper over a bad key or bad body.
        return None
    if "无法连接模型服务" in text:
        return "连接失败"
    if "服务未就绪" in text:
        return "服务未就绪"
    if "没有回应" in text or "超时" in text or "timed out" in lowered:
        return "响应超时"
    if "连接" in text or "断开" in text or "broken" in lowered:
        return "连接中断"
    return None
