"""Render bedtime plans as an .ics file that any calendar client can subscribe to."""

import os
from datetime import datetime, timedelta, timezone
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


def _describe(plan: BedtimePlan, profile: SleepProfile, wake_note: str) -> str:
    """The event description: the numbers, then why they came out that way."""
    lines = [
        f"Bedtime: {plan.bedtime.strftime('%H:%M')}",
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
    lines.append(wake_note)
    lines.extend(plan.reasons)

    if profile.source == "personal":
        lines.append(
            f"Learned from {profile.good_nights} good nights "
            f"of {profile.nights_analyzed} analysed."
        )
    lines.extend(profile.notes)

    return "\n".join(lines)


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
    bed_event_duration_minutes: int = 15,
    bed_alarm_minutes_before=15,
    wake_event: bool = True,
    wake_alarm_minutes_before=0,
) -> Calendar:
    """Merge freshly computed plans into an existing calendar.

    Plans are forecasts, not facts: any plan event for a day being re-planned is
    replaced outright, while plan events for days outside the window (yesterday,
    last week) are kept as a record of what was recommended at the time.
    """
    calendar = _empty_calendar()
    now_utc = datetime.now(timezone.utc)

    planned_days = {plan.day.isoformat() for plan in plans}
    kept = 0

    for component in existing_calendar.walk('VEVENT'):
        if not _is_plan_event(component):
            calendar.add_component(component)
            kept += 1
            continue
        # A plan event for a day we're re-planning is stale; drop it so the
        # fresh one takes its place.
        uid = str(component.get('uid'))
        if not any(uid.endswith(f"-{day}@ramhee98") for day in planned_days):
            calendar.add_component(component)
            kept += 1

    if kept:
        print(f"Kept {kept} existing event(s) outside the planning window.")

    added = 0
    for plan in plans:
        day = plan.day.isoformat()
        wake_note = wake_schedule.describe(plan.day)
        description = _describe(plan, profile, wake_note)

        bed_end = plan.bedtime + timedelta(minutes=bed_event_duration_minutes)
        calendar.add_component(_build_event(
            uid=f"{PLAN_UID_PREFIX}bed-{day}@ramhee98",
            summary=f"🛏️ Go to bed ({hhmm(plan.actual_time_in_bed_seconds)} in bed)",
            start=plan.bedtime,
            end=bed_end,
            description=description,
            alarms=_alarms(bed_alarm_minutes_before, "Bedtime",
                           "BED_ALARM_MINUTES_BEFORE"),
            now_utc=now_utc,
        ))
        added += 1

        if wake_event:
            calendar.add_component(_build_event(
                uid=f"{PLAN_UID_PREFIX}wake-{day}@ramhee98",
                summary=f"⏰ Wake up ({plan.wake_time.strftime('%H:%M')})",
                start=plan.wake_time,
                end=plan.wake_time + timedelta(minutes=15),
                description=description,
                alarms=_alarms(wake_alarm_minutes_before, "Time to get up",
                               "WAKE_ALARM_MINUTES_BEFORE"),
                now_utc=now_utc,
            ))
            added += 1

    print(f"Added {added} planned event(s) for {len(plans)} night(s).")
    return calendar


def save_calendar(calendar: Calendar, path: str) -> None:
    """Write the calendar to disk, creating parent directories as needed."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(calendar.to_ical())
