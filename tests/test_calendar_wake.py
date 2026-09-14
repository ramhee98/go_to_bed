"""Tests for calendar-driven wake times.

All of these run against tests/fixtures/sample.ics — no network, so the rules
are pinned rather than dependent on whatever is in a real calendar today.
"""

import os
import sys
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wake import build_wake_schedule
from wake.calendar import CalendarWakeSchedule, _duration_minutes, _matches_any
from wake.fixed import FixedWakeSchedule

TZ = ZoneInfo("Europe/Zurich")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample.ics")


def _schedule(**kwargs):
    defaults = dict(
        skip_zero_length=False,
        sources=[FIXTURE],
        fallback=FixedWakeSchedule("07:00", "09:00", "09:00", tz=TZ),
        lead_minutes=90,
        earliest_wake="05:00",
        tz=TZ,
    )
    defaults.update(kwargs)
    return CalendarWakeSchedule(**defaults)


def test_early_event_pulls_the_alarm_forward():
    # Lecture at 08:15, 90 minutes to get ready → 06:45.
    assert _schedule().wake_time_for(date(2026, 9, 15)).time() == time(6, 45)


def test_late_event_does_not_cause_a_lie_in():
    # A 14:00 meeting is no reason to sleep past the usual 07:00.
    assert _schedule().wake_time_for(date(2026, 9, 16)).time() == time(7, 0)


def test_only_earlier_can_be_switched_off():
    # With the guard off, the calendar drives the wake time both ways.
    loose = _schedule(only_earlier=False)
    assert loose.wake_time_for(date(2026, 9, 16)).time() == time(12, 30)


def test_all_day_events_are_ignored():
    assert _schedule().wake_time_for(date(2026, 9, 17)).time() == time(7, 0)


def test_free_events_are_ignored():
    # A 06:00 block marked TRANSPARENT would otherwise force a 04:30 start.
    assert _schedule().wake_time_for(date(2026, 9, 18)).time() == time(7, 0)


def test_cancelled_events_are_ignored():
    assert _schedule().wake_time_for(date(2026, 9, 19)).time() == time(9, 0)


def test_earliest_wake_floor_holds():
    # A 03:00 entry would imply 01:30; the floor holds it at 05:00.
    plan = _schedule()
    assert plan.wake_time_for(date(2026, 9, 20)).time() == time(5, 0)
    assert "EARLIEST_WAKE" in plan.describe(date(2026, 9, 20))


def test_recurring_events_expand():
    # The lecture recurs weekly; a fortnight later it still sets the alarm.
    assert _schedule().wake_time_for(date(2026, 9, 29)).time() == time(6, 45)


def test_exdate_cancellations_are_honoured():
    # 22 Sep is EXDATE'd out of the weekly lecture.
    assert _schedule().wake_time_for(date(2026, 9, 22)).time() == time(7, 0)


def test_days_without_events_use_the_fallback():
    plan = _schedule()
    assert plan.wake_time_for(date(2026, 10, 3)).time() == time(9, 0)   # Saturday
    assert "No calendar events" in plan.describe(date(2026, 10, 3))


def test_unreachable_source_degrades_to_the_fallback():
    plan = _schedule(sources=["https://invalid.invalid/nope.ics",
                              "/no/such/file.ics"])
    assert plan.wake_time_for(date(2026, 9, 15)).time() == time(7, 0)


def test_empty_url_list_degrades_to_the_fallback():
    assert _schedule(sources=[]).wake_time_for(date(2026, 9, 15)).time() == time(7, 0)


def test_lead_time_is_applied():
    # A longer lead means an earlier alarm. Checked against a late baseline so
    # only_earlier doesn't mask the effect being measured: with the usual 07:00
    # wake, a 15-minute lead derives 08:00 and is correctly discarded.
    late = FixedWakeSchedule("11:00", "11:00", "11:00", tz=TZ)
    for lead, expected in [(0, time(8, 15)), (15, time(8, 0)), (135, time(6, 0))]:
        derived = _schedule(lead_minutes=lead, fallback=late)
        assert derived.wake_time_for(date(2026, 9, 15)).time() == expected, lead


def test_a_lead_longer_than_the_day_is_held_at_midnight():
    # A 10-hour lead before an 08:15 event would land on the previous evening;
    # the alarm is held at the start of the day instead, then by the floor.
    plan = _schedule(lead_minutes=600, earliest_wake=None,
                     fallback=FixedWakeSchedule("11:00", "11:00", "11:00", tz=TZ))
    assert plan.wake_time_for(date(2026, 9, 15)).time() == time(0, 0)


def _short_day(**kwargs):
    # Lead of 30m and a 09:00 fallback so the filters' effect is visible.
    # Zero-length skipping defaults on, so it is off here unless a test asks
    # for it — otherwise it would mask the filter under test.
    kwargs.setdefault("skip_zero_length", False)
    return _schedule(lead_minutes=30, earliest_wake=None,
                     fallback=FixedWakeSchedule("09:00", "09:00", "09:00", tz=TZ),
                     **kwargs)


def test_short_events_can_be_ignored():
    # 23 Sep: a 15-minute standup at 07:00, then a 90-minute review at 09:30.
    assert _short_day().wake_time_for(date(2026, 9, 23)).time() == time(6, 30)
    assert (_short_day(min_event_minutes=30)
            .wake_time_for(date(2026, 9, 23)).time() == time(9, 0))


def test_min_event_minutes_zero_disables_the_filter():
    assert (_short_day(min_event_minutes=0)
            .wake_time_for(date(2026, 9, 23)).time() == time(6, 30))


def test_zero_length_events_can_be_ignored():
    # 26 Sep: a 05:30 reminder with DTEND == DTSTART, then a real 10:00 meeting.
    assert (_short_day(skip_zero_length=False)
            .wake_time_for(date(2026, 9, 26)).time() == time(5, 0))
    assert (_short_day(skip_zero_length=True)
            .wake_time_for(date(2026, 9, 26)).time() == time(9, 0))


def test_a_timed_event_without_an_end_is_zero_length():
    # RFC 5545: DTSTART with no DTEND and no DURATION ends at its start, so
    # 25 Sep's 06:30 entry is a point in time, not an event of unknown length.
    assert (_short_day(skip_zero_length=False)
            .wake_time_for(date(2026, 9, 25)).time() == time(6, 0))
    assert (_short_day(skip_zero_length=True)
            .wake_time_for(date(2026, 9, 25)).time() == time(9, 0))


def test_events_of_undeterminable_length_are_kept():
    # 27 Sep pairs a timestamp start with a date end, so the length genuinely
    # cannot be worked out. Dropping what can't be measured risks oversleeping.
    plan = _short_day(skip_zero_length=True, min_event_minutes=60)
    assert plan.wake_time_for(date(2026, 9, 27)).time() == time(6, 0)


def test_duration_is_read_from_every_shape_of_event():
    from icalendar import Calendar
    calendar = Calendar.from_ical(open(FIXTURE, "rb").read())
    lengths = {str(e.get("uid")): _duration_minutes(e)
               for e in calendar.walk("VEVENT")}
    assert lengths["standup@test"] == 15          # DTSTART + DTEND
    assert lengths["explicitzero@test"] == 0      # DTEND == DTSTART
    assert lengths["nodur@test"] == 0             # timed, no DTEND
    assert lengths["malformed@test"] is None      # undeterminable


def test_events_can_be_ignored_by_name():
    # 24 Sep: "Team Lunch with the department" at 06:30.
    assert _short_day().wake_time_for(date(2026, 9, 24)).time() == time(6, 0)
    assert (_short_day(ignore_summaries=["lunch"])
            .wake_time_for(date(2026, 9, 24)).time() == time(9, 0))


def test_name_matching_is_case_insensitive_and_substring():
    assert _matches_any("Daily standup", ["standup"])
    assert _matches_any("Daily standup", ["STANDUP"])
    assert not _matches_any("Daily standup", ["lunch"])
    assert not _matches_any("", ["anything"])
    assert not _matches_any("Daily standup", [])
    assert not _matches_any("Daily standup", ["  ", None])


def test_glob_patterns_match_the_whole_title():
    # A pattern with a wildcard is anchored, so it can be precise where a
    # plain substring would be too eager.
    assert _matches_any("Daily standup", ["daily*"])
    assert _matches_any("Daily standup", ["*standup*"])
    assert not _matches_any("Daily standup", ["standup*"])
    assert _matches_any("OOO - Ramon", ["ooo*"])
    assert not _matches_any("Room booking", ["ooo*"])


def test_both_filters_apply_together():
    plan = _short_day(min_event_minutes=30, ignore_summaries=["lunch"])
    assert plan.wake_time_for(date(2026, 9, 23)).time() == time(9, 0)
    assert plan.wake_time_for(date(2026, 9, 24)).time() == time(9, 0)


def test_config_selects_the_calendar_source():
    class Cfg:
        WAKE_SOURCE = "calendar"
        CALENDAR_URLS = [FIXTURE]
        CALENDAR_LEAD_MINUTES = 90
        WAKE_TIME_WEEKDAY = "07:00"
        WAKE_TIME_SATURDAY = "09:00"
        WAKE_TIME_SUNDAY = "09:00"

    schedule = build_wake_schedule(Cfg, TZ)
    assert isinstance(schedule, CalendarWakeSchedule)
    assert schedule.wake_time_for(date(2026, 9, 15)).time() == time(6, 45)


def test_config_without_calendar_keys_still_uses_fixed():
    class OldConfig:
        WAKE_TIME_WEEKDAY = "06:20"

    assert isinstance(build_wake_schedule(OldConfig, TZ), FixedWakeSchedule)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except AssertionError as e:
                failures += 1
                print(f"❌ {name}: {e or 'assertion failed'}")
    print(f"\n{'All tests passed.' if not failures else f'{failures} test(s) failed.'}")
    sys.exit(1 if failures else 0)
