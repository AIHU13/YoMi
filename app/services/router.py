"""意图识别路由：把用户主动发来的消息分发到对应服务。

设计：规则优先（确定性、零成本）+ LLM 兜底（模糊表达）；
超范围需求统一委婉回绝并说明本产品能力边界。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from app.ai.llm import LLMClient
from app.ai.parse import join_points
from app.ai.prompts import INTENT_SYSTEM
from app.core.config import Settings
from app.core.cycles import fmt_date, today_local
from app.core.exceptions import ConfigurationError, FeishuAPIError, LLMError
from app.data.tables import ABILITY_DIMENSIONS, GOALS_TABLE
from app.feishu.api import FeishuClient
from app.feishu.cards import base_card, divider, md, open_url_button
from app.services.daily import DailyReportService
from app.services.goals import field_text
from app.services.records import RecordQueryService
from app.services.week_goal import WeekGoalService
from app.services.weekly import WeeklyReportService


logger = logging.getLogger(__name__)

# 意图
GREETING = "greeting"
HELP = "help"
DAILY_START = "daily_start"
DAILY_QUERY = "daily_query"
WEEKLY_GENERATE = "weekly_generate"
WEEKLY_QUERY = "weekly_query"
PORTRAIT_QUERY = "portrait_query"
PORTRAIT_GENERATE = "portrait_generate"
WEEK_GOAL_SUGGEST = "week_goal_suggest"
WEEK_GOAL_VIEW = "week_goal_view"
STANDARDS_VIEW = "standards_view"
OUT_OF_SCOPE = "out_of_scope"

ALL_INTENTS = (
    GREETING,
    HELP,
    DAILY_START,
    DAILY_QUERY,
    WEEKLY_GENERATE,
    WEEKLY_QUERY,
    PORTRAIT_QUERY,
    PORTRAIT_GENERATE,
    WEEK_GOAL_SUGGEST,
    WEEK_GOAL_VIEW,
    STANDARDS_VIEW,
    OUT_OF_SCOPE,
)

INTRO = (
    "我是 YoMi，你的工作复盘助手：负责日报记录、周报整理、日报/周报查询与个人能力画像。\n"
    "直接说「写日报」「查昨天的日报」「整理周报」「看画像」就行。"
)
OUT_OF_SCOPE_REPLY = (
    "抱歉，这个需求不在我的能力范围内。\n"
    "我专注于工作记录与复盘：**日报记录、周报整理、日报/周报查询、个人能力画像**。\n"
    "上面这几类需求可以直接跟我说。"
)

_GREETING_WORDS = ("你好", "您好", "hi", "hello", "嗨", "在吗", "在么", "早上好", "晚上好", "下午好")
_HELP_WORDS = ("你是谁", "你是什么", "自我介绍", "介绍下你", "你能做什么", "能干什么", "帮助", "功能", "怎么用", "如何使用")
_VIEW_WORDS = (
    "查看",
    "查询",
    "查",
    "看",
    "链接",
    "回顾",
    "打开",
    "找一下",
    "最近的",
    "最近",
)
_DAILY_WORDS = ("日报",)
_WEEKLY_WORDS = ("周报",)
_PORTRAIT_WORDS = ("画像", "能力分析", "能力评估", "能力查询")
_PORTRAIT_GENERATE_WORDS = ("生成", "更新", "刷新", "月度总结", "月度分析", "总结一下", "做一次")
_GOAL_SUGGEST_WORDS = ("建议", "候选", "拆解", "定目标", "设置目标", "制定目标", "规划目标")
_STANDARDS_WORDS = ("工作规范", "周报格式", "汇报风格", "汇报要求", "团队要求", "规范")

_RELATIVE_DAYS = (
    ("今天", 0),
    ("今日", 0),
    ("昨天", -1),
    ("昨日", -1),
    ("前天", -2),
    ("本周", 0),
    ("这周", 0),
    ("本星期", 0),
    ("上周", -7),
)
_FULL_DATE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")
_SHORT_DATE = re.compile(r"(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日")


def extract_text(content: Any) -> str:
    """飞书文本消息内容为 JSON 字符串（{"text": "..."}）。"""

    if isinstance(content, dict):
        return str(content.get("text") or "").strip()
    if not isinstance(content, str):
        return ""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return str(data.get("text") or "").strip()
    except ValueError:
        pass
    return content.strip()


def parse_day(text: str, today: date) -> date | None:
    match = _FULL_DATE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = _SHORT_DATE.search(text)
    if match:
        try:
            return date(today.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None
    for word, delta in _RELATIVE_DAYS:
        if word in text:
            return today + timedelta(days=delta)
    return None


def classify_rules(text: str, today: date) -> tuple[str, date | None] | None:
    """规则优先的意图识别；无法判断返回 None 交给 LLM。"""

    raw = text.strip()
    if not raw:
        return None
    lowered = raw.lower()
    if len(raw) <= 12 and any(word in lowered for word in _GREETING_WORDS):
        return GREETING, None
    if any(word in raw for word in _HELP_WORDS):
        return HELP, None
    if any(word in raw for word in _PORTRAIT_WORDS):
        if any(word in raw for word in _PORTRAIT_GENERATE_WORDS):
            return PORTRAIT_GENERATE, parse_day(raw, today)
        return PORTRAIT_QUERY, parse_day(raw, today)
    wants_view = any(word in raw for word in _VIEW_WORDS)
    if any(word in raw for word in _WEEKLY_WORDS):
        return (WEEKLY_QUERY if wants_view else WEEKLY_GENERATE), parse_day(raw, today)
    if any(word in raw for word in _DAILY_WORDS):
        return (DAILY_QUERY if wants_view else DAILY_START), parse_day(raw, today)
    if any(word in raw for word in _STANDARDS_WORDS):
        return STANDARDS_VIEW, None
    if "目标" in raw:
        if any(word in raw for word in _GOAL_SUGGEST_WORDS):
            return WEEK_GOAL_SUGGEST, None
        return WEEK_GOAL_VIEW, None
    return None


class IntentRouter:
    """消息 → 意图 → 服务调用。"""

    def __init__(
        self,
        settings: Settings,
        feishu: FeishuClient,
        daily: DailyReportService,
        weekly: WeeklyReportService,
        records: RecordQueryService,
        week_goal: WeekGoalService | None = None,
        llm: LLMClient | None = None,
        portrait: Any | None = None,
    ) -> None:
        self.settings = settings
        self.feishu = feishu
        self.daily = daily
        self.weekly = weekly
        self.records = records
        self.week_goal = week_goal
        self.llm = llm
        self.portrait = portrait

    # ------------------------------------------------------------------ 意图

    async def classify(self, text: str, today: date) -> tuple[str, date | None]:
        ruled = classify_rules(text, today)
        if ruled is not None:
            return ruled
        if self.llm is not None and self.settings.llm_ready:
            try:
                raw = await self.llm.complete_json(INTENT_SYSTEM, text)
            except LLMError as exc:
                logger.warning("意图识别失败，按超范围处理: %s", exc)
            else:
                intent = str(raw.get("intent") or "").strip()
                if intent in ALL_INTENTS:
                    day: date | None = None
                    raw_date = str(raw.get("date") or "").strip()
                    if raw_date:
                        try:
                            day = date.fromisoformat(raw_date)
                        except ValueError:
                            day = None
                    return intent, day
        return OUT_OF_SCOPE, None

    # ------------------------------------------------------------------ 入口

    async def handle_message(self, payload: dict[str, Any]) -> None:
        chat_type = str(payload.get("chat_type") or "")
        open_id = str(payload.get("open_id") or "") or self.settings.feishu_user_open_id
        if chat_type and chat_type != "p2p":
            logger.info("忽略非单聊消息: chat_type=%s", chat_type)
            return
        if not open_id:
            logger.warning("消息缺少 open_id，无法回复")
            return

        message_type = str(payload.get("message_type") or "text")
        if message_type and message_type != "text":
            await self.feishu.send_text(
                open_id, "目前只支持文字消息，直接说「写日报 / 查昨天日报 / 整理周报 / 看画像」即可。"
            )
            return

        text = extract_text(payload.get("content"))
        if not text:
            return
        today = today_local(self.settings.app_timezone)
        intent, day = await self.classify(text, today)
        logger.info("意图识别: text=%r intent=%s day=%s", text, intent, day)
        try:
            await self._dispatch(intent, day, today, open_id)
        except (FeishuAPIError, ConfigurationError) as exc:
            logger.exception("处理用户请求失败: intent=%s", intent)
            await self.feishu.send_text(open_id, f"处理失败：{exc}")

    # ------------------------------------------------------------------ 分发

    async def _dispatch(
        self,
        intent: str,
        day: date | None,
        today: date,
        open_id: str,
    ) -> None:
        if intent in (GREETING, HELP):
            await self.feishu.send_text(open_id, INTRO)
            return
        if intent == DAILY_START:
            await self.daily.push_daily(today)
            return
        if intent == DAILY_QUERY:
            await self._reply_daily(open_id, day, today)
            return
        if intent == WEEKLY_GENERATE:
            result = await self.weekly.generate(day or today)
            if result.get("ok"):
                await self.feishu.send_card(
                    open_id,
                    self.weekly.build_weekly_card(
                        result, record_id=str(result.get("record_id") or "")
                    ),
                )
            else:
                await self.feishu.send_text(open_id, str(result.get("message") or "周报生成失败。"))
            return
        if intent == WEEKLY_QUERY:
            await self._reply_weekly(open_id, day, today)
            return
        if intent == PORTRAIT_QUERY:
            await self._reply_portrait(open_id)
            return
        if intent == PORTRAIT_GENERATE:
            if self.portrait is None:
                await self.feishu.send_text(open_id, "月度画像服务不可用，请稍后再试。")
                return
            month_key = f"{day.year:04d}-{day.month:02d}" if day else None
            result = await self.portrait.generate(month_key)
            if result.get("ok"):
                await self.feishu.send_card(
                    open_id, self.portrait.build_portrait_card(result)
                )
            else:
                await self.feishu.send_text(
                    open_id, str(result.get("message") or "月度画像生成失败。")
                )
            return
        if intent == WEEK_GOAL_SUGGEST:
            if self.week_goal is None:
                await self.feishu.send_text(open_id, "目标建议服务不可用，请稍后再试。")
                return
            await self.week_goal.suggest(day, open_id=open_id)
            return
        if intent == WEEK_GOAL_VIEW:
            await self._reply_goal(open_id, today)
            return
        if intent == STANDARDS_VIEW:
            await self._reply_standards(open_id)
            return
        await self.feishu.send_text(open_id, OUT_OF_SCOPE_REPLY)

    # ------------------------------------------------------------------ 回复

    async def _reply_daily(self, open_id: str, day: date | None, today: date) -> None:
        record = (
            await self.records.daily_on(day)
            if day
            else await self.records.latest_daily(today)
        )
        if not record:
            target = fmt_date(day) if day else "最近"
            await self.feishu.send_text(
                open_id, f"没有找到{target}的日报记录。要现在写今天的日报吗？直接回复「写日报」。"
            )
            return
        fields = record.get("fields", {})
        lines = [
            f"**日期**：{field_text(record, '日期')}　**项目**：{field_text(record, '项目') or '-'}",
            f"**状态**：{field_text(record, '状态') or '-'}　**确认**：{field_text(record, '确认状态') or '-'}",
        ]
        if field_text(record, "任务"):
            lines.append(f"\n**任务**\n{field_text(record, '任务')}")
        if field_text(record, "实际产出"):
            lines.append(f"\n**实际产出**\n{field_text(record, '实际产出')}")
        if field_text(record, "下一步"):
            lines.append(f"\n**下一步**：{field_text(record, '下一步')}")
        if field_text(record, "AI简要分析"):
            lines.append(f"\n**简要分析**：{field_text(record, 'AI简要分析')}")
        card = base_card(f"日报 · {field_text(record, '日期')}", color="blue")
        elements: list[dict[str, Any]] = [md("\n".join(lines))]
        if record.get("record_url"):
            elements.append(
                open_url_button("查看日报总表记录", record["record_url"], primary=True)
            )
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)

    async def _reply_weekly(self, open_id: str, day: date | None, today: date) -> None:
        record = (
            await self.records.weekly_on(day)
            if day
            else await self.records.latest_weekly(today)
        )
        if not record:
            target = fmt_date(day) if day else "最近"
            await self.feishu.send_text(
                open_id, f"没有找到{target}的周报记录。回复「整理周报」可以生成本周周报。"
            )
            return
        lines = [
            f"**周期**：{field_text(record, '周期')}",
            f"**区间**：{field_text(record, '周起始')} ~ {field_text(record, '周结束')}",
            f"**反馈状态**：{field_text(record, '反馈状态') or '-'}",
        ]
        if field_text(record, "本周目标"):
            lines.append(f"\n**本周目标**：{field_text(record, '本周目标')}")
        for name in ("工作成果", "问题处理", "下周重点", "工作分析", "改进建议"):
            if field_text(record, name):
                lines.append(f"\n**{name}**\n{field_text(record, name)}")
        scores = "　".join(
            f"{dim} {field_text(record, f'能力评分_{dim}')}"
            for dim in ABILITY_DIMENSIONS
            if field_text(record, f"能力评分_{dim}")
        )
        if scores:
            lines.append(f"\n**能力评分**：{scores}")
        card = base_card(f"周报 · {field_text(record, '周期')}", color="turquoise")
        elements: list[dict[str, Any]] = [md("\n".join(lines))]
        if record.get("record_url"):
            elements.append(
                open_url_button("查看周报总表记录", record["record_url"], primary=True)
            )
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)

    async def _reply_portrait(self, open_id: str) -> None:
        record = await self.records.latest_portrait()
        if not record:
            await self.feishu.send_text(open_id, "还没有能力画像记录，月度画像生成后即可查询。")
            return
        lines = [f"**月份**：{field_text(record, '月份')}"]
        for name in ("当前能力状态", "主要优势", "主要问题", "改进方向", "下一阶段建议"):
            if field_text(record, name):
                lines.append(f"\n**{name}**：{field_text(record, name)}")
        scores = "　".join(
            f"{dim} {field_text(record, dim)}"
            for dim in ABILITY_DIMENSIONS
            if field_text(record, dim)
        )
        if scores:
            lines.append(f"\n**维度得分**：{scores}")
        card = base_card(f"能力画像 · {field_text(record, '月份')}", color="violet")
        elements: list[dict[str, Any]] = [md("\n".join(lines))]
        if record.get("record_url"):
            elements.append(
                open_url_button("查看能力画像表记录", record["record_url"], primary=True)
            )
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)
    async def _reply_goal(self, open_id: str, today: date) -> None:
        record = await self._goal_record(today)
        if not record:
            await self.feishu.send_text(
                open_id, "还没有配置工作目标。回复「建议本周目标」我可以给你候选方案。"
            )
            return
        lines = [
            f"**目标单位**：{field_text(record, '目标单位') or '-'}",
            f"**周期**：{field_text(record, '周起始')} ~ {field_text(record, '周结束')}",
            f"**状态**：{field_text(record, '状态') or '-'}",
            f"\n**本周目标**：{field_text(record, '本周目标') or '-'}",
        ]
        if field_text(record, "月度目标"):
            lines.append(f"\n**月度目标**：{field_text(record, '月度目标')}")
        if field_text(record, "长期目标"):
            lines.append(f"\n**长期目标**：{field_text(record, '长期目标')}")
        card = base_card("当前工作目标", color="indigo")
        elements: list[dict[str, Any]] = [md("\n".join(lines))]
        if record.get("record_url"):
            elements.append(
                open_url_button("查看工作目标表记录", record["record_url"], primary=True)
            )
        card["body"]["elements"] = elements
        await self.feishu.send_card(open_id, card)

    async def _goal_record(self, day: date) -> dict[str, Any]:
        """目标记录 + 官方访问链接。"""

        from app.services.goals import load_goal_record

        record = await load_goal_record(self.records.bitable, self.settings, day)
        if not record or not self.records.bitable:
            return {}
        url = ""
        try:
            full = await self.records.bitable.get_record(
                GOALS_TABLE, str(record["record_id"])
            )
            url = str((full or {}).get("record_url") or "")
        except FeishuAPIError:
            logger.warning("获取目标记录链接失败", exc_info=True)
        return {**record, "record_url": url}

    async def _reply_standards(self, open_id: str) -> None:
        from app.services.goals import load_standards

        standards = await load_standards(self.records.bitable, self.settings)
        if not standards:
            await self.feishu.send_text(open_id, "还没有配置工作规范。")
            return
        lines = [f"**{standards.get('规范名称') or '工作规范'}**"]
        for name in ("周报格式", "汇报风格", "必要字段", "企业/团队要求"):
            if standards.get(name):
                lines.append(f"\n**{name}**：{standards[name]}")
        await self.feishu.send_text(open_id, "\n".join(lines))
