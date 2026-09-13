"""全量备份：把 5 张业务表导出到 data/backups/snapshots/<时间戳>/。

用法（项目根目录）：
    conda run -n yomi python -m scripts.backup
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.data.bitable import BitableClient
from app.data.local_store import LocalStore
from app.data.tables import TABLES
from app.feishu.api import FeishuClient


async def run() -> None:
    settings = get_settings()
    setup_logging(log_dir=settings.data_path / "logs")
    feishu = FeishuClient(settings)
    bitable = BitableClient(feishu, settings)
    try:
        tables: dict[str, list[dict]] = {}
        for spec in TABLES.values():
            tables[spec.title] = await bitable.list_records(spec, page_size=500)
        target = LocalStore(settings.data_path).snapshot(tables)
        total = sum(len(rows) for rows in tables.values())
        print(f"备份完成：{target}（{len(tables)} 张表 / {total} 条记录）")
    finally:
        await feishu.close()


if __name__ == "__main__":
    asyncio.run(run())
