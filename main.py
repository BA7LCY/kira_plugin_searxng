"""kira_plugin_searxng 适配层（Kira 插件本体）。

本文件是薄适配层：
- 读取配置（section 嵌套）、构造服务层对象（依赖注入）；
- 注册 3 个 LLM 工具：web_search / search_images / fetch_webpage；
- 图片 user/both 模式时下载图片并通过消息链发给用户。

业务逻辑全部在 searxng_client 服务层（纯 Python，不依赖 core.*，
可独立测试，也便于迁移到其他机器人框架）。
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from pathlib import Path
from typing import Any

from core.chat import KiraMessageBatchEvent, MessageChain
from core.chat.message_elements import Image
from core.plugin import BasePlugin, logger, register

from .searxng_client import (
    BS4_AVAILABLE,
    CATEGORIES,
    READABILITY_AVAILABLE,
    ImageService,
    SearxngService,
    TtlCache,
    WebFetchService,
    build_http_client,
    detect_query_language,
    to_image_list,
    to_tavily_json,
)
from .searxng_client.errors import InvalidQueryError, InvalidUrlError
from .searxng_client.models import SearchResult

# 插件 id（与目录名、manifest.plugin_id 保持一致）
PLUGIN_ID = "kira_plugin_searxng"

# SearXNG 实例默认地址
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

# 插件注册的 3 个工具名
TOOL_NAMES = ("web_search", "search_images", "fetch_webpage")

# user 模式单次直接发给用户的图片上限（避免刷屏）
USER_SEND_IMAGE_LIMIT = 5

# 图片交付模式的可选值（与 schema.json 的 options 保持一致）
DELIVERY_MODES = ("model", "user", "both")

# 配置文件里必须存在的 section（缺了就补空字典，避免读取时还原成非字典）
CONFIG_SECTIONS = (
    "section_basic",
    "section_search",
    "section_image",
    "section_fetch",
    "section_network",
)


def _to_int(value: Any, default: int) -> int:
    """安全转整数：空值、非法值一律回落默认值（配置被填坏时不让插件崩掉）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    """安全转浮点数：空值、非法值一律回落默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SearxngPlugin(BasePlugin):
    """SearXNG 搜索插件：网页搜索、图片搜索、网页正文提取。"""

    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self._data_dir: Path | None = None
        self._search: SearxngService | None = None
        self._fetch: WebFetchService | None = None
        self._images: ImageService | None = None
        self._cache: TtlCache | None = None
        self._cache_path: Path | None = None
        # 初始化失败原因（None 表示一切正常）；失败时插件仍加载，工具返回可读错误
        self._init_error: str | None = None
        # 测试注入点：非 None 时用该 HTTP 客户端（生产环境保持 None）
        self._http_override: Any = None

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _cfg(self, *path: str, default: Any = None) -> Any:
        """按 section 嵌套路径读配置，缺失时返回 default。"""
        node = self.plugin_cfg or {}
        for key in path:
            if not isinstance(node, dict):
                return default
            node = node.get(key)
        return node if node is not None else default

    def _proxy(self) -> str | None:
        """代理配置：配置项优先，其次环境变量。"""
        proxy = (self._cfg("section_network", "proxy", default="") or "").strip()
        if proxy:
            return proxy
        return os.environ.get("https_proxy") or os.environ.get("http_proxy") or None

    def _repair_config(self) -> None:
        """修正配置文件里不合法的值，并写回配置文件。

        起因：早期版本的 schema.json 把 options 写成了对象数组，设置页每次点选都把
        {"label": ..., "value": ...} 这种对象存进配置，导致下拉框匹配不到选项、
        只能显示提示文案。这里在加载时把非法值统一改回合法取值，用户不用手动改配置文件。
        """
        if not isinstance(self.plugin_cfg, dict):
            return

        changed = False
        for section_key in CONFIG_SECTIONS:
            if not isinstance(self.plugin_cfg.get(section_key), dict):
                self.plugin_cfg[section_key] = {}
                changed = True

        image_cfg = self.plugin_cfg["section_image"]
        raw_mode = image_cfg.get("image_delivery_mode")
        mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
        if mode not in DELIVERY_MODES:
            mode = "model"
        if raw_mode != mode:
            image_cfg["image_delivery_mode"] = mode
            changed = True
            logger.warning(
                "[searxng] 配置项 image_delivery_mode 的原值不合法（%r），已改回 %s",
                raw_mode,
                mode,
            )

        if changed:
            self._persist_config()

    def _persist_config(self) -> None:
        """把内存里的插件配置写回 data/config/plugins/<插件 id>.json（失败只告警）。"""
        try:
            from core.utils.path_utils import get_config_path

            config_path = Path(get_config_path()) / "plugins" / f"{PLUGIN_ID}.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(self.plugin_cfg, ensure_ascii=False, indent=4), encoding="utf-8"
            )
            logger.info("[searxng] 已修正并写回配置文件：%s", config_path)
        except Exception as exc:  # noqa: BLE001 配置修复失败不该拖垮加载
            logger.warning("[searxng] 修正后的配置写回失败（忽略）：%s", exc)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self):
        """初始化服务。

        这里刻意不向外抛异常：框架的 init_plugin 捕获异常后只会丢弃插件实例，
        插件状态会永远停在“加载中”，用户在 WebUI 上看不到任何失败原因
        （见 core/plugin/manager.py 的 load_plugin_from_dir / init_plugin）。
        因此把可预期的问题降级成“功能受限 + 日志”，工具调用时返回可读错误。
        """
        self._init_error = None
        try:
            self._setup_services()
        except Exception as exc:  # noqa: BLE001 兜底：初始化失败也必须让插件完成加载
            self._init_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "[searxng] 初始化失败（版本 %s），插件以“功能受限”状态运行：%s\n%s",
                self._plugin_version(),
                self._init_error,
                traceback.format_exc(),
            )

    def _setup_services(self):
        """真正构建服务对象；任何一步出错都由 initialize 记录并降级。"""
        self._repair_config()
        self._data_dir = self._resolve_data_dir()
        base_url = (
            str(self._cfg("section_basic", "searxng_api_url", default=DEFAULT_BASE_URL) or "").strip()
            or DEFAULT_BASE_URL
        )
        timeout = max(3, _to_int(self._cfg("section_network", "request_timeout", default=15), 15))
        # 留空表示用默认 UA；这里传空字符串而不是 None，避免下游把 None 塞进请求头
        user_agent = str(self._cfg("section_network", "user_agent", default="") or "").strip()

        http = self._http_override or build_http_client(
            timeout=timeout, proxy=self._proxy(), user_agent=user_agent
        )

        # 缓存：相同关键词在 TTL 内命中，降低对 SearXNG 实例的请求频率
        cache_enabled = bool(self._cfg("section_search", "cache_enabled", default=True))
        cache_ttl = max(
            0.0,
            _to_float(self._cfg("section_search", "cache_ttl_seconds", default=300), 300.0),
        )
        cache_state = "关闭"
        if cache_enabled:
            self._cache = TtlCache(
                cache_ttl,
                # 持久化时把 SearchResult 转成可 JSON 的字典，加载时还原
                encode=lambda v: v.to_dict() if isinstance(v, SearchResult) else v,
                decode=lambda v: SearchResult.from_dict(v) if isinstance(v, dict) else v,
            )
            cache_state = f"{cache_ttl:.0f}s"
            if self._data_dir is not None:
                self._cache_path = self._data_dir / "search_cache.json"
                try:
                    self._cache.load(self._cache_path)
                except Exception as exc:  # noqa: BLE001 缓存只是加速手段，坏了不影响搜索
                    logger.warning("[searxng] 读取搜索缓存失败（忽略）：%s", exc)

        image_candidate_limit = max(
            1, _to_int(self._cfg("section_image", "image_candidate_limit", default=120), 120)
        )
        image_result_limit = max(
            1, _to_int(self._cfg("section_image", "image_result_limit", default=8), 8)
        )
        self._images = ImageService(http)
        self._search = SearxngService(
            http,
            base_url,
            image_candidate_limit=image_candidate_limit,
            image_result_limit=image_result_limit,
            cache=self._cache,
            image_service=self._images,
        )
        self._fetch = WebFetchService(
            http,
            max_chars=max(
                200, _to_int(self._cfg("section_fetch", "max_content_length", default=3000), 3000)
            ),
        )
        logger.info(
            "[searxng] 初始化完成｜版本=%s｜搜索实例=%s｜数据目录=%s｜缓存=%s｜正文提取=%s",
            self._plugin_version(),
            base_url,
            self._data_dir or "不可用（缓存不落盘）",
            cache_state,
            "readability"
            if READABILITY_AVAILABLE
            else ("beautifulsoup4" if BS4_AVAILABLE else "内置解析器"),
        )

    def _resolve_data_dir(self) -> Path | None:
        """定位插件数据目录；框架取不到时用 data/plugin_data/<插件 id> 兜底。"""
        candidates: list[Any] = []
        try:
            candidates.append(self.ctx.get_plugin_data_dir())
        except Exception as exc:  # noqa: BLE001 取不到目录不该拖垮整个插件
            logger.warning("[searxng] 读取插件数据目录失败：%s", exc)
        candidates.append(self._fallback_data_dir())
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except OSError as exc:
                logger.warning("[searxng] 无法创建数据目录 %s：%s", path, exc)
        return None

    @staticmethod
    def _plugin_version() -> str:
        """读取本插件 manifest.json 里的版本号（读不到就返回 unknown，不影响加载）。"""
        try:
            manifest = json.loads((Path(__file__).with_name("manifest.json")).read_text(encoding="utf-8"))
            return str(manifest.get("version") or "unknown")
        except Exception:  # noqa: BLE001 版本号只用于日志展示
            return "unknown"

    @staticmethod
    def _fallback_data_dir() -> Path | None:
        """框架的插件数据目录接口失效时的兜底路径。"""
        try:
            from core.utils.path_utils import get_data_path

            base = Path(get_data_path())
        except Exception:  # noqa: BLE001 连框架路径都取不到时放弃落盘
            return None
        return base / "plugin_data" / PLUGIN_ID

    def _service_error(self, name: str, service: Any) -> str | None:
        """工具入口守卫：服务没建好时返回可读错误，而不是抛异常。

        带上初始化失败的原因，这样用户在聊天里就能直接看到问题，
        不必再去翻控制台日志（完整堆栈仍会写进日志）。
        """
        if service is not None:
            return None
        reason = (self._init_error or "").strip()
        if reason:
            return f"插件初始化未完成，暂时无法{name}。原因：{reason}（完整详情见 Kira 控制台日志）。"
        return f"插件初始化未完成，暂时无法{name}（详情见 Kira 控制台日志）。"

    async def terminate(self):
        """退出前把缓存写回插件数据目录（失败仅告警，不影响退出）。"""
        if self._cache is not None and self._cache_path is not None:
            try:
                self._cache.persist(self._cache_path)
            except Exception as exc:  # noqa: BLE001 缓存是加速手段，写失败不阻断
                logger.warning("[searxng] 缓存保存失败（忽略）：%s", exc)

    # ------------------------------------------------------------------
    # 工具：web_search
    # ------------------------------------------------------------------

    @register.tool(
        name="web_search",
        description=(
            "使用自有 SearXNG 实例搜索网页，返回 JSON 结果列表"
            "（每行一条：title 标题、url 链接、content 摘要、score 相关度、source 来源）。"
            "作为内置搜索工具的补充：适合需要指定分类、或内置搜索不可用时使用。"
            "本工具不返回图片，需要图片请用 search_images。"
            "category 可选：general 通用、technical 技术、academic 学术、science 科学、"
            "news 新闻、music 音乐、videos 视频，不传则用通用。"
        ),
        params={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，例如：北京今天的天气"},
                "category": {"type": "string", "description": "搜索分类", "enum": list(CATEGORIES)},
                "limit": {"type": "integer", "description": "返回条数，1-10，默认 5"},
            },
            "required": ["query"],
        },
    )
    async def web_search(
        self,
        event: KiraMessageBatchEvent,
        query: str,
        category: str = "general",
        limit: int = 0,
    ) -> str:
        error = self._service_error("搜索网页", self._search)
        if error:
            return error
        limit = self._clamp_limit(limit, self._cfg("section_search", "default_result_limit", default=5))
        if category not in CATEGORIES:
            category = "general"
        try:
            result = await self._search.search(
                query, category=category, limit=limit, lang=self._lang_for(query)
            )
        except InvalidQueryError as exc:
            return f"搜索参数有误：{exc}"
        return to_tavily_json(result, limit)

    # ------------------------------------------------------------------
    # 工具：search_images
    # ------------------------------------------------------------------

    @register.tool(
        name="search_images",
        description=(
            "搜索图片并返回候选清单（编号、标题、图片链接、分辨率），"
            "是更快获取图片结果的搜索工具。"
            "发送图片请用 <file type=\"image\">图片链接</file> 标签；"
            "拿不准发哪张时，可先返回清单让用户挑选。"
        ),
        params={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，例如：猫 壁纸"},
                "limit": {"type": "integer", "description": "返回条数，1-10，默认 8"},
            },
            "required": ["query"],
        },
    )
    async def search_images(
        self,
        event: KiraMessageBatchEvent,
        query: str,
        limit: int = 0,
    ) -> str:
        error = self._service_error("搜索图片", self._search)
        if error:
            return error
        limit = self._clamp_limit(
            limit, self._cfg("section_image", "image_result_limit", default=8)
        )
        mode = str(self._cfg("section_image", "image_delivery_mode", default="model") or "").strip().lower()
        if mode not in DELIVERY_MODES:
            mode = "model"
        try:
            result = await self._search.search_images(
                query, limit=limit, lang=self._lang_for(query)
            )
        except InvalidQueryError as exc:
            return f"图片搜索参数有误：{exc}"
        if result.error_message:
            return to_image_list(result, limit)
        if mode in ("user", "both"):
            sent_count = await self._send_images_to_user(event, result.results)
            if mode == "user":
                if sent_count:
                    return f"已直接发送 {sent_count} 张图片给用户。"
                return "图片下载失败，没有图片可发送。"
        return to_image_list(result, limit)

    # ------------------------------------------------------------------
    # 工具：fetch_webpage
    # ------------------------------------------------------------------

    @register.tool(
        name="fetch_webpage",
        description=(
            "抓取网页并提取可读正文（去掉导航、脚本、广告等噪音），"
            "适合查看搜索结果或链接的完整内容，作为内置提取工具的补充。"
        ),
        params={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "网页链接（http 或 https）"},
                "max_chars": {"type": "integer", "description": "正文最多返回字符数，200-20000，默认 3000"},
            },
            "required": ["url"],
        },
    )
    async def fetch_webpage(
        self,
        event: KiraMessageBatchEvent,
        url: str,
        max_chars: int = 0,
    ) -> str:
        error = self._service_error("抓取网页", self._fetch)
        if error:
            return error
        try:
            text = await self._fetch.fetch(url, max_chars=_to_int(max_chars, 0) or None)
        except InvalidUrlError as exc:
            return f"链接无效：{exc}"
        return text

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _clamp_limit(self, limit: int, default: int) -> int:
        value = _to_int(limit, 0) or _to_int(default, 5)
        return max(1, min(value, 10))

    def _lang_for(self, query: str) -> str:
        """按查询文本特征推断语言（zh/en/ja/ko/auto），让结果更贴近语言。"""
        return detect_query_language(query)

    async def _send_images_to_user(self, event: KiraMessageBatchEvent, items) -> int:
        """下载图片并发给用户（user/both 模式），返回实际成功发送的张数。"""
        sid = getattr(event, "sid", None)
        if not sid or not items:
            return 0
        b64_list = await asyncio.gather(
            *(self._download_image(item) for item in items[:USER_SEND_IMAGE_LIMIT])
        )
        chain = MessageChain()
        for b64 in b64_list:
            if b64:
                chain.append(Image(image=b64))
        if not chain:
            return 0
        try:
            sent = await self.ctx.send_message_chain(sid, chain)
            if not sent.ok:
                logger.warning("[searxng] 图片发送失败：%s", sent.err)
                return 0
            return len(chain.items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[searxng] 图片发送异常：%s", exc)
            return 0

    async def _download_image(self, item) -> str | None:
        """下载一张图片为 base64；失败返回 None。"""
        url = item.img_src or item.url
        if not url or self._images is None:
            return None
        try:
            return await self._images.download_base64(url)
        except Exception:  # noqa: BLE001 单个图片失败不影响其余
            return None
