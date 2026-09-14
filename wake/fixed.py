"""Wake times from a fixed weekly configuration."""

from datetime import date, datetime, time, tzinfo
from typing import Dict, Optional

from .base import WakeSchedule

SATURDAY = 5
SUNDAY = 6

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def parse_optional_time(value, label: str) -> Optional[time]:
    """Parse "HH:MM" into a time. Returns None if unset, warns if unusable."""
    if value is None or str(value).strip() == "":
        return None
    try:
        hours, minutes = str(value).strip().split(":")
        hours, minutes = int(hours), int(minutes)
    except (ValueError, AttributeError):
        print(f"⚠️  Could not read {label} ('{value}'); falling back.")
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        print(f"⚠️  {label} ('{value}') is not a valid time; falling back.")
        return None
    return time(hours, minutes)


def parse_time(value, label: str, default: str) -> time:
    """Parse "HH:MM" into a time, falling back loudly rather than crashing."""
    parsed = parse_optional_time(value, label)
    if parsed is not None:
        return parsed
    hours, minutes = default.split(":")
    print(f"   Using {default} for {label}.")
    return time(int(hours), int(minutes))


class FixedWakeSchedule(WakeSchedule):
    """Wake times from configuration, resolved per day of the week.

    Three layers, most specific first:

    1. a per-day override (``WAKE_TIME_MONDAY`` … ``WAKE_TIME_FRIDAY``)
    2. the Saturday and Sunday times
    3. ``WAKE_TIME_WEEKDAY`` for any weekday left unset

    Keeping the weekend days separate from the weekday default means a late
    Sunday start never drags the Monday-to-Friday bedtime with it, and the
    per-day layer is there for the day that doesn't fit the pattern — a late
    Wednesday lecture, an early Friday shift.
    """

    def __init__(
        self,
        weekday: str = "06:30",
        saturday: str = "08:00",
        sunday: str = "08:00",
        per_day: Optional[Dict[int, str]] = None,
        tz: Optional[tzinfo] = None,
    ):
        self.weekday = parse_time(weekday, "WAKE_TIME_WEEKDAY", "06:30")
        self.saturday = parse_time(saturday, "WAKE_TIME_SATURDAY", "08:00")
        self.sunday = parse_time(sunday, "WAKE_TIME_SUNDAY", "08:00")

        # A malformed or absent override simply doesn't register, so the day
        # falls through to the layer below instead of failing the run.
        self.per_day: Dict[int, time] = {}
        for index, value in (per_day or {}).items():
            if not (0 <= index <= 6):
                continue
            parsed = parse_optional_time(
                value, f"WAKE_TIME_{DAY_NAMES[index].upper()}"
            )
            if parsed is not None:
                self.per_day[index] = parsed

        self.tz = tz

    def time_for_weekday(self, weekday: int) -> time:
        if weekday in self.per_day:
            return self.per_day[weekday]
        if weekday == SATURDAY:
            return self.saturday
        if weekday == SUNDAY:
            return self.sunday
        return self.weekday

    def wake_time_for(self, day: date) -> Optional[datetime]:
        return self.localize(day, self.time_for_weekday(day.weekday()), self.tz)

    def describe(self, day: date) -> str:
        weekday = day.weekday()
        at = self.time_for_weekday(weekday).strftime("%H:%M")
        name = DAY_NAMES[weekday]

        # Saturday and Sunday are named before the per-day layer: they have
        # dedicated settings, so "Fixed Saturday" describes them better than
        # "per-day" would, even though they arrive through the same dict.
        if weekday == SATURDAY:
            return f"Fixed Saturday wake time ({at})."
        if weekday == SUNDAY:
            return f"Fixed Sunday wake time ({at})."
        if weekday in self.per_day:
            return f"Per-day {name} wake time ({at})."
        return f"Fixed weekday wake time ({at})."
