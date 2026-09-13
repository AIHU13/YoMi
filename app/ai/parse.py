"""结构化输出解析与兜底校验。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*[.、)）]\s*")
_CLAUSE_SPLIT = re.compile(r"(?<=[，,、；;])")


def parse_json_object(text: str) -> dict[str, Any]:
    """解析模型返回的 JSON。

    容错策略（依次尝试）：
    1. 去掉 ```json 代码块与首尾空白；
    2. 截取第一个 ``{`` 到最后一个 ``}`` 之间的内容；
    3. 修复字符串字面量内未转义的换行/制表符后再解析。
    """

    cleaned = _FENCE.sub("", str(text)).strip()
    if not cleaned:
        raise ValueError("模型返回为空")

    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        snippet = cleaned[start : end + 1]
        candidates.append(snippet)
        candidates.append(_escape_controls_in_strings(snippet))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("模型返回不是合法 JSON 对象")


_CONTROL_MAP = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_controls_in_strings(text: str) -> str:
    """把 JSON 字符串字面量内未转义的控制字符转义（结构换行不受影响）。"""

    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            out.append(_CONTROL_MAP.get(ch, ch))
        else:
            out.append(ch)
            if ch == '"':
                in_string = True
    return "".join(out)


def _split_clauses(text: str) -> list[str]:
    return [p.strip() for p in _CLAUSE_SPLIT.split(text) if p.strip()]


def _split_to_limit(text: str, limit: int) -> list[str]:
    """把过长句子在逗号/顿号处切分为不超过 limit 的片段。"""

    clauses = _split_clauses(text)
    if len(clauses) <= 1:
        return [text]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if current and len(current) + len(clause) > limit:
            chunks.append(current)
            current = clause
        else:
            current += clause
    if current:
        chunks.append(current)
    return chunks


def normalize_points(
    value: Any,
    *,
    limit: int = 35,
    max_items: int = 6,
    total_limit: int | None = None,
) -> list[str]:
    """把模型输出规整为分点列表：单点不超过 limit，可选限制总字数。"""

    if value is None:
        return []
    raw_items: list[str] = []
    if isinstance(value, str):
        raw_items = [p for p in re.split(r"[\n；;]+", value)]
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                raw_items.append(item)
            elif item is not None:
                raw_items.append(str(item))
    else:
        raw_items = [str(value)]

    result: list[str] = []
    for item in raw_items:
        text = _NUMBER_PREFIX.sub("", item.strip())
        text = text.strip(" 　")
        if not text:
            continue
        if len(text) <= limit:
            result.append(text)
        else:
            result.extend(_split_to_limit(text, limit))
    if len(result) > max_items:
        logger.warning("AI 输出分点过多(%s)，已截断", len(result))
        result = result[:max_items]
    if total_limit is not None and result:
        kept: list[str] = []
        total = 0
        for point in result:
            if total + len(point) > total_limit:
                logger.warning("AI 输出总字数超限(%s)，已截断", total_limit)
                break
            kept.append(point)
            total += len(point)
        result = kept or result[:1]
    return result


def join_points(points: list[str]) -> str:
    """分点列表落表格式：编号 + 换行。"""

    return "\n".join(f"{i}. {p}" for i, p in enumerate(points, start=1))
