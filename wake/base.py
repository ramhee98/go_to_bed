"""The interface every wake-time source implements.

Adding a new source (a calendar feed, a shift roster, a travel schedule) means
writing one subclass and registering it in `wake/__init__.py`. Nothing in
`model/`, `ical/`, `main.py` or the Streamlit pages needs to change.
"""

from abc import ABC, abstractmethod
from datetime import date, datetime, time, tzinfo
from typing import Optional


class WakeSchedule(ABC):
    """Answers "what time do I get up on this day?"."""

    @abstractmethod
    def wake_time_for(self, day: date) -> Optional[datetime]:
        """Return the wake datetime for `day`, or None to skip planning it.

        Returning None is how a source says "no opinion about this day" — a
        calendar source with nothing scheduled, say. Those days are simply left
        out of the plan rather than guessed at.
        """

    @abstractmethod
    def describe(self, day: date) -> str:
        """One short line explaining where this day's wake time came from.

        Shown in the calendar event description and in the Streamlit UI, so a
        surprising bedtime can always be traced back to its cause.
        """

    def localize(self, day: date, at: time, tz: Optional[tzinfo]) -> datetime:
        """Combine a day and a wall-clock time into a tz-aware datetime."""
        stamp = datetime.combine(day, at)
        return stamp.replace(tzinfo=tz) if tz is not None else stamp.astimezone()
