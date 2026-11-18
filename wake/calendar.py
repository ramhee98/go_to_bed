"""Wake times derived from what's actually in your calendar.

Reads one or more .ics feeds, finds the first commitment of each day, and works
backwards: wake = first event − travel and getting-ready time.

The rule that makes this usable rather than annoying is that it only ever moves
the alarm **earlier**. A 14:00 meeting is not a reason to sleep until 13:00, so
a day whose first event is late keeps the configured wake time. Only a morning
that starts earlier than usual pulls the alarm forward — and with it, by way of
the sleep model, that night's bedtime.
"""

import fnmatch
import os
import re
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


# Properties whose value is a date, timestamp or list of them, so the value
# itself never contains a colon.
_DATE_PROPERTIES = ("DTSTART", "DTEND", "DUE", "RECURRENCE-ID", "EXDATE", "RDATE")

_UNQUOTED_TZID = re.compile(r';TZID=([^";][^;]*)', re.IGNORECASE)


def _repair_tzids(text: str) -> str:
    """Quote TZID parameters that contain a colon.

    Outlook/Exchange feeds write timezone names such as
    ``DTSTART;TZID=(UTC+01:00) Amsterdam, Berlin, …:20260819T121500`` without
    the quotes RFC 5545 requires, so a parser splits the line at the colon
    inside "+01:00" and the start time becomes garbage. Because the value of a
    date property never contains a colon, the last colon is the real separator
    and everything before it can be safely re-quoted.
    """
    # Unfold continuation lines first: long timezone names are often folded.
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    lines = unfolded.splitlines()
    for index, line in enumerate(lines):
        name = line.split(";", 1)[0].split(":", 1)[0].upper()
        if name not in _DATE_PROPERTIES or ";" not in line.split(":", 1)[0]:
            continue
        head, separator, value = line.rpartition(":")
        if not separator or ":" not in head:
            continue  # Already well-formed.

        def quote(match):
            tzid = match.group(1)
            return f';TZID="{tzid}"' if ":" in tzid else match.group(0)

        lines[index] = _UNQUOTED_TZID.sub(quote, head) + ":" + value
    return "\r\n".join(lines) + "\r\n"


def _times_readable(component) -> bool:
    """False when DTSTART, DTEND or DURATION can't be parsed.

    Newer icalendar releases keep a malformed value as a "broken" property that
    raises only when its .dt is read, so one bad event in a feed would
    otherwise take the whole plan down. Such an event is skipped instead.
    """
    for name in ("dtstart", "dtend", "duration"):
        prop = component.get(name)
        if prop is None:
            continue
        try:
            prop.dt
        except Exception:
            return False
    return True


def _is_all_day(component) -> bool:
    """True when DTSTART is a bare date rather than a timestamp."""
    start = component.get("dtstart")
    return start is not None and not isinstance(start.dt, datetime)


def _is_busy(component) -> bool:
    """False for events explicitly marked free or cancelled."""
    transparency = str(component.get("transp") or "").strip().upper()
    status = str(component.get("status") or "").strip().upper()
    return transparency != "TRANSPARENT" and status != "CANCELLED"


def _duration_minutes(component) -> Optional[float]:
    """Length of an event in minutes, or None when it can't be determined.

    A timed event with neither DTEND nor DURATION is zero-length, not unknown:
    RFC 5545 §3.6.1 says it "ends on the same calendar date and time of day"
    as its start. Those are the point-in-time reminders a calendar collects.

    None is reserved for genuinely undeterminable cases — a malformed pairing
    of a date with a timestamp. Those are kept, because wrongly dropping a real
    early meeting means oversleeping, while wrongly keeping one only means a
    needlessly early night.
    """
    duration = component.get("duration")
    if duration is not None:
        try:
            return duration.dt.total_seconds() / 60
        except AttributeError:
            return None

    start = component.get("dtstart")
    end = component.get("dtend")

    if end is None:
        # All-day events without DTEND span the day; timed ones are a point.
        return None if (start is None or not isinstance(start.dt, datetime)) else 0.0

    try:
        return (end.dt - start.dt).total_seconds() / 60
    except (TypeError, AttributeError):
        # A DTSTART date paired with a DTEND timestamp, or vice versa.
        return None


def _matches_any(summary, patterns: List[str]) -> bool:
    """True if an event title matches one of the ignore patterns.

    Matching is case-insensitive. A plain word matches anywhere in the title,
    so "lunch" catches "Team Lunch"; a pattern containing * or ? is matched
    against the whole title as a glob, so "standup*" catches only titles that
    start with it.
    """
    text = str(summary or "").strip().lower()
    if not text:
        return False

    for pattern in patterns:
        needle = str(pattern).strip().lower()
        if not needle:
            continue
        if any(character in needle for character in "*?["):
            if fnmatch.fnmatch(text, needle):
                return True
        elif needle in text:
            return True

    return False


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
            try:
                stamps = [stamp.dt for stamp in getattr(entry, "dts", [])]
            except Exception:
                # An unreadable exclusion is dropped rather than the event: at
                # worst a cancelled occurrence still counts, which only means
                # an earlier night, never an overslept morning.
                print("⚠️  Ignoring an unreadable EXDATE in "
                      f"'{component.get('summary', '')}'.")
                continue
            for value in stamps:
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
        skip_zero_length: bool = True,
        min_event_minutes: int = 0,
        ignore_summaries: Optional[List[str]] = None,
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
        self.skip_zero_length = skip_zero_length
        self.min_event_minutes = max(0, int(min_event_minutes or 0))
        self.ignore_summaries = [str(p) for p in (ignore_summaries or [])
                                 if str(p).strip()]
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
        skipped_by_name = 0
        skipped_by_length = 0
        skipped_zero = 0
        skipped_broken = 0

        for source in self.sources:
            text = _load_source(source)
            if not text:
                continue
            try:
                calendar = Calendar.from_ical(_repair_tzids(text))
            except Exception as error:
                print(f"⚠️  Could not parse {source}: {error}; skipping.")
                continue

            loaded += 1
            for component in calendar.walk("VEVENT"):
                if component.get("dtstart") is None:
                    continue
                if not _times_readable(component):
                    skipped_broken += 1
                    continue
                if self.skip_free and not _is_busy(component):
                    continue
                if self.skip_all_day and _is_all_day(component):
                    continue
                if _matches_any(component.get("summary"), self.ignore_summaries):
                    skipped_by_name += 1
                    continue
                if self.skip_zero_length or self.min_event_minutes:
                    length = _duration_minutes(component)
                    # Undeterminable length is kept: see _duration_minutes.
                    if length is not None:
                        if self.skip_zero_length and length <= 0:
                            skipped_zero += 1
                            continue
                        if length < self.min_event_minutes:
                            skipped_by_length += 1
                            continue

                start_value = component.get("dtstart").dt
                horizon = (horizon_naive.replace(tzinfo=start_value.tzinfo)
                           if isinstance(start_value, datetime) else horizon_naive)

                try:
                    occurrences = list(_expand(component, horizon))
                except Exception as error:
                    # Last resort: whatever else a feed gets wrong costs this
                    # one event, never the whole plan.
                    print(f"⚠️  Skipping '{component.get('summary', '')}': "
                          f"{error}")
                    skipped_broken += 1
                    continue

                for occurrence in occurrences:
                    if not isinstance(occurrence, datetime):
                        # An all-day occurrence that survived the filter has no
                        # meaningful start time to wake before.
                        continue
                    local = self._localize(occurrence)
                    day = local.date()
                    if day not in earliest or local < earliest[day]:
                        earliest[day] = local

        if skipped_broken:
            print(f"⚠️  Skipped {skipped_broken} unreadable event(s).")

        filtered = []
        if skipped_by_name:
            filtered.append(f"{skipped_by_name} by name")
        if skipped_zero:
            filtered.append(f"{skipped_zero} zero-length")
        if skipped_by_length:
            filtered.append(f"{skipped_by_length} shorter than "
                            f"{self.min_event_minutes}m")
        suffix = f" ({', '.join(filtered)} ignored)" if filtered else ""

        print(f"   Read {loaded} of {len(self.sources)} calendar source(s); "
              f"{len(earliest)} day(s) with events{suffix}.")
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
