"""工作目标读取：日报卡片、周报与 AI 提示词共用。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.core.config import Settings
from app.core.cycles import fmt_date
from app.core.exceptions import FeishuAPIError
from app.data.bitable import BitableClient
from app.data.tables import GOALS_TABLE, STANDARDS_TABLE


logger = logging.getLogger(__name__)


def field_text(record: dict[str, Any] | None, name: str) -> str:
    if not record:
        return ""
    value = (record.get("fields") or {}).get(name)
    return str(value).strip() if value is not None else ""


async def load_goal_record(
    bitable: BitableClient | None,
    settings: Settings,
    day: date,
) -> dict[str, Any] | None:
    """取当周有效的目标记录；无匹配时回退到最近的非暂停目标。"""

    if not (bitable and settings.bitable_ready):
        return None
    try:
        records = await bitable.list_records(GOALS_TABLE)
    except FeishuAPIError:
        logger.warning("读取工作目标表失败", exc_info=True)
        return None

    day_key = fmt_date(day)
    in_period = [
        record
        for record in records
        if field_text(record, "周起始")
        and field_text(record, "周结束")
        and field_text(record, "周起始") <= day_key <= field_text(record, "周结束")
    ]
    pool = in_period or [
        record
        for record in records
        if field_text(record, "本周目标") and field_text(record, "状态") != "已暂停"
    ]
    if not pool:
        return None
    pool.sort(key=lambda r: field_text(r, "更新时间"), reverse=True)
    return pool[0]


async def load_goal_context(
    bitable: BitableClient | None,
    settings: Settings,
    day: date,
) -> dict[str, str]:
    """供 AI 关联长短期目标、说明工作价值。"""

    record = await load_goal_record(bitable, settings, day)
    if not record:
        return {}
    return {
        name: field_text(record, name)
        for name in ("目标单位", "本周目标", "月度目标", "长期目标")
    }


async def save_goal(
    bitable: BitableClient,
    settings: Settings,
    *,
    unit: str,
    week_goal: str,
    week_start: date,
    week_end: date,
    long_term: str = "",
    monthly: str = "",
    status: str = "进行中",
    updated_at: str = "",
) -> str:
    """写入/更新某个目标单位的当前周期目标（按目标单位去重）。"""

    fields: dict[str, Any] = {
        "目标单位": unit,
        "本周目标": week_goal,
        "周起始": fmt_date(week_start),
        "周结束": fmt_date(week_end),
        "状态": status,
        "更新时间": updated_at,
    }
    existing = await _current_goal_record(bitable, unit, week_start)
    if existing:
        record_id = str(existing["record_id"])
        # 未显式传入的长期/月度目标保持原值，避免误清空
        if long_term:
            fields["长期目标"] = long_term
        if monthly:
            fields["月度目标"] = monthly
        elif field_text(existing, "月度目标"):
            fields["月度目标"] = field_text(existing, "月度目标")
        if field_text(existing, "长期目标") and not long_term:
            fields["长期目标"] = field_text(existing, "长期目标")
        await bitable.update_record(GOALS_TABLE, record_id, fields)
        logger.info("目标已更新: %s -> %s", unit, week_goal)
        return record_id
    fields["长期目标"] = long_term
    fields["月度目标"] = monthly
    record_id = await bitable.add_record(GOALS_TABLE, fields)
    logger.info("目标已新增: %s -> %s", unit, week_goal)
    return record_id


async def _current_goal_record(
    bitable: BitableClient,
    unit: str,
    week_start: date,
) -> dict[str, Any] | None:
    """定位该目标单位"当前应更新"的记录：优先周期命中，其次最近更新。

    与 load_goal_record 的读取口径保持一致，避免写入与读取指向不同记录。
    """

    try:
        records = await bitable.list_records(GOALS_TABLE)
    except FeishuAPIError:
        logger.warning("读取工作目标表失败", exc_info=True)
        return None
    same_unit = [r for r in records if field_text(r, "目标单位") == unit]
    if not same_unit:
        return None
    start_key = fmt_date(week_start)
    in_period = [
        r
        for r in same_unit
        if field_text(r, "周起始")
        and field_text(r, "周结束")
        and field_text(r, "周起始") <= start_key <= field_text(r, "周结束")
    ]
    pool = in_period or same_unit
    pool.sort(key=lambda r: field_text(r, "更新时间"), reverse=True)
    return pool[0]


async def load_standards(
    bitable: BitableClient | None,
    settings: Settings,
) -> dict[str, str]:
    """读取工作规范（取最近更新的一条）；用于周报生成提示词。"""

    if not (bitable and settings.bitable_ready):
        return {}
    try:
        records = await bitable.list_records(STANDARDS_TABLE)
    except FeishuAPIError:
        logger.warning("读取工作规范表失败", exc_info=True)
        return {}
    if not records:
        return {}
    records.sort(key=lambda r: field_text(r, "更新时间"), reverse=True)
    record = records[0]
    return {
        name: field_text(record, name)
        for name in ("规范名称", "周报格式", "汇报风格", "必要字段", "企业/团队要求")
    }


async def save_standards(
    bitable: BitableClient,
    settings: Settings,
    *,
    values: dict[str, str],
    name: str = "默认工作规范",
    updated_at: str = "",
) -> str:
    """写入/更新工作规范（按规范名称去重）。"""

    fields = {k: v for k, v in values.items() if v is not None}
    fields["规范名称"] = name
    fields["更新时间"] = updated_at
    existing = await bitable.find_by_unique(STANDARDS_TABLE, name)
    if existing:
        record_id = str(existing["record_id"])
        await bitable.update_record(STANDARDS_TABLE, record_id, fields)
        return record_id
    return await bitable.add_record(STANDARDS_TABLE, fields)
