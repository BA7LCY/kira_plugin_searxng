"""分类策略：每个分类对应的主搜索源组合与回退顺序。

SearXNG 的 categories 参数决定这次查询转发给哪些上游引擎。
不同分类适合不同的引擎集合：学术只问学术源、技术只问技术源，
取不到结果时按回退顺序降级到更通用的分类。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchStrategy:
    """一个分类的搜索策略。

    primary_category: 首选分类（传给 SearXNG 的 categories 参数）。
    fallback_categories: 首选无结果时依次尝试的分类。
    ranking_profile: 结果排序时使用的域名加分档位（见 ranking.py）。
    """

    primary_category: str
    fallback_categories: tuple[str, ...] = ()
    ranking_profile: str | None = None


# 分类 -> 策略 映射。unknown 分类回落通用搜索。
_SEARCH_STRATEGIES: dict[str, SearchStrategy] = {
    "general": SearchStrategy("general", ranking_profile=None),
    "technical": SearchStrategy("it", ("technical", "general"), ranking_profile="technical"),
    "academic": SearchStrategy("academic", ("science", "general"), ranking_profile="academic"),
    "science": SearchStrategy("science", ("academic", "general"), ranking_profile="science"),
    "news": SearchStrategy("news", ("general",), ranking_profile="news"),
    "music": SearchStrategy("music", ("general",)),
    "videos": SearchStrategy("videos", ("general",)),
    "images": SearchStrategy("images", ranking_profile=None),
}

# 模型可用的分类白名单（对外展示顺序）
CATEGORIES = (
    "general",
    "technical",
    "academic",
    "science",
    "news",
    "music",
    "videos",
    "images",
)


def strategy_for(category: str) -> SearchStrategy:
    """取分类对应的策略；未知分类回落通用策略。"""
    return _SEARCH_STRATEGIES.get(category or "general", _SEARCH_STRATEGIES["general"])
