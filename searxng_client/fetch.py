"""网页正文提取。

优先用 readability-lxml 提取可读正文（去掉导航、脚本、广告等噪音），
再截断到配置长度。

readability / beautifulsoup4 属于可选依赖：缺失时自动降级
（有 beautifulsoup4 就用它，没有就用标准库 html.parser），
这样依赖没装好也不会让插件加载失败。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

try:  # 可选依赖：缺失时降级，不影响插件加载
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - 只在依赖缺失时触发
    BeautifulSoup = None  # type: ignore[assignment]

try:  # 可选依赖：readability-lxml
    from readability import Document
except ImportError:  # pragma: no cover - 只在依赖缺失时触发
    Document = None  # type: ignore[assignment]

from .errors import ExternalServiceError, InvalidUrlError
from .http import HttpClient
from .url_helpers import is_http_url

# 依赖可用性（适配层会把结果写进日志，方便排查正文提取质量）
READABILITY_AVAILABLE = Document is not None
BS4_AVAILABLE = BeautifulSoup is not None

# 需要从正文里剔除的噪音标签
_NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside")

# 正文过短时认为提取失败，改用整页文字
_MIN_TEXT_LENGTH = 80


class _PlainTextParser(HTMLParser):
    """标准库兜底解析器：跳过噪音标签，只收集可见文字。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D102 - 标准库回调
        if tag in _NOISE_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):  # noqa: D102 - 标准库回调
        if tag in _NOISE_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):  # noqa: D102 - 标准库回调
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.chunks)).strip()


def _plain_text(html: str) -> str:
    """把 HTML 转成纯文本：优先 beautifulsoup4，缺失时用标准库解析器。"""
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True)).strip()

    parser = _PlainTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 解析异常不应该影响整个流程
        return ""
    return parser.text()


def _title_of(html: str) -> str:
    """取页面标题；没有 beautifulsoup4 时用正则。"""
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("title")
        return tag.get_text(strip=True) if tag else ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _meta_description(html: str) -> str:
    """取页面描述；没有 beautifulsoup4 时用正则。"""
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "description"})
        return meta["content"].strip() if meta and meta.get("content") else ""
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)',
        html,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


class WebFetchService:
    """网页正文提取服务（依赖注入 HTTP 客户端）。"""

    def __init__(self, http: HttpClient, *, max_chars: int = 3000) -> None:
        self._http = http
        self._max_chars = max(200, int(max_chars))

    async def fetch(self, url: str, *, max_chars: int | None = None) -> str:
        """抓取并提取网页正文。

        返回可读正文；不可读时返回带标题/描述的回退说明。
        始终截断到 max_chars（默认取构造时配置）。
        """
        if not is_http_url(url):
            raise InvalidUrlError("只支持 http:// 或 https:// 链接。")
        limit = self._max_chars if max_chars is None else max(200, int(max_chars))
        try:
            html = await self._http.get_text(url)
        except ExternalServiceError as exc:
            return f"网页抓取失败：{exc}"

        if not html or len(html) < 50:
            return "该网页没有可抓取的内容。"

        # 1) 优先 readability 提取正文；不可用或失败时留空
        summary = ""
        if Document is not None:
            try:
                summary = Document(html).summary()
            except Exception:  # noqa: BLE001 readability 解析失败不算错误，继续降级
                summary = ""

        text = _plain_text(summary) if summary else ""
        # 2) readability 不可用或提取过短 → 直接用整页去噪文字
        if len(text) < _MIN_TEXT_LENGTH:
            text = _plain_text(html)
        if len(text) >= _MIN_TEXT_LENGTH:
            return text[:limit]

        # 3) 仍然太短 → 退化为元信息摘要
        return self._build_metadata_fallback(html, _title_of(html), limit)

    # ---- 内部 ----

    def _build_metadata_fallback(self, html: str, title: str, limit: int) -> str:
        """正文提取失败时的回退：标题 + 描述 + 可见文字片段。"""
        description = _meta_description(html)
        visible = _plain_text(html)
        parts = []
        if title:
            parts.append(f"标题：{title}")
        if description:
            parts.append(f"描述：{description}")
        if visible:
            parts.append(f"页面文字：{visible[:400]}")
        if not parts:
            return "该网页内容可能是动态加载（需要浏览器渲染），无法直接提取正文。"
        return "\n".join(parts)[:limit]
