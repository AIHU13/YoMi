"""飞书开放平台客户端（官方 SDK：lark-oapi）与回调加解密。

只做"通道"，不含业务字段含义；业务模块统一经本客户端访问飞书。
消息发送与鉴权走官方 SDK；长连接事件订阅见 app.feishu.websocket。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from typing import Any, Mapping

import lark_oapi as lark
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lark_oapi.api.auth.v3 import (
    InternalTenantAccessTokenRequest,
    InternalTenantAccessTokenRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, FeishuAPIError


logger = logging.getLogger(__name__)


def _unpad(data: bytes) -> bytes:
    padder = padding.PKCS7(128).unpadder()
    return padder.update(data) + padder.finalize()


def decrypt_encrypt_payload(body: dict[str, Any], encrypt_key: str) -> dict[str, Any]:
    """解密飞书事件体：base64(iv + AES-256-CBC(encrypted_event))。"""

    raw = body.get("encrypt")
    if not raw:
        # 未启用事件加密策略：回调 body 本身即明文事件。
        return body
    if not encrypt_key:
        raise ConfigurationError("已收到加密回调但未配置 FEISHU_ENCRYPT_KEY")
    try:
        decoded = base64.b64decode(raw)
        if len(decoded) < 17:
            raise ValueError("密文过短")
        iv, ciphertext = decoded[:16], decoded[16:]
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = _unpad(decryptor.update(ciphertext) + decryptor.finalize())
        return json.loads(plain.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 统一转业务异常便于路由层处理
        raise FeishuAPIError(f"解密飞书回调失败: {exc}") from exc


def verify_event_signature(
    headers: Mapping[str, str],
    raw_body: bytes,
    encrypt_key: str,
) -> bool:
    """校验 X-Lark-Signature：sha256(timestamp + nonce + encrypt_key + body)。"""

    if not encrypt_key:
        # 未启用加密策略时无签名头，由回调内 verification token 校验。
        return True
    timestamp = headers.get("x-lark-request-timestamp", "")
    nonce = headers.get("x-lark-request-nonce", "")
    expected = headers.get("x-lark-signature", "")
    if not (timestamp and nonce and expected):
        return False
    digest = hashlib.sha256()
    digest.update(f"{timestamp}{nonce}{encrypt_key}".encode("utf-8"))
    digest.update(raw_body)
    return digest.hexdigest() == expected


class FeishuClient:
    """封装官方 SDK：鉴权、文本与卡片消息发送/更新。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sdk: lark.Client | None = None
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def _dry_run(self) -> bool:
        return self.settings.feishu_dry_run

    @property
    def sdk(self) -> lark.Client:
        """官方 SDK 客户端（同步）；多维表格等模块共用同一实例。"""

        if self._sdk is None:
            if not self.settings.feishu_ready:
                raise ConfigurationError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
            self._sdk = (
                lark.Client.builder()
                .app_id(self.settings.feishu_app_id)
                .app_secret(self.settings.feishu_app_secret)
                .domain(self.settings.feishu_sdk_domain)
                .log_level(lark.LogLevel.INFO)
                .build()
            )
        return self._sdk

    async def close(self) -> None:
        """官方 SDK 基于 requests，无长驻连接需要释放。"""

        return None

    def _fetch_tenant_access_token_sync(self) -> tuple[str, int]:
        request = (
            InternalTenantAccessTokenRequest.builder()
            .request_body(
                InternalTenantAccessTokenRequestBody.builder()
                .app_id(self.settings.feishu_app_id)
                .app_secret(self.settings.feishu_app_secret)
                .build()
            )
            .build()
        )
        resp = self.sdk.auth.v3.tenant_access_token.internal(request)
        if not resp.success() or resp.data is None:
            raise FeishuAPIError(
                f"获取 tenant_access_token 失败: {resp.msg}",
                code=resp.code,
                raw=resp.raw,
            )
        token = resp.data.tenant_access_token or ""
        expire = int(resp.data.expire or 7200)
        if not token:
            raise FeishuAPIError("飞书未返回 tenant_access_token")
        return token, expire

    async def get_tenant_access_token(self) -> str:
        if self._dry_run:
            return "dry-run-token"
        if self._token and time.monotonic() < self._token_expire_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expire_at:
                return self._token
            token, expire = await asyncio.to_thread(self._fetch_tenant_access_token_sync)
            self._token = token
            self._token_expire_at = time.monotonic() + expire - 120
            return token

    def _create_message_sync(self, receive_id: str, msg_type: str, content: str) -> str:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(body)
            .build()
        )
        resp = self.sdk.im.v1.message.create(request)
        if not resp.success() or resp.data is None:
            raise FeishuAPIError(
                f"发送飞书消息失败: {resp.msg}", code=resp.code, raw=resp.raw
            )
        return str(resp.data.message_id or "")

    async def send_text(self, open_id: str, text: str) -> str:
        if self._dry_run:
            logger.info("[dry-run] 发送文本给 %s: %s", open_id, text)
            return f"om_dry_{uuid.uuid4().hex[:10]}"
        content = json.dumps({"text": text}, ensure_ascii=False)
        return await asyncio.to_thread(
            self._create_message_sync, open_id, "text", content
        )

    async def send_card(self, open_id: str, card: dict[str, Any]) -> str:
        """发送卡片消息，返回 message_id（可后续按 message_id 更新）。"""

        if self._dry_run:
            logger.info("[dry-run] 发送卡片给 %s", open_id)
            return f"om_dry_{uuid.uuid4().hex[:10]}"
        content = json.dumps(card, ensure_ascii=False)
        return await asyncio.to_thread(
            self._create_message_sync, open_id, "interactive", content
        )

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        """按 message_id 更新已发送卡片。"""

        if self._dry_run:
            logger.info("[dry-run] 更新卡片 %s", message_id)
            return
        content = json.dumps(card, ensure_ascii=False)
        await asyncio.to_thread(self._patch_message_sync, message_id, content)

    def _patch_message_sync(self, message_id: str, content: str) -> None:
        body = PatchMessageRequestBody.builder().content(content).build()
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = self.sdk.im.v1.message.patch(request)
        if not resp.success():
            raise FeishuAPIError(
                f"更新飞书卡片失败: {resp.msg}", code=resp.code, raw=resp.raw
            )
