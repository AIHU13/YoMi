"""配置加载（.env + 环境变量，单一来源）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """YoMi 运行配置。

    所有配置项可通过项目根目录 .env 或同名环境变量覆盖；
    本模块是全局配置唯一入口。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 基础运行
    app_name: str = "YoMi助手"
    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_timezone: str = "Asia/Shanghai"
    data_dir: str = "data"
    scheduler_enabled: bool = True

    # 飞书自建应用
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_api_base: str = "https://open.feishu.cn/open-apis"
    feishu_user_open_id: str = ""
    feishu_dry_run: bool = False
    # 官方 SDK 长连接（WebSocket）订阅事件与卡片回调，无需公网域名
    feishu_long_conn_enabled: bool = True

    # 多维表格（5 张表固定在同一 base）
    bitable_app_token: str = ""
    bitable_goals_table_id: str = ""
    bitable_standards_table_id: str = ""
    bitable_daily_table_id: str = ""
    bitable_weekly_table_id: str = ""
    bitable_portrait_table_id: str = ""

    # LLM（OpenAI 兼容接口）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = 60
    llm_max_tokens: int = 2000
    llm_temperature: float = 0.2

    # 定时触发
    daily_push_time: str = "22:00"
    weekly_push_weekday: int = 5
    weekly_push_time: str = "22:00"
    monthly_push_day: int = 1
    monthly_push_time: str = "22:00"
    daily_confirm_deadline: str = "23:59"
    # 周报反馈窗口（天）：超时未反馈记为「无反馈(超时)」
    weekly_feedback_window_days: int = 3

    # HTTP 通用
    request_retry_times: int = 3
    request_timeout_seconds: int = 20

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def state_db_path(self) -> Path:
        return self.data_path / "state.db"

    @property
    def backup_path(self) -> Path:
        return self.data_path / "backups"

    @property
    def feishu_ready(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)

    @property
    def feishu_sdk_domain(self) -> str:
        """官方 SDK 域名：由 FEISHU_API_BASE 去掉 /open-apis 推导。"""

        base = (self.feishu_api_base or "").rstrip("/")
        suffix = "/open-apis"
        if base.endswith(suffix):
            base = base[: -len(suffix)]
        return base or "https://open.feishu.cn"

    @property
    def bitable_ready(self) -> bool:
        return bool(
            self.bitable_app_token
            and self.bitable_goals_table_id
            and self.bitable_standards_table_id
            and self.bitable_daily_table_id
            and self.bitable_weekly_table_id
            and self.bitable_portrait_table_id
        )

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
