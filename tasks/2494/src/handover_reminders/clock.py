from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An explicit UTC offset is required")
    return value.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return aware_utc(self.value)


def next_scan_after(now: datetime) -> datetime:
    """Next :00/:30 Shanghai boundary, strictly after now, returned in UTC."""
    local = aware_utc(now).astimezone(SHANGHAI)
    floor = local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)
    return (floor + timedelta(minutes=30)).astimezone(timezone.utc)

