"""Fixed-bedtime sources.

`BED_SOURCE` in config.py selects one:

* ``"computed"`` (default) — no fixed bedtime; the model back-calculates it
  from your wake time, sleep need and sleep debt.
* ``"fixed"`` — take the bedtime from BED_TIME_WEEKDAY / _SATURDAY / _SUNDAY
  and the optional per-day overrides.

Adding another source means writing one BedSchedule subclass and registering
it in SOURCES below; nothing else changes.
"""

from typing import Optional

from daily_schedule import DAY_NAMES, collect_overrides

from .base import BedSchedule
from .fixed import FixedBedSchedule

# Maps the BED_SOURCE config value to a builder taking (config_module, tz).
# "computed" maps to None: no schedule, so the model computes the bedtime.
SOURCES = {
    "computed": lambda cfg, tz: None,
    "fixed": lambda cfg, tz: FixedBedSchedule(
        weekday=getattr(cfg, "BED_TIME_WEEKDAY", "23:00"),
        saturday=getattr(cfg, "BED_TIME_SATURDAY", "23:30"),
        sunday=getattr(cfg, "BED_TIME_SUNDAY", "22:30"),
        per_day=collect_overrides(cfg, "BED_TIME"),
        tz=tz,
    ),
}


def build_bed_schedule(cfg, tz=None) -> Optional[BedSchedule]:
    """Build the bed schedule named by `cfg.BED_SOURCE`, or None if computed.

    An unknown source falls back to "computed" with a warning rather than
    stopping the run, matching how the rest of the app degrades.
    """
    name = str(getattr(cfg, "BED_SOURCE", "computed") or "computed").strip().lower()

    if name not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        print(f"⚠️  Unknown BED_SOURCE '{name}' (known: {known}); using 'computed'.")
        name = "computed"

    return SOURCES[name](cfg, tz)


__all__ = ["BedSchedule", "FixedBedSchedule", "build_bed_schedule",
           "SOURCES", "DAY_NAMES"]
