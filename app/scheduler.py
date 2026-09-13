"""定时任务（第 4 轮仅注册每日日报推送）。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings
from app.core.cycles import parse_clock


logger = logging.getLogger(__name__)


def build_scheduler(
    settings: Settings,
    daily_push: Callable[[], Awaitable[str]],
    daily_finalize: Callable[[], Awaitable[int]] | None = None,
    weekly_feedback_finalize: Callable[[], Awaitable[int]] | None = None,
    weekly_push: Callable[[], Awaitable[str]] | None = None,
    monthly_push: Callable[[], Awaitable[str]] | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.app_timezone)
    clock = parse_clock(settings.daily_push_time)
    scheduler.add_job(
        daily_push,
        CronTrigger(hour=clock.hour, minute=clock.minute),
        id="daily_push",
        name="每日 22:00 日报卡片推送",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60 * 60,
    )
    if daily_finalize is not None:
        # 当天未确认的日报在截止时间标记为"超时自动保存"（内容已先行保存，不丢数据）
        deadline = parse_clock(settings.daily_confirm_deadline)
        scheduler.add_job(
            daily_finalize,
            CronTrigger(hour=deadline.hour, minute=deadline.minute),
            id="daily_finalize",
            name="日报确认超时收尾",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60 * 60,
        )
    if weekly_feedback_finalize is not None:
        # 超过反馈窗口仍未反馈的周报 → 「无反馈(超时)」，不阻塞月度画像
        feedback_deadline = parse_clock(settings.daily_confirm_deadline)
        scheduler.add_job(
            weekly_feedback_finalize,
            CronTrigger(hour=feedback_deadline.hour, minute=feedback_deadline.minute),
            id="weekly_feedback_finalize",
            name="周报反馈超时收尾",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60 * 60,
        )
    if weekly_push is not None:
        # 每周六 22:00：汇总本周日报生成周报并推送反馈卡片
        weekly_clock = parse_clock(settings.weekly_push_time)
        scheduler.add_job(
            weekly_push,
            CronTrigger(
                day_of_week=settings.weekly_push_weekday,
                hour=weekly_clock.hour,
                minute=weekly_clock.minute,
            ),
            id="weekly_push",
            name="每周周报生成与推送",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60 * 60,
        )
    if monthly_push is not None:
        # 每月固定日 22:00：月度能力画像
        monthly_clock = parse_clock(settings.monthly_push_time)
        scheduler.add_job(
            monthly_push,
            CronTrigger(
                day=settings.monthly_push_day,
                hour=monthly_clock.hour,
                minute=monthly_clock.minute,
            ),
            id="monthly_push",
            name="月度能力画像生成与推送",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60 * 60,
        )
    return scheduler
