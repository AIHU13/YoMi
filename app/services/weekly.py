"""周报流程服务：汇总本周日报 → AI 生成固定结构周报 → 写入周报总表。"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.ai.llm import LLMClient
from app.ai.parse import join_points, normalize_points
from app.ai.prompts import WEEKLY_SYSTEM, build_weekly_user
from app.core.config import Settings
from app.core.cycles import fmt_date, today_local, week_bounds
from app.core.exceptions import ConfigurationError, FeishuAPIError, LLMError
from app.core.validate import text_value
from app.data.bitable import BitableClient
from app.data.local_store import LocalStore
from app.data.tables import ABILITY_DIMENSIONS, DAILY_TABLE, WEEKLY_TABLE
from app.feishu.api import FeishuClient
from app.feishu.cards import (
    base_card,
    column,
    column_set,
    divider,
    form_container,
    input_element,
    md,
    open_url_button,
    select_element,
    submit_button,
)
from app.services.goals import field_text, load_goal_context, load_standards


logger = logging.getLogger(__name__)

SECTIONS = ("工作成果", "问题处理", "下周重点", "工作分析", "改进建议")

# 反馈动作与字段
FEEDBACK_SUBMIT = "weekly_feedback_submit"
FEEDBACK_SKIP = "weekly_feedback_skip"
FEEDBACK_FORM = "weekly_feedback_form"
FEEDBACK_ACCURACY = "内容准确性"
FEEDBACK_HELPFUL = "建议是否有帮助"
FEEDBACK_NOTE = "补充意见"
ACCURACY_OPTIONS = ("准确", "有遗漏", "有错误")
HELPFUL_OPTIONS = ("有帮助", "一般", "不符合实际")


def week_period_label(day: date) -> str:
    """周标签：2026年第37周（按 ISO 周）。"""

    iso = day.isocalendar()
    return f"{iso.year}年第{iso.week}周"


class WeeklyReportService:
    """一个周期一条记录；永远追加/更新到同一张周报总表。"""

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

    # ------------------------------------------------------------------ 数据

    async def week_dailies(self, start: date, end: date) -> list[dict[str, Any]]:
        if not (self.bitable and self.settings.bitable_ready):
            raise ConfigurationError("未配置多维表格，无法生成周报")
        records = await self.bitable.list_records(DAILY_TABLE, page_size=500)
        start_key, end_key = fmt_date(start), fmt_date(end)
        picked = [
            record
            for record in records
            if start_key <= field_text(record, "日期") <= end_key
        ]
        picked.sort(key=lambda r: field_text(r, "日期"))
        return picked

    # ------------------------------------------------------------------ 生成

    async def generate(self, day: date | None = None) -> dict[str, Any]:
        """生成并写入周报；返回 {ok, message, period, record_id, record_url, sections, scores}。"""

        day = day or today_local(self.settings.app_timezone)
        start, end = week_bounds(day)
        period = week_period_label(start)
        dailies = await self.week_dailies(start, end)
        if not dailies:
            return {
                "ok": False,
                "message": (
                    f"{fmt_date(start)} ~ {fmt_date(end)} 还没有日报记录，"
                    "先提交几天日报后再让我整理周报。"
                ),
            }
        if self.llm is None or not self.settings.llm_ready:
            return {"ok": False, "message": "AI 未配置，暂时无法生成周报。"}

        goal = await load_goal_context(self.bitable, self.settings, start)
        payload = {
            "周期": period,
            "周起始": fmt_date(start),
            "周结束": fmt_date(end),
            "本周目标": goal.get("本周目标", ""),
            "月度目标": goal.get("月度目标", ""),
            "长期目标": goal.get("长期目标", ""),
            "日报记录": [
                {
                    "日期": field_text(r, "日期"),
                    "项目": field_text(r, "项目"),
                    "任务": field_text(r, "任务"),
                    "实际产出": field_text(r, "实际产出"),
                    "下一步": field_text(r, "下一步"),
                    "问题": field_text(r, "遇到的问题"),
                    "工作负担": field_text(r, "工作负担"),
                }
                for r in dailies
            ],
        }
        standards = await load_standards(self.bitable, self.settings)
        if standards:
            payload["工作规范"] = standards
        try:
            raw = await self.llm.complete_json(WEEKLY_SYSTEM, build_weekly_user(payload))
        except LLMError as exc:
            logger.warning("周报生成失败: %s", exc)
            return {"ok": False, "message": f"周报生成失败：{exc}"}

        sections = {
            name: normalize_points(raw.get(name), limit=40) for name in SECTIONS
        }
        scores = self._normalize_scores(raw.get("能力评分"))
        record_id, record_url = await self._save(period, start, end, goal, sections, scores, dailies)
        return {
            "ok": True,
            "period": period,
            "record_id": record_id,
            "record_url": record_url,
            "本周目标": str(raw.get("本周目标") or goal.get("本周目标") or "").strip(),
            "sections": sections,
            "scores": scores,
        }

    @staticmethod
    def _normalize_scores(value: Any) -> dict[str, int]:
        result: dict[str, int] = {}
        source = value if isinstance(value, dict) else {}
        for dim in ABILITY_DIMENSIONS:
            try:
                score = int(float(source.get(dim)))
            except (TypeError, ValueError):
                score = 0
            result[dim] = max(0, min(100, score))
        return result

    async def _save(
        self,
        period: str,
        start: date,
        end: date,
        goal: dict[str, str],
        sections: dict[str, list[str]],
        scores: dict[str, int],
        dailies: list[dict[str, Any]],
    ) -> tuple[str, str]:
        fields: dict[str, Any] = {
            "周期": period,
            "周起始": fmt_date(start),
            "周结束": fmt_date(end),
            "本周目标": goal.get("本周目标", ""),
            "原始记录": json.dumps(
                [
                    {"日期": field_text(r, "日期"), "任务": field_text(r, "任务")}
                    for r in dailies
                ],
                ensure_ascii=False,
            ),
            "反馈状态": "待反馈",
            "更新时间": datetime.now(ZoneInfo(self.settings.app_timezone)).strftime(
                "%Y-%m-%d %H:%M"
            ),
        }
        for name in SECTIONS:
            fields[name] = join_points(sections[name])
        for dim, score in scores.items():
            fields[f"能力评分_{dim}"] = score

        existing = await self.bitable.find_by_unique(WEEKLY_TABLE, period)
        if existing:
            record_id = str(existing["record_id"])
            await self.bitable.update_record(WEEKLY_TABLE, record_id, fields)
            logger.info("周报已更新: %s", record_id)
        else:
            record_id = await self.bitable.add_record(WEEKLY_TABLE, fields)
            logger.info("周报已新增: %s", record_id)
        store = LocalStore(self.settings.data_path)
        store.backup_record(WEEKLY_TABLE.title, {"record_id": record_id, "fields": fields})
        store.log_event("weekly_saved", {"record_id": record_id, "period": period})
        record = await self.bitable.get_record(WEEKLY_TABLE, record_id)
        return record_id, str((record or {}).get("record_url") or "")

    # ------------------------------------------------------------------ 展示

    def build_weekly_card(
        self, result: dict[str, Any], record_id: str = ""
    ) -> dict[str, Any]:
        card = base_card(f"周报已生成 · {result.get('period', '')}", color="turquoise")
        elements: list[dict[str, Any]] = []
        if result.get("本周目标"):
            elements.append(md(f"**本周目标**：{result['本周目标']}"))
        elements.append(divider())
        for name in SECTIONS:
            points = (result.get("sections") or {}).get(name) or []
            if not points:
                continue
            elements.append(md(f"**{name}**\n" + join_points(points), text_size="small"))
        scores = result.get("scores") or {}
        if scores:
            score_text = "　".join(f"{dim} {score}" for dim, score in scores.items())
            elements.append(divider())
            elements.append(md(f"**能力评分（阶段观察）**\n{score_text}", text_size="small"))
        if result.get("record_url"):
            elements.append(
                open_url_button("查看周报总表记录", result["record_url"], primary=True)
            )
        if record_id:
            elements.append(divider())
            elements.append(
                md(
                    "**反馈（选填）**：反馈会写入周报总表，用于月度画像分析，不影响历史原始数据。",
                    text_size="small",
                )
            )
            elements.append(
                form_container(
                    FEEDBACK_FORM,
                    [
                        select_element(
                            FEEDBACK_ACCURACY, ACCURACY_OPTIONS, placeholder="内容准确性"
                        ),
                        select_element(
                            FEEDBACK_HELPFUL, HELPFUL_OPTIONS, placeholder="建议是否有帮助"
                        ),
                        input_element(
                            FEEDBACK_NOTE,
                            "补充意见",
                            "如：遗漏了 X 项；建议 Y 不贴合实际",
                            multiline=True,
                            rows=2,
                        ),
                        column_set(
                            [
                                column(
                                    1,
                                    [
                                        submit_button(
                                            FEEDBACK_SUBMIT,
                                            "提交反馈",
                                            {"action": FEEDBACK_SUBMIT, "record_id": record_id},
                                        )
                                    ],
                                ),
                                column(
                                    1,
                                    [
                                        submit_button(
                                            FEEDBACK_SKIP,
                                            "稍后再说",
                                            {"action": FEEDBACK_SKIP, "record_id": record_id},
                                            primary=False,
                                        )
                                    ],
                                ),
                            ]
                        ),
                    ],
                )
            )
        card["body"]["elements"] = elements
        return card

    # ------------------------------------------------------------------ 反馈

    def _now(self) -> str:
        return datetime.now(ZoneInfo(self.settings.app_timezone)).strftime("%Y-%m-%d %H:%M")

    async def handle_card_action(self, event: dict[str, Any]) -> None:
        """处理周报反馈提交 / 稍后再说。"""

        action = event.get("action") or {}
        name = str(action.get("name") or "").strip()
        value = action.get("value") or {}
        form_value = action.get("form_value") or {}
        kind = str((value or {}).get("action") or name).strip()
        if kind not in (FEEDBACK_SUBMIT, FEEDBACK_SKIP):
            return
        open_id = (
            (event.get("operator") or {}).get("open_id")
            or self.settings.feishu_user_open_id
            or ""
        )
        if self.settings.feishu_dry_run:
            logger.info("[dry-run] 周报反馈动作=%s", kind)
            return
        if not (self.bitable and self.settings.bitable_ready):
            logger.warning("未配置多维表格，无法写入反馈")
            return

        record_id = str((value or {}).get("record_id") or "").strip()
        if not record_id:
            record_id = await self._latest_pending_record_id()
        if not record_id:
            if open_id:
                await self.feishu.send_text(open_id, "未找到对应的周报记录，请重新生成周报后再反馈。")
            return
        if kind == FEEDBACK_SKIP:
            if open_id:
                await self.feishu.send_text(open_id, "好的，本次周报暂不反馈。")
            return

        accuracy = text_value(form_value.get(FEEDBACK_ACCURACY))
        helpful = text_value(form_value.get(FEEDBACK_HELPFUL))
        note = text_value(form_value.get(FEEDBACK_NOTE))
        if not (accuracy or helpful or note):
            if open_id:
                await self.feishu.send_text(open_id, "没有收到反馈内容，可填写后重新提交。")
            return

        parts = []
        if accuracy:
            parts.append(f"内容准确性：{accuracy}")
        if helpful:
            parts.append(f"建议是否有帮助：{helpful}")
        if note:
            parts.append(f"补充意见：{note}")
        feedback = "；".join(parts)
        # 只写反馈字段，不触碰工作成果等历史原始数据
        await self.bitable.update_record(
            WEEKLY_TABLE,
            record_id,
            {"用户反馈": feedback, "反馈状态": "已反馈", "更新时间": self._now()},
        )
        logger.info("周报反馈已写入: %s -> %s", record_id, feedback)
        if not open_id:
            return
        card = base_card("反馈已记录", color="green")
        elements: list[dict[str, Any]] = [
            md(f"**周报**：{await self._period_of(record_id)}\n**反馈**：{feedback}")
        ]
        url = await self._record_url(record_id)
        if url:
            elements.append(open_url_button("查看周报总表记录", url, primary=True))
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)

    async def _latest_pending_record_id(self) -> str:
        """表单提交按钮不回传 value 时，按"最近一条待反馈周报"定位。"""

        try:
            records = await self.bitable.list_records(WEEKLY_TABLE, page_size=200)
        except FeishuAPIError:
            logger.warning("定位待反馈周报失败", exc_info=True)
            return ""
        pending = [
            r for r in records if field_text(r, "反馈状态") == "待反馈"
        ]
        if not pending:
            return ""
        pending.sort(key=lambda r: field_text(r, "更新时间"), reverse=True)
        return str(pending[0]["record_id"])

    async def _period_of(self, record_id: str) -> str:
        record = await self.bitable.get_record(WEEKLY_TABLE, record_id)
        return field_text(record, "周期")

    async def _record_url(self, record_id: str) -> str:
        record = await self.bitable.get_record(WEEKLY_TABLE, record_id)
        return str((record or {}).get("record_url") or "")

    async def finalize_pending_feedback(self) -> int:
        """超过反馈窗口仍未反馈的周报，标记为「无反馈(超时)」。"""

        if self.settings.feishu_dry_run or not (self.bitable and self.settings.bitable_ready):
            return 0
        try:
            records = await self.bitable.list_records(WEEKLY_TABLE, page_size=200)
        except FeishuAPIError:
            logger.exception("周报反馈超时收尾失败")
            return 0
        cutoff = fmt_date(
            today_local(self.settings.app_timezone)
            - timedelta(days=self.settings.weekly_feedback_window_days)
        )
        count = 0
        for record in records:
            if field_text(record, "反馈状态") != "待反馈":
                continue
            updated = field_text(record, "更新时间")[:10]
            if updated and updated <= cutoff:
                await self.bitable.update_record(
                    WEEKLY_TABLE,
                    str(record["record_id"]),
                    {"反馈状态": "无反馈(超时)", "更新时间": self._now()},
                )
                count += 1
        if count:
            logger.info("周报反馈超时收尾 %s 条", count)
        return count

    async def push_weekly(self, day: date | None = None) -> str:
        """周六定时/手动触发：生成本周周报并推送带反馈表单的卡片。"""

        if self.settings.feishu_dry_run:
            logger.info("[dry-run] 跳过周报生成与推送")
            return ""
        if not self.settings.feishu_user_open_id:
            raise ConfigurationError("未配置 FEISHU_USER_OPEN_ID，无法推送周报")
        result = await self.generate(day)
        if not result.get("ok"):
            logger.warning("周报未生成: %s", result.get("message"))
            return ""
        card = self.build_weekly_card(
            result, record_id=str(result.get("record_id") or "")
        )
        return await self.feishu.send_card(self.settings.feishu_user_open_id, card)
