"""可注入时钟。

查询时刻由调用方注入，生产用系统时钟，测试与重放用固定时钟；
跨时区旅行只改变展示时区，不改变任何绝对期限。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    """真实时钟；可固定展示时区，但期限始终基于绝对时刻。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FixedClock:
    """把时钟钉在某个绝对时刻，用于临界时刻与离线重放测试。"""

    instant: datetime

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock 需要带时区的绝对时刻")
        object.__setattr__(self, "instant", instant.astimezone(timezone.utc))

    def now(self) -> datetime:
        return self.instant


def display_instant(instant: datetime, zone_name: str) -> str:
    """以 IANA 时区渲染绝对时刻；非法时区回退 UTC 并显式标注。"""
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        zone = timezone.utc
        return instant.astimezone(zone).isoformat() + "Z[UTC:未知时区]"
    return instant.astimezone(zone).isoformat()
