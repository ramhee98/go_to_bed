"""Wake times derived from what's actually in your calendar.

Reads one or more .ics feeds, finds the first commitment of each day, and works
backwards: wake = first event − travel and getting-ready time.

The rule that makes this usable rather than annoying is that it only ever moves
the alarm **earlier**. A 14:00 meeting is not a reason to sleep until 13:00, so
a day whose first event is late keeps the configured wake time. Only a morning
that starts earlier than usual pulls the alarm forward — and with it, by way of
the sleep model, that night's bedtime.
"""

import os
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Dict, List, Optional

import requests
from icalendar import Calendar

from daily_schedule import parse_optional_time

from .base import WakeSchedule

# A feed that can't be read must not take the run down; the day simply falls
# back to the configured wake time.
FETCH_TIMEOUT_SECONDS = 20

# Recurrence is expanded only as far as the plan reaches, plus slack.
DEFAULT_HORIZON_DAYS = 120


def _load_source(source: str) -> Optional[str]:
    """Fetch a URL or read a local path. Returns None on any failure."""
    try:
        if source.startswith(("http://", "https://")):
            response = requests.get(source, timeout=FETCH_TIMEOUT_SECONDS)
            if not response.ok:
                print(f"⚠️  Calendar {source} returned HTTP {response.status_code}; skipping.")
                return None
            return response.text
        if os.path.exists(source):
            with open(source, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        print(f"⚠️  Calendar source not found: {source}; skipping.")
        return None
    except requests.RequestException as error:
        print(f"⚠️  Could not fetch {source}: {error}; skipping.")
        return None
    except OSError as error:
        print(f"⚠️  Could not read {source}: {error}; skipping.")
        return None


def _is_all_day(component) -> bool:
    """True when DTSTART is a bare date rather than a timestamp."""
    start = component.get("dtstart")
    return start is not None and not isinstance(start.dt, datetime)


def _is_busy(component) -> bool:
    """False for events explicitly marked free or cancelled."""
    transparency = str(component.get("transp") or "").strip().upper()
    status = str(component.get("status") or "").strip().upper()
    return transparency != "TRANSPARENT" and status != "CANCELLED"


def _expand(component, horizon: datetime) -> List[datetime]:
    """Every start time this component produces up to `horizon`.

    icalendar does not expand recurrence, so a weekly lecture would otherwise
    register only on its first date. RRULE is materialised with dateutil, and
    EXDATE cancellations are removed.
    """
    start = component.get("dtstart").dt
    rule_property = component.get("rrule")

    if rule_property is None:
        return [start]

    try:
        from dateutil.rrule import rrulestr
    except ImportError:
        print("⚠️  python-dateutil is not installed; recurring events are "
              "counted once only.")
        return [start]

    try:
        rule_text = rule_property.to_ical().decode()
        rule = rrulestr(rule_text, dtstart=start)
    except Exception:
        # A malformed rule shouldn't lose the event entirely.
        return [start]

    excluded = set()
    exdates = component.get("exdate")
    if exdates is not None:
        for entry in (exdates if isinstance(exdates, list) else [exdates]):
            for stamp in getattr(entry, "dts", []):
                value = stamp.dt
                excluded.add(value.date() if isinstance(value, datetime) else value)

    starts = []
    for occurrence in rule:
        if occurrence > horizon:
            break
        occurrence_date = (occurrence.date() if isinstance(occurrence, datetime)
                           else occurrence)
        if occurrence_date in excluded:
            continue
        starts.append(occurrence)
        if len(starts) > 2000:
            break
    return starts or [start]


class CalendarWakeSchedule(WakeSchedule):
    """Wake times from calendar feeds, falling back to a fixed schedule."""

    def __init__(
        self,
        sources: List[str],
        fallback: WakeSchedule,
        lead_minutes: int = 90,
        only_earlier: bool = True,
        earliest_wake: Optional[str] = "05:00",
        skip_all_day: bool = True,
        skip_free: bool = True,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        tz: Optional[tzinfo] = None,
    ):
        self.sources = [s for s in (sources or []) if str(s).strip()]
        self.fallback = fallback
        self.lead = timedelta(minutes=max(0, int(lead_minutes)))
        self.only_earlier = only_earlier
        self.floor = parse_optional_time(earliest_wake, "CALENDAR_EARLIEST_WAKE")
        self.skip_all_day = skip_all_day
        self.skip_free = skip_free
        self.horizon_days = horizon_days
        self.tz = tz
        self._first_event: Optional[Dict[date, datetime]] = None

    # -- loading ------------------------------------------------------------

    def _localize(self, stamp: datetime) -> datetime:
        """Put a start time in the plan's timezone so days line up."""
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=self.tz) if self.tz else stamp.astimezone()
        return stamp.astimezone(self.tz) if self.tz else stamp.astimezone()

    def _build_index(self) -> Dict[date, datetime]:
        """Map each day to the earliest qualifying event start on it."""
        earliest: Dict[date, datetime] = {}
        if not self.sources:
            print("⚠️  WAKE_SOURCE is 'calendar' but CALENDAR_URLS is empty; "
                  "using the fixed wake times.")
            return earliest

        horizon_naive = datetime.now() + timedelta(days=self.horizon_days)
        loaded = 0

        for source in self.sources:
            text = _load_source(source)
            if not text:
                continue
            try:
                calendar = Calendar.from_ical(text)
            except Exception as error:
                print(f"⚠️  Could not parse {source}: {error}; skipping.")
                continue

            loaded += 1
            for component in calendar.walk("VEVENT"):
                if component.get("dtstart") is None:
                    continue
                if self.skip_free and not _is_busy(component):
                    continue
                if self.skip_all_day and _is_all_day(component):
                    continue

                start_value = component.get("dtstart").dt
                horizon = (horizon_naive.replace(tzinfo=start_value.tzinfo)
                           if isinstance(start_value, datetime) else horizon_naive)

                for occurrence in _expand(component, horizon):
                    if not isinstance(occurrence, datetime):
                        # An all-day occurrence that survived the filter has no
                        # meaningful start time to wake before.
                        continue
                    local = self._localize(occurrence)
                    day = local.date()
                    if day not in earliest or local < earliest[day]:
                        earliest[day] = local

        print(f"   Read {loaded} of {len(self.sources)} calendar source(s); "
              f"{len(earliest)} day(s) with events.")
        return earliest

    def _index(self) -> Dict[date, datetime]:
        if self._first_event is None:
            self._first_event = self._build_index()
        return self._first_event

    # -- the schedule -------------------------------------------------------

    def _derived(self, day: date) -> Optional[datetime]:
        """Wake time implied by the first event, or None if nothing qualifies."""
        first = self._index().get(day)
        if first is None:
            return None

        wake = first - self.lead

        if wake.date() < day:
            # The lead time would push the alarm into the previous day. Hold it
            # at midnight rather than waking someone the day before.
            wake = datetime.combine(day, time(0, 0)).replace(tzinfo=wake.tzinfo)

        if self.floor is not None:
            floor_stamp = datetime.combine(day, self.floor).replace(tzinfo=wake.tzinfo)
            if wake < floor_stamp:
                wake = floor_stamp

        return wake

    def wake_time_for(self, day: date) -> Optional[datetime]:
        baseline = self.fallback.wake_time_for(day)
        derived = self._derived(day)

        if derived is None:
            return baseline
        if baseline is None:
            return derived
        if self.only_earlier:
            return min(baseline, derived)
        return derived

    def describe(self, day: date) -> str:
        baseline = self.fallback.wake_time_for(day)
        derived = self._derived(day)

        if derived is None:
            return f"No calendar events. {self.fallback.describe(day)}"

        first = self._index()[day]
        lead = int(self.lead.total_seconds() // 60)

        if baseline is not None and self.only_earlier and baseline <= derived:
            return (f"Calendar starts at {first.strftime('%H:%M')}, no earlier "
                    f"than usual. {self.fallback.describe(day)}")

        # Say so when the floor, not the lead time, decided the answer —
        # otherwise the stated lead doesn't add up against the event time.
        if (self.floor is not None
                and derived.time() == self.floor
                and first - self.lead < derived):
            return (f"Calendar: up at {derived.strftime('%H:%M')} — first event "
                    f"{first.strftime('%H:%M')}, held at CALENDAR_EARLIEST_WAKE.")

        return (f"Calendar: up at {derived.strftime('%H:%M')} (first event "
                f"{first.strftime('%H:%M')}, {lead}m to get ready and travel).")
