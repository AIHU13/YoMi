"""记录查询：定位日报 / 周报 / 画像记录并返回官方访问链接。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.core.cycles import fmt_date
from app.core.exceptions import FeishuAPIError
from app.data.bitable import BitableClient
from app.data.tables import DAILY_TABLE, PORTRAIT_TABLE, WEEKLY_TABLE
from app.services.goals import field_text
from app.services.weekly import week_period_label


logger = logging.getLogger(__name__)


class RecordQueryService:
    def __init__(self, bitable: BitableClient | None) -> None:
        self.bitable = bitable

    @property
    def ready(self) -> bool:
        return self.bitable is not None

    async def _all(self, spec: Any) -> list[dict[str, Any]]:
        if not self.bitable:
            return []
        try:
            return await self.bitable.list_records(spec, page_size=500)
        except FeishuAPIError:
            logger.warning("读取%s失败", spec.title, exc_info=True)
            return []

    async def _with_url(self, spec: Any, record: dict[str, Any]) -> dict[str, Any]:
        url = ""
        try:
            full = await self.bitable.get_record(spec, str(record["record_id"]))
            url = str((full or {}).get("record_url") or "")
        except FeishuAPIError:
            logger.warning("获取%s记录链接失败", spec.title, exc_info=True)
        return {**record, "record_url": url}

    # ------------------------------------------------------------------ 日报

    async def daily_on(self, day: date) -> dict[str, Any]:
        key = fmt_date(day)
        for record in await self._all(DAILY_TABLE):
            if field_text(record, "日期") == key:
                return await self._with_url(DAILY_TABLE, record)
        return {}

    async def latest_daily(self, today: date | None = None) -> dict[str, Any]:
        """最近一条日报：优先取不晚于今天的记录，避免命中未来样例数据。"""

        records = sorted(
            await self._all(DAILY_TABLE), key=lambda r: field_text(r, "日期"), reverse=True
        )
        if today is not None:
            key = fmt_date(today)
            past = [r for r in records if field_text(r, "日期") and field_text(r, "日期") <= key]
            records = past or records
        return await self._with_url(DAILY_TABLE, records[0]) if records else {}

    # ------------------------------------------------------------------ 周报

    async def weekly_on(self, day: date) -> dict[str, Any]:
        """优先按周期精确匹配（与本系统生成口径一致），再回退日期区间匹配。"""

        records = await self._all(WEEKLY_TABLE)
        period = week_period_label(day)
        for record in records:
            if field_text(record, "周期") == period:
                return await self._with_url(WEEKLY_TABLE, record)
        key = fmt_date(day)
        matched = [
            record
            for record in records
            if field_text(record, "周起始")
            and field_text(record, "周结束")
            and field_text(record, "周起始") <= key <= field_text(record, "周结束")
        ]
        if matched:
            matched.sort(key=lambda r: field_text(r, "更新时间"), reverse=True)
            return await self._with_url(WEEKLY_TABLE, matched[0])
        return {}

    async def latest_weekly(self, today: date | None = None) -> dict[str, Any]:
        """最近一条周报：优先取本周或更早的记录。"""

        records = sorted(
            await self._all(WEEKLY_TABLE),
            key=lambda r: field_text(r, "周起始"),
            reverse=True,
        )
        if today is not None:
            key = fmt_date(today)
            past = [
                r
                for r in records
                if field_text(r, "周起始") and field_text(r, "周起始") <= key
            ]
            records = past or records
        return await self._with_url(WEEKLY_TABLE, records[0]) if records else {}

    # ------------------------------------------------------------------ 画像

    async def latest_portrait(self) -> dict[str, Any]:
        records = sorted(
            await self._all(PORTRAIT_TABLE),
            key=lambda r: field_text(r, "月份"),
            reverse=True,
        )
        return await self._with_url(PORTRAIT_TABLE, records[0]) if records else {}
