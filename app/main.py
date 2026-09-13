"""应用入口：FastAPI 装配、飞书路由挂载、定时调度与生命周期管理。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.ai.llm import LLMClient
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.data.bitable import BitableClient
from app.feishu.api import FeishuClient
from app.feishu.websocket import FeishuLongConnection
from app.routes.feishu import router as feishu_router
from app.scheduler import build_scheduler
from app.services.daily import DailyReportService
from app.services.records import RecordQueryService
from app.services.router import IntentRouter
from app.services.portrait import PortraitService
from app.services.week_goal import WeekGoalService
from app.services.weekly import WeeklyReportService


logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version=__version__)
    app.state.settings = settings

    # 运行时对象统一挂在 app.state，路由 / 调度器均从这里取，避免全局单例。
    feishu = FeishuClient(settings)
    bitable = BitableClient(feishu, settings)
    llm = LLMClient(settings)
    daily = DailyReportService(settings, feishu, bitable, llm)
    weekly = WeeklyReportService(settings, feishu, bitable, llm)
    week_goal = WeekGoalService(settings, feishu, bitable, llm)
    portrait = PortraitService(settings, feishu, bitable, llm)
    records = RecordQueryService(bitable)
    router = IntentRouter(
        settings, feishu, daily, weekly, records, week_goal, llm, portrait
    )
    app.state.feishu = feishu
    app.state.bitable = bitable
    app.state.llm = llm
    app.state.daily = daily
    app.state.weekly = weekly
    app.state.week_goal = week_goal
    app.state.portrait = portrait
    app.state.records = records
    app.state.router = router
    async def handle_card_action(event):
        """卡片回调分发：goal_* 交给目标服务，其余走日报流程。"""

        action_name = str((event.get("action") or {}).get("name") or "")
        if action_name.startswith("goal_"):
            await week_goal.handle_card_action(event)
            return
        if action_name.startswith("weekly_"):
            await weekly.handle_card_action(event)
            return
        await daily.handle_card_action(event)

    app.state.card_action_handler = handle_card_action
    app.state.feishu_ws = FeishuLongConnection(
        settings, handle_card_action, router.handle_message
    )
    app.state.scheduler = (
        build_scheduler(
            settings,
            daily.scheduled_push,
            daily.finalize_pending,
            weekly.finalize_pending_feedback,
            weekly.push_weekly,
            portrait.push_portrait,
        )
        if settings.scheduler_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler = app.state.scheduler
        if scheduler is not None:
            scheduler.start()
            logger.info("定时调度器已启动: %s", settings.daily_push_time)
        # 官方 SDK 长连接：接收 im.message.receive_v1 与卡片回调，无需公网地址
        app.state.feishu_ws.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            app.state.feishu_ws.stop()
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            await feishu.close()
            await llm.close()

    app.router.lifespan_context = lifespan

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "app": settings.app_name,
            "env": settings.app_env,
            "version": __version__,
        }

    app.include_router(feishu_router)
    return app


settings = get_settings()
app = create_app(settings)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
