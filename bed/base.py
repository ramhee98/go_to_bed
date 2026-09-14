"""The interface every fixed-bedtime source implements.

A bed source answers "given that I get up at this time, when should I turn in?"
with a configured answer, bypassing the computed one. It takes the wake
datetime rather than a date so it can anchor to the right evening and inherit
the timezone the plan is being built in.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


class BedSchedule(ABC):
    """Supplies a fixed bedtime for the night before a given wake time."""

    @abstractmethod
    def bedtime_for(self, wake_time: datetime) -> Optional[datetime]:
        """Return the configured bedtime, or None to let the model compute it.

        Returning None is how a source says "no opinion about this night", so
        a partially configured week falls back to the computed bedtime on the
        days it doesn't cover.
        """

    @abstractmethod
    def describe(self, wake_time: datetime) -> str:
        """One short line explaining where this night's bedtime came from."""
