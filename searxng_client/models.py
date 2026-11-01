"""领域数据模型：搜索结果条目与结果集。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class SearchResultItem:
    """一条搜索结果。

    文字结果用 title/url/content；图片结果额外有 img_src/resolution。
    """

    title: str = ""
    url: str = ""
    img_src: str = ""
    resolution: str = ""
    iframe_src: str = ""
    content: str = ""
    engine: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转可 JSON 序列化的字典（缓存持久化用）。"""
        return {
            "title": self.title,
            "url": self.url,
            "img_src": self.img_src,
            "resolution": self.resolution,
            "iframe_src": self.iframe_src,
            "content": self.content,
            "engine": self.engine,
            "score": self.score,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "SearchResultItem":
        """从 SearXNG JSON 接口的原始条目构造（字段缺失时安全兜底）。"""
        return cls(
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            img_src=str(data.get("img_src") or ""),
            resolution=str(data.get("resolution") or ""),
            iframe_src=str(data.get("iframe_src") or ""),
            content=str(data.get("content") or ""),
            engine=str(data.get("engine") or ""),
            score=float(data.get("score") or 0.0),
        )


@dataclass(slots=True)
class SearchResult:
    """一次搜索的结果集。

    error_message 非空时表示请求层面失败（实例不可达、接口关闭等），
    results 为空。
    """

    results: list[SearchResultItem] = field(default_factory=list)
    error_message: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.results

    def limited(self, limit: int) -> "SearchResult":
        """返回至多 limit 条的结果副本（不修改自身）。"""
        return SearchResult(
            results=self.results[:limit],
            error_message=self.error_message,
        )

    @classmethod
    def from_iterable(cls, items: Iterable[SearchResultItem]) -> "SearchResult":
        return cls(results=list(items))

    def to_dict(self) -> dict[str, Any]:
        """转可 JSON 序列化的字典（缓存持久化用）。"""
        return {
            "results": [item.to_dict() for item in self.results],
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchResult":
        """从 to_dict 的产物还原（缓存加载用）。"""
        raw_items = data.get("results") or []
        return cls(
            results=[SearchResultItem.from_mapping(item) for item in raw_items],
            error_message=str(data.get("error_message") or ""),
        )

    @classmethod
    def failed(cls, message: str) -> "SearchResult":
        return cls(results=[], error_message=message)
