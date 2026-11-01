"""图片结果的处理：去重、有效性探测、排序、下载。

有效性探测用 HEAD 请求确认图片链接真实可达（2xx/3xx），
避免把失效链接交给模型或用户。

排序看查询词：标题/摘要/地址里和关键词对得上的图优先，其次才看分数与尺寸；
同时剔除明显的图库站水印图（shutterstock、alamy 之类），
避免把带水印的素材图当成结果发出去。
"""

from __future__ import annotations

import asyncio
import base64
import re
import unicodedata
from urllib.parse import unquote

from .errors import ExternalServiceError
from .http import HttpClient, HttpResponse
from .models import SearchResultItem
from .ranking import filter_by_engines

# 图片尺寸文本，如 "1920x1080" / "1920×1080"
_RESOLUTION_RE = re.compile(r"(\d+)\s*[x×X]\s*(\d+)")

# 常见图片格式的文件头魔数 -> 扩展名（用于不依赖 Pillow 的真图校验）
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),  # 再校验 'WEBP'
)

# 图库站/水印图的特征词（出现在标题、摘要、页面地址或图片地址里）
_WATERMARK_MARKERS: tuple[str, ...] = (
    "watermark",
    "水印",
    "shutterstock",
    "alamy",
    "istockphoto",
    "gettyimages",
    "dreamstime",
    "123rf",
    "depositphotos",
    "freepik",
    "vecteezy",
)

# 关键词匹配用：中日韩文字、长度 ≥2 的英文数字词
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_ASCII_TOKEN_PATTERN = re.compile(r"[a-z0-9]{2,}")

# 相关度阈值与最少条数：达标的图够 2 张才只留它们，否则全留（避免把结果清空）
_MIN_RELEVANCE = 0.3
_MIN_RELEVANT_ITEMS = 2


def parse_resolution(resolution: str) -> tuple[int, int] | None:
    """把 '1920x1080' 或 '1920×1080' 解析为 (宽, 高)；解析失败返回 None。"""
    if not resolution:
        return None
    match = _RESOLUTION_RE.search(resolution)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def resolution_area(resolution: str) -> int:
    """图片分辨率面积；无法解析返回 0。"""
    dims = parse_resolution(resolution)
    return dims[0] * dims[1] if dims else 0


def _normalize_text(value: str) -> str:
    """归一化文本：全角转半角（NFKC）并转小写，便于比对。"""
    return unicodedata.normalize("NFKC", value or "").casefold()


def _ascii_tokens(value: str) -> set[str]:
    """取出长度 ≥2 的英文/数字词。"""
    return set(_ASCII_TOKEN_PATTERN.findall(_normalize_text(value)))


def _cjk_bigrams(value: str) -> set[str]:
    """把中日韩文字切成相邻两个字一组（bigram），用来衡量中文短词是否命中。"""
    characters = _CJK_PATTERN.findall(_normalize_text(value))
    return {
        characters[index] + characters[index + 1]
        for index in range(len(characters) - 1)
    }


def _result_text(item: SearchResultItem) -> str:
    """把标题、摘要、页面地址、图片地址拼成一段用于匹配的小写文本（URL 先解码）。"""
    return " ".join(
        _normalize_text(unquote(value))
        for value in (item.title, item.content, item.url, item.img_src)
    )


def image_relevance(query: str, item: SearchResultItem) -> float:
    """图片与查询词的字面相关度（0~1）。

    分别算英文词覆盖率与中日韩双字覆盖率，取平均：
    - 英文按长度 ≥2 的词比对，命中的词占查询词的比例即分数；
    - 中文/日文/韩文按相邻两个字切开比对，命中比例即分数。
    """
    query_text = _normalize_text(query)
    result_text = _result_text(item)
    scores: list[float] = []

    query_tokens = _ascii_tokens(query_text)
    if query_tokens:
        hit = len(query_tokens & _ascii_tokens(result_text))
        scores.append(hit / len(query_tokens))

    query_bigrams = _cjk_bigrams(query_text)
    if query_bigrams:
        hit = len(query_bigrams & _cjk_bigrams(result_text))
        scores.append(hit / len(query_bigrams))

    return sum(scores) / len(scores) if scores else 0.0


def is_likely_watermarked(item: SearchResultItem) -> bool:
    """标题/摘要/地址里带图库站或水印特征词时，认为很可能是带水印的素材图。"""
    text = _result_text(item)
    return any(marker in text for marker in _WATERMARK_MARKERS)


def deduplicate_image_results(items: list[SearchResultItem]) -> list[SearchResultItem]:
    """按图片直链去重（img_src 优先，回退 url），保留第一条；没有地址的条目丢弃。"""
    seen: set[str] = set()
    out: list[SearchResultItem] = []
    for item in items:
        key = item.img_src or item.url
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def rank_image_results(
    items: list[SearchResultItem],
    query: str | None = None,
) -> list[SearchResultItem]:
    """排序图片结果。

    - 不传查询词：按 (SearXNG 分数, 分辨率面积) 倒序；
    - 传查询词：按 (与查询的相关度, 分数, 面积) 倒序，并做两步清理：
      1. 只要还有没命中图库水印特征的图，就剔除命中的那些；
      2. 相关度达标的图够 2 张时只保留它们，否则全留（避免结果被清空）。
    """
    deduped = deduplicate_image_results(items)

    if not query:
        return sorted(
            deduped,
            key=lambda item: (item.score, resolution_area(item.resolution)),
            reverse=True,
        )

    scored = [(image_relevance(query, item), item) for item in deduped]
    scored.sort(
        key=lambda entry: (
            entry[0],
            entry[1].score,
            resolution_area(entry[1].resolution),
        ),
        reverse=True,
    )

    clean = [entry for entry in scored if not is_likely_watermarked(entry[1])]
    if clean:
        scored = clean

    relevant = [item for relevance, item in scored if relevance >= _MIN_RELEVANCE]
    if len(relevant) >= _MIN_RELEVANT_ITEMS:
        return relevant
    return [item for _, item in scored]


def is_image_bytes(data: bytes) -> bool:
    """按文件头魔数判断是否为常见图片格式。"""
    if not data or len(data) < 3:
        return False
    if data[:4] == b"RIFF":
        return data[8:12] == b"WEBP"
    return any(data.startswith(magic) for magic, _ in _IMAGE_MAGIC)


class ImageService:
    """图片有效性探测与下载（依赖注入 HTTP 客户端）。"""

    def __init__(
        self,
        http: HttpClient,
        *,
        max_concurrency: int = 8,
        max_download_depth: int = 2,
    ) -> None:
        self._http = http
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._max_depth = max(0, max_download_depth)

    async def filter_image_items(
        self,
        items: list[SearchResultItem],
        *,
        engines: list[str] | tuple[str, ...] | None = None,
        limit: int | None = None,
        query: str | None = None,
    ) -> list[SearchResultItem]:
        """过滤出有效图片结果：引擎过滤 → 去重 → HEAD 探测 → 排序 → 截断。"""
        by_engine = filter_by_engines(items, engines)
        deduped = deduplicate_image_results(by_engine)
        alive = await self._keep_alive(deduped)
        ranked = rank_image_results(alive, query=query)
        if limit is not None and limit > 0:
            ranked = ranked[:limit]
        return ranked

    async def _keep_alive(self, items: list[SearchResultItem]) -> list[SearchResultItem]:
        """并发 HEAD 探测，只保留链接可达的条目。"""
        async def check(item: SearchResultItem) -> bool:
            url = item.img_src or item.url
            if not url:
                return False
            async with self._semaphore:
                return await self._http.head_ok(url)

        checks = await asyncio.gather(*(check(item) for item in items))
        return [item for item, ok in zip(items, checks) if ok]

    async def download_bytes(
        self,
        url: str,
        *,
        depth: int = 0,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> bytes | None:
        """下载图片字节；失败/超限/非图片返回 None。"""
        if depth > self._max_depth:
            return None
        try:
            resp: HttpResponse = await self._http.request("GET", url)
        except ExternalServiceError:
            return None
        if not (200 <= resp.status < 400):
            return None
        if not resp.body or len(resp.body) > max_bytes:
            return None
        if is_image_bytes(resp.body):
            return resp.body
        # 拿到的是 HTML（图床的反盗链页）→ 找 og:image 再递归一次
        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type:
            return None
        og = self._extract_og_image(resp.text())
        if not og:
            return None
        return await self.download_bytes(og, depth=depth + 1, max_bytes=max_bytes)

    async def download_base64(
        self,
        url: str,
        *,
        depth: int = 0,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> str | None:
        """下载图片并返回 base64 数据（可直接塞进 Image 元素发送）。"""
        data = await self.download_bytes(url, depth=depth, max_bytes=max_bytes)
        if data is None:
            return None
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def _extract_og_image(html: str) -> str:
        """从 HTML 中提取 og:image 属性值（简易正则，够用于图床反盗链页）。"""
        match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
        if not match:
            match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, re.I)
        return match.group(1).strip() if match else ""
