"""查询语言检测。

SearXNG 的 lang 参数影响返回内容的语言（如 'zh'）。这里根据查询文本
的字符特征推断最可能的语言，让中文查询尽量命中中文内容。
"""

from __future__ import annotations

import re


def _is_kana(ch: str) -> bool:
    """日文假名（平假名+片假名+假名补充区）。"""
    code = ord(ch)
    return (
        0x3040 <= code <= 0x309F
        or 0x30A0 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
    )


def _is_hangul(ch: str) -> bool:
    """韩文谚文。"""
    code = ord(ch)
    return (
        0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0xAC00 <= code <= 0xD7AF
    )


def _is_han(ch: str) -> bool:
    """汉字（基本区+扩展 A）。"""
    code = ord(ch)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF


def detect_query_language(query: str) -> str:
    """返回 'zh' / 'en' / 'ja' / 'ko' / 'auto'。"""
    text = query or ""
    if any(_is_kana(ch) for ch in text):
        return "ja"
    if any(_is_hangul(ch) for ch in text):
        return "ko"
    # 全 ASCII：视为英文
    if text and all(ord(ch) < 128 for ch in text) and re.search(r"[A-Za-z]", text):
        return "en"
    han_count = sum(1 for ch in text if _is_han(ch))
    word_count = len(re.findall(r"[A-Za-z]+", text))
    if han_count > 0 and word_count <= 1:
        return "zh"
    return "auto"
