"""MVP 闭环自检：目标 → 日报 → 整理/确认 → 周报 → 反馈 → 月度画像。

用法（项目根目录）：
    conda run -n yomi python -m scripts.e2e_check

说明：这是**真实**联调（会调用算力与飞书写入），用于验收前自检；
重复执行会更新当天/当周/当月记录，不产生重复数据。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from app.ai.llm import LLMClient
from app.core.config import get_settings
from app.core.cycles import fmt_date, today_local
from app.data.bitable import BitableClient
from app.data.tables import DAILY_TABLE, PORTRAIT_TABLE, WEEKLY_TABLE
from app.feishu.api import FeishuClient
from app.services.daily import CONFIRM_ACTION, DailyReportService
from app.services.goals import field_text, load_goal_record
from app.services.portrait import PortraitService
from app.services.weekly import FEEDBACK_SUBMIT, WeeklyReportService


async def main() -> int:
    settings = get_settings()
    today = today_local(settings.app_timezone)
    feishu = FeishuClient(settings)
    bitable = BitableClient(feishu, settings)
    llm = LLMClient(settings)
    daily = DailyReportService(settings, feishu, bitable, llm)
    weekly = WeeklyReportService(settings, feishu, bitable, llm)
    portrait = PortraitService(settings, feishu, bitable, llm)
    results: list[tuple[str, bool, str]] = []
    open_id = settings.feishu_user_open_id

    try:
        # 1 目标
        goal = await load_goal_record(bitable, settings, today)
        ok = bool(field_text(goal, "本周目标"))
        results.append(("1 工作目标可读取", ok, field_text(goal, "本周目标")[:30]))

        # 2 日报提交（分点输入 → AI 整理 → 落表）
        await daily.handle_card_action(
            {
                "event_id": f"e2e-{today}-submit",
                "operator": {"open_id": open_id},
                "action": {
                    "tag": "button",
                    "name": "daily_submit",
                    "value": {},
                    "form_value": {
                        "项目": field_text(goal, "目标单位") or "智能研发部",
                        "状态": "已完成",
                        "任务_1": "闭环自检：跑通日报到画像全链路",
                        "产出_1": "自检脚本输出全部环节通过结果",
                        "下一步": "修复自检发现的问题",
                        "工作负担": "正常",
                    },
                },
            }
        )
        record = await bitable.find_by_unique(DAILY_TABLE, fmt_date(today))
        fields = (record or {}).get("fields", {})
        results.append(
            (
                "2 日报落表（原始+AI 分离）",
                bool(record) and bool(field_text(record, "任务")) and bool(field_text(record, "AI整理")),
                f"确认状态={field_text(record, '确认状态')}",
            )
        )

        # 3 用户确认
        if record:
            await daily.handle_card_action(
                {
                    "event_id": f"e2e-{today}-confirm",
                    "operator": {"open_id": open_id},
                    "action": {
                        "tag": "button",
                        "name": CONFIRM_ACTION,
                        "value": {"action": CONFIRM_ACTION, "record_id": record["record_id"]},
                        "form_value": {"意见": ""},
                    },
                }
            )
            rec = await bitable.get_record(DAILY_TABLE, str(record["record_id"]))
            results.append(
                (
                    "3 日报确认存档",
                    field_text(rec, "确认状态") == "用户确认",
                    field_text(rec, "确认状态"),
                )
            )

        # 4 周报生成（汇总本周日报 → 落表）
        weekly_result = await weekly.generate(today)
        ok = bool(weekly_result.get("ok"))
        results.append(
            (
                "4 周报生成并落表",
                ok,
                str(weekly_result.get("period") or weekly_result.get("message"))[:40],
            )
        )

        # 5 周报反馈回写
        if ok:
            rid = str(weekly_result.get("record_id"))
            await weekly.handle_card_action(
                {
                    "event_id": f"e2e-{today}-feedback",
                    "operator": {"open_id": open_id},
                    "action": {
                        "tag": "button",
                        "name": FEEDBACK_SUBMIT,
                        "value": {"action": FEEDBACK_SUBMIT, "record_id": rid},
                        "form_value": {
                            "内容准确性": "准确",
                            "建议是否有帮助": "有帮助",
                            "补充意见": "闭环自检",
                        },
                    },
                }
            )
            wrec = await bitable.get_record(WEEKLY_TABLE, rid)
            results.append(
                (
                    "5 周报反馈回写（不改原始数据）",
                    field_text(wrec, "反馈状态") == "已反馈",
                    field_text(wrec, "反馈状态"),
                )
            )

        # 6 月度画像（月度总结 + 能力分析 → 更新画像表）
        month_key = f"{today.year:04d}-{today.month:02d}"
        p_result = await portrait.generate(month_key)
        ok = bool(p_result.get("ok"))
        detail = str(p_result.get("month") or p_result.get("message"))[:40]
        if ok:
            prec = await bitable.get_record(PORTRAIT_TABLE, str(p_result.get("record_id")))
            ok = bool(field_text(prec, "工作总结")) and bool(field_text(prec, "能力分析"))
            detail = f"月份={field_text(prec, '月份')} 维度得分已写入"
        results.append(("6 月度画像生成/更新", ok, detail))
    finally:
        await llm.close()
        await feishu.close()

    print("\n================ MVP 闭环自检 ================")
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    failed = [name for name, ok, _ in results if not ok]
    print("=============================================")
    print(f"通过 {len(results) - len(failed)}/{len(results)}" + (f" 失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
