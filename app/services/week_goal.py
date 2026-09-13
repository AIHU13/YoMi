"""每周目标服务：AI 拆解候选周目标 → 用户确认 → 写入工作目标表。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.ai.llm import LLMClient
from app.ai.parse import normalize_points
from app.ai.prompts import WEEK_GOAL_SYSTEM, build_week_goal_user
from app.core.config import Settings
from app.core.cycles import fmt_date, monday_of, today_local, week_bounds
from app.core.exceptions import ConfigurationError, FeishuAPIError, LLMError
from app.data.bitable import BitableClient
from app.data.tables import DAILY_TABLE, GOALS_TABLE
from app.feishu.api import FeishuClient
from app.feishu.cards import (
    base_card,
    column,
    column_set,
    divider,
    md,
    open_url_button,
    text_button,
)
from app.services.goals import (
    field_text,
    load_goal_record,
    load_standards,
    save_goal,
)


logger = logging.getLogger(__name__)

GOAL_PICK = "goal_pick"
GOAL_KEEP = "goal_keep"
GOAL_RESUGGEST = "goal_resuggest"


def target_week(day: date) -> tuple[date, date]:
    """工作日规划本周，周末规划下周。"""

    base = monday_of(day)
    if day.weekday() >= 5:
        base = base + timedelta(days=7)
    return week_bounds(base)


class WeekGoalService:
    def __init__(
        self,
        settings: Settings,
        feishu: FeishuClient,
        bitable: BitableClient | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.feishu = feishu
        self.bitable = bitable
        self.llm = llm

    def _now(self) -> str:
        return datetime.now(ZoneInfo(self.settings.app_timezone)).strftime("%Y-%m-%d %H:%M")

    # ------------------------------------------------------------------ 建议

    async def suggest(self, day: date | None = None, open_id: str = "") -> dict[str, Any]:
        """生成候选周目标并推送确认卡片。"""

        if not (self.bitable and self.settings.bitable_ready):
            raise ConfigurationError("未配置多维表格，无法生成目标建议")
        if self.llm is None or not self.settings.llm_ready:
            raise ConfigurationError("未配置 AI，无法生成目标建议")

        day = day or today_local(self.settings.app_timezone)
        start, end = target_week(day)
        goal_record = await load_goal_record(self.bitable, self.settings, day)
        unit = field_text(goal_record, "目标单位") or "未设置目标单位"
        current = field_text(goal_record, "本周目标")

        records = await self.bitable.list_records(DAILY_TABLE, page_size=200)
        since = fmt_date(day - timedelta(days=7))
        recent = [
            {
                "日期": field_text(r, "日期"),
                "任务": field_text(r, "任务"),
                "实际产出": field_text(r, "实际产出"),
            }
            for r in records
            if field_text(r, "日期") and field_text(r, "日期") >= since
        ][-6:]
        payload = {
            "目标单位": unit,
            "长期目标": field_text(goal_record, "长期目标"),
            "月度目标": field_text(goal_record, "月度目标"),
            "当前周目标": current,
            "最近工作记录": recent,
        }
        raw = await self.llm.complete_json(
            WEEK_GOAL_SYSTEM, build_week_goal_user(payload)
        )
        candidates = normalize_points(raw.get("候选目标"), limit=30, max_items=3)
        if not candidates:
            raise LLMError("AI 未返回有效候选目标")

        card = self.build_card(unit, start, end, candidates, current)
        if open_id:
            await self.feishu.send_card(open_id, card)
        return {"unit": unit, "start": start, "end": end, "candidates": candidates}

    def build_card(
        self,
        unit: str,
        start: date,
        end: date,
        candidates: list[str],
        current: str = "",
    ) -> dict[str, Any]:
        card = base_card(f"周目标建议 · {fmt_date(start)} ~ {fmt_date(end)}", color="indigo")
        lines = [f"**目标单位**：{unit}"]
        if current:
            lines.append(f"**当前目标**：{current}")
        elements: list[dict[str, Any]] = [
            md("\n".join(lines), text_size="small"),
            md("点击下面任一候选即可设为该周期目标（AI 建议，确认后生效）："),
        ]
        for index, candidate in enumerate(candidates, start=1):
            elements.append(
                text_button(
                    f"{GOAL_PICK}_{index}",
                    {
                        "action": GOAL_PICK,
                        "goal": candidate,
                        "unit": unit,
                        "week_start": fmt_date(start),
                        "week_end": fmt_date(end),
                    },
                    f"{index}. {candidate}",
                    primary=index == 1,
                )
            )
        elements.append(divider())
        elements.append(
            column_set(
                [
                    column(1, [text_button(GOAL_KEEP, {"action": GOAL_KEEP}, "保持当前目标")]),
                    column(
                        1,
                        [text_button(GOAL_RESUGGEST, {"action": GOAL_RESUGGEST}, "重新建议")],
                    ),
                ]
            )
        )
        card["body"]["elements"] = elements
        return card

    # ------------------------------------------------------------------ 确认

    async def handle_card_action(self, event: dict[str, Any]) -> None:
        action = event.get("action") or {}
        name = str(action.get("name") or "").strip()
        value = action.get("value") or {}
        kind = str((value or {}).get("action") or name).strip()
        open_id = (
            (event.get("operator") or {}).get("open_id")
            or self.settings.feishu_user_open_id
            or ""
        )
        if kind == GOAL_KEEP:
            if open_id:
                await self.feishu.send_text(open_id, "好的，保持当前周目标不变。")
            return
        if kind == GOAL_RESUGGEST:
            if open_id:
                await self.suggest(open_id=open_id)
            return
        if kind != GOAL_PICK:
            return

        goal = str(value.get("goal") or "").strip()
        unit = str(value.get("unit") or "").strip()
        start_raw = str(value.get("week_start") or "").strip()
        end_raw = str(value.get("week_end") or "").strip()
        if not (goal and unit and start_raw and end_raw):
            logger.warning("目标确认回调参数不完整: %s", value)
            return
        if not (self.bitable and self.settings.bitable_ready):
            logger.warning("未配置多维表格，无法写入目标")
            return

        record_id = await save_goal(
            self.bitable,
            self.settings,
            unit=unit,
            week_goal=goal,
            week_start=date.fromisoformat(start_raw),
            week_end=date.fromisoformat(end_raw),
            updated_at=self._now(),
        )
        record = await self.bitable.get_record(GOALS_TABLE, record_id)
        url = str((record or {}).get("record_url") or "")
        if not open_id:
            return
        card = base_card("周目标已更新", color="green")
        elements: list[dict[str, Any]] = [
            md(
                f"**目标单位**：{unit}\n"
                f"**周期**：{start_raw} ~ {end_raw}\n"
                f"**本周目标**：{goal}"
            )
        ]
        standards = await load_standards(self.bitable, self.settings)
        if standards.get("必要字段"):
            elements.append(md("已按工作规范同步到日报卡片顶部。", text_size="small"))
        if url:
            elements.append(open_url_button("查看工作目标表记录", url, primary=True))
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)
