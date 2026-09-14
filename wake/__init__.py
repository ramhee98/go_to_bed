"""Wake-time sources.

`WAKE_SOURCE` in config.py selects one. To add a calendar-driven source:

  1. Write `wake/calendar.py` with a `CalendarWakeSchedule(WakeSchedule)` that
     reads your .ics URLs, finds the first commitment of each day and subtracts
     travel and morning routine. Return None for days with nothing scheduled,
     or delegate to FixedWakeSchedule to fall back to the configured time.
  2. Register it in SOURCES below.
  3. Add its settings to config.py.template.

Nothing else changes: main.py, the model and the Streamlit pages all talk to
the WakeSchedule interface.
"""

from typing import Optional

from .base import WakeSchedule
from daily_schedule import DAY_NAMES, collect_overrides

from .fixed import FixedWakeSchedule

def _fixed(cfg, tz) -> FixedWakeSchedule:
    return FixedWakeSchedule(
        weekday=getattr(cfg, "WAKE_TIME_WEEKDAY", "06:30"),
        saturday=getattr(cfg, "WAKE_TIME_SATURDAY", "08:00"),
        sunday=getattr(cfg, "WAKE_TIME_SUNDAY", "08:00"),
        per_day=collect_overrides(cfg, "WAKE_TIME"),
        tz=tz,
    )


def _calendar(cfg, tz) -> WakeSchedule:
    # The fixed schedule is the baseline the calendar can only pull earlier
    # from, so a day with no events — or a late first event — still has an
    # answer.
    from .calendar import CalendarWakeSchedule

    return CalendarWakeSchedule(
        sources=getattr(cfg, "CALENDAR_URLS", []),
        fallback=_fixed(cfg, tz),
        lead_minutes=getattr(cfg, "CALENDAR_LEAD_MINUTES", 90),
        only_earlier=getattr(cfg, "CALENDAR_ONLY_EARLIER", True),
        earliest_wake=getattr(cfg, "CALENDAR_EARLIEST_WAKE", "05:00"),
        skip_all_day=getattr(cfg, "CALENDAR_SKIP_ALL_DAY", True),
        skip_free=getattr(cfg, "CALENDAR_SKIP_FREE", True),
        min_event_minutes=getattr(cfg, "CALENDAR_MIN_EVENT_MINUTES", 0),
        ignore_summaries=getattr(cfg, "CALENDAR_IGNORE_SUMMARIES", []),
        tz=tz,
    )


# Maps the WAKE_SOURCE config value to a builder taking (config_module, tz).
SOURCES = {
    "fixed": _fixed,
    "calendar": _calendar,
}


def build_wake_schedule(cfg, tz=None) -> WakeSchedule:
    """Build the wake schedule named by `cfg.WAKE_SOURCE`.

    An unknown source falls back to "fixed" with a warning rather than
    stopping the run, matching how the rest of the app degrades.
    """
    name = str(getattr(cfg, "WAKE_SOURCE", "fixed") or "fixed").strip().lower()

    if name not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        print(f"⚠️  Unknown WAKE_SOURCE '{name}' (known: {known}); using 'fixed'.")
        name = "fixed"

    return SOURCES[name](cfg, tz)


__all__ = ["WakeSchedule", "FixedWakeSchedule", "build_wake_schedule", "SOURCES"]
