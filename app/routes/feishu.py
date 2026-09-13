"""飞书事件与卡片回调（URL 验证 / card.action.trigger）。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.core.exceptions import FeishuAPIError
from app.feishu.api import decrypt_encrypt_payload, verify_event_signature


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/feishu", tags=["feishu"])


def _check_verification_token(inner: dict[str, Any], settings: Settings) -> bool:
    token = settings.feishu_verification_token
    return (not token) or inner.get("token") == token


@router.post("/webhook")
async def feishu_webhook(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    raw_body = await request.body()

    if not verify_event_signature(dict(request.headers), raw_body, settings.feishu_encrypt_key):
        logger.warning("飞书回调验签失败")
        return JSONResponse({"code": 1, "msg": "bad signature"}, status_code=403)

    try:
        outer = await request.json()
        inner = decrypt_encrypt_payload(outer, settings.feishu_encrypt_key)
    except (ValueError, FeishuAPIError) as exc:
        logger.warning("飞书回调解析失败: %s", exc)
        return JSONResponse({"code": 1, "msg": "bad request"}, status_code=400)

    # URL 验证：原样返回 challenge
    if inner.get("type") == "url_verification":
        if not _check_verification_token(inner, settings):
            return JSONResponse({"code": 1, "msg": "bad token"}, status_code=403)
        return JSONResponse({"challenge": inner["challenge"]})

    header = inner.get("header", {})
    event_type = header.get("event_type", "")
    event = inner.get("event", {})

    if event_type == "card.action.trigger":
        handler = getattr(request.app.state, "card_action_handler", None)
        if handler is None:
            logger.warning("收到卡片回调但尚未注册业务处理器")
        else:
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 - 回调失败不能阻塞飞书重试
                logger.exception("处理卡片回调失败")
        return JSONResponse({"code": 0, "msg": "ok"})

    logger.info("收到未处理事件: %s", event_type)
    return JSONResponse({"code": 0, "msg": "ok"})
