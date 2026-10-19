"""Tests for merging plans into an existing .ics.

The merge is the only part of the app that touches a file the user already
has, so it has two jobs it must never get wrong: leave events it does not own
alone, and not grow without bound.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icalendar import Calendar, Event

from ical.generator import (
    PLAN_UID_PREFIX,
    _plan_event_day,
    generate_bedtime_calendar,
)
from model.bedtime import BedtimePlan
from model.sleep_need import SleepProfile

TODAY = date(2026, 10, 19)


class _Schedule:
    """The slice of a WakeSchedule the generator actually uses."""

    def describe(self, _day):
        return "test schedule"

    def wake_time_for(self, _day):
        return None


def _profile():
    return SleepProfile(sleep_need_seconds=7 * 3600, efficiency=0.95,
                        latency_seconds=600, nights_analyzed=30,
                        good_nights=10, source="personal")


def _plan(day):
    bedtime = datetime.combine(day - timedelta(days=1), datetime.min.time()) \
        .replace(hour=23)
    wake = datetime.combine(day, datetime.min.time()).replace(hour=7)
    return BedtimePlan(day=day, bedtime=bedtime, wake_time=wake,
                       time_in_bed_seconds=8 * 3600,
                       debt_adjustment_seconds=0, clamped=False)


def _existing(days, foreign=()):
    """A calendar holding plan events for `days` plus any foreign UIDs."""
    cal = Calendar()
    cal.add("prodid", "-//test//EN")
    cal.add("version", "2.0")
    for day in days:
        event = Event()
        event.add("uid", f"{PLAN_UID_PREFIX}bed-{day.isoformat()}@ramhee98")
        event.add("dtstart", datetime.combine(day, datetime.min.time()))
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("summary", "old plan")
        cal.add_component(event)
    for uid, day in foreign:
        event = Event()
        event.add("uid", uid)
        event.add("dtstart", datetime.combine(day, datetime.min.time()))
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("summary", "not ours")
        cal.add_component(event)
    return cal


def _merge(existing, plans, **kwargs):
    return generate_bedtime_calendar(plans, _profile(), _Schedule(), existing,
                                     **kwargs)


def _uids(calendar):
    return {str(c.get("uid")) for c in calendar.walk("VEVENT")}


def _days_kept(calendar):
    """Distinct nights present — each one carries a bed and a wake event."""
    return sorted({d for d in (_plan_event_day(u) for u in _uids(calendar))
                   if d is not None})


# -- parsing -----------------------------------------------------------------

def test_plan_event_day_reads_the_uid():
    assert _plan_event_day(f"{PLAN_UID_PREFIX}bed-2026-09-15@ramhee98") == \
        date(2026, 9, 15)
    assert _plan_event_day(f"{PLAN_UID_PREFIX}wake-2026-09-15@ramhee98") == \
        date(2026, 9, 15)


def test_plan_event_day_is_none_for_anything_else():
    assert _plan_event_day("some-other-event@example.com") is None
    assert _plan_event_day(f"{PLAN_UID_PREFIX}bed-not-a-date@ramhee98") is None


# -- pruning -----------------------------------------------------------------

def test_old_plans_are_pruned():
    old = [TODAY - timedelta(days=n) for n in (200, 120, 91)]
    recent = [TODAY - timedelta(days=n) for n in (90, 30, 1)]
    merged = _merge(_existing(old + recent), [_plan(TODAY)], keep_past_days=90)
    assert _days_kept(merged) == sorted(recent + [TODAY])


def test_keeping_nothing_drops_every_past_plan():
    past = [TODAY - timedelta(days=n) for n in (1, 10, 100)]
    merged = _merge(_existing(past), [_plan(TODAY)], keep_past_days=0)
    assert _days_kept(merged) == [TODAY]


def test_none_keeps_everything():
    # The behaviour before the setting existed, so an old config.py that
    # lacks the key keeps working exactly as it did.
    past = [TODAY - timedelta(days=n) for n in (1, 400)]
    merged = _merge(_existing(past), [_plan(TODAY)], keep_past_days=None)
    assert _days_kept(merged) == sorted(past + [TODAY])


def test_foreign_events_are_never_pruned():
    # Someone else's events may share the file. However old they are, they
    # are not ours to delete.
    foreign = [("dentist@example.com", TODAY - timedelta(days=900))]
    merged = _merge(_existing([TODAY - timedelta(days=900)], foreign),
                    [_plan(TODAY)], keep_past_days=30)
    assert "dentist@example.com" in _uids(merged)
    assert _days_kept(merged) == [TODAY]


def test_cutoff_counts_from_the_first_planned_night():
    # Not from the clock: the run reasons in terms of its planning window, and
    # a cutoff measured differently would prune a day the window still covers.
    plans = [_plan(TODAY + timedelta(days=n)) for n in range(3)]
    edge = TODAY - timedelta(days=10)
    merged = _merge(_existing([edge]), plans, keep_past_days=10)
    assert edge in _days_kept(merged)

    merged = _merge(_existing([edge]), plans, keep_past_days=9)
    assert edge not in _days_kept(merged)


def test_replanned_days_are_replaced_not_duplicated():
    plans = [_plan(TODAY), _plan(TODAY + timedelta(days=1))]
    merged = _merge(_existing([TODAY]), plans, keep_past_days=90)
    uids = [str(c.get("uid")) for c in merged.walk("VEVENT")]
    assert len(uids) == len(set(uids))
    assert sum(1 for u in uids if u.endswith(f"-{TODAY.isoformat()}@ramhee98")) == 2


# -- timestamp churn ---------------------------------------------------------

def _stamp(calendar, uid, name):
    for component in calendar.walk("VEVENT"):
        if str(component.get("uid")) == uid:
            value = component.get(name)
            return value.dt if value is not None else None
    raise AssertionError(f"no event {uid}")


def _bed_uid(day):
    return f"{PLAN_UID_PREFIX}bed-{day.isoformat()}@ramhee98"


def _rewind(calendar, hours=5):
    """Age every plan event's timestamps.

    Two merges in the same process usually land in the same second, and
    iCalendar only records seconds — so a restamped event would come out
    byte-identical anyway and the test would pass for the wrong reason.
    Ageing the stored timestamps first makes any restamping visible.
    """
    shift = timedelta(hours=hours)
    for component in calendar.walk("VEVENT"):
        for name in ("created", "last-modified", "dtstamp"):
            value = component.get(name)
            if value is not None:
                moved = value.dt - shift
                component.pop(name.upper(), None)
                component.add(name, moved)
    return calendar


def test_unchanged_plans_keep_their_timestamps():
    # Every night is rebuilt on every run. If that restamped an unchanged
    # event, clients would see a modification that never happened.
    plans = [_plan(TODAY)]
    first = _rewind(_merge(_existing([]), plans, keep_past_days=90))
    second = _merge(first, plans, keep_past_days=90)

    uid = _bed_uid(TODAY)
    for name in ("created", "last-modified", "dtstamp"):
        assert _stamp(first, uid, name) == _stamp(second, uid, name), name


def test_an_unchanged_run_is_byte_identical():
    plans = [_plan(TODAY), _plan(TODAY + timedelta(days=1))]
    first = _rewind(_merge(_existing([]), plans, keep_past_days=90))
    before = first.to_ical()
    second = _merge(first, plans, keep_past_days=90)
    assert before == second.to_ical()


def test_a_changed_plan_keeps_created_and_moves_last_modified():
    uid = _bed_uid(TODAY)
    first = _rewind(_merge(_existing([]), [_plan(TODAY)], keep_past_days=90))

    moved = _plan(TODAY)
    moved.bedtime = moved.bedtime - timedelta(minutes=30)
    second = _merge(first, [moved], keep_past_days=90)

    assert _stamp(second, uid, "created") == _stamp(first, uid, "created"), \
        "CREATED must record when the event first appeared"
    assert _stamp(second, uid, "last-modified") > _stamp(first, uid, "last-modified"), \
        "a real change must advance LAST-MODIFIED"
    assert second.to_ical() != first.to_ical()


def test_a_new_night_is_stamped_fresh():
    first = _merge(_existing([]), [_plan(TODAY)], keep_past_days=90)
    tomorrow = TODAY + timedelta(days=1)
    second = _merge(first, [_plan(TODAY), _plan(tomorrow)], keep_past_days=90)
    assert _stamp(second, _bed_uid(tomorrow), "created") is not None


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
