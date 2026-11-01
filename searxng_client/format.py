"""结果格式化。

- 给模型看的文字清单（Tavily 兼容 JSON 行格式）；
- user 模式直接发图时的图片清单文本。
"""

from __future__ import annotations

import json

from .models import SearchResult, SearchResultItem


def to_tavily_json(result: SearchResult, limit: int) -> str:
    """Tavily 兼容格式：每行一个 JSON 对象（title/url/content/score/source）。

    结果为空或请求失败时返回一行 {"error": ...} 可读提示。
    """
    if result.error_message:
        return json.dumps({"error": result.error_message}, ensure_ascii=False)
    if not result.results:
        return json.dumps({"error": "没有找到相关结果，换个关键词试试。"}, ensure_ascii=False)
    lines = []
    for item in result.results[:limit]:
        lines.append(
            json.dumps(
                {
                    "title": item.title,
                    "url": item.url,
                    "content": item.content,
                    "score": item.score,
                    "source": item.engine,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def to_text_list(result: SearchResult, limit: int) -> str:
    """纯文本清单（模型更易读时使用）。"""
    if result.error_message:
        return f"搜索失败：{result.error_message}"
    if not result.results:
        return "没有找到相关结果，换个关键词试试。"
    lines = []
    for idx, item in enumerate(result.results[:limit], start=1):
        lines.append(f"{idx}. {item.title}")
        lines.append(f"   链接：{item.url}")
        if item.content:
            lines.append(f"   摘要：{item.content}")
    return "\n".join(lines)


def to_image_list(result: SearchResult, limit: int, with_title: bool = True) -> str:
    """图片结果文字清单（给模型决定发哪张）。"""
    if result.error_message:
        return f"图片搜索失败：{result.error_message}"
    if not result.results:
        return "没有找到相关图片，换个关键词试试。"
    lines = []
    for idx, item in enumerate(result.results[:limit], start=1):
        lines.append(f"{idx}. {item.title if with_title and item.title else '（无标题）'}")
        lines.append(f"   图片链接：{item.img_src or item.url}")
        if item.resolution:
            lines.append(f"   分辨率：{item.resolution}")
    return "\n".join(lines)
