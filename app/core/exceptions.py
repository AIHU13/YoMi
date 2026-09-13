"""项目统一异常体系。"""

from __future__ import annotations


class YoMiError(Exception):
    """所有业务异常基类。"""


class ConfigurationError(YoMiError):
    """配置缺失或非法。"""


class YoMiValidationError(YoMiError, ValueError):
    """用户输入或字段校验失败。"""


class DuplicateRecordError(YoMiError):
    """记录已存在（防重拦截）。"""


class StateError(YoMiError):
    """本地流程状态异常。"""


class FeishuAPIError(YoMiError):
    """飞书开放平台调用失败。"""

    def __init__(self, message: str, *, code: int | None = None, raw: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


class LLMError(YoMiError):
    """LLM 调用或结构化输出解析失败。"""

