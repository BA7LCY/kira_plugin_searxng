"""TTL 缓存。

相同关键词在 TTL 内重复搜索直接返回缓存，降低对 SearXNG 实例的
请求频率（防止被上游限制）。支持注入时钟与可选的 JSON 文件持久化，
便于测试与跨重启保留。

纯内存实现，线程安全由调用方保证（插件单事件循环内使用）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import CacheError


class TtlCache:
    """带过期时间的键值缓存。"""

    def __init__(
        self,
        ttl_seconds: float = 300.0,
        *,
        clock: Optional[Callable[[], float]] = None,
        encode: Optional[Callable[[Any], Any]] = None,
        decode: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._clock = clock or time.monotonic
        self._data: dict[str, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0
        # 持久化时的值编解码（默认直通）；复杂对象需提供可 JSON 化的转换
        self._encode = encode or (lambda v: v)
        self._decode = decode or (lambda v: v)

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses}

    def get(self, key: str) -> Any:
        """取缓存；命中但已过期则删除并返回 None。"""
        entry = self._data.get(key)
        if entry is None:
            self._misses += 1
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._data.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (self._clock() + self._ttl, value)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    @property
    def size(self) -> int:
        return len(self._data)

    # ---- 持久化（可选） ----

    def persist(self, path: Path) -> None:
        """把当前未过期条目写入 JSON 文件。失败抛 CacheError（调用方降级）。"""
        try:
            now = self._clock()
            payload = {
                "ttl": self._ttl,
                "items": {
                    k: {"expires_at": exp, "value": self._encode(v)}
                    for k, (exp, v) in self._data.items()
                    if exp > now
                },
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            raise CacheError(f"缓存写入失败：{exc}") from exc

    def load(self, path: Path) -> None:
        """从 JSON 文件加载缓存。文件不存在或损坏时静默忽略（缓存仅是加速）。"""
        try:
            if not path.exists():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload.get("items", {})
            now = self._clock()
            for key, entry in items.items():
                if not isinstance(entry, dict) or "value" not in entry:
                    continue
                expires_at = float(entry.get("expires_at", 0))
                if expires_at > now:
                    self._data[str(key)] = (expires_at, self._decode(entry["value"]))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # 缓存只是加速手段，读失败不抛错
            return
