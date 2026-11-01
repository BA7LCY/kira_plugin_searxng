"""kira_plugin_searxng 服务层。

与 Kira 框架无关的纯 Python 库：搜索、图片、网页提取、缓存。
所有外部依赖（HTTP 客户端、时钟、缓存实现）通过构造函数注入，
便于 mock 测试，也便于迁移到其他机器人框架。
"""

from __future__ import annotations

from .cache import TtlCache
from .errors import (
    CacheError,
    ExternalServiceError,
    InvalidQueryError,
    InvalidUrlError,
    PluginError,
)
from .fetch import BS4_AVAILABLE, READABILITY_AVAILABLE, WebFetchService
from .format import to_image_list, to_tavily_json, to_text_list
from .http import HttpClient, HttpResponse, HttpxClient, build_http_client
from .images import ImageService
from .models import SearchResult, SearchResultItem
from .query import detect_query_language
from .ranking import deduplicate_results, rank_text_results
from .searxng import DEFAULT_IMAGE_ENGINES, SearxngService
from .strategy import CATEGORIES, strategy_for

__all__ = [
    "BS4_AVAILABLE",
    "CacheError",
    "CATEGORIES",
    "DEFAULT_IMAGE_ENGINES",
    "ExternalServiceError",
    "HttpClient",
    "HttpResponse",
    "HttpxClient",
    "build_http_client",
    "ImageService",
    "InvalidQueryError",
    "InvalidUrlError",
    "PluginError",
    "READABILITY_AVAILABLE",
    "SearchResult",
    "SearchResultItem",
    "SearxngService",
    "TtlCache",
    "WebFetchService",
    "detect_query_language",
    "deduplicate_results",
    "rank_text_results",
    "strategy_for",
    "to_image_list",
    "to_tavily_json",
    "to_text_list",
]
