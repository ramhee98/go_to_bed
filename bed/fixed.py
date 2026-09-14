"""Fixed bedtimes from a weekly configuration."""

from datetime import date, datetime, timedelta, tzinfo
from typing import Dict, Optional

from daily_schedule import DailyTimeTable

from .base import BedSchedule

# A configured bedtime at or after noon belongs to the evening you turn in;
# anything earlier is the small hours of the morning you wake up.
EVENING_CUTOFF_HOUR = 12


class FixedBedSchedule(BedSchedule):
    """Bedtimes from configuration, resolved per day of the week.

    The day names the **evening you go to bed**, not the morning you get up:
    ``BED_TIME_MONDAY`` is when you turn in on Monday night, so it pairs with
    Tuesday's wake time. That matches how people describe their own routine.

    A time from midnight to 11:59 is read as the small hours of the wake day,
    so ``"00:30"`` on Monday means half past midnight on Tuesday morning —
    still "Monday night" in every sense that matters.
    """

    def __init__(
        self,
        weekday: str = "23:00",
        saturday: str = "23:30",
        sunday: str = "22:30",
        per_day: Optional[Dict[int, str]] = None,
        tz: Optional[tzinfo] = None,
    ):
        self.table = DailyTimeTable(
            weekday, saturday, sunday, per_day,
            prefix="BED_TIME", defaults=("23:00", "23:30", "22:30"),
        )
        self.tz = tz

    def _evening_day(self, wake_time: datetime) -> date:
        return wake_time.date() - timedelta(days=1)

    def bedtime_for(self, wake_time: datetime) -> Optional[datetime]:
        evening = self._evening_day(wake_time)
        at = self.table.for_weekday(evening.weekday())

        # Late evening stays on the evening day; the small hours roll forward
        # onto the wake day.
        on = evening if at.hour >= EVENING_CUTOFF_HOUR else wake_time.date()

        stamp = datetime.combine(on, at)
        return stamp.replace(tzinfo=wake_time.tzinfo or self.tz)

    def describe(self, wake_time: datetime) -> str:
        evening = self._evening_day(wake_time)
        return self.table.describe_weekday(evening.weekday(), "bedtime")
