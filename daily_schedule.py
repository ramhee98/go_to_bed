"""Resolving a wall-clock time per day of the week.

Wake times and fixed bedtimes answer the same shape of question — "what time
on this weekday?" — through the same three layers, so the resolution lives
here once and both `wake/` and `bed/` build on it.
"""

from datetime import time
from typing import Dict, Optional

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


class DailyTimeTable:
    """A time for each weekday, resolved through three layers.

    Most specific first:

    1. a per-day override (Monday … Friday)
    2. the Saturday and Sunday times
    3. the weekday time, for any weekday left unset

    Keeping the weekend separate from the weekday default means a later
    weekend never drags the Monday-to-Friday time with it, and the per-day
    layer covers the one day that doesn't fit the pattern.
    """

    def __init__(
        self,
        weekday: str,
        saturday: str,
        sunday: str,
        per_day: Optional[Dict[int, str]] = None,
        prefix: str = "TIME",
        defaults=("06:30", "08:00", "08:00"),
    ):
        self.prefix = prefix
        self.weekday = parse_time(weekday, f"{prefix}_WEEKDAY", defaults[0])
        self.saturday = parse_time(saturday, f"{prefix}_SATURDAY", defaults[1])
        self.sunday = parse_time(sunday, f"{prefix}_SUNDAY", defaults[2])

        # A malformed or absent override simply doesn't register, so the day
        # falls through to the layer below instead of failing the run.
        self.per_day: Dict[int, time] = {}
        for index, value in (per_day or {}).items():
            if not (0 <= index <= 6):
                continue
            parsed = parse_optional_time(
                value, f"{prefix}_{DAY_NAMES[index].upper()}"
            )
            if parsed is not None:
                self.per_day[index] = parsed

    def for_weekday(self, weekday: int) -> time:
        if weekday in self.per_day:
            return self.per_day[weekday]
        if weekday == SATURDAY:
            return self.saturday
        if weekday == SUNDAY:
            return self.sunday
        return self.weekday

    def describe_weekday(self, weekday: int, noun: str) -> str:
        """Name the layer a weekday's time came from."""
        at = self.for_weekday(weekday).strftime("%H:%M")
        name = DAY_NAMES[weekday]

        # Saturday and Sunday are named before the per-day layer: they have
        # dedicated settings, so "Fixed Saturday" describes them better than
        # "per-day" would, even though they arrive through the same dict.
        if weekday == SATURDAY:
            return f"Fixed Saturday {noun} ({at})."
        if weekday == SUNDAY:
            return f"Fixed Sunday {noun} ({at})."
        if weekday in self.per_day:
            return f"Per-day {name} {noun} ({at})."
        return f"Fixed weekday {noun} ({at})."


def collect_overrides(cfg, prefix: str) -> dict:
    """Collect <PREFIX>_MONDAY … <PREFIX>_SUNDAY, skipping unset days."""
    return {
        index: getattr(cfg, f"{prefix}_{name.upper()}", None)
        for index, name in enumerate(DAY_NAMES)
        if getattr(cfg, f"{prefix}_{name.upper()}", None)
    }
