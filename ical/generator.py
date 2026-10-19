"""Render bedtime plans as an .ics file that any calendar client can subscribe to."""

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Alarm, Calendar, Event

from model.bedtime import BedtimePlan, hhmm
from model.sleep_need import SleepProfile

# Prefix marking events this tool owns, so a plan can be recomputed without
# touching anything else the user keeps in the same file.
PLAN_UID_PREFIX = "go-to-bed-"


def _empty_calendar() -> Calendar:
    cal = Calendar()
    cal.add('prodid', '-//ramhee98//go_to_bed//EN')
    cal.add('version', '2.0')
    return cal


def load_existing_calendar(path: str) -> Calendar:
    """
    Load an existing calendar file and return the calendar object.
    Returns a new empty calendar if the file doesn't exist or can't be parsed.
    """
    if not os.path.exists(path):
        print(f"No existing calendar found at {path}. Starting fresh.")
        return _empty_calendar()

    try:
        with open(path, 'rb') as f:
            existing_cal = Calendar.from_ical(f.read())

        event_count = sum(1 for c in existing_cal.walk('VEVENT'))
        print(f"Loaded existing calendar with {event_count} events.")
        return existing_cal

    except Exception as e:
        print(f"Error loading existing calendar: {e}. Starting fresh.")
        return _empty_calendar()


def resolve_timezone(tz_name: Optional[str]):
    """Resolve a config-supplied IANA timezone name to a ZoneInfo, or None."""
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        print(f"⚠️  Unknown TIMEZONE '{tz_name}'; using the local timezone.")
        return None


def _is_plan_event(component) -> bool:
    uid = component.get('uid')
    return uid is not None and str(uid).startswith(PLAN_UID_PREFIX)


# go-to-bed-bed-2026-09-15@ramhee98 -> 2026-09-15
_PLAN_DAY = re.compile(re.escape(PLAN_UID_PREFIX) + r"(?:bed|wake)-(\d{4}-\d{2}-\d{2})@")


def _plan_event_day(uid: str) -> Optional[date]:
    """The night a plan event belongs to, or None if the UID doesn't say."""
    match = _PLAN_DAY.match(str(uid))
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def parse_lead_times(spec, label: str) -> List[int]:
    """Normalise an alarm setting into a list of minutes-before values.

    Accepts a single number (``15``), several (``[15, 60]`` or ``(15, 60)``),
    a comma-separated string (``"15,60"``), or None for no alarm at all.
    Unusable entries are dropped with a warning rather than stopping the run.
    Results are de-duplicated and ordered furthest-out first, so the reminders
    read in the order they will actually fire.
    """
    if spec is None:
        return []

    if isinstance(spec, str):
        items = [part for part in spec.replace(";", ",").split(",")]
    elif isinstance(spec, (list, tuple, set)):
        items = list(spec)
    else:
        items = [spec]

    minutes = []
    for item in items:
        if item is None or (isinstance(item, str) and not item.strip()):
            continue
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):
            print(f"⚠️  Ignoring unreadable {label} entry '{item}'.")
            continue
        if value < 0:
            print(f"⚠️  Ignoring negative {label} entry '{item}'.")
            continue
        minutes.append(value)

    return sorted(set(minutes), reverse=True)


def _lead_text(minutes: int, event_text: str) -> str:
    """Wording for one reminder, so stacked alarms aren't all identical."""
    if minutes == 0:
        return event_text
    if minutes % 60 == 0:
        hours = minutes // 60
        ahead = f"{hours} hour" if hours == 1 else f"{hours} hours"
    elif minutes > 60:
        ahead = f"{minutes // 60}h {minutes % 60}m"
    else:
        ahead = f"{minutes} min"
    return f"{event_text} in {ahead}"


def _alarms(spec, event_text: str, label: str) -> List[Alarm]:
    """Build one display alarm per configured lead time."""
    alarms = []
    for minutes in parse_lead_times(spec, label):
        alarm = Alarm()
        alarm.add('action', 'DISPLAY')
        alarm.add('description', _lead_text(minutes, event_text))
        alarm.add('trigger', timedelta(minutes=-minutes))
        alarms.append(alarm)
    return alarms


def _describe(plan: BedtimePlan, profile: SleepProfile, wake_note: str,
              bed_note: str = "") -> str:
    """The event description: the numbers, then why they came out that way."""
    lines = [
        f"Bedtime: {plan.bedtime.strftime('%H:%M')}"
        + (" (fixed)" if plan.fixed else ""),
        f"Wake up: {plan.wake_time.strftime('%H:%M')}",
        f"Time in bed: {hhmm(plan.actual_time_in_bed_seconds)}",
        f"Sleep need: {hhmm(profile.sleep_need_seconds)}",
        f"Efficiency: {profile.efficiency * 100:.0f}%",
    ]
    if profile.latency_seconds:
        lines.append(f"Typical latency: {hhmm(profile.latency_seconds)}")
    if plan.debt_adjustment_seconds > 0:
        lines.append(f"Sleep debt adjustment: -{hhmm(plan.debt_adjustment_seconds)}")

    lines.append("")
    if bed_note:
        lines.append(bed_note)
    lines.append(wake_note)
    lines.extend(plan.reasons)

    if profile.source == "personal":
        lines.append(
            f"Learned from {profile.good_nights} good nights "
            f"of {profile.nights_analyzed} analysed."
        )
    lines.extend(profile.notes)

    return "\n".join(lines)


# The properties a subscriber actually sees. If every one of these matches,
# the event has not changed in any way worth restamping it for.
_COMPARED_PROPERTIES = ('dtstart', 'dtend', 'summary', 'description', 'transp')
_COMPARED_ALARM_PROPERTIES = ('action', 'trigger', 'description')


def _ical_value(component, name) -> bytes:
    value = component.get(name)
    if value is None:
        return b''
    try:
        return value.to_ical()
    except AttributeError:
        return str(value).encode()


def _visible_content(component):
    """What the event says, ignoring the bookkeeping timestamps."""
    alarms = sorted(
        tuple(_ical_value(alarm, name) for name in _COMPARED_ALARM_PROPERTIES)
        for alarm in component.walk('VALARM')
    )
    return (tuple(_ical_value(component, name) for name in _COMPARED_PROPERTIES),
            tuple(alarms))


def _carry_timestamps(fresh: Event, previous: Optional[Event], now_utc: datetime):
    """Reconcile a rebuilt event against the one already in the file.

    Every planned night is rebuilt from scratch on every run, so without this
    an unchanged event gets a new CREATED, LAST-MODIFIED and DTSTAMP each
    time. CREATED moving is simply false, and a LAST-MODIFIED that advances
    when nothing was modified invites clients to re-sync or re-notify for a
    plan that has not budged.

    Unchanged events are therefore returned exactly as they were found, which
    also keeps the file byte-stable between runs. A genuinely changed event
    keeps its original CREATED and takes a fresh LAST-MODIFIED.
    """
    if previous is None:
        return fresh
    if _visible_content(previous) == _visible_content(fresh):
        return previous

    created = previous.get('created')
    if created is not None:
        fresh.pop('CREATED', None)
        fresh.add('created', created.dt)
    return fresh


def _build_event(
    uid: str,
    summary: str,
    start: datetime,
    end: datetime,
    description: str,
    alarms: List[Alarm],
    now_utc: datetime,
) -> Event:
    event = Event()
    event.add('uid', uid)
    event.add('dtstart', start)
    event.add('dtend', end)
    event.add('dtstamp', now_utc)
    event.add('created', now_utc)
    event.add('last-modified', now_utc)
    event.add('summary', summary)
    event.add('description', description)
    event.add('transp', 'TRANSPARENT')
    for alarm in alarms:
        event.add_component(alarm)
    return event


def generate_bedtime_calendar(
    plans: List[BedtimePlan],
    profile: SleepProfile,
    wake_schedule,
    existing_calendar: Calendar,
    bed_schedule=None,
    bed_event_duration_minutes: int = 15,
    bed_alarm_minutes_before=15,
    wake_event: bool = True,
    wake_alarm_minutes_before=0,
    keep_past_days: Optional[int] = None,
) -> Calendar:
    """Merge freshly computed plans into an existing calendar.

    Plans are forecasts, not facts: any plan event for a day being re-planned is
    replaced outright, while plan events for days outside the window (yesterday,
    last week) are kept as a record of what was recommended at the time.

    `keep_past_days` bounds that record. Without it the file only ever grows —
    a daily run adds a night and removes nothing, so a year of cron leaves some
    730 events for a subscriber to download. Days older than the cutoff are
    dropped. None keeps everything, which is the old behaviour.

    Only events this tool owns are ever dropped; anything else in the file is
    passed through untouched however old it is.
    """
    calendar = _empty_calendar()
    now_utc = datetime.now(timezone.utc)

    planned_days = {plan.day.isoformat() for plan in plans}
    # Keep the events we are about to rebuild, so an unchanged plan can be
    # emitted unchanged rather than restamped. See _carry_timestamps.
    previous_plans = {str(component.get('uid')): component
                      for component in existing_calendar.walk('VEVENT')
                      if _is_plan_event(component)}
    kept = 0
    pruned = 0
    unchanged = 0

    cutoff = None
    if keep_past_days is not None and int(keep_past_days) >= 0 and plans:
        # Measured from the first night planned (today), not from the clock,
        # so the cutoff matches the window the rest of the run reasons about.
        cutoff = min(plan.day for plan in plans) - timedelta(days=int(keep_past_days))

    for component in existing_calendar.walk('VEVENT'):
        if not _is_plan_event(component):
            calendar.add_component(component)
            kept += 1
            continue
        # A plan event for a day we're re-planning is stale; drop it so the
        # fresh one takes its place.
        uid = str(component.get('uid'))
        if any(uid.endswith(f"-{day}@ramhee98") for day in planned_days):
            continue

        day = _plan_event_day(uid)
        if cutoff is not None and day is not None and day < cutoff:
            pruned += 1
            continue

        calendar.add_component(component)
        kept += 1

    if kept:
        print(f"Kept {kept} existing event(s) outside the planning window.")
    if pruned:
        print(f"Pruned {pruned} plan event(s) older than {keep_past_days} day(s).")

    added = 0
    for plan in plans:
        day = plan.day.isoformat()
        wake_note = wake_schedule.describe(plan.day)
        bed_note = bed_schedule.describe(plan.wake_time) if bed_schedule else ""
        description = _describe(plan, profile, wake_note, bed_note)

        bed_end = plan.bedtime + timedelta(minutes=bed_event_duration_minutes)
        bed_uid = f"{PLAN_UID_PREFIX}bed-{day}@ramhee98"
        bed_event = _build_event(
            uid=bed_uid,
            summary=f"🛏️ Go to bed ({hhmm(plan.actual_time_in_bed_seconds)} in bed)",
            start=plan.bedtime,
            end=bed_end,
            description=description,
            alarms=_alarms(bed_alarm_minutes_before, "Bedtime",
                           "BED_ALARM_MINUTES_BEFORE"),
            now_utc=now_utc,
        )
        reconciled = _carry_timestamps(bed_event, previous_plans.get(bed_uid),
                                       now_utc)
        unchanged += reconciled is not bed_event
        calendar.add_component(reconciled)
        added += 1

        if wake_event:
            wake_uid = f"{PLAN_UID_PREFIX}wake-{day}@ramhee98"
            wake_built = _build_event(
                uid=wake_uid,
                summary=f"⏰ Wake up ({plan.wake_time.strftime('%H:%M')})",
                start=plan.wake_time,
                end=plan.wake_time + timedelta(minutes=15),
                description=description,
                alarms=_alarms(wake_alarm_minutes_before, "Time to get up",
                               "WAKE_ALARM_MINUTES_BEFORE"),
                now_utc=now_utc,
            )
            reconciled = _carry_timestamps(wake_built,
                                           previous_plans.get(wake_uid), now_utc)
            unchanged += reconciled is not wake_built
            calendar.add_component(reconciled)
            added += 1

    suffix = f", {unchanged} unchanged" if unchanged else ""
    print(f"Added {added} planned event(s) for {len(plans)} night(s){suffix}.")
    return calendar


def save_calendar(calendar: Calendar, path: str) -> None:
    """Write the calendar to disk, creating parent directories as needed."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(calendar.to_ical())
