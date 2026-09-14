"""Wake times from a fixed weekday / Saturday / Sunday configuration."""

from datetime import date, datetime, time, tzinfo
from typing import Optional

from .base import WakeSchedule

SATURDAY = 5
SUNDAY = 6

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def parse_time(value: str, label: str, default: str) -> time:
    """Parse "HH:MM" into a time, falling back loudly rather than crashing."""
    try:
        hours, minutes = str(value).strip().split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        print(f"⚠️  Could not read {label} ('{value}'); using {default}.")
        hours, minutes = default.split(":")
        return time(int(hours), int(minutes))


class FixedWakeSchedule(WakeSchedule):
    """Three configured wake times: Monday-Friday, Saturday, Sunday.

    Keeping the weekend days separate means a late Sunday start never drags the
    Monday-to-Friday bedtime with it.
    """

    def __init__(
        self,
        weekday: str = "06:30",
        saturday: str = "08:00",
        sunday: str = "08:00",
        tz: Optional[tzinfo] = None,
    ):
        self.weekday = parse_time(weekday, "WAKE_TIME_WEEKDAY", "06:30")
        self.saturday = parse_time(saturday, "WAKE_TIME_SATURDAY", "08:00")
        self.sunday = parse_time(sunday, "WAKE_TIME_SUNDAY", "08:00")
        self.tz = tz

    def time_for_weekday(self, weekday: int) -> time:
        if weekday == SATURDAY:
            return self.saturday
        if weekday == SUNDAY:
            return self.sunday
        return self.weekday

    def wake_time_for(self, day: date) -> Optional[datetime]:
        return self.localize(day, self.time_for_weekday(day.weekday()), self.tz)

    def describe(self, day: date) -> str:
        weekday = day.weekday()
        if weekday == SATURDAY:
            bucket = "Saturday"
        elif weekday == SUNDAY:
            bucket = "Sunday"
        else:
            bucket = "weekday"
        at = self.time_for_weekday(weekday)
        return f"Fixed {bucket} wake time ({at.strftime('%H:%M')})."
