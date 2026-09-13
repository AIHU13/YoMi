"""卡片 JSON 模板（schema 2.0）与公共构造工具。

模板结构为固定代码，不允许 LLM 动态修改；字段名引用 data.tables 定义。
"""

from __future__ import annotations

from typing import Any, Iterable


def base_card(header_title: str, *, color: str = "blue") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": color,
        },
        "body": {"elements": []},
    }


def md(
    content: str,
    *,
    text_size: str | None = None,
    text_align: str | None = None,
) -> dict[str, Any]:
    element: dict[str, Any] = {"tag": "markdown", "content": content}
    if text_size:
        element["text_size"] = text_size
    if text_align:
        element["text_align"] = text_align
    return element


def divider() -> dict[str, Any]:
    return {"tag": "hr"}


def note(content: str) -> dict[str, Any]:
    """辅助说明文字（schema 2.0 已移除 note，用 markdown 小字号替代）。"""

    return {"tag": "markdown", "content": content, "text_size": "small"}


def input_element(
    name: str,
    label: str,
    placeholder: str = "",
    required: bool = False,
    *,
    multiline: bool = False,
    rows: int | None = None,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "tag": "input",
        "name": name,
        "label": {"tag": "plain_text", "content": label},
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "required": required,
        "width": "fill",
    }
    if multiline:
        element["input_type"] = "multiline_text"
        element["rows"] = rows or 3
        element["auto_resize"] = True
    return element


def select_element(
    name: str,
    options: Iterable[str],
    *,
    required: bool = False,
    placeholder: str = "请选择",
) -> dict[str, Any]:
    return {
        "tag": "select_static",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "required": required,
        "width": "fill",
        "options": [
            {"text": {"tag": "plain_text", "content": str(option)}, "value": str(option)}
            for option in options
        ],
    }


def form_container(
    name: str,
    elements: list[dict[str, Any]],
    *,
    direction: str = "vertical",
) -> dict[str, Any]:
    return {
        "tag": "form",
        "name": name,
        "direction": direction,
        "elements": elements,
        "fallback": {
            "tag": "fallback_text",
            "text": {"tag": "plain_text", "content": "请升级飞书客户端后填写"},
        },
    }


def column(weight: int, elements: list[dict[str, Any]]) -> dict[str, Any]:
    """分栏中的一列（weight 决定宽度占比）。"""

    return {
        "tag": "column",
        "width": "weighted",
        "weight": weight,
        "elements": elements,
    }


def column_set(
    columns: list[dict[str, Any]],
    *,
    flex_mode: str = "none",
) -> dict[str, Any]:
    """横向分栏容器；用于压缩表单纵向高度。"""

    return {"tag": "column_set", "flex_mode": flex_mode, "columns": columns}


def collapsible_panel(
    title: str,
    elements: list[dict[str, Any]],
    *,
    expanded: bool = False,
    icon_token: str = "down-small-ccm_outlined",
) -> dict[str, Any]:
    """折叠面板：非必要内容默认收起，降低卡片占比。

    header 右侧固定展示展开箭头（展开后旋转 180°），避免用户误以为是普通文字。
    """

    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": title},
            "icon": {"tag": "standard_icon", "token": icon_token, "color": "grey"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": elements,
    }


def submit_button(
    name: str,
    text: str = "提交",
    value: dict[str, Any] | None = None,
    *,
    primary: bool = True,
) -> dict[str, Any]:
    """表单内提交按钮：点击后连同表单值一起回调。"""

    element: dict[str, Any] = {
        "tag": "button",
        "name": name,
        "text": {"tag": "plain_text", "content": text},
        "type": "primary_filled" if primary else "default",
        "form_action_type": "submit",
    }
    if value:
        element["value"] = value
    return element


def field_label(content: str) -> dict[str, Any]:
    """表单内的字段说明（select 等组件自身不支持 label 时使用）。"""

    return md(f"**{content}**")


def text_button(name: str, value: dict[str, Any], text: str, *, primary: bool = False) -> dict[str, Any]:
    return {
        "tag": "button",
        "name": name,
        "text": {"tag": "plain_text", "content": text},
        "type": "primary_filled" if primary else "default",
        "value": value,
    }


def open_url_button(text: str, url: str, *, primary: bool = False) -> dict[str, Any]:
    """跳转按钮：点击后用浏览器打开指定链接。"""

    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "primary_filled" if primary else "default",
        "behaviors": [{"type": "open_url", "default_url": url}],
    }


def elements_group(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """普通（非表单）元素组；用于确认 / 反馈等按钮卡片。"""

    return {"elements": elements}
