"""第 3/4 轮冒烟自检：校验、加签、URL 验证、日报卡片与 dry-run。"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.validate import merge_point_values, validate_daily_values
from app.data.tables import DAILY_TABLE
from app.feishu.api import FeishuClient, decrypt_encrypt_payload, verify_event_signature
from app.feishu.websocket import FeishuLongConnection, card_action_to_event
from app.main import create_app
from app.ai.parse import normalize_points
from app.ai.parse import parse_json_object
from app.services.daily import DailyContext
from app.services.daily import DailyReportService
from app.services.records import RecordQueryService
from app.services.router import (
    DAILY_QUERY,
    DAILY_START,
    GREETING,
    OUT_OF_SCOPE,
    PORTRAIT_QUERY,
    WEEKLY_GENERATE,
    WEEKLY_QUERY,
    IntentRouter,
    classify_rules,
    extract_text,
    parse_day,
)
from app.services.weekly import WeeklyReportService
from app.services.goals import save_goal
from app.services.week_goal import GOAL_PICK, WeekGoalService, target_week
from app.services.portrait import PortraitService


def make_settings(**overrides) -> Settings:
    # 显式清空外部凭证，避免测试受本地 .env 影响（保持可重复）。
    base = dict(
        app_env="test",
        scheduler_enabled=False,
        feishu_dry_run=True,
        feishu_app_id="",
        feishu_app_secret="",
        feishu_user_open_id="",
        bitable_app_token="",
        llm_api_key="",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def app():
    return create_app(make_settings())


def test_health(app) -> None:
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_url_verification(app) -> None:
    resp = TestClient(app).post(
        "/feishu/webhook", json={"type": "url_verification", "challenge": "abc123"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}


def test_validate_daily_values() -> None:
    ok = validate_daily_values(
        {
            "项目": "YoMi",
            "状态": "进行中",
            "任务": "完成日报卡片",
            "实际产出": "卡片已发送",
            "下一步": "联调飞书",
            "工作负担": "正常",
        }
    )
    assert ok["ok"] is True
    assert ok["errors"] == []

    bad = validate_daily_values({"状态": "已完成", "工作负担": "不存在"})
    assert bad["ok"] is False
    assert any("任务" in e for e in bad["errors"])
    assert any("工作负担" in e for e in bad["errors"])


def test_build_daily_card(app) -> None:
    service = app.state.daily
    card = service.build_daily_card(
        DailyContext(
            target_date=date(2026, 9, 6),
            week_goal="本周目标",
            previous_example="任务：A\n实际产出：B",
            project_options=("YoMi",),
        )
    )
    assert card["schema"] == "2.0"
    form = card["body"]["elements"][-1]
    if form["tag"] == "collapsible_panel":
        form = card["body"]["elements"][-2]
    assert form["tag"] == "form"

    def collect_names(elements) -> set:
        names = set()
        for el in elements:
            if el.get("name"):
                names.add(el["name"])
            for key in ("elements", "columns"):
                children = el.get(key) or []
                if children and isinstance(children[0], dict) and "tag" in children[0]:
                    names |= collect_names(children)
        return names

    names = collect_names(form["elements"])
    assert {
        "项目",
        "状态",
        "工作负担",
        "任务_1",
        "产出_1",
        "任务_2",
        "产出_2",
        "下一步",
        "遇到的问题",
        "计划外工作",
        "相关资料",
    } <= names


def test_merge_point_values() -> None:
    """分点输入合并回日报表字段，空项跳过；整段提交保持兼容。"""

    merged = merge_point_values(
        {"任务_1": "完成卡片改造", "任务_3": "整理字段映射", "产出_1": "通过 3 个场景验证"}
    )
    assert merged["任务"] == "1. 完成卡片改造\n2. 整理字段映射"
    assert merged["实际产出"] == "1. 通过 3 个场景验证"
    assert merge_point_values({"任务": "整段文本"})["任务"] == "整段文本"


def test_normalize_points_enforces_sentence_limit() -> None:
    """AI 输出分点应规整并保证单句不超过 35 字。"""

    long_text = "完成日报卡片表单改造工作，联调飞书长连接回调并修复超时问题，整理多维表格字段映射关系"
    points = normalize_points([f"1. {long_text}"])
    assert points
    assert all(len(p) <= 35 for p in points)
    assert normalize_points("完成接口联调；修复线上问题") == ["完成接口联调", "修复线上问题"]


def test_normalize_points_total_limit() -> None:
    """总字数受限时保留前面的分点，避免超出日报篇幅。"""

    points = normalize_points(
        ["一二三四五六七八九十", "甲乙丙丁戊己庚辛壬癸", "子丑寅卯辰巳午未申酉"],
        total_limit=25,
    )
    assert sum(len(p) for p in points) <= 25
    assert len(points) == 2


def test_parse_json_object_tolerance() -> None:
    """模型输出带说明文字或未转义换行时仍应解析成功。"""

    with_prose = '整理结果如下：\n{"任务": ["完成卡片改版"], "实际产出": []}\n以上。'
    assert parse_json_object(with_prose)["任务"] == ["完成卡片改版"]

    with_newline = '{"任务": ["第一行\n第二行"], "实际产出": []}'
    assert parse_json_object(with_newline)["任务"] == ["第一行\n第二行"]


@pytest.mark.asyncio
async def test_dry_run_confirm_actions(app) -> None:
    """确认 / 重新生成 回调在 dry-run 下应正常返回，不触网。"""

    service = app.state.daily
    for action in ("daily_confirm", "daily_regenerate"):
        event = {
            "operator": {"open_id": "ou_test"},
            "action": {
                "tag": "button",
                "name": action,
                "value": {"action": action, "record_id": "rec_test"},
                "form_value": {"意见": "更突出与本周目标的关联"},
            },
        }
        assert await service.handle_card_action(event) is None


@pytest.mark.asyncio
async def test_dry_run_confirm_without_record_id(app) -> None:
    """飞书表单按钮不回传 value：不带 record_id 也不应抛异常。"""

    event = {
        "operator": {"open_id": "ou_test"},
        "action": {"tag": "button", "name": "daily_confirm", "form_value": {"意见": ""}},
    }
    assert await app.state.daily.handle_card_action(event) is None


@pytest.mark.asyncio
async def test_dry_run_push_and_submit(app) -> None:
    service = app.state.daily
    message_id = await service.push_daily(date(2026, 9, 6))
    assert message_id.startswith("om_dry_")

    event = {
        "operator": {"open_id": "ou_test"},
        "action": {
            "tag": "button",
            "value": {"action": "daily_submit"},
            "form_value": {
                "项目": "YoMi",
                "状态": "进行中",
                "任务_1": "联调日报卡片",
                "产出_1": "提交链路通过",
                "下一步": "继续",
                "工作负担": "正常",
            },
        },
    }
    # dry-run 下只记录日志，不真实发送，应正常返回。
    assert await service.handle_card_action(event) is None


def test_signature_and_encrypt_decrypt() -> None:
    encrypt_key = "test-encrypt-key"
    settings = make_settings(feishu_encrypt_key=encrypt_key)
    body = {"type": "url_verification", "challenge": "xyz"}
    raw = json.dumps(body).encode()

    digest = hashlib.sha256()
    digest.update(f"1000nonce{encrypt_key}".encode())
    digest.update(raw)
    headers = {
        "x-lark-request-timestamp": "1000",
        "x-lark-request-nonce": "nonce",
        "x-lark-signature": digest.hexdigest(),
    }
    assert verify_event_signature(headers, raw, settings.feishu_encrypt_key)
    assert not verify_event_signature(headers, b"tampered", settings.feishu_encrypt_key)

    # 加密回调：base64(iv + AES-CBC(pkcs7(body)))
    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = b"0123456789abcdef"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    outer = {"encrypt": base64.b64encode(iv + ciphertext).decode()}
    assert decrypt_encrypt_payload(outer, settings.feishu_encrypt_key) == body


@pytest.mark.asyncio
async def test_dry_run_feishu_client() -> None:
    settings = make_settings(feishu_user_open_id="ou_test")
    client = FeishuClient(settings)
    assert (await client.send_text("ou_test", "hello")).startswith("om_dry_")


@pytest.mark.asyncio
async def test_bitable_dry_run(app) -> None:
    """dry-run 下多维表格读写不触网，且返回占位数据。"""

    bitable = app.state.bitable
    record_id = await bitable.add_record(DAILY_TABLE, {"日期": "2026-09-06"})
    assert record_id.startswith("rec_dry_")
    assert await bitable.list_records(DAILY_TABLE) == []
    assert await bitable.list_tables() == []


def test_long_connection_disabled_without_credentials() -> None:
    """未配置凭证时不启动长连接，避免无意义连接。"""

    conn = FeishuLongConnection(make_settings(), None)
    assert conn.enabled is False
    assert conn.start() is False


def test_card_action_to_event_mapping() -> None:
    """官方回调对象应正确映射为业务事件字典。"""

    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )

    data = P2CardActionTrigger(
        {
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_x"},
                "action": {
                    "tag": "button",
                    "value": {"action": "daily_submit"},
                    "form_value": {"任务": "联调"},
                },
                "context": {"open_message_id": "om_1"},
            },
        }
    )
    event = card_action_to_event(data)
    assert event["operator"]["open_id"] == "ou_x"
    assert event["action"]["form_value"] == {"任务": "联调"}
    assert event["context"]["open_message_id"] == "om_1"


def test_card_action_without_button_value() -> None:
    """真实点击回归：表单提交按钮只回传 name/form_value，不回传 value。"""

    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )

    data = P2CardActionTrigger(
        {
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_x"},
                "action": {
                    "tag": "button",
                    "name": "daily_confirm",
                    "form_value": {"意见": ""},
                },
                "context": {"open_message_id": "om_1"},
            },
        }
    )
    event = card_action_to_event(data)
    assert event["action"]["name"] == "daily_confirm"
    assert event["action"]["value"] == {}
    assert event["action"]["form_value"] == {"意见": ""}


def test_long_connection_dispatcher_registers_handlers() -> None:
    """长连接分发器需注册卡片回调与消息事件。"""

    async def handler(event):  # pragma: no cover - 仅验证注册
        return None

    conn = FeishuLongConnection(make_settings(), handler)
    dispatcher = conn.build_dispatcher()
    assert "p2.card.action.trigger" in dispatcher._callback_processor_map
    assert "p2.im.message.receive_v1" in dispatcher._processorMap


class _StubFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[dict] = []

    async def send_text(self, open_id: str, text: str) -> str:  # noqa: ARG002
        self.texts.append(text)
        return "om_stub"

    async def send_card(self, open_id: str, card: dict) -> str:  # noqa: ARG002
        self.cards.append(card)
        return "om_stub_card"

    async def close(self) -> None:
        return None


class _StubBitable:
    def __init__(self, record: dict | None) -> None:
        self.record = record
        self.updates: list[dict] = []
        self.adds: list[dict] = []

    async def get_record(self, spec, record_id):  # noqa: ARG002
        return self.record

    async def update_record(self, spec, record_id, fields):  # noqa: ARG002
        self.updates.append(fields)

    async def list_records(self, spec, page_size=100):  # noqa: ARG002
        return [self.record] if self.record else []

    async def find_by_unique(self, spec, value):  # noqa: ARG002
        return self.record

    async def add_record(self, spec, fields):  # noqa: ARG002
        self.adds.append(fields)
        return "rec_new"


def _bitable_settings() -> Settings:
    return make_settings(
        feishu_dry_run=False,
        feishu_user_open_id="ou_test",
        bitable_app_token="app",
        bitable_goals_table_id="t1",
        bitable_standards_table_id="t2",
        bitable_daily_table_id="t3",
        bitable_weekly_table_id="t4",
        bitable_portrait_table_id="t5",
    )


def _confirm_event(action: str, event_id: str = "ev-1") -> dict:
    return {
        "event_id": event_id,
        "operator": {"open_id": "ou_test"},
        "action": {"tag": "button", "name": action, "form_value": {"意见": "更突出目标关联"}},
    }


def _today_key() -> str:
    from app.core.cycles import fmt_date, today_local

    return fmt_date(today_local("Asia/Shanghai"))


@pytest.mark.asyncio
async def test_confirm_is_noop_when_already_confirmed() -> None:
    """历史卡片重复点击：已确认的日报不再响应确认/重新生成。"""

    feishu = _StubFeishu()
    bitable = _StubBitable(
        {
            "record_id": "rec1",
            "fields": {"日期": _today_key(), "确认状态": "用户确认"},
        }
    )
    service = DailyReportService(_bitable_settings(), feishu, bitable)

    await service.handle_card_action(_confirm_event("daily_confirm"))
    await service.handle_card_action(_confirm_event("daily_regenerate", event_id="ev-2"))

    assert bitable.updates == []
    assert len(feishu.cards) == 0
    assert all("已失效" in t for t in feishu.texts)


@pytest.mark.asyncio
async def test_regenerate_works_when_pending() -> None:
    """待确认状态下重新生成会更新 AI 结果并推新确认卡片。"""

    feishu = _StubFeishu()
    record = {
        "record_id": "rec1",
        "fields": {
            "日期": _today_key(),
            "确认状态": "待确认",
            "原始记录": json.dumps({"项目": "YoMi", "任务": "1. 卡片改版"}, ensure_ascii=False),
        },
    }
    bitable = _StubBitable(record)
    service = DailyReportService(_bitable_settings(), feishu, bitable)

    await service.handle_card_action(_confirm_event("daily_regenerate"))

    assert bitable.updates and bitable.updates[0]["确认状态"] == "待确认"
    assert len(feishu.cards) == 1


def test_event_dedup(app) -> None:
    """同一 event_id 只处理一次，避免飞书重试造成重复落库。"""

    service = app.state.daily
    assert service._mark_event("event-a") is True
    assert service._mark_event("event-a") is False
    assert service._mark_event("") is True


@pytest.mark.asyncio
async def test_confirmed_notice_contains_record_link() -> None:
    """确认回执需带飞书表格记录链接，方便用户后续自行查看。"""

    feishu = _StubFeishu()
    service = DailyReportService(_bitable_settings(), feishu, _StubBitable(None))
    record = {
        "record_id": "rec1",
        "record_url": "https://x.feishu.cn/record/abc123",
        "fields": {"日期": "2026-09-12", "项目": "YoMi", "状态": "进行中"},
    }
    await service._send_confirmed_notice("ou_test", record)

    card = feishu.cards[0]
    payload = json.dumps(card, ensure_ascii=False)
    assert "https://x.feishu.cn/record/abc123" in payload
    assert any(el.get("behaviors") for el in card["body"]["elements"])


# --------------------------------------------------------------------- 意图路由


class _StubDaily:
    def __init__(self) -> None:
        self.pushed: list = []

    async def push_daily(self, day):  # noqa: ARG002
        self.pushed.append(day)
        return "om_x"


class _StubWeekly:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"ok": False, "message": "本周还没有日报记录"}
        self.generated: list = []

    async def generate(self, day=None):
        self.generated.append(day)
        return self.result

    def build_weekly_card(self, result):
        return {"schema": "2.0", "body": {"elements": []}}


class _StubRecords:
    def __init__(self, daily: dict | None = None) -> None:
        self._daily = daily or {}

    async def daily_on(self, day):  # noqa: ARG002
        return self._daily

    async def latest_daily(self, today=None):  # noqa: ARG002
        return self._daily

    async def weekly_on(self, day):  # noqa: ARG002
        return {}

    async def latest_weekly(self, today=None):  # noqa: ARG002
        return {}

    async def latest_portrait(self):
        return {}


def _text_message(text: str, event_id: str = "msg-1") -> dict:
    return {
        "event_id": event_id,
        "open_id": "ou_test",
        "chat_type": "p2p",
        "message_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }


def _router(records: _StubRecords | None = None) -> tuple[IntentRouter, _StubFeishu, _StubDaily, _StubWeekly]:
    settings = make_settings(feishu_user_open_id="ou_test")
    feishu = _StubFeishu()
    daily = _StubDaily()
    weekly = _StubWeekly()
    router = IntentRouter(settings, feishu, daily, weekly, records or _StubRecords())
    return router, feishu, daily, weekly


def test_intent_rules_and_text_parsing() -> None:
    today = date(2026, 9, 13)
    assert classify_rules("你好", today)[0] == GREETING
    assert classify_rules("你是谁", today)[0] not in (OUT_OF_SCOPE, None)
    assert classify_rules("日报记录", today)[0] == DAILY_START
    assert classify_rules("写日报", today)[0] == DAILY_START
    assert classify_rules("查昨天的日报", today)[0] == DAILY_QUERY
    assert classify_rules("整理周报", today)[0] == WEEKLY_GENERATE
    assert classify_rules("看看上周的周报", today)[0] == WEEKLY_QUERY
    assert classify_rules("个人画像查询", today)[0] == PORTRAIT_QUERY
    assert classify_rules("帮我订机票", today) is None

    assert parse_day("查昨天的日报", today) == date(2026, 9, 12)
    assert parse_day("看看本周的周报", today) == today
    assert parse_day("2026-09-01 的日报", today) == date(2026, 9, 1)
    assert parse_day("9月3日日报", today) == date(2026, 9, 3)
    assert extract_text(json.dumps({"text": " 你好 "})) == "你好"
    assert extract_text("纯文本") == "纯文本"


@pytest.mark.asyncio
async def test_router_greeting_and_out_of_scope() -> None:
    router, feishu, _, _ = _router()
    await router.handle_message(_text_message("你好", "m1"))
    assert "YoMi" in feishu.texts[-1]

    await router.handle_message(_text_message("帮我写一首诗", "m2"))
    assert "不在我的能力范围内" in feishu.texts[-1]


@pytest.mark.asyncio
async def test_router_daily_start_pushes_card() -> None:
    router, _, daily, _ = _router()
    await router.handle_message(_text_message("日报记录", "m3"))
    assert len(daily.pushed) == 1


@pytest.mark.asyncio
async def test_router_daily_query_returns_link() -> None:
    records = _StubRecords(
        daily={
            "record_id": "rec1",
            "record_url": "https://x.feishu.cn/record/abc",
            "fields": {"日期": "2026-09-12", "项目": "YoMi", "任务": "A"},
        }
    )
    router, feishu, _, _ = _router(records)
    await router.handle_message(_text_message("查昨天的日报", "m4"))
    assert "https://x.feishu.cn/record/abc" in json.dumps(feishu.cards[-1], ensure_ascii=False)


@pytest.mark.asyncio
async def test_router_weekly_generate_without_data() -> None:
    router, feishu, _, weekly = _router()
    await router.handle_message(_text_message("整理周报", "m5"))
    assert weekly.generated
    assert "还没有日报记录" in feishu.texts[-1]


@pytest.mark.asyncio
async def test_scheduled_push_skips_when_already_submitted() -> None:
    """用户已主动完成当日日报时，22:00 定时提醒应跳过。"""

    settings = _bitable_settings()
    feishu = _StubFeishu()
    bitable = _StubBitable(
        {"record_id": "rec1", "fields": {"日期": _today_key(), "确认状态": "待确认"}}
    )
    service = DailyReportService(settings, feishu, bitable)
    assert await service.scheduled_push() == ""
    assert feishu.cards == []


def test_weekly_scores_normalized_and_clamped() -> None:
    scores = WeeklyReportService._normalize_scores(
        {"Agent开发": "88", "技术能力": 120, "产品能力": -5}
    )
    assert scores["Agent开发"] == 88
    assert scores["技术能力"] == 100
    assert scores["产品能力"] == 0
    assert scores["项目交付"] == 0


# --------------------------------------------------------- 周报反馈闭环（#2）


def test_weekly_card_includes_feedback_form() -> None:
    service = WeeklyReportService(make_settings(), _StubFeishu())
    card = service.build_weekly_card(
        {"period": "2026年第37周", "sections": {}, "scores": {}, "record_url": ""},
        record_id="rec1",
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "weekly_feedback_form" in payload
    assert "提交反馈" in payload
    assert "rec1" in payload


@pytest.mark.asyncio
async def test_weekly_feedback_submit_writes_only_feedback_fields() -> None:
    feishu = _StubFeishu()
    bitable = _StubBitable(
        {"record_id": "rec1", "fields": {"周期": "2026年第37周", "反馈状态": "待反馈"}}
    )
    service = WeeklyReportService(_bitable_settings(), feishu, bitable)
    await service.handle_card_action(
        {
            "operator": {"open_id": "ou_test"},
            "action": {
                "tag": "button",
                "name": "weekly_feedback_submit",
                "form_value": {
                    "内容准确性": "有遗漏",
                    "建议是否有帮助": "有帮助",
                    "补充意见": "漏了周报反馈这一项",
                },
                "value": {"action": "weekly_feedback_submit", "record_id": "rec1"},
            },
        }
    )
    fields = bitable.updates[0]
    assert fields["反馈状态"] == "已反馈"
    assert "有遗漏" in fields["用户反馈"]
    # 不得修改历史原始数据
    assert "工作成果" not in fields
    assert "问题处理" not in fields


@pytest.mark.asyncio
async def test_weekly_feedback_skip_does_not_write() -> None:
    feishu = _StubFeishu()
    bitable = _StubBitable(
        {"record_id": "rec1", "fields": {"周期": "2026年第37周", "反馈状态": "待反馈"}}
    )
    service = WeeklyReportService(_bitable_settings(), feishu, bitable)
    await service.handle_card_action(
        {
            "operator": {"open_id": "ou_test"},
            "action": {
                "tag": "button",
                "name": "weekly_feedback_skip",
                "value": {"action": "weekly_feedback_skip", "record_id": "rec1"},
            },
        }
    )
    assert bitable.updates == []


@pytest.mark.asyncio
async def test_weekly_feedback_timeout_marks_stale() -> None:
    """超过反馈窗口仍未反馈 → 无反馈(超时)。"""

    feishu = _StubFeishu()
    bitable = _StubBitable(
        {
            "record_id": "rec1",
            "fields": {"周期": "2026年第36周", "反馈状态": "待反馈", "更新时间": "2026-09-01 10:00"},
        }
    )
    service = WeeklyReportService(_bitable_settings(), feishu, bitable)
    assert await service.finalize_pending_feedback() == 1
    assert bitable.updates[0]["反馈状态"] == "无反馈(超时)"


# ----------------------------------------------------------- 目标与规范（#3）


def test_target_week_weekday_vs_weekend() -> None:
    """工作日规划本周，周末规划下周。"""

    assert target_week(date(2026, 9, 16)) == (date(2026, 9, 14), date(2026, 9, 20))
    assert target_week(date(2026, 9, 13)) == (date(2026, 9, 14), date(2026, 9, 20))


def test_intent_rules_for_goal_and_standards() -> None:
    today = date(2026, 9, 13)
    from app.services.router import STANDARDS_VIEW, WEEK_GOAL_SUGGEST, WEEK_GOAL_VIEW

    assert classify_rules("建议下周目标", today)[0] == WEEK_GOAL_SUGGEST
    assert classify_rules("帮我拆解一下周目标", today)[0] == WEEK_GOAL_SUGGEST
    assert classify_rules("查看当前目标", today)[0] == WEEK_GOAL_VIEW
    assert classify_rules("工作规范是什么", today)[0] == STANDARDS_VIEW


def test_week_goal_card_has_candidate_buttons() -> None:
    service = WeekGoalService(make_settings(), _StubFeishu())
    card = service.build_card(
        "智能研发部",
        date(2026, 9, 14),
        date(2026, 9, 20),
        ["候选A", "候选B", "候选C"],
        current="当前目标",
    )
    values = [
        el.get("value")
        for el in card["body"]["elements"]
        if el.get("tag") == "button"
    ]
    picks = [v for v in values if v and v.get("action") == GOAL_PICK]
    assert len(picks) == 3
    assert picks[0]["week_start"] == "2026-09-14"


@pytest.mark.asyncio
async def test_save_goal_upsert_insert() -> None:
    bitable = _StubBitable(None)
    record_id = await save_goal(
        bitable,
        _bitable_settings(),
        unit="智能研发部",
        week_goal="完成卡片改版",
        week_start=date(2026, 9, 14),
        week_end=date(2026, 9, 20),
        long_term="长期目标",
        monthly="月度目标",
        updated_at="2026-09-13 20:00",
    )
    assert record_id == "rec_new"
    assert bitable.adds[0]["本周目标"] == "完成卡片改版"
    assert bitable.adds[0]["周起始"] == "2026-09-14"


@pytest.mark.asyncio
async def test_save_goal_upsert_update_keeps_existing_long_term() -> None:
    """更新目标时未传长期/月度目标应保留原值，避免误清空。"""

    bitable = _StubBitable(
        {
            "record_id": "rec1",
            "fields": {"目标单位": "智能研发部", "长期目标": "原长期", "月度目标": "原月度"},
        }
    )
    await save_goal(
        bitable,
        _bitable_settings(),
        unit="智能研发部",
        week_goal="新周目标",
        week_start=date(2026, 9, 14),
        week_end=date(2026, 9, 20),
        updated_at="2026-09-13 20:00",
    )
    assert bitable.updates[0]["长期目标"] == "原长期"
    assert bitable.updates[0]["月度目标"] == "原月度"


# ------------------------------------------------------- 月度总结与画像（#1）


def test_portrait_scores_normalized_to_ten_scale() -> None:
    """画像维度得分为 0~10；模型误用 0~100 时自动折算并裁剪。"""

    scores = PortraitService._normalize_scores(
        {"Agent开发": 8.5, "技术能力": 85, "产品能力": 12, "项目交付": -3}
    )
    assert scores["Agent开发"] == 8.5
    assert scores["技术能力"] == 8.5
    # 12 视为 0~100 口径 → 折算 1.2；超范围（>100）裁剪到 10.0
    assert scores["产品能力"] == 1.2
    assert scores["项目交付"] == 0.0
    assert scores["表达沟通"] == 0.0
    assert PortraitService._normalize_scores({"技术能力": 950})["技术能力"] == 10.0


@pytest.mark.asyncio
async def test_portrait_generate_without_weeklies() -> None:
    """当月无周报时应给出明确提示，而不是生成空画像。"""

    feishu = _StubFeishu()
    bitable = _StubBitable(None)
    service = PortraitService(_bitable_settings(), feishu, bitable)
    result = await service.generate("2026-08")
    assert result["ok"] is False
    assert "没有周报记录" in result["message"]


def test_portrait_card_contains_summary_and_link() -> None:
    service = PortraitService(make_settings(), _StubFeishu())
    card = service.build_portrait_card(
        {
            "month": "2026-09",
            "summary": {"工作总结": ["完成卡片改版"], "能力分析": ["技术能力稳步提升"]},
            "narrative": {
                "当前能力状态": "稳步提升",
                "主要优势": "交付稳定",
                "主要问题": "产品视角不足",
                "改进方向": "加强需求拆解",
                "下一阶段建议": "独立负责模块",
                "分析文字": "整体向好。",
            },
            "scores": {"Agent开发": 6.5},
            "source": "2026年第37周",
            "record_url": "https://x.feishu.cn/record/portrait",
        }
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "工作总结" in payload
    assert "能力分析" in payload
    assert "https://x.feishu.cn/record/portrait" in payload


def test_intent_rules_for_portrait_generate() -> None:
    from app.services.router import PORTRAIT_GENERATE, PORTRAIT_QUERY

    today = date(2026, 9, 13)
    assert classify_rules("生成月度画像", today)[0] == PORTRAIT_GENERATE
    assert classify_rules("更新能力画像", today)[0] == PORTRAIT_GENERATE
    assert classify_rules("个人画像查询", today)[0] == PORTRAIT_QUERY


# ------------------------------------------------------- 关键缺失追问（#4）


class _StubLLM:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def complete_json(self, system, user, *, max_tokens=None):  # noqa: ARG002
        self.calls.append(system)
        return self.reply


@pytest.mark.asyncio
async def test_missing_check_asks_when_output_vague() -> None:
    settings = _bitable_settings()
    settings = settings.model_copy(update={"llm_api_key": "k"})
    llm = _StubLLM({"缺失": True, "追问": "这次调研的具体产出是什么？", "字段": "实际产出"})
    service = DailyReportService(settings, _StubFeishu(), _StubBitable(None), llm)
    missing = await service._check_missing(
        {"任务": "完成竞品调研", "实际产出": "输出了 5 家竞品的对比结论"}
    )
    assert missing == {"追问": "这次调研的具体产出是什么？", "字段": "实际产出"}


@pytest.mark.asyncio
async def test_missing_check_skips_when_complete() -> None:
    settings = _bitable_settings().model_copy(update={"llm_api_key": "k"})
    llm = _StubLLM({"缺失": False, "追问": "", "字段": ""})
    service = DailyReportService(settings, _StubFeishu(), _StubBitable(None), llm)
    assert await service._check_missing({"任务": "完成卡片改版", "实际产出": "通过 3 个场景验证"}) is None


@pytest.mark.asyncio
async def test_missing_check_ignores_invalid_field() -> None:
    """字段非法时不追问，避免把补充写到错误字段。"""

    settings = _bitable_settings().model_copy(update={"llm_api_key": "k"})
    llm = _StubLLM({"缺失": True, "追问": "?", "字段": "工作负担"})
    service = DailyReportService(settings, _StubFeishu(), _StubBitable(None), llm)
    assert await service._check_missing(
        {"任务": "完成竞品调研", "实际产出": "输出了 5 家竞品的对比结论"}
    ) is None


@pytest.mark.asyncio
async def test_missing_check_detects_vague_output_without_llm() -> None:
    """产出过短属于确定性缺失，即使未配置 AI 也应追问。"""

    service = DailyReportService(make_settings(), _StubFeishu(), _StubBitable(None))
    missing = await service._check_missing({"任务": "完成竞品调研", "实际产出": "1. 调研完成"})
    assert missing is not None
    assert missing["字段"] == "实际产出"
    assert await service._check_missing(
        {"任务": "完成竞品调研", "实际产出": "输出 5 家竞品对比结论并同步团队"}
    ) is None


# --------------------------------------------------- 本地备份与事件日志（#5）


def test_local_store_backup_and_snapshot() -> None:
    import shutil
    from pathlib import Path

    from app.data.local_store import LocalStore

    base = Path("data") / "_test_store"
    shutil.rmtree(base, ignore_errors=True)
    try:
        store = LocalStore(base)
        store.backup_record("日报总表", {"record_id": "rec1", "fields": {"日期": "2026-09-13"}})
        store.log_event("daily_saved", {"record_id": "rec1"})

        record_file = base / "backups" / "records" / "日报总表.jsonl"
        assert record_file.exists()
        assert "rec1" in record_file.read_text(encoding="utf-8")

        event_file = base / "logs" / "events.jsonl"
        assert event_file.exists()
        assert "daily_saved" in event_file.read_text(encoding="utf-8")

        target = store.snapshot({"日报总表": [{"record_id": "rec1"}]})
        assert (target / "日报总表.json").exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)
