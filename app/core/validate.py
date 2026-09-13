"""确定性的字段校验纯函数（不依赖飞书 / LLM）。

字段名一律取自 app.data.tables 中的字段定义，禁止在业务代码中散落硬编码。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.data.tables import BURDEN_OPTIONS, DAILY_TABLE, WORK_STATUS_OPTIONS


# 分点式表单：多个"要点 / 产出"输入合并回日报表字段
POINT_FIELDS: dict[str, tuple[str, ...]] = {
    "任务": ("任务_1", "任务_2", "任务_3"),
    "实际产出": ("产出_1", "产出_2", "产出_3"),
}


def text_value(value: Any) -> str:
    """把飞书回调中的各种取值形态规整为纯文本。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return str(value).strip()


def merge_point_values(form_value: Mapping[str, Any]) -> dict[str, Any]:
    """把分点输入合并为日报表字段：编号列出、跳过空项。

    兼容直接提交整段文本（旧卡片）的情况：若分点键全为空则保留原值。
    """

    combined = dict(form_value)
    for target, keys in POINT_FIELDS.items():
        points = [text_value(form_value.get(key)) for key in keys]
        points = [p for p in points if p]
        if not points:
            continue
        combined[target] = "\n".join(f"{i}. {p}" for i, p in enumerate(points, start=1))
    return combined


def normalize_daily_values(form_value: Mapping[str, Any]) -> dict[str, str]:
    """把卡片表单回调值规整为 {表字段名: 文本} 映射。"""

    merged = merge_point_values(form_value)
    return {name: text_value(merged.get(name)) for name in DAILY_TABLE.field_names}


def _required_fields() -> tuple[str, ...]:
    # 手写量最小化：日报仅“任务 / 实际产出 / 下一步”必须手写，
    # 其余以选择为主；项目与状态也作为必填项以保证落库结构完整。
    return ("项目", "状态", "任务", "实际产出", "下一步")


def validate_daily_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """校验并返回 {ok, errors, data}；只做确定性规则检查。"""

    normalized = normalize_daily_values(values)
    errors: list[str] = []

    for name in _required_fields():
        if not normalized.get(name):
            errors.append(f"缺少必填项：{name}")

    status = normalized.get("状态", "")
    if status and status not in WORK_STATUS_OPTIONS:
        errors.append(f"状态选项非法：{status}")

    burden = normalized.get("工作负担", "")
    if burden and burden not in BURDEN_OPTIONS:
        errors.append(f"工作负担选项非法：{burden}")

    # 未知字段不应进入后续流程，防止脏数据外溢。
    known = DAILY_TABLE.field_names
    data = {name: normalized[name] for name in known if normalized.get(name)}
    return {"ok": not errors, "errors": errors, "data": data}


def option_is_valid(value: str, options: Iterable[str]) -> bool:
    return value in set(options)
