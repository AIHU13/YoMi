"""手动触发各流程（联调与补漏）。

用法（项目根目录）：
    conda run -n yomi python -m scripts.manual_trigger daily [--date YYYY-MM-DD]
    conda run -n yomi python -m scripts.manual_trigger weekly [--date YYYY-MM-DD]
    conda run -n yomi python -m scripts.manual_trigger portrait [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.ai.llm import LLMClient
from app.data.bitable import BitableClient
from app.feishu.api import FeishuClient
from app.services.daily import DailyReportService
from app.services.portrait import PortraitService
from app.services.weekly import WeeklyReportService


async def trigger_daily(day: date | None, dry_run: bool) -> None:
    settings = get_settings()
    if dry_run:
        settings = settings.model_copy(
            update={"feishu_dry_run": True, "feishu_user_open_id": "ou_dry_run"}
        )
    setup_logging(log_dir=settings.data_path / "logs")
    feishu = FeishuClient(settings)
    try:
        service = DailyReportService(settings, feishu, BitableClient(feishu, settings))
        message_id = await service.push_daily(day)
        print(f"日报卡片已发送: message_id={message_id}")
    finally:
        await feishu.close()


async def trigger_weekly(day: date | None, dry_run: bool) -> None:
    settings = get_settings()
    if dry_run:
        settings = settings.model_copy(
            update={"feishu_dry_run": True, "feishu_user_open_id": "ou_dry_run"}
        )
    setup_logging(log_dir=settings.data_path / "logs")
    feishu = FeishuClient(settings)
    llm = LLMClient(settings)
    try:
        service = WeeklyReportService(
            settings, feishu, BitableClient(feishu, settings), llm
        )
        message_id = await service.push_weekly(day)
        print(f"周报已生成并推送: message_id={message_id or '（dry-run 已跳过）'}")
    finally:
        await llm.close()
        await feishu.close()


async def trigger_portrait(day: date | None, dry_run: bool) -> None:
    settings = get_settings()
    if dry_run:
        settings = settings.model_copy(
            update={"feishu_dry_run": True, "feishu_user_open_id": "ou_dry_run"}
        )
    setup_logging(log_dir=settings.data_path / "logs")
    feishu = FeishuClient(settings)
    llm = LLMClient(settings)
    try:
        service = PortraitService(settings, feishu, BitableClient(feishu, settings), llm)
        message_id = await service.push_portrait(day)
        print(f"月度画像已生成并推送: message_id={message_id or '（dry-run 已跳过）'}")
    finally:
        await llm.close()
        await feishu.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="YoMi 手动触发脚本")
    parser.add_argument(
        "flow", choices=("daily", "weekly", "portrait"), help="要触发的流程"
    )
    parser.add_argument("--date", help="目标日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--dry-run", action="store_true", help="不真实调用飞书，仅走本地流程")
    args = parser.parse_args()
    day = date.fromisoformat(args.date) if args.date else None
    if args.flow == "weekly":
        asyncio.run(trigger_weekly(day, args.dry_run))
    elif args.flow == "portrait":
        asyncio.run(trigger_portrait(day, args.dry_run))
    else:
        asyncio.run(trigger_daily(day, args.dry_run))


if __name__ == "__main__":
    main()
