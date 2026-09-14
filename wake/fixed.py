"""Wake times from a fixed weekly configuration."""

from datetime import date, datetime, time, tzinfo
from typing import Dict, Optional

from daily_schedule import (
    DAY_NAMES,
    SATURDAY,
    SUNDAY,
    DailyTimeTable,
    parse_optional_time,
    parse_time,
)

from .base import WakeSchedule

__all__ = ["FixedWakeSchedule", "DAY_NAMES", "SATURDAY", "SUNDAY",
           "parse_time", "parse_optional_time"]


class FixedWakeSchedule(WakeSchedule):
    """Wake times from configuration, resolved per day of the week.

    See `daily_schedule.DailyTimeTable` for how the three layers resolve:
    a per-day override, then Saturday/Sunday, then the weekday default.
    """

    def __init__(
        self,
        weekday: str = "06:30",
        saturday: str = "08:00",
        sunday: str = "08:00",
        per_day: Optional[Dict[int, str]] = None,
        tz: Optional[tzinfo] = None,
    ):
        self.table = DailyTimeTable(
            weekday, saturday, sunday, per_day,
            prefix="WAKE_TIME", defaults=("06:30", "08:00", "08:00"),
        )
        self.tz = tz

    # Kept so existing callers and tests read unchanged.
    @property
    def weekday(self) -> time:
        return self.table.weekday

    @property
    def saturday(self) -> time:
        return self.table.saturday

    @property
    def sunday(self) -> time:
        return self.table.sunday

    @property
    def per_day(self) -> Dict[int, time]:
        return self.table.per_day

    def time_for_weekday(self, weekday: int) -> time:
        return self.table.for_weekday(weekday)

    def wake_time_for(self, day: date) -> Optional[datetime]:
        return self.localize(day, self.table.for_weekday(day.weekday()), self.tz)

    def describe(self, day: date) -> str:
        return self.table.describe_weekday(day.weekday(), "wake time")
