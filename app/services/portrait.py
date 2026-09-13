"""月度能力画像：汇总当月周报 → 月度总结（工作总结 + 能力分析）→ 更新能力画像表。

设计原则（对应计划书模块 06）：
- 月度频次硬约束：一个月一条记录，按「月份」去重更新；
- 评分可追溯：只用当月周报的能力评分与用户反馈，数据来源写入画像表；
- 不让 LLM 自由增加能力维度。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.ai.llm import LLMClient
from app.ai.parse import join_points, normalize_points
from app.ai.prompts import MONTHLY_SYSTEM, build_monthly_user
from app.core.config import Settings
from app.core.cycles import fmt_date, month_bounds, previous_month_bounds, today_local
from app.core.exceptions import ConfigurationError, FeishuAPIError, LLMError
from app.data.bitable import BitableClient
from app.data.local_store import LocalStore
from app.data.tables import ABILITY_DIMENSIONS, PORTRAIT_TABLE, WEEKLY_TABLE
from app.feishu.api import FeishuClient
from app.feishu.cards import base_card, divider, md, open_url_button
from app.services.goals import field_text


logger = logging.getLogger(__name__)

SUMMARY_SECTIONS = ("工作总结", "能力分析")
PORTRAIT_FIELDS = ("当前能力状态", "主要优势", "主要问题", "改进方向", "下一阶段建议")


class PortraitService:
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

    # ------------------------------------------------------------------ 数据

    async def month_weeklies(self, month_key: str) -> list[dict[str, Any]]:
        """取「周起始」落在该月的周报（避免跨月重复计入）。"""

        if not (self.bitable and self.settings.bitable_ready):
            raise ConfigurationError("未配置多维表格，无法生成画像")
        year, month = (int(part) for part in month_key.split("-"))
        start, end = month_bounds(year, month)
        records = await self.bitable.list_records(WEEKLY_TABLE, page_size=200)
        picked = [
            r
            for r in records
            if field_text(r, "周起始")
            and fmt_date(start) <= field_text(r, "周起始") <= fmt_date(end)
        ]
        picked.sort(key=lambda r: field_text(r, "周起始"))
        return picked

    async def history_portraits(self, month_key: str, limit: int = 6) -> list[dict[str, Any]]:
        if not (self.bitable and self.settings.bitable_ready):
            return []
        records = await self.bitable.list_records(PORTRAIT_TABLE, page_size=200)
        history = [r for r in records if field_text(r, "月份") < month_key]
        history.sort(key=lambda r: field_text(r, "月份"), reverse=True)
        return history[:limit]

    # ------------------------------------------------------------------ 生成

    async def generate(self, month_key: str | None = None) -> dict[str, Any]:
        """生成月度总结与能力画像并写入画像表。"""

        if not (self.bitable and self.settings.bitable_ready):
            raise ConfigurationError("未配置多维表格，无法生成画像")

        if not month_key:
            prev_start, _ = previous_month_bounds(today_local(self.settings.app_timezone))
            month_key = f"{prev_start.year:04d}-{prev_start.month:02d}"

        weeklies = await self.month_weeklies(month_key)
        if not weeklies:
            return {
                "ok": False,
                "message": f"{month_key} 没有周报记录，先在每周六生成周报后再做月度画像。",
            }
        if self.llm is None or not self.settings.llm_ready:
            raise ConfigurationError("未配置 AI，无法生成画像")

        payload: dict[str, Any] = {
            "月份": month_key,
            "周报": [
                {
                    "周期": field_text(r, "周期"),
                    "本周目标": field_text(r, "本周目标"),
                    "工作成果": field_text(r, "工作成果"),
                    "工作分析": field_text(r, "工作分析"),
                    "问题处理": field_text(r, "问题处理"),
                    "用户反馈": field_text(r, "用户反馈"),
                    "能力评分": {
                        dim: field_text(r, f"能力评分_{dim}") for dim in ABILITY_DIMENSIONS
                    },
                }
                for r in weeklies
            ],
        }
        history = await self.history_portraits(month_key)
        if history:
            payload["历史画像"] = [
                {
                    "月份": field_text(r, "月份"),
                    **{dim: field_text(r, dim) for dim in ABILITY_DIMENSIONS},
                }
                for r in history
            ]

        try:
            raw = await self.llm.complete_json(
                MONTHLY_SYSTEM,
                build_monthly_user(payload),
                # 月度输入长、输出字段多，推理模型需更高上限
                max_tokens=max(self.settings.llm_max_tokens, 4096),
            )
        except LLMError as exc:
            logger.warning("月度画像生成失败: %s", exc)
            return {"ok": False, "message": f"月度画像生成失败：{exc}"}

        summary = {
            name: normalize_points(raw.get(name), limit=40, max_items=5)
            for name in SUMMARY_SECTIONS
        }
        scores = self._normalize_scores(raw.get("能力评分"))
        narrative = {
            name: str(raw.get(name) or "").strip() for name in PORTRAIT_FIELDS
        }
        narrative["分析文字"] = str(raw.get("分析文字") or "").strip()
        periods = "、".join(field_text(r, "周期") for r in weeklies if field_text(r, "周期"))
        record_id, record_url = await self._save(
            month_key, summary, narrative, scores, periods
        )
        return {
            "ok": True,
            "month": month_key,
            "record_id": record_id,
            "record_url": record_url,
            "summary": summary,
            "narrative": narrative,
            "scores": scores,
            "source": periods,
        }

    @staticmethod
    def _normalize_scores(value: Any) -> dict[str, float]:
        """维度得分统一为 0~10 一位小数（画像表口径）。"""

        source = value if isinstance(value, dict) else {}
        result: dict[str, float] = {}
        for dim in ABILITY_DIMENSIONS:
            try:
                score = float(source.get(dim))
            except (TypeError, ValueError):
                score = 0.0
            if score > 10:  # 模型误用 0~100 口径时折算
                score = score / 10
            result[dim] = round(max(0.0, min(10.0, score)), 1)
        return result

    async def _save(
        self,
        month_key: str,
        summary: dict[str, list[str]],
        narrative: dict[str, str],
        scores: dict[str, float],
        source: str,
    ) -> tuple[str, str]:
        fields: dict[str, Any] = {
            "月份": month_key,
            "工作总结": join_points(summary["工作总结"]),
            "能力分析": join_points(summary["能力分析"]),
            "数据来源": source,
            "更新时间": self._now(),
            **{name: narrative.get(name, "") for name in PORTRAIT_FIELDS},
            "分析文字": narrative.get("分析文字", ""),
        }
        for dim, score in scores.items():
            fields[dim] = score

        existing = await self.bitable.find_by_unique(PORTRAIT_TABLE, month_key)
        if existing:
            record_id = str(existing["record_id"])
            await self.bitable.update_record(PORTRAIT_TABLE, record_id, fields)
            logger.info("能力画像已更新: %s", month_key)
        else:
            record_id = await self.bitable.add_record(PORTRAIT_TABLE, fields)
            logger.info("能力画像已新增: %s", month_key)
        store = LocalStore(self.settings.data_path)
        store.backup_record(PORTRAIT_TABLE.title, {"record_id": record_id, "fields": fields})
        store.log_event("portrait_saved", {"record_id": record_id, "month": month_key})
        record = await self.bitable.get_record(PORTRAIT_TABLE, record_id)
        return record_id, str((record or {}).get("record_url") or "")

    # ------------------------------------------------------------------ 展示

    def build_portrait_card(self, result: dict[str, Any]) -> dict[str, Any]:
        card = base_card(f"月度总结与能力画像 · {result.get('month', '')}", color="violet")
        elements: list[dict[str, Any]] = []
        for name in SUMMARY_SECTIONS:
            points = (result.get("summary") or {}).get(name) or []
            if points:
                elements.append(md(f"**{name}**\n" + join_points(points), text_size="small"))
        elements.append(divider())
        narrative = result.get("narrative") or {}
        lines = []
        for name in PORTRAIT_FIELDS:
            if narrative.get(name):
                lines.append(f"**{name}**：{narrative[name]}")
        if narrative.get("分析文字"):
            lines.append(f"\n{narrative['分析文字']}")
        if lines:
            elements.append(md("\n".join(lines)))
        scores = result.get("scores") or {}
        if scores:
            score_text = "　".join(f"{dim} {score}" for dim, score in scores.items())
            elements.append(md(f"**能力评分（0~10）**\n{score_text}", text_size="small"))
        if result.get("source"):
            elements.append(md(f"数据来源：{result['source']}", text_size="small"))
        if result.get("record_url"):
            elements.append(
                open_url_button("查看能力画像表记录", result["record_url"], primary=True)
            )
        card["body"]["elements"] = elements
        return card

    async def push_portrait(self, day: date | None = None) -> str:
        """每月 1 日定时/手动触发：生成上月画像并推送。"""

        if self.settings.feishu_dry_run:
            logger.info("[dry-run] 跳过月度画像生成与推送")
            return ""
        if not self.settings.feishu_user_open_id:
            raise ConfigurationError("未配置 FEISHU_USER_OPEN_ID，无法推送月度画像")
        target = today_local(self.settings.app_timezone) if day is None else day
        prev_start, _ = previous_month_bounds(target)
        result = await self.generate(f"{prev_start.year:04d}-{prev_start.month:02d}")
        if not result.get("ok"):
            logger.warning("月度画像未生成: %s", result.get("message"))
            return ""
        return await self.feishu.send_card(
            self.settings.feishu_user_open_id, self.build_portrait_card(result)
        )
