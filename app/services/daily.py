"""日报流程服务：卡片生成 / 发送 / 提交解析 / AI 整理 / 落表。

设计要点：
- 卡片只收"最小事实"（要点 + 产出分点），降低填写成本；
- 提交后由 AI 串联成句（单句 ≤35 字，不虚构不扩写），失败则回退原始分点；
- 原始输入与 AI 结果分开保存，落"日报总表"，同一天一条（重复提交更新）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.ai.llm import LLMClient
from app.ai.parse import join_points, normalize_points
from app.ai.prompts import (
    DAILY_REFINE_SYSTEM,
    DAILY_REGENERATE_SYSTEM,
    build_daily_refine_user,
    DAILY_MISSING_SYSTEM,
    build_daily_missing_user,
)
from app.core.config import Settings
from app.core.cycles import fmt_date, today_local
from app.core.exceptions import ConfigurationError, FeishuAPIError, LLMError
from app.core.validate import text_value, validate_daily_values
from app.data.bitable import BitableClient
from app.data.local_store import LocalStore
from app.data.tables import (
    BURDEN_OPTIONS,
    DAILY_TABLE,
    GOALS_TABLE,
    WORK_STATUS_OPTIONS,
)
from app.feishu.api import FeishuClient
from app.feishu.cards import (
    base_card,
    collapsible_panel,
    column,
    column_set,
    divider,
    field_label,
    form_container,
    input_element,
    md,
    open_url_button,
    select_element,
    submit_button,
)
from app.services.goals import load_goal_context, load_goal_record


logger = logging.getLogger(__name__)

FORM_NAME = "daily_report_form"
SUBMIT_ACTION = "daily_submit"
CONFIRM_ACTION = "daily_confirm"
REGENERATE_ACTION = "daily_regenerate"
CONFIRM_FORM_NAME = "daily_confirm_form"
CONFIRM_INPUT = "意见"
FOLLOWUP_ACTION = "daily_followup_submit"
FOLLOWUP_FORM = "daily_followup_form"
FOLLOWUP_INPUT = "补充说明"
_FOLLOWUP_MAX_ROUNDS = 1
# 产出过于笼统时直接追问（不依赖 LLM，保证可预期）
_VAGUE_OUTPUT_MAX_LEN = 6
_VAGUE_OUTPUT_QUESTION = "这次工作的具体产出是什么？（如：完成 X 份 / Y 个 Z）"
_NO_GOAL_TEXT = "（尚未配置本周目标，将在目标设置轮次补齐）"
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 分点式表单：任务与产出成对，第 1 组必填，其余选填
POINT_GROUPS = 3
_SEEN_EVENT_LIMIT = 500
_TASK_PLACEHOLDERS = (
    "如：完成日报卡片表单改造",
    "如：联调飞书长连接回调",
    "如：整理多维表格字段映射",
)
_OUTPUT_PLACEHOLDERS = (
    "如：改造完成并通过 3 个场景验证",
    "如：回调稳定在 1 秒内返回",
    "如：输出 1 份字段映射说明",
)
_NEXT_PLACEHOLDER = "如：明天接入 AI 整理与确认流程"
_PROBLEM_PLACEHOLDER = "阻塞点 / 依赖方 / 需要谁协助；无则留空"
_EXTRA_PLACEHOLDER = "临时插入、非计划内事项；无则留空"
_MATERIAL_PLACEHOLDER = "文档 / 链接 / PR，可多条；无则留空"
_HINT = "只写事实，不必润色；空项可跳过，提交后由 AI 帮你规整表达。"


@dataclass(frozen=True)
class DailyContext:
    """日报卡片所需全部展示数据。"""

    target_date: date
    week_goal: str
    previous_example: str
    project_options: tuple[str, ...]


def _field(record: dict[str, Any], name: str) -> str:
    return text_value(record.get("fields", {}).get(name))


def weekday_cn(day: date) -> str:
    return _WEEKDAY_CN[day.weekday()]


class DailyReportService:
    """日报卡片模板固定；数据注入与校验由本服务完成。"""

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
        # 已处理回调事件（防止飞书重试 / 重复点击造成重复处理）
        self._seen_events: OrderedDict[str, float] = OrderedDict()

    def _mark_event(self, event_id: str) -> bool:
        """首次出现返回 True；重复事件返回 False（幂等保护）。"""

        if not event_id:
            return True
        if event_id in self._seen_events:
            return False
        self._seen_events[event_id] = time.monotonic()
        while len(self._seen_events) > _SEEN_EVENT_LIMIT:
            self._seen_events.popitem(last=False)
        return True

    # ------------------------------------------------------------------ 数据

    async def _load_goal_record(self, day: date) -> dict[str, Any] | None:
        """取当周有效的目标记录（含长期/月度/本周目标）。"""

        return await load_goal_record(self.bitable, self.settings, day)

    async def _load_goal(self, day: date) -> str:
        record = await self._load_goal_record(day)
        return _field(record, "本周目标") if record else ""

    async def _load_goal_context(self, day: date) -> dict[str, str]:
        """供 AI 关联长短期目标、说明工作价值。"""

        return await load_goal_context(self.bitable, self.settings, day)

    async def _load_project_options(self) -> tuple[str, ...]:
        if not (self.bitable and self.settings.bitable_ready):
            return ()
        try:
            records = await self.bitable.list_records(GOALS_TABLE)
        except FeishuAPIError:
            logger.warning("读取项目（目标单位）失败，项目选项置空", exc_info=True)
            return ()
        seen: list[str] = []
        for record in records:
            unit = _field(record, "目标单位")
            status = _field(record, "状态")
            if unit and unit not in seen and status != "已暂停":
                seen.append(unit)
        return tuple(seen)

    async def _load_previous_example(self, day: date) -> str:
        if not (self.bitable and self.settings.bitable_ready):
            return ""
        try:
            records = await self.bitable.list_records(DAILY_TABLE)
        except FeishuAPIError:
            logger.warning("读取日报总表失败，前一日示例置空", exc_info=True)
            return ""
        yesterday = fmt_date(day - timedelta(days=1))
        for record in records:
            if _field(record, "日期") != yesterday:
                continue
            task = _field(record, "任务")
            output = _field(record, "实际产出")
            next_step = _field(record, "下一步")
            lines = [f"任务：{task}", f"实际产出：{output}"]
            if next_step:
                lines.append(f"下一步：{next_step}")
            return "\n".join(lines)
        return ""

    async def load_context(self, target_day: date) -> DailyContext:
        goal = await self._load_goal(target_day)
        projects = await self._load_project_options()
        example = await self._load_previous_example(target_day)
        return DailyContext(
            target_date=target_day,
            week_goal=goal or _NO_GOAL_TEXT,
            previous_example=example,
            project_options=projects,
        )

    # ------------------------------------------------------------------ 卡片

    def build_daily_card(self, ctx: DailyContext) -> dict[str, Any]:
        """固定模板；字段名引用 DAILY_TABLE，禁止 LLM 动态修改。

        布局目标：首屏紧凑、分点结构化、非必要内容折叠。
        """

        day = ctx.target_date
        card = base_card(f"{fmt_date(day)} · {weekday_cn(day)} 工作日报")
        elements: list[dict[str, Any]] = [
            md(f"**本周目标**：{ctx.week_goal}", text_size="small"),
            md(_HINT, text_size="small"),
            divider(),
            field_label("今日进展"),
        ]

        form_elements: list[dict[str, Any]] = []
        # 任务与产出成对分点：对应关系清晰，纵向高度远小于多个多行框
        for index in range(POINT_GROUPS):
            required = index == 0
            mark = " *" if required else ""
            form_elements.append(
                column_set(
                    [
                        column(
                            1,
                            [
                                input_element(
                                    f"任务_{index + 1}",
                                    f"要点 {index + 1}{mark}",
                                    _TASK_PLACEHOLDERS[index],
                                    required=required,
                                )
                            ],
                        ),
                        column(
                            1,
                            [
                                input_element(
                                    f"产出_{index + 1}",
                                    f"产出 {index + 1}{mark}",
                                    _OUTPUT_PLACEHOLDERS[index],
                                    required=required,
                                )
                            ],
                        ),
                    ]
                )
            )

        form_elements.append(
            input_element(
                "下一步",
                "下一步 *",
                _NEXT_PLACEHOLDER,
                required=True,
            )
        )
        form_elements.append(self._build_selectors(ctx))
        form_elements.append(
            collapsible_panel(
                "**补充信息（选填）**　点击展开：遇到的问题 / 计划外工作 / 相关资料",
                [
                    input_element(
                        "遇到的问题",
                        "遇到的问题",
                        _PROBLEM_PLACEHOLDER,
                        multiline=True,
                        rows=2,
                    ),
                    input_element(
                        "计划外工作",
                        "计划外工作",
                        _EXTRA_PLACEHOLDER,
                        multiline=True,
                        rows=2,
                    ),
                    input_element(
                        "相关资料",
                        "相关资料",
                        _MATERIAL_PLACEHOLDER,
                        multiline=True,
                        rows=2,
                    ),
                ],
            )
        )
        form_elements.append(submit_button(SUBMIT_ACTION, "提交日报"))
        elements.append(form_container(FORM_NAME, form_elements))

        if ctx.previous_example:
            elements.append(
                collapsible_panel(
                    "**参考：昨日记录**　点击展开",
                    [md(ctx.previous_example, text_size="small")],
                )
            )
        card["body"]["elements"] = elements
        return card

    def _build_selectors(self, ctx: DailyContext) -> dict[str, Any]:
        """项目 / 状态 / 工作负担：一行三列，压缩纵向空间。"""

        if ctx.project_options:
            project_element = select_element(
                "项目",
                ctx.project_options,
                required=True,
                placeholder="选择项目",
            )
        else:
            project_element = input_element(
                "项目",
                "项目 *",
                "如：YoMi 助手",
                required=True,
            )
        return column_set(
            [
                column(1, [project_element]),
                column(
                    1,
                    [
                        select_element(
                            "状态",
                            WORK_STATUS_OPTIONS,
                            required=True,
                            placeholder="状态",
                        )
                    ],
                ),
                column(
                    1,
                    [
                        select_element(
                            "工作负担",
                            BURDEN_OPTIONS,
                            placeholder="工作负担",
                        )
                    ],
                ),
            ]
        )

    # ------------------------------------------------------------------ 发送

    async def push_daily(self, target_day: date | None = None) -> str:
        day = target_day or today_local(self.settings.app_timezone)
        if not self.settings.feishu_dry_run and not self.settings.feishu_user_open_id:
            raise ConfigurationError("未配置 FEISHU_USER_OPEN_ID，无法推送日报卡片")
        ctx = await self.load_context(day)
        card = self.build_daily_card(ctx)
        return await self.feishu.send_card(self.settings.feishu_user_open_id, card)

    async def has_submitted_today(self, day: date | None = None) -> bool:
        """当天是否已有日报记录：已由用户主动填写则无需再提醒。"""

        if not (self.bitable and self.settings.bitable_ready):
            return False
        target = day or today_local(self.settings.app_timezone)
        try:
            existing = await self.bitable.find_by_unique(DAILY_TABLE, fmt_date(target))
        except FeishuAPIError:
            logger.warning("检查当日日报是否存在失败", exc_info=True)
            return False
        return existing is not None

    async def scheduled_push(self) -> str:
        """22:00 定时提醒：当天已填写过日报则自动跳过。"""

        day = today_local(self.settings.app_timezone)
        if await self.has_submitted_today(day):
            logger.info("当天日报已提交，跳过 %s 的定时提醒", fmt_date(day))
            return ""
        return await self.push_daily(day)

    # ------------------------------------------------------------------ 回调

    async def handle_card_action(self, event: dict[str, Any]) -> None:
        """处理日报卡片回调：表单提交 / 确认 / 重新生成。"""

        action = event.get("action", {})
        form_value = action.get("form_value")
        value = action.get("value") or {}
        if not isinstance(form_value, dict) and not isinstance(value, dict):
            logger.info("忽略未知卡片回调: %s", action.get("tag"))
            return

        event_id = str(event.get("event_id") or "")
        if not self._mark_event(event_id):
            logger.info("重复回调事件已忽略: %s", event_id)
            return

        open_id = (
            (event.get("operator", {}) or {}).get("open_id")
            or self.settings.feishu_user_open_id
            or ""
        )
        action_name = str(action.get("name") or (value or {}).get("action") or "").strip()
        if action_name == FOLLOWUP_ACTION:
            await self._handle_followup(open_id, value, form_value or {})
            return
        if action_name in (CONFIRM_ACTION, REGENERATE_ACTION):
            await self._handle_confirm(open_id, action_name, value, form_value or {})
            return

        # 兜底：部分版本回调把表单值放进 value 顶层（过滤 action 标识本身）。
        values: dict[str, Any] = form_value or {
            k: v for k, v in value.items() if k != "action"
        }
        if not values:
            logger.info("回调不包含表单数据，忽略")
            return

        result = validate_daily_values(values)
        if not result["ok"]:
            logger.info("日报校验未通过: %s", result["errors"])
            if open_id and not self.settings.feishu_dry_run:
                await self.feishu.send_text(
                    open_id,
                    "日报未通过校验，请修正后重新提交：\n- " + "\n- ".join(result["errors"]),
                )
            return

        data = result["data"]
        logger.info("收到日报提交: %s", json.dumps(data, ensure_ascii=False))

        if self.settings.feishu_dry_run:
            refined = self._fallback_refine(data)
            logger.info("[dry-run] 跳过落表，AI整理=%s", json.dumps(refined, ensure_ascii=False))
            return
        if not open_id:
            logger.warning("无法确定提交人 open_id，跳过回执")
            return

        goal_ctx = await self._load_goal_context(today_local(self.settings.app_timezone))
        refined = await self._refine(data, goal=goal_ctx)
        try:
            record_id, record_url = await self._save_daily(data, refined)
        except (FeishuAPIError, ConfigurationError) as exc:
            logger.exception("日报写入飞书失败")
            await self.feishu.send_text(open_id, f"日报整理完成，但写入总表失败：{exc}")
            return
        missing = await self._check_missing(data, goal_ctx)
        if missing:
            await self._send_followup_card(open_id, data, record_id, missing)
            return
        await self._send_confirm_card(
            open_id, data, refined, record_id, record_url=record_url
        )

    # ------------------------------------------------------------------ 关键缺失追问

    async def _check_missing(
        self, data: dict[str, str], goal: dict[str, str] | None = None
    ) -> dict[str, str] | None:
        """AI 判断关键信息是否缺失；不需要追问或 AI 不可用时返回 None。"""

        if self.llm is None or not self.settings.llm_ready:
            return self._vague_output_missing(data)
        vague = self._vague_output_missing(data)
        if vague:
            return vague
        payload = {
            "任务": data.get("任务", ""),
            "实际产出": data.get("实际产出", ""),
            "下一步": data.get("下一步", ""),
            "遇到的问题": data.get("遇到的问题", ""),
            "本周目标": (goal or {}).get("本周目标", ""),
        }
        try:
            raw = await self.llm.complete_json(
                DAILY_MISSING_SYSTEM, build_daily_missing_user(payload)
            )
        except LLMError as exc:
            logger.warning("关键缺失检查失败，跳过追问: %s", exc)
            return None
        if str(raw.get("缺失")).lower() not in ("true", "1"):
            return None
        question = text_value(raw.get("追问"))
        field = text_value(raw.get("字段"))
        if not question or field not in ("任务", "实际产出", "下一步", "遇到的问题"):
            return None
        return {"追问": question, "字段": field}

    @staticmethod
    def _vague_output_missing(data: dict[str, str]) -> dict[str, str] | None:
        """产出过短/过于笼统时的确定性追问规则。"""

        raw = re.sub(r"^\s*\d+[.、)]\s*", "", (data.get("实际产出") or "").strip())
        if not raw:
            return {"追问": _VAGUE_OUTPUT_QUESTION, "字段": "实际产出"}
        if len(raw) <= _VAGUE_OUTPUT_MAX_LEN:
            return {"追问": _VAGUE_OUTPUT_QUESTION, "字段": "实际产出"}
        return None

    async def _send_followup_card(
        self,
        open_id: str,
        data: dict[str, str],
        record_id: str,
        missing: dict[str, str],
    ) -> None:
        field = missing["字段"]
        # 记录追问轮次与字段，避免重复追问（同一天最多追问一次）
        marker = dict(data, _追问轮次=_FOLLOWUP_MAX_ROUNDS, _追问字段=field)
        await self.bitable.update_record(
            DAILY_TABLE,
            record_id,
            {"原始记录": json.dumps(marker, ensure_ascii=False), "更新时间": self._now()},
        )
        card = base_card("日报补充确认", color="orange")
        elements: list[dict[str, Any]] = [
            md(f"**需要补充一点信息**\n{missing['追问']}"),
            md("（已按当前内容暂存，补充后会自动重新整理）", text_size="small"),
        ]
        elements.append(
            form_container(
                FOLLOWUP_FORM,
                [
                    input_element(
                        FOLLOWUP_INPUT,
                        f"补充 {field}",
                        "一句话说明即可，如：完成 8 个部门的费用核对",
                        required=True,
                    ),
                    submit_button(
                        FOLLOWUP_ACTION,
                        "提交补充",
                        {"action": FOLLOWUP_ACTION, "record_id": record_id, "字段": field},
                    ),
                ],
            )
        )
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)

    async def _handle_followup(
        self, open_id: str, value: dict[str, Any], form_value: dict[str, Any]
    ) -> None:
        answer = text_value(form_value.get(FOLLOWUP_INPUT))
        if not answer:
            return
        if self.settings.feishu_dry_run:
            logger.info("[dry-run] 收到日报补充: %s", answer)
            return
        if not (self.bitable and self.settings.bitable_ready):
            return
        record_id = str((value or {}).get("record_id") or "") or await self._resolve_daily_record_id()
        if not record_id:
            return
        record = await self.bitable.get_record(DAILY_TABLE, record_id)
        if not record:
            return
        raw_record = record["fields"].get("原始记录")
        try:
            stored = json.loads(raw_record) if isinstance(raw_record, str) and raw_record else {}
        except ValueError:
            stored = {}
        data = {
            k: text_value(v) for k, v in stored.items() if not str(k).startswith("_")
        }
        if not data:
            logger.warning("补充回调缺少原始记录: %s", record_id)
            return
        field = text_value((value or {}).get("字段")) or text_value(stored.get("_追问字段")) or "实际产出"
        if field not in ("任务", "实际产出", "下一步", "遇到的问题"):
            field = "实际产出"
        data[field] = (data.get(field, "") + "\n1. " + answer).strip()

        goal_ctx = await self._load_goal_context(today_local(self.settings.app_timezone))
        refined = await self._refine(data, goal=goal_ctx)
        fields: dict[str, Any] = {
            "任务": data.get("任务", ""),
            "实际产出": data.get("实际产出", ""),
            "下一步": data.get("下一步", ""),
            "遇到的问题": data.get("遇到的问题", ""),
            "原始记录": json.dumps(
                dict(data, _追问轮次=_FOLLOWUP_MAX_ROUNDS, _已补充=True),
                ensure_ascii=False,
            ),
            "AI整理": self._format_refined(refined),
            "AI简要分析": refined.get("AI简要分析", ""),
            "确认状态": "待确认",
            "更新时间": self._now(),
        }
        await self.bitable.update_record(DAILY_TABLE, record_id, fields)
        logger.info("日报已补充并重新整理: %s", record_id)
        if open_id:
            await self._send_confirm_card(
                open_id,
                data,
                refined,
                record_id,
                record_url=await self._record_url(record_id),
                regenerated=True,
            )

    # ------------------------------------------------------------------ AI 整理

    @staticmethod
    def _fallback_refine(data: dict[str, str]) -> dict[str, Any]:
        return {
            "任务": normalize_points(data.get("任务", "")),
            "实际产出": normalize_points(data.get("实际产出", "")),
            "下一步": text_value(data.get("下一步", "")),
            "AI简要分析": "",
            "ai_used": False,
        }

    async def _refine(
        self,
        data: dict[str, str],
        *,
        goal: dict[str, str] | None = None,
        feedback: str = "",
    ) -> dict[str, Any]:
        """把简要要点串联成句；AI 不可用时回退原始分点，保证数据不丢。

        规则：单条 ≤35 字、任务合计 ≤30 字、产出合计 ≤20 字、不虚构不扩写；
        原始输入单独保留在"任务/实际产出"，AI 结果只写"AI整理"。
        """

        fallback = self._fallback_refine(data)
        if self.llm is None or not self.settings.llm_ready:
            return fallback

        payload = {
            "项目": data.get("项目", ""),
            "状态": data.get("状态", ""),
            "任务要点": data.get("任务", ""),
            "实际产出": data.get("实际产出", ""),
            "下一步": data.get("下一步", ""),
            "遇到的问题": data.get("遇到的问题", ""),
            "计划外工作": data.get("计划外工作", ""),
            "工作负担": data.get("工作负担", ""),
        }
        if goal:
            payload.update({k: v for k, v in goal.items() if v})
        system = DAILY_REFINE_SYSTEM
        if feedback:
            payload["修改意见"] = feedback
            system = DAILY_REGENERATE_SYSTEM
        try:
            raw = await self.llm.complete_json(
                system, build_daily_refine_user(payload)
            )
        except LLMError as exc:
            logger.warning("AI 整理失败，回退原始分点: %s", exc)
            return fallback

        return {
            "任务": normalize_points(raw.get("任务"), total_limit=30) or fallback["任务"],
            "实际产出": (
                normalize_points(raw.get("实际产出"), total_limit=20)
                or fallback["实际产出"]
            ),
            "下一步": text_value(raw.get("下一步")) or fallback["下一步"],
            "AI简要分析": text_value(raw.get("AI简要分析")),
            "ai_used": True,
        }

    # ------------------------------------------------------------------ 落表

    async def _save_daily(
        self, data: dict[str, str], refined: dict[str, Any]
    ) -> tuple[str, str]:
        """写入日报总表：同一天一条，重复提交更新原记录；返回 (record_id, 记录链接)。"""

        if not (self.bitable and self.settings.bitable_ready):
            raise ConfigurationError("未配置多维表格，无法写入日报总表")

        day = today_local(self.settings.app_timezone)
        day_key = fmt_date(day)
        goal = await self._load_goal(day)
        fields: dict[str, Any] = {
            "日期": day_key,
            "项目": data.get("项目", ""),
            "本周目标": goal,
            "状态": data.get("状态", ""),
            "任务": data.get("任务", ""),
            "实际产出": data.get("实际产出", ""),
            "下一步": data.get("下一步", ""),
            "遇到的问题": data.get("遇到的问题", ""),
            "计划外工作": data.get("计划外工作", ""),
            "工作负担": data.get("工作负担", ""),
            "相关资料": data.get("相关资料", ""),
            "原始记录": json.dumps(data, ensure_ascii=False),
            "AI整理": self._format_refined(refined),
            "AI简要分析": refined.get("AI简要分析", ""),
            "确认状态": "待确认",
            "更新时间": self._now(),
        }
        existing = await self.bitable.find_by_unique(DAILY_TABLE, day_key)
        if existing:
            record_id = str(existing["record_id"])
            await self.bitable.update_record(DAILY_TABLE, record_id, fields)
            logger.info("日报已更新: %s", record_id)
        else:
            record_id = await self.bitable.add_record(DAILY_TABLE, fields)
            logger.info("日报已新增: %s", record_id)
        store = LocalStore(self.settings.data_path)
        store.backup_record(DAILY_TABLE.title, {"record_id": record_id, "fields": fields})
        store.log_event("daily_saved", {"record_id": record_id, "date": day_key})
        return record_id, await self._record_url(record_id)

    async def _record_url(self, record_id: str) -> str:
        """取该记录的官方访问链接（含租户域名的 record_url）。"""

        if not (self.bitable and self.settings.bitable_ready):
            return ""
        try:
            record = await self.bitable.get_record(DAILY_TABLE, record_id)
        except FeishuAPIError:
            logger.warning("获取日报记录链接失败", exc_info=True)
            return ""
        return str((record or {}).get("record_url") or "")

    @staticmethod
    def _format_refined(refined: dict[str, Any]) -> str:
        parts: list[str] = []
        if refined.get("任务"):
            parts.append("任务：\n" + join_points(refined["任务"]))
        if refined.get("实际产出"):
            parts.append("实际产出：\n" + join_points(refined["实际产出"]))
        return "\n".join(parts)

    def _now(self) -> str:
        return datetime.now(ZoneInfo(self.settings.app_timezone)).strftime("%Y-%m-%d %H:%M")

    # ------------------------------------------------------------------ 确认 / 重新生成

    async def _send_confirm_card(
        self,
        open_id: str,
        data: dict[str, str],
        refined: dict[str, Any],
        record_id: str,
        *,
        regenerated: bool = False,
        record_url: str = "",
    ) -> None:
        """确认卡片：默认已保存，可确认或填写意见后重新生成。"""

        lines = [f"**项目**：{data.get('项目') or '-'}　**状态**：{data.get('状态') or '-'}"]
        if refined.get("任务"):
            lines.append("\n**今日要点**\n" + join_points(refined["任务"]))
        if refined.get("实际产出"):
            lines.append("\n**实际产出**\n" + join_points(refined["实际产出"]))
        if refined.get("下一步"):
            lines.append(f"\n**下一步**：{refined['下一步']}")
        if refined.get("AI简要分析"):
            lines.append(f"\n**简要分析**：{refined['AI简要分析']}")
        title = "日报已整理 · 待确认" + ("（已重新生成）" if regenerated else "")
        card = base_card(title, color="blue")
        hint = (
            "已按当前内容保存；如不满意，填写意见后点「重新生成」。"
            if refined.get("ai_used")
            else "已按原始表达保存（AI 未启用）。"
        )
        if record_url:
            hint += f"\n[在日报总表中查看本条记录]({record_url})"
        elements: list[dict[str, Any]] = [md("\n".join(lines)), md(hint, text_size="small")]
        if record_url:
            elements.append(open_url_button("查看日报总表记录", record_url))
        elements.append(
            form_container(
                CONFIRM_FORM_NAME,
                [
                    input_element(
                        CONFIRM_INPUT,
                        "修改意见（选填）",
                        "如：更突出与本周目标的关联 / 产出描述不够具体",
                        multiline=True,
                        rows=2,
                    ),
                    column_set(
                        [
                            column(
                                1,
                                [
                                    submit_button(
                                        CONFIRM_ACTION,
                                        "内容可以",
                                        {"action": CONFIRM_ACTION, "record_id": record_id},
                                    )
                                ],
                            ),
                            column(
                                1,
                                [
                                    submit_button(
                                        REGENERATE_ACTION,
                                        "重新生成",
                                        {
                                            "action": REGENERATE_ACTION,
                                            "record_id": record_id,
                                        },
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
        await self.feishu.send_card(open_id, card)

    async def _handle_confirm(
        self,
        open_id: str,
        action_name: str,
        value: dict[str, Any],
        form_value: dict[str, Any],
    ) -> None:
        record_id = str((value or {}).get("record_id") or "").strip()
        feedback = text_value(form_value.get(CONFIRM_INPUT))
        if self.settings.feishu_dry_run:
            logger.info("[dry-run] 确认动作=%s record=%s 意见=%s", action_name, record_id, feedback)
            return
        if not (self.bitable and self.settings.bitable_ready):
            logger.warning("未配置多维表格，无法处理确认动作")
            return

        # 飞书表单提交按钮不回传 value，需要按当天日报记录定位
        if not record_id:
            record_id = await self._resolve_daily_record_id()
        if not record_id:
            logger.warning("未能定位待确认的日报记录，动作=%s", action_name)
            if open_id:
                await self.feishu.send_text(open_id, "未找到对应的日报记录，请重新提交卡片。")
            return

        record = await self.bitable.get_record(DAILY_TABLE, record_id)
        if record is None:
            logger.warning("确认回调对应记录不存在: %s", record_id)
            if open_id:
                await self.feishu.send_text(open_id, "未找到对应日报记录，可能已被删除。")
            return

        # 历史卡片防冲突：只有"待确认"状态才允许确认 / 重新生成
        status = str(record["fields"].get("确认状态") or "")
        if status != "待确认":
            logger.info(
                "历史卡片操作已失效: record=%s 状态=%s 动作=%s", record_id, status, action_name
            )
            if open_id:
                await self.feishu.send_text(
                    open_id,
                    f"该日报当前状态为「{status}」，本次操作已失效，无需重复点击。",
                )
            return

        if action_name == CONFIRM_ACTION:
            await self.bitable.update_record(
                DAILY_TABLE, record_id, {"确认状态": "用户确认", "更新时间": self._now()}
            )
            logger.info("用户已确认日报: %s", record_id)
            if open_id:
                await self._send_confirmed_notice(open_id, record)
            return

        # 重新生成：原始输入保持不变，仅更新 AI 整理结果
        raw_record = record["fields"].get("原始记录")
        try:
            data = json.loads(raw_record) if isinstance(raw_record, str) and raw_record else {}
        except ValueError:
            data = {}
        if not data:
            logger.warning("原始记录缺失，无法重新生成: %s", record_id)
            if open_id:
                await self.feishu.send_text(open_id, "缺少原始记录，无法重新生成，请重新提交卡片。")
            return

        goal_ctx = await self._load_goal_context(today_local(self.settings.app_timezone))
        refined = await self._refine(data, goal=goal_ctx, feedback=feedback)
        await self.bitable.update_record(
            DAILY_TABLE,
            record_id,
            {
                "AI整理": self._format_refined(refined),
                "AI简要分析": refined.get("AI简要分析", ""),
                "确认状态": "待确认",
                "更新时间": self._now(),
            },
        )
        logger.info("已按用户意见重新生成日报: %s 意见=%s", record_id, feedback)
        if open_id:
            await self._send_confirm_card(
                open_id,
                data,
                refined,
                record_id,
                regenerated=True,
                record_url=str(record.get("record_url") or ""),
            )

    async def _send_confirmed_notice(self, open_id: str, record: dict[str, Any]) -> None:
        """确认回执：附飞书多维表格记录链接，便于后续自行查看。"""

        fields = record.get("fields", {})
        record_url = str(record.get("record_url") or "")
        lines = [
            "已确认，本次日报已存档。",
            f"**日期**：{text_value(fields.get('日期')) or '-'}　"
            f"**项目**：{text_value(fields.get('项目')) or '-'}　"
            f"**状态**：{text_value(fields.get('状态')) or '-'}",
        ]
        card = base_card("日报已确认存档", color="green")
        elements: list[dict[str, Any]] = [md("\n".join(lines))]
        if record_url:
            elements.append(md(f"[在日报总表中查看本条记录]({record_url})", text_size="small"))
            elements.append(open_url_button("查看日报总表记录", record_url, primary=True))
        else:
            elements.append(md("未取到记录链接，可打开日报总表查看。", text_size="small"))
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)

    async def _resolve_daily_record_id(self) -> str:
        """定位待处理的日报记录：优先当天、其次最近的待确认记录。"""

        if not (self.bitable and self.settings.bitable_ready):
            return ""
        day_key = fmt_date(today_local(self.settings.app_timezone))
        try:
            records = await self.bitable.list_records(DAILY_TABLE)
        except FeishuAPIError:
            logger.exception("定位日报记录失败")
            return ""
        pending: list[dict[str, Any]] = []
        for record in records:
            fields = record.get("fields", {})
            if str(fields.get("日期")) == day_key:
                return str(record["record_id"])
            if str(fields.get("确认状态") or "") == "待确认":
                pending.append(record)
        if pending:
            pending.sort(key=lambda r: str(r.get("fields", {}).get("更新时间") or ""), reverse=True)
            return str(pending[0]["record_id"])
        return ""

    # ------------------------------------------------------------------ 超时收尾

    async def finalize_pending(self) -> int:
        """当天 24:00 前未点"重新生成"的日报标记为超时自动保存。"""

        if self.settings.feishu_dry_run or not (self.bitable and self.settings.bitable_ready):
            return 0
        day_key = fmt_date(today_local(self.settings.app_timezone))
        try:
            records = await self.bitable.list_records(DAILY_TABLE)
        except FeishuAPIError:
            logger.exception("超时收尾读取日报失败")
            return 0
        count = 0
        for record in records:
            fields = record.get("fields", {})
            if str(fields.get("日期")) != day_key:
                continue
            if str(fields.get("确认状态") or "") != "待确认":
                continue
            await self.bitable.update_record(
                DAILY_TABLE,
                record["record_id"],
                {"确认状态": "超时自动保存", "更新时间": self._now()},
            )
            count += 1
        if count:
            logger.info("超时自动保存日报 %s 条", count)
        return count
