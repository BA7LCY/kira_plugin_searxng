"""URL 工具：http 链接校验。"""

from __future__ import annotations

from urllib.parse import urlparse


def is_http_url(url: str) -> bool:
    """判断是否为 http/https 链接（有 scheme 且有主机名）。"""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
