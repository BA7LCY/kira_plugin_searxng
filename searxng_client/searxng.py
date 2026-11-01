"""SearxngService：与 SearXNG 实例交互的核心服务。

职责：
- 按分类策略组装请求（主分类 + 回退分类）；
- 调用 SearXNG JSON 接口（format=json）；
- 结果后处理：引擎过滤、去重、域名加分排序；
- 图片分类：候选数量拉取 → 过滤 → 探测 → 排序 → 截断；
- 可选 TTL 缓存（相同关键词短时间命中缓存，降低请求频率）。

依赖全部通过构造参数注入（HTTP 客户端、缓存、图片服务、时钟），
不 import 任何 core.*，方便 mock 测试与迁移。
"""

from __future__ import annotations

from .cache import TtlCache
from .errors import ExternalServiceError, InvalidQueryError
from .http import HttpClient
from .images import ImageService
from .models import SearchResult, SearchResultItem
from .ranking import deduplicate_results, filter_by_engines, rank_text_results
from .strategy import strategy_for

# 查询关键词长度上限（防滥用）
MAX_QUERY_LENGTH = 200

# 图片分类固定使用的引擎候选（同时发给 SearXNG 做前置过滤，本地再按引擎名复核一次）
DEFAULT_IMAGE_ENGINES = ("google cse images", "google images", "bing images")


class SearxngService:
    """SearXNG 搜索服务。"""

    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        *,
        image_candidate_limit: int = 120,
        image_result_limit: int = 8,
        cache: TtlCache | None = None,
        lang: str | None = None,
        image_service: ImageService | None = None,
    ) -> None:
        self._http = http
        self._base_url = (base_url or "http://127.0.0.1:8080").rstrip("/")
        self._image_candidate_limit = max(1, int(image_candidate_limit))
        self._image_result_limit = max(1, int(image_result_limit))
        self._cache = cache
        self._lang = lang
        self._image_service = image_service or ImageService(http)

    # ---- 对外 API ----

    async def search(
        self,
        query: str,
        category: str = "general",
        *,
        limit: int = 5,
        engines: list[str] | tuple[str, ...] | None = None,
        lang: str | None = None,
    ) -> SearchResult:
        """文字搜索。category 为 'images' 时自动转图片搜索。"""
        query = self._validate_query(query)
        if category == "images":
            return await self.search_images(query, engines=engines, limit=limit, lang=lang)

        cache_key = self._text_cache_key(query, category, limit, engines)
        if self._cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        strategy = strategy_for(category)
        result = await self._search_text(query, strategy, limit, engines, lang)
        if self._cache and not result.error_message:
            self._cache.set(cache_key, result)
        return result

    async def search_images(
        self,
        query: str,
        *,
        engines: list[str] | tuple[str, ...] | None = None,
        limit: int | None = None,
        lang: str | None = None,
    ) -> SearchResult:
        """图片搜索：拉候选 → 过滤 → 探测 → 排序 → 截断。"""
        query = self._validate_query(query)
        result_limit = max(1, int(limit or self._image_result_limit))
        candidate = max(result_limit, self._image_candidate_limit)

        cache_key = self._image_cache_key(query, result_limit, engines)
        if self._cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        engines = engines or DEFAULT_IMAGE_ENGINES
        raw = await self._request_search(
            query,
            "images",
            candidate,
            lang=lang or "zh",
            engines=engines,
        )
        if raw.error_message:
            return raw

        items = await self._image_service.filter_image_items(
            raw.results,
            engines=engines,
            limit=result_limit,
            query=query,
        )
        result = SearchResult.from_iterable(items)
        if self._cache:
            self._cache.set(cache_key, result)
        return result

    # ---- 内部 ----

    def _validate_query(self, query: str) -> str:
        text = (query or "").strip()
        if not text:
            raise InvalidQueryError("搜索关键词不能为空。")
        if len(text) > MAX_QUERY_LENGTH:
            raise InvalidQueryError(f"搜索关键词太长（最多 {MAX_QUERY_LENGTH} 个字符）。")
        return text

    def _text_cache_key(
        self,
        query: str,
        category: str,
        limit: int,
        engines: list[str] | tuple[str, ...] | None,
    ) -> str:
        engine_part = ",".join(sorted(engines)) if engines else ""
        return f"text|{category}|{limit}|{engine_part}|{query}"

    def _image_cache_key(
        self,
        query: str,
        limit: int,
        engines: list[str] | tuple[str, ...] | None,
    ) -> str:
        engine_part = ",".join(sorted(engines)) if engines else ""
        return f"image|{limit}|{engine_part}|{query}"

    async def _search_text(
        self,
        query: str,
        strategy,
        limit: int,
        engines: list[str] | tuple[str, ...] | None,
        lang: str | None,
    ) -> SearchResult:
        """按主分类 + 回退顺序尝试，直到拿到结果。"""
        categories = [strategy.primary_category, *strategy.fallback_categories]
        last_error = ""
        for category in categories:
            raw = await self._request_search(query, category, limit, lang)
            if raw.error_message:
                # 请求层面失败：尝试下一个回退分类（错误信息留到最后兜底）
                last_error = raw.error_message
                continue
            filtered = raw.results
            if engines:
                filtered = filter_by_engines(filtered, engines)
            if not filtered:
                continue
            ranked = rank_text_results(filtered, strategy.ranking_profile)
            deduped = deduplicate_results(ranked)
            # SearXNG 会无视 limit 返回一大把结果，这里按调用方要求截断
            return SearchResult.from_iterable(deduped).limited(limit)
        if last_error:
            return SearchResult.failed(last_error)
        return SearchResult()

    async def _request_search(
        self,
        query: str,
        category: str,
        limit: int,
        lang: str | None,
        engines: list[str] | tuple[str, ...] | None = None,
    ) -> SearchResult:
        """向 SearXNG 发一次 JSON 请求并解析。"""
        params: dict[str, object] = {
            "q": query,
            "categories": category,
            "format": "json",
            "limit": limit,
        }
        if lang and lang != "auto":
            params["lang"] = lang
        if engines:
            params["engines"] = ",".join(engines)
        url = f"{self._base_url}/search"
        try:
            resp = await self._http.request("GET", url, params=params)
        except ExternalServiceError as exc:
            return SearchResult.failed(f"无法连接搜索服务：{exc}")

        if resp.status == 403:
            return SearchResult.failed(
                "搜索服务拒绝了请求（403）。请确认 SearXNG 实例的设置里勾选了 JSON 输出格式（Settings → Search → Formats 勾选 JSON）。"
            )
        if resp.status != 200:
            return SearchResult.failed(f"搜索服务返回异常状态码 {resp.status}，请检查搜索服务是否正常。")
        try:
            data = resp.json()
        except ValueError:
            return SearchResult.failed("搜索服务返回的不是 JSON 数据，请确认已开启 JSON 输出格式。")

        if not isinstance(data, dict):
            return SearchResult()
        if data.get("error"):
            return SearchResult.failed(f"搜索服务返回错误：{data['error']}")

        items = [
            SearchResultItem.from_mapping(item)
            for item in data.get("results", [])
            if isinstance(item, dict)
        ]
        return SearchResult.from_iterable(items)
