"""演示数据种子：应届生 AI 应用开发工程师画像（最近一周）。

用途：为「目标与规范 / 日报 / 周报 / 能力画像」提供**前后一致**的可验证数据。

用法（项目根目录）：
    conda run -n yomi python -m scripts.seed_demo --dry-run     # 仅预览
    conda run -n yomi python -m scripts.seed_demo --reset       # 清空 6 张表后重建

说明：`--reset` 会删除 5 张业务表的全部记录后重建，请谨慎使用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.cycles import fmt_date, today_local, week_bounds
from app.data.bitable import BitableClient
from app.data.tables import (
    ABILITY_DIMENSIONS,
    DAILY_TABLE,
    GOALS_TABLE,
    PORTRAIT_TABLE,
    STANDARDS_TABLE,
    WEEKLY_TABLE,
)
from app.feishu.api import FeishuClient


UNIT = "智能研发部"
LONG_TERM = "独立负责一个 AI Agent 模块的设计、开发与上线"
MONTHLY = "9 月掌握 Agent 开发全链路，独立完成 1 个模块并通过评审"
WEEK_GOAL = "完成 YoMi 日报助手核心功能开发与联调"

STANDARD = {
    "规范名称": "默认工作规范",
    "周报格式": "本周目标 / 工作成果 / 问题处理 / 下周重点 / 工作分析 / 能力评分",
    "汇报风格": "结论先行、量化结果、不写空话",
    "必要字段": "任务、实际产出、问题、下一步",
    "企业/团队要求": "日报 22:00 前提交；周报周六提交；阻塞问题当日同步",
}

# 最近一周（周一~周五）日报：任务 / 产出 / 下一步
DAILY_SAMPLES = (
    (
        "完成日报卡片表单改版，要点与产出支持分点填写",
        "新版卡片通过 3 个场景验证，填写项由 6 个多行框压缩为 3 组",
        "联调飞书长连接卡片回调",
    ),
    (
        "联调飞书长连接与卡片回调，接入官方 SDK",
        "卡片回调 1 秒内返回，长连接连续 2 小时无断连",
        "接入 AI 整理与确认流程",
    ),
    (
        "接入 AI 整理与确认流程，支持按意见重新生成",
        "确认/重新生成闭环通过验证，单句控制在 35 字内",
        "补充关键信息缺失追问",
    ),
    (
        "排查并修复卡片点击失效问题",
        "定位为表单按钮不回传 value，修复后确认与重新生成恢复正常",
        "完善目标与规范服务",
    ),
    (
        "实现周目标建议与工作规范读取，注入周报提示词",
        "3 个候选周目标可确认写入，规范约束进入周报生成",
        "补齐周报用户反馈闭环",
    ),
)

SCORES = {
    "Agent开发": (5.2, 5.8, 6.0, 6.2),
    "技术能力": (6.0, 6.3, 6.6, 6.8),
    "产品能力": (4.8, 5.2, 5.3, 5.4),
    "项目交付": (5.5, 5.8, 6.0, 6.1),
    "表达沟通": (5.0, 5.4, 5.5, 5.6),
}

PORTRAIT_TEXT = (
    {
        "当前能力状态": "起步阶段",
        "主要优势": "学习态度积极，基础知识正在补齐",
        "主要问题": "各维度得分偏低，缺乏实战经验",
        "改进方向": "以跟学和模仿为主，尽快参与真实任务",
        "下一阶段建议": "完成基础学习清单，争取参与一个完整项目",
        "分析文字": "整体处于起步期，五个维度均有较大提升空间。",
    },
    {
        "当前能力状态": "基础补齐",
        "主要优势": "技术基础扎实，上手速度加快",
        "主要问题": "产品思维不足，需求理解常出现偏差",
        "改进方向": "加强需求拆解练习，多与产品同事交流",
        "下一阶段建议": "独立完成小型模块的方案设计",
        "分析文字": "各维度均小幅上升，技术能力保持领先。",
    },
    {
        "当前能力状态": "稳步提升",
        "主要优势": "能独立推进模块开发，交付节奏稳定",
        "主要问题": "问题定位耗时偏长，复盘意识不足",
        "改进方向": "建立问题定位清单，坚持每日复盘",
        "下一阶段建议": "尝试独立负责一个完整模块",
        "分析文字": "能力曲线平稳上行，交付能力提升明显。",
    },
    {
        "当前能力状态": "独立承担",
        "主要优势": "可独立承担模块开发并主动同步风险",
        "主要问题": "跨团队沟通经验不足",
        "改进方向": "主动参与评审，提升表达结构化程度",
        "下一阶段建议": "争取在项目中承担对外沟通角色",
        "分析文字": "技术与交付维度增长较快，沟通维度待突破。",
    },
)

def _history_months(today: date, count: int = 4) -> list[str]:
    keys: list[str] = []
    year, month = today.year, today.month - 1
    if month == 0:
        year, month = year - 1, 12
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def build_payload(today: date) -> dict[str, list[dict]]:
    monday, sunday = week_bounds(today)
    now = datetime.now(ZoneInfo(get_settings().app_timezone)).strftime("%Y-%m-%d %H:%M")

    goals = [
        {
            "目标单位": UNIT,
            "长期目标": LONG_TERM,
            "月度目标": MONTHLY,
            "本周目标": WEEK_GOAL,
            "周起始": fmt_date(monday),
            "周结束": fmt_date(sunday),
            "状态": "进行中",
            "备注": "演示数据：应届生入职首月",
            "更新时间": now,
        }
    ]

    dailies: list[dict] = []
    for index, (task, output, next_step) in enumerate(DAILY_SAMPLES):
        day = monday + timedelta(days=index)
        if day > today:
            break
        dailies.append(
            {
                "日期": fmt_date(day),
                "项目": UNIT,
                "本周目标": WEEK_GOAL,
                "状态": "已完成",
                "任务": f"1. {task}",
                "实际产出": f"1. {output}",
                "下一步": next_step,
                "遇到的问题": "",
                "计划外工作": "",
                "工作负担": "正常",
                "相关资料": "",
                "原始记录": json.dumps(
                    {"任务": task, "实际产出": output, "下一步": next_step},
                    ensure_ascii=False,
                ),
                "AI整理": f"任务：\n1. {task}\n实际产出：\n1. {output}",
                "AI简要分析": f"{task.split('，')[0]}，推进本周目标落地。",
                "确认状态": "用户确认",
                "更新时间": f"{fmt_date(day)} 21:30",
            }
        )

    portraits: list[dict] = []
    for index, month in enumerate(_history_months(today)):
        row: dict = {"月份": month, "更新时间": f"{month}-28 10:00"}
        row.update(PORTRAIT_TEXT[index])
        for dim in ABILITY_DIMENSIONS:
            row[dim] = SCORES[dim][index]
        portraits.append(row)

    return {
        "goals": goals,
        "standards": [dict(STANDARD, 更新时间=now)],
        "daily": dailies,
        "portrait": portraits,
    }


async def clear_tables(bitable: BitableClient, table_ids: list[tuple[str, str]]) -> None:
    for label, table_id in table_ids:
        if not table_id:
            continue
        rows = await bitable.list_records_raw(table_id)
        if not rows:
            print(f"{label}: 无数据")
            continue
        deleted = await bitable.delete_records_raw(
            table_id, [r["record_id"] for r in rows]
        )
        print(f"{label}: 已清除 {deleted} 条")


async def seed(args: argparse.Namespace) -> None:
    settings = get_settings()
    today = today_local(settings.app_timezone)
    payload = build_payload(today)
    specs = {
        "goals": GOALS_TABLE,
        "standards": STANDARDS_TABLE,
        "daily": DAILY_TABLE,
        "portrait": PORTRAIT_TABLE,
    }

    if args.dry_run:
        for key, rows in payload.items():
            print(f"[dry-run] {specs[key].title}: {len(rows)} 条")
        goal = payload["goals"][0]
        print(f"[dry-run] 目标周期: {goal['周起始']} ~ {goal['周结束']}")
        print(f"[dry-run] 日报天数: {len(payload['daily'])}")
        return

    feishu = FeishuClient(settings)
    bitable = BitableClient(feishu, settings)
    try:
        if args.reset:
            print("=== 清空历史数据 ===")
            await clear_tables(
                bitable,
                [
                    (GOALS_TABLE.title, settings.bitable_goals_table_id),
                    (STANDARDS_TABLE.title, settings.bitable_standards_table_id),
                    (DAILY_TABLE.title, settings.bitable_daily_table_id),
                    (WEEKLY_TABLE.title, settings.bitable_weekly_table_id),
                    (PORTRAIT_TABLE.title, settings.bitable_portrait_table_id),
                ],
            )

        print("=== 写入演示数据 ===")
        portrait_ids: dict[str, str] = {}
        for key, rows in payload.items():
            spec = specs[key]
            assert spec.unique_field is not None
            created = updated = 0
            for row in rows:
                fields = dict(row)
                if key == "portrait":
                    fields = {
                        k: v
                        for k, v in fields.items()
                        if k in {
                            "月份", "当前能力状态", "主要优势", "主要问题",
                            "改进方向", "下一阶段建议", "分析文字", "更新时间",
                            *ABILITY_DIMENSIONS,
                        }
                    }
                existing = await bitable.find_by_unique(spec, fields[spec.unique_field])
                if existing:
                    record_id = str(existing["record_id"])
                    await bitable.update_record(spec, record_id, fields)
                    updated += 1
                else:
                    record_id = await bitable.add_record(spec, fields)
                    created += 1
                if key == "portrait":
                    portrait_ids[str(fields["月份"])] = record_id
            print(f"{spec.title}: 新增 {created}，更新 {updated}")

        if not args.no_weekly:
            from app.ai.llm import LLMClient
            from app.services.weekly import WeeklyReportService

            llm = LLMClient(settings)
            try:
                weekly = WeeklyReportService(settings, feishu, bitable, llm)
                result = await weekly.generate(today - timedelta(days=1))
                if result.get("ok"):
                    print(f"周报总表: 生成 {result['period']}（{result['record_id']}）")
                else:
                    print(f"周报总表: 未生成 - {result.get('message')}")
            finally:
                await llm.close()
    finally:
        await feishu.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="YoMi 演示数据种子（最近一周）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入飞书")
    parser.add_argument("--reset", action="store_true", help="先清空 6 张表再写入")
    parser.add_argument("--no-weekly", action="store_true", help="不生成周报")
    args = parser.parse_args()
    asyncio.run(seed(args))


if __name__ == "__main__":
    main()
