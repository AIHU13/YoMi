"""飞书官方 SDK 长连接（WebSocket）事件订阅。

无需公网域名/回调地址：由官方 SDK 主动连接飞书，接收事件与卡片回调。
SDK 的 ``ws.Client.start()`` 为阻塞调用，故放在后台守护线程中运行；
回调发生在该线程，通过 ``run_coroutine_threadsafe`` 桥接到主事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from app.core.config import Settings


logger = logging.getLogger(__name__)

CardActionHandler = Callable[[dict[str, Any]], Awaitable[None]]
MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


def card_action_to_event(data: P2CardActionTrigger) -> dict[str, Any]:
    """把官方回调对象转成业务层既有的事件字典结构。"""

    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    action = getattr(event, "action", None)
    context = getattr(event, "context", None)
    header = getattr(data, "header", None)
    return {
        "event_id": getattr(header, "event_id", "") or "",
        "operator": {
            "open_id": getattr(operator, "open_id", "") or "",
            "user_id": getattr(operator, "user_id", "") or "",
            "union_id": getattr(operator, "union_id", "") or "",
        },
        "action": {
            "tag": getattr(action, "tag", "") or "",
            "name": getattr(action, "name", "") or "",
            "value": getattr(action, "value", None) or {},
            "form_value": getattr(action, "form_value", None) or {},
        },
        "context": {
            "open_message_id": getattr(context, "open_message_id", "") or "",
            "open_chat_id": getattr(context, "open_chat_id", "") or "",
        },
    }


class FeishuLongConnection:
    """管理官方 SDK 长连接的构建、启动与回调分发。"""

    def __init__(
        self,
        settings: Settings,
        card_action_handler: CardActionHandler | None = None,
        message_handler: MessageHandler | None = None,
        *,
        action_timeout: float = 2.5,
    ) -> None:
        self.settings = settings
        self.card_action_handler = card_action_handler
        self.message_handler = message_handler
        self.action_timeout = action_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: lark.ws.Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.feishu_long_conn_enabled and self.settings.feishu_ready)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------------- 构建

    def build_dispatcher(self) -> lark.EventDispatcherHandler:
        # 长连接模式：encrypt_key 与 verification_token 传空字符串。
        builder = lark.EventDispatcherHandler.builder("", "")
        if self.card_action_handler is not None:
            builder = builder.register_p2_card_action_trigger(self._on_card_action)
        builder = builder.register_p2_im_message_receive_v1(self._on_message)
        return builder.build()

    def build_client(self) -> lark.ws.Client:
        return lark.ws.Client(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            event_handler=self.build_dispatcher(),
            log_level=lark.LogLevel.INFO,
            domain=self.settings.feishu_sdk_domain,
        )

    # ---------------------------------------------------------------- 生命周期

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        if not self.enabled:
            logger.warning("未启用飞书长连接（缺少凭证或 FEISHU_LONG_CONN_ENABLED=false）")
            return False
        if self.is_running:
            return True
        self._loop = loop
        self._client = self.build_client()
        self._thread = threading.Thread(
            target=self._run, name="yomi-feishu-ws", daemon=True
        )
        self._thread.start()
        logger.info("飞书长连接已在后台线程启动")
        return True

    def _run(self) -> None:
        assert self._client is not None
        try:
            self._client.start()
        except Exception:  # noqa: BLE001 - 长连接异常不应拖垮主服务
            logger.exception("飞书长连接异常退出")

    def stop(self) -> None:
        # 官方 SDK start() 阻塞且无优雅关闭接口；守护线程随进程退出即可。
        self._thread = None
        self._client = None

    # ---------------------------------------------------------------- 回调

    def _schedule(self, coro: Awaitable[None], label: str) -> bool:
        """投递到主循环后立即返回：飞书要求 3 秒内响应，AI 处理不能阻塞响应。"""

        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("主事件循环不可用，无法处理%s", label)
            return False
        future = asyncio.run_coroutine_threadsafe(coro, loop)

        def _done(fut: Any) -> None:
            try:
                fut.result()
            except Exception:  # noqa: BLE001 - 异步任务异常只记录，不影响长连接
                logger.exception("异步处理%s失败", label)

        future.add_done_callback(_done)
        return True

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        event = card_action_to_event(data)
        logger.info("收到卡片回调: %s", event.get("action", {}))
        if self.card_action_handler is None:
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "已收到"}}
            )
        # 立即回执，业务处理（含 AI 整理）在后台异步执行
        self._schedule(self.card_action_handler(event), "卡片回调")
        return P2CardActionTriggerResponse(
            {"toast": {"type": "success", "content": "已收到，正在处理…"}}
        )

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        event = getattr(data, "event", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        message = getattr(event, "message", None)
        open_id = getattr(sender_id, "open_id", "") or ""
        chat_type = getattr(message, "chat_type", "") or ""
        logger.info("收到飞书消息: sender_open_id=%s chat_type=%s", open_id, chat_type)
        if not self.settings.feishu_user_open_id and open_id:
            logger.info("可将 FEISHU_USER_OPEN_ID 配置为: %s", open_id)
        if self.message_handler is None:
            return
        payload = {
            "open_id": open_id,
            "chat_type": chat_type,
            "message_type": getattr(message, "message_type", "") or "",
            "content": getattr(message, "content", "") or "",
            "message_id": getattr(message, "message_id", "") or "",
        }
        self._schedule(self.message_handler(payload), "飞书消息")
