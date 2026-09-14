"""Tests for the bedtime model.

Run with `pytest` from the project root, or `python3 tests/test_model.py`.
These cover the parts that are easy to get quietly wrong: times either side of
midnight, and the fallbacks that keep a thin history from producing nonsense.
"""

import os
import sys
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.bedtime import plan_night, sleep_debt_seconds, hhmm
from model.sleep_need import build_profile, normalize_efficiency, SleepProfile
from ical.generator import parse_lead_times, _lead_text
from wake import build_wake_schedule
from wake.fixed import FixedWakeSchedule
from bed import build_bed_schedule
from bed.fixed import FixedBedSchedule

TZ = ZoneInfo("Europe/Zurich")


def _profile(hours=8.0, efficiency=1.0):
    return SleepProfile(
        sleep_need_seconds=hours * 3600,
        efficiency=efficiency,
        latency_seconds=600,
        nights_analyzed=30,
        good_nights=10,
        source="personal",
    )


def _night(day, hours, score=85, efficiency=95):
    return {
        "id": day,
        "day": day,
        "type": "long_sleep",
        "total_sleep_duration": hours * 3600,
        "time_in_bed": hours * 3600 * 1.05,
        "efficiency": efficiency,
        "latency": 600,
        "awake_time": 900,
        "bedtime_start": f"{day}T23:00:00+02:00",
        "bedtime_end": f"{day}T07:00:00+02:00",
    }


def test_efficiency_is_normalized_from_percent():
    assert normalize_efficiency(94) == 0.94
    assert normalize_efficiency(0.94) == 0.94
    assert normalize_efficiency(None) is None
    assert normalize_efficiency("nonsense") is None
    assert normalize_efficiency(0) is None


def test_bedtime_is_wake_minus_time_in_bed():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    plan = plan_night(wake, _profile(hours=8.0, efficiency=1.0))
    assert plan.bedtime == datetime(2026, 9, 14, 22, 30, tzinfo=TZ)


def test_lower_efficiency_means_earlier_bedtime():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    perfect = plan_night(wake, _profile(efficiency=1.0))
    poor = plan_night(wake, _profile(efficiency=0.8))
    assert poor.bedtime < perfect.bedtime


def test_earliest_bedtime_guard_rail_holds():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    plan = plan_night(wake, _profile(hours=11.0), earliest_bedtime="21:00")
    assert plan.bedtime == datetime(2026, 9, 14, 21, 0, tzinfo=TZ)
    assert plan.clamped


def test_latest_bedtime_after_midnight_lands_on_the_wake_day():
    # A LATEST_BEDTIME of "01:00" means 1am on the morning you wake up, not
    # 1am the previous day — the easiest thing to get wrong here.
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    plan = plan_night(wake, _profile(hours=2.0), latest_bedtime="01:00")
    assert plan.bedtime == datetime(2026, 9, 15, 1, 0, tzinfo=TZ)
    assert plan.clamped


def test_evening_latest_bedtime_stays_on_the_evening_before():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    plan = plan_night(wake, _profile(hours=2.0), latest_bedtime="23:30")
    assert plan.bedtime == datetime(2026, 9, 14, 23, 30, tzinfo=TZ)


def test_unparseable_guard_rail_is_ignored_not_fatal():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    plan = plan_night(wake, _profile(), earliest_bedtime="quarter past nine")
    assert not plan.clamped


def test_debt_adjustment_is_capped():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    plan = plan_night(
        wake, _profile(efficiency=1.0),
        debt_seconds=20 * 3600, debt_recovery_nights=7,
        max_debt_adjustment_minutes=45,
    )
    assert plan.debt_adjustment_seconds == 45 * 60


def test_debt_counts_shortfalls_only_not_surplus():
    profile = _profile(hours=8.0)
    sessions = [_night("2026-09-10", 6.0), _night("2026-09-11", 12.0)]
    daily = [{"day": n["day"], "score": 85} for n in sessions]
    debt = sleep_debt_seconds(sessions, daily, profile,
                              window_days=14, today=date(2026, 9, 14))
    # Two hours short on the 10th; the twelve-hour night does not repay it.
    assert debt == 2 * 3600


def test_naps_do_not_count_towards_sleep_need():
    sessions = [_night(f"2026-09-{d:02d}", 8.0) for d in range(1, 11)]
    for nap in sessions[:3]:
        nap["type"] = "late_nap"
        nap["total_sleep_duration"] = 0.5 * 3600
    daily = [{"day": n["day"], "score": 85} for n in sessions]
    profile = build_profile(sessions, daily, min_good_nights=5)
    assert profile.sleep_need_seconds == 8 * 3600


def test_sleep_need_can_be_taken_from_oura():
    # Oura does not publish its sleep need, so it is typed in. Efficiency and
    # latency must still come from the user's own history.
    sessions = [_night(f"2026-09-{d:02d}", 8.0, efficiency=91) for d in range(1, 11)]
    daily = [{"day": n["day"], "score": 85} for n in sessions]
    profile = build_profile(sessions, daily, sleep_need_override=7.267 * 3600)
    assert profile.sleep_need_seconds == 7.267 * 3600
    assert profile.source == "oura"
    assert profile.efficiency == 0.91
    assert profile.latency_seconds == 600
    assert profile.nights_analyzed == 10


def test_oura_sleep_need_changes_the_time_in_bed_target():
    sessions = [_night(f"2026-09-{d:02d}", 8.0) for d in range(1, 11)]
    daily = [{"day": n["day"], "score": 85} for n in sessions]
    computed = build_profile(sessions, daily)
    from_oura = build_profile(sessions, daily, sleep_need_override=7.0 * 3600)
    assert from_oura.time_in_bed_seconds < computed.time_in_bed_seconds


def test_thin_history_falls_back_to_configured_need():
    sessions = [_night("2026-09-10", 6.0, score=40)]
    daily = [{"day": "2026-09-10", "score": 40}]
    profile = build_profile(sessions, daily, min_good_nights=5,
                            fallback_sleep_need_hours=8.0)
    assert profile.source == "fallback"
    assert profile.sleep_need_seconds == 8 * 3600


def test_weekend_wake_times_are_separate():
    schedule = FixedWakeSchedule("06:30", "08:00", "09:15", tz=TZ)
    friday = schedule.wake_time_for(date(2026, 9, 18))
    saturday = schedule.wake_time_for(date(2026, 9, 19))
    sunday = schedule.wake_time_for(date(2026, 9, 20))
    assert friday.time() == time(6, 30)
    assert saturday.time() == time(8, 0)
    assert sunday.time() == time(9, 15)
    assert "Saturday" in schedule.describe(date(2026, 9, 19))


def test_per_day_override_beats_the_weekday_default():
    schedule = FixedWakeSchedule("06:30", "08:00", "08:00",
                                 per_day={2: "09:45"}, tz=TZ)
    assert schedule.wake_time_for(date(2026, 9, 16)).time() == time(9, 45)   # Wed
    assert schedule.wake_time_for(date(2026, 9, 17)).time() == time(6, 30)   # Thu
    assert "Wednesday" in schedule.describe(date(2026, 9, 16))


def test_unset_days_fall_through_to_the_weekday_default():
    schedule = FixedWakeSchedule("06:30", "08:00", "08:00",
                                 per_day={0: None, 1: ""}, tz=TZ)
    assert schedule.wake_time_for(date(2026, 9, 14)).time() == time(6, 30)
    assert schedule.wake_time_for(date(2026, 9, 15)).time() == time(6, 30)


def test_weekend_keeps_its_own_label_not_per_day():
    # Saturday and Sunday arrive through the same dict, but they have
    # dedicated settings, so the description must not call them "per-day".
    schedule = FixedWakeSchedule("06:30", "08:00", "09:00",
                                 per_day={5: "08:00", 6: "09:00"}, tz=TZ)
    assert schedule.describe(date(2026, 9, 19)) == "Fixed Saturday wake time (08:00)."
    assert schedule.describe(date(2026, 9, 20)) == "Fixed Sunday wake time (09:00)."


def test_config_without_per_day_keys_still_works():
    # An existing config.py predating per-day overrides must keep working.
    class OldConfig:
        WAKE_SOURCE = "fixed"
        WAKE_TIME_WEEKDAY = "06:20"
        WAKE_TIME_SATURDAY = "08:00"
        WAKE_TIME_SUNDAY = "08:00"

    schedule = build_wake_schedule(OldConfig, TZ)
    assert schedule.wake_time_for(date(2026, 9, 14)).time() == time(6, 20)
    assert schedule.wake_time_for(date(2026, 9, 19)).time() == time(8, 0)


def test_malformed_override_falls_through_instead_of_crashing():
    schedule = FixedWakeSchedule("06:30", "08:00", "08:00",
                                 per_day={2: "25:99"}, tz=TZ)
    assert schedule.wake_time_for(date(2026, 9, 16)).time() == time(6, 30)


def test_bad_wake_time_falls_back_without_crashing():
    schedule = FixedWakeSchedule("not a time", "08:00", "08:00", tz=TZ)
    assert schedule.wake_time_for(date(2026, 9, 14)).time() == time(6, 30)


def test_alarm_spec_accepts_every_documented_form():
    assert parse_lead_times(None, "X") == []
    assert parse_lead_times(15, "X") == [15]            # single int, as before
    assert parse_lead_times([15, 60], "X") == [60, 15]  # list
    assert parse_lead_times((60, 15), "X") == [60, 15]  # tuple
    assert parse_lead_times("15,60", "X") == [60, 15]   # comma string
    assert parse_lead_times("60, 15", "X") == [60, 15]  # whitespace tolerated
    assert parse_lead_times(0, "X") == [0]              # at the event itself


def test_alarm_spec_orders_furthest_out_first_and_dedupes():
    assert parse_lead_times([15, 60, 15], "X") == [60, 15]


def test_alarm_spec_drops_junk_without_crashing():
    assert parse_lead_times(["15", "soon", None, "", -5, 60], "X") == [60, 15]


def test_alarm_text_names_its_lead_time():
    assert _lead_text(0, "Time to get up") == "Time to get up"
    assert _lead_text(15, "Bedtime") == "Bedtime in 15 min"
    assert _lead_text(60, "Bedtime") == "Bedtime in 1 hour"
    assert _lead_text(120, "Bedtime") == "Bedtime in 2 hours"
    assert _lead_text(90, "Bedtime") == "Bedtime in 1h 30m"


def test_fixed_bedtime_is_used_as_given():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    fixed = datetime(2026, 9, 14, 23, 0, tzinfo=TZ)
    plan = plan_night(wake, _profile(hours=8.0), debt_seconds=20 * 3600,
                      fixed_bedtime=fixed, earliest_bedtime="21:00")
    assert plan.bedtime == fixed
    assert plan.fixed
    # A configured bedtime is a decision: neither debt nor guard rails move it.
    assert plan.debt_adjustment_seconds == 0
    assert not plan.clamped
    assert not any("Sleep debt" in r for r in plan.reasons)


def test_fixed_bedtime_warns_when_it_is_short():
    wake = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)
    short = plan_night(wake, _profile(hours=8.0, efficiency=1.0),
                       fixed_bedtime=datetime(2026, 9, 14, 23, 30, tzinfo=TZ))
    assert any("short of" in r for r in short.reasons)

    ample = plan_night(wake, _profile(hours=7.0, efficiency=1.0),
                       fixed_bedtime=datetime(2026, 9, 14, 22, 0, tzinfo=TZ))
    assert any("over your" in r for r in ample.reasons)


def test_bed_time_day_names_the_evening_you_turn_in():
    # BED_TIME_MONDAY is Monday night, so it pairs with Tuesday's wake time.
    schedule = FixedBedSchedule("23:00", "23:30", "22:30",
                                per_day={0: "21:15"}, tz=TZ)
    wake_tuesday = datetime(2026, 9, 15, 6, 30, tzinfo=TZ)   # Tue
    bedtime = schedule.bedtime_for(wake_tuesday)
    assert bedtime == datetime(2026, 9, 14, 21, 15, tzinfo=TZ)   # Mon evening
    assert "Monday" in schedule.describe(wake_tuesday)


def test_small_hours_bedtime_rolls_onto_the_wake_day():
    # "00:30" on Friday means half past midnight on Saturday morning.
    schedule = FixedBedSchedule("23:00", "23:30", "22:30",
                                per_day={4: "00:30"}, tz=TZ)
    wake_saturday = datetime(2026, 9, 19, 8, 0, tzinfo=TZ)
    assert schedule.bedtime_for(wake_saturday) == datetime(2026, 9, 19, 0, 30, tzinfo=TZ)


def test_bed_source_computed_means_no_schedule():
    class Cfg:
        BED_SOURCE = "computed"
    assert build_bed_schedule(Cfg, TZ) is None


def test_unknown_bed_source_falls_back_to_computed():
    class Cfg:
        BED_SOURCE = "nonsense"
    assert build_bed_schedule(Cfg, TZ) is None


def test_config_without_bed_keys_still_computes():
    class OldConfig:
        WAKE_TIME_WEEKDAY = "06:20"
    assert build_bed_schedule(OldConfig, TZ) is None


def test_hhmm_formatting():
    assert hhmm(0) == "0:00"
    assert hhmm(3600) == "1:00"
    assert hhmm(27 * 3600 + 5 * 60) == "27:05"


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
