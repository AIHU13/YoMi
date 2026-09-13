"""LLM 调用封装（OpenAI 兼容接口，如 DeepSeek）。

只负责"调用与解析"，不含业务提示词与业务判断；不直接访问飞书与存储。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import LLMError


logger = logging.getLogger(__name__)


class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端，强制 JSON 输出。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.llm_base_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.llm_timeout_seconds),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """请求结构化 JSON 输出；失败抛 LLMError。

        `max_tokens` 可覆盖全局配置：推理类模型会先消耗大量思考 token，
        长输入场景需调高，否则正文可能为空。
        """

        if not self.settings.llm_ready:
            raise LLMError("未配置 LLM_API_KEY")

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.llm_temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}

        client = self._ensure_client()
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                resp = await client.post("/chat/completions", json=payload, headers=headers)
                if resp.status_code >= 400:
                    raise LLMError(f"LLM 返回 HTTP {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"].get("content") or ""
                if not content.strip():
                    raise LLMError(
                        "模型返回为空（finish_reason="
                        f"{choice.get('finish_reason')}，可能达到 max_tokens 上限）"
                    )
                from app.ai.parse import parse_json_object

                return parse_json_object(content)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning("LLM 请求失败(attempt %s/2): %s", attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(1)
            except LLMError as exc:
                # 模型偶发返回空/非 JSON：重试一次，仍失败才上抛
                last_error = exc
                logger.warning("LLM 输出异常(attempt %s/2): %s", attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(1)
            except (KeyError, IndexError, ValueError) as exc:
                raise LLMError(f"LLM 响应结构异常: {exc}") from exc
        raise LLMError(f"LLM 请求多次失败: {last_error}")
