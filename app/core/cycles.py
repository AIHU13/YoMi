"""周期计算纯函数：日 / 周 / 月边界与日期键。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def local_zone(tz_name: str) -> ZoneInfo:
    return ZoneInfo(tz_name)


def now_local(tz_name: str) -> datetime:
    return datetime.now(local_zone(tz_name))


def today_local(tz_name: str) -> date:
    return now_local(tz_name).date()


def fmt_date(d: date) -> str:
    return d.isoformat()


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_bounds(day: date) -> tuple[date, date]:
    """返回 [周一, 周日] 边界（含）。"""

    monday = monday_of(day)
    return monday, monday + timedelta(days=6)


def is_saturday(day: date) -> bool:
    return day.weekday() == 5


def month_bounds(year: int, month: int) -> tuple[date, date]:
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return date(year, month, 1), end


def previous_month_bounds(day: date) -> tuple[date, date]:
    if day.month == 1:
        return month_bounds(day.year - 1, 12)
    return month_bounds(day.year, day.month - 1)


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def parse_clock(value: str) -> time:
    """解析 HH:MM；非法时抛 ValueError。"""

    try:
        hour, minute = (int(p) for p in value.split(":", 1))
        result = time(hour=hour, minute=minute)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"非法时间配置: {value!r}，应为 HH:MM") from exc
    return result


def deadline_passed(tz_name: str, deadline: str, now: datetime | None = None) -> bool:
    """判断本地时间是否已过当日截止时刻（如 23:59 自动保存）。"""

    target = now or now_local(tz_name)
    return target.time() >= parse_clock(deadline)

