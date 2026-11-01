"""插件异常类型。

统一异常体系：所有可预期失败都抛这里定义的异常，
适配层捕获后转成模型可读的中文提示。
"""

from __future__ import annotations


class PluginError(Exception):
    """插件业务异常基类。"""


class ExternalServiceError(PluginError):
    """外部服务（SearXNG 实例、目标网页等）不可用或返回异常。"""


class InvalidQueryError(PluginError):
    """查询参数非法（如空查询、超过长度限制）。"""


class InvalidUrlError(PluginError):
    """URL 非法（不是 http/https 链接）。"""


class CacheError(PluginError):
    """缓存读写失败（不影响主流程，调用方应降级为直连）。"""
