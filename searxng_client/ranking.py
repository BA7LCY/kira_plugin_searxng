"""结果排序与去重。

SearXNG 返回的 score 是上游引擎给的相关度分（不同引擎量纲不一），
插件在其上加"域名加分"（ranking_profile）表达来源偏好：
技术资料更信官方文档/Stack Overflow，学术更信论文库等。
"""

from __future__ import annotations

from .models import SearchResultItem

# 域名关键字 -> 加分。匹配 url 子串（不要求精确域名）。
_PROFILE_PATTERNS: dict[str, dict[str, float]] = {
    "technical": {
        "docs.python.org": 0.50,
        "developer.mozilla.org": 0.45,
        "readthedocs.io": 0.35,
        "stackoverflow.com": 0.25,
        "github.com": 0.20,
    },
    "academic": {
        "scholar.google.com": 0.40,
        "arxiv.org": 0.45,
        "doi.org": 0.35,
        "ncbi.nlm.nih.gov": 0.35,
        "researchgate.net": 0.20,
    },
    "science": {
        "nature.com": 0.45,
        "science.org": 0.45,
        "scientificamerican.com": 0.30,
        "nasa.gov": 0.35,
        "ncbi.nlm.nih.gov": 0.30,
    },
    "news": {
        "reuters.com": 0.45,
        "apnews.com": 0.45,
        "bbc.com": 0.30,
        "nytimes.com": 0.25,
    },
}

_SUPPORTED_PROFILES = frozenset(_PROFILE_PATTERNS)


def domain_bonus(url: str, profile: str | None) -> float:
    """按加分档位计算域名加分；无档位或未匹配返回 0。"""
    if not profile or profile not in _PROFILE_PATTERNS:
        return 0.0
    url_lower = url.lower()
    return sum(bonus for key, bonus in _PROFILE_PATTERNS[profile].items() if key in url_lower)


def rank_text_results(
    results: list[SearchResultItem],
    profile: str | None = None,
) -> list[SearchResultItem]:
    """按 score+域名加分排序（同分保持原顺序，排序稳定）。"""
    def key(item: SearchResultItem) -> tuple[float, float]:
        return (item.score + domain_bonus(item.url, profile), item.score)

    return sorted(results, key=key, reverse=True)


def deduplicate_results(results: list[SearchResultItem]) -> list[SearchResultItem]:
    """按 url 去重，保留第一条（排序后调用可保留最高分）。"""
    seen: set[str] = set()
    out: list[SearchResultItem] = []
    for item in results:
        key = item.url or item.title
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def filter_by_engines(
    results: list[SearchResultItem],
    engines: list[str] | tuple[str, ...] | None,
) -> list[SearchResultItem]:
    """按引擎名过滤（引擎名不区分大小写子串匹配）。engines 为空/None 不过滤。"""
    if not engines:
        return results
    wanted = [e.lower() for e in engines]
    out: list[SearchResultItem] = []
    for item in results:
        engine = item.engine.lower()
        if any(w in engine for w in wanted):
            out.append(item)
    return out
