"""HTTP 客户端抽象与 httpx 实现。

设计要点（依赖注入）：
- HttpClient 是协议（Protocol），服务层只依赖它，不直接 import httpx；
- 测试时注入 FakeClient 即可，无需真实网络；
- 所有网络异常统一转成 ExternalServiceError，业务层只捕获这一种。
"""

from __future__ import annotations

import json as _json
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

import httpx

from .errors import ExternalServiceError

# 默认请求头：伪装常规浏览器，降低被上游搜索源拦截的概率。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class HttpResponse:
    """统一封装的 HTTP 响应，屏蔽底层库差异。"""

    __slots__ = ("status", "headers", "body", "url")

    def __init__(
        self,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        url: str,
    ) -> None:
        self.status = status
        # 键统一转小写，查找时大小写不敏感（HTTP 头名本就大小写无关）
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body
        self.url = url

    def text(self, encoding: Optional[str] = None) -> str:
        """解码响应体为文本。未指定编码时优先按响应头 charset，否则 UTF-8 容错。"""
        if encoding:
            return self.body.decode(encoding, errors="replace")
        charset = ""
        content_type = self.headers.get("content-type", "")
        for part in content_type.split(";"):
            part = part.strip()
            if part.lower().startswith("charset="):
                charset = part.split("=", 1)[1].strip().strip('"')
                break
        try:
            return self.body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.text())


@runtime_checkable
class HttpClient(Protocol):
    """服务层依赖的 HTTP 客户端抽象。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        allow_redirects: bool = True,
    ) -> HttpResponse: ...

    async def get_json(self, url: str, **kwargs: Any) -> Any: ...

    async def get_text(self, url: str, **kwargs: Any) -> str: ...

    async def get_bytes(self, url: str, **kwargs: Any) -> bytes: ...

    async def head_ok(self, url: str, **kwargs: Any) -> bool: ...

    async def aclose(self) -> None: ...


class HttpxClient:
    """基于 httpx 的 HttpClient 实现（生产用）。

    每次请求使用独立的 ClientSession，避免共享连接导致的
    连接复用异常；请求完成后即关闭。
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        proxy: Optional[str] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        verify: bool = True,
    ) -> None:
        self._timeout = timeout
        self._proxy = proxy or None
        # httpx 不允许 header 值为 None：配置留空时回落到默认 UA
        self._user_agent = str(user_agent).strip() if user_agent else DEFAULT_USER_AGENT
        self._user_agent = self._user_agent or DEFAULT_USER_AGENT
        self._verify = verify

    def _build_headers(self, headers: Optional[Mapping[str, str]]) -> dict[str, str]:
        merged = {"User-Agent": self._user_agent, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        if headers:
            merged.update({str(k): str(v) for k, v in headers.items()})
        return merged

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = dict(params)
        if self._proxy:
            kwargs["proxy"] = self._proxy
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                verify=self._verify,
                follow_redirects=allow_redirects,
            ) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=self._build_headers(headers),
                    **kwargs,
                )
                final_url = str(resp.url)
                return HttpResponse(
                    status=resp.status_code,
                    headers=dict(resp.headers.items()),
                    body=resp.content,
                    url=final_url,
                )
        except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
            raise ExternalServiceError(f"请求 {url} 失败：{exc}") from exc

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", url, **kwargs)
        return resp.json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        resp = await self.request("GET", url, **kwargs)
        return resp.text()

    async def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        resp = await self.request("GET", url, **kwargs)
        return resp.body

    async def head_ok(self, url: str, **kwargs: Any) -> bool:
        """HEAD 探测链接是否可达（2xx/3xx 视为有效）。"""
        try:
            resp = await self.request("HEAD", url, **kwargs)
        except ExternalServiceError:
            return False
        return 200 <= resp.status < 400

    async def aclose(self) -> None:
        """无状态客户端，无需清理。"""


def build_http_client(
    *,
    timeout: float = 15.0,
    proxy: Optional[str] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    verify: bool = True,
) -> HttpxClient:
    """便捷工厂：按配置构造生产客户端。"""
    return HttpxClient(
        timeout=timeout,
        proxy=proxy,
        user_agent=user_agent,
        verify=verify,
    )
