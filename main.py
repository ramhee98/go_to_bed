import config
from config import OURA_TOKEN, ICAL_OUTPUT_PATH

from oura_api.client import fetch_sleep_data, fetch_daily_sleep
from model.sleep_need import build_profile
from model.bedtime import sleep_debt_seconds, plan_nights, hhmm
from wake import build_wake_schedule
from ical.generator import (
    load_existing_calendar,
    generate_bedtime_calendar,
    resolve_timezone,
    save_calendar,
)


def setting(name, default):
    """Read a config value, tolerating older config.py files without it."""
    return getattr(config, name, default)


def main():
    history_days = setting("HISTORY_DAYS", 90)

    print(f"Fetching sleep history for the past {history_days} days...")
    sessions = fetch_sleep_data(OURA_TOKEN, days_back=history_days)
    daily_sleep = fetch_daily_sleep(OURA_TOKEN, days_back=history_days)

    if not sessions:
        print("No sleep data found — cannot compute a personal bedtime.")
        return

    print("Learning your personal sleep need...")
    profile = build_profile(
        sessions,
        daily_sleep,
        good_sleep_score=setting("GOOD_SLEEP_SCORE", 80),
        min_good_nights=setting("MIN_GOOD_NIGHTS", 5),
        fallback_sleep_need_hours=setting("FALLBACK_SLEEP_NEED_HOURS", 8.0),
    )
    print(f"  Sleep need: {hhmm(profile.sleep_need_seconds)} "
          f"({profile.source}, from {profile.good_nights}/{profile.nights_analyzed} nights)")
    print(f"  Efficiency: {profile.efficiency * 100:.0f}% "
          f"→ {hhmm(profile.time_in_bed_seconds)} in bed")
    for note in profile.notes:
        print(f"  ⚠️  {note}")

    debt = sleep_debt_seconds(
        sessions,
        daily_sleep,
        profile,
        window_days=setting("DEBT_WINDOW_DAYS", 14),
    )
    print(f"  Sleep debt: {hhmm(debt)} over the last "
          f"{setting('DEBT_WINDOW_DAYS', 14)} days")

    tz = resolve_timezone(setting("TIMEZONE", None))
    wake_schedule = build_wake_schedule(config, tz)

    days_ahead = setting("DAYS_AHEAD", 14)
    print(f"Planning the next {days_ahead} nights...")
    plans = plan_nights(
        wake_schedule,
        profile,
        days_ahead=days_ahead,
        debt_seconds=debt,
        debt_recovery_nights=setting("DEBT_RECOVERY_NIGHTS", 7),
        max_debt_adjustment_minutes=setting("MAX_DEBT_ADJUSTMENT_MINUTES", 45),
        earliest_bedtime=setting("EARLIEST_BEDTIME", None),
        latest_bedtime=setting("LATEST_BEDTIME", None),
    )

    if not plans:
        print("No nights to plan.")
        return

    tonight = plans[0]
    print()
    print(f"  🛏️  Tonight: bed at {tonight.bedtime.strftime('%H:%M')}, "
          f"up at {tonight.wake_time.strftime('%H:%M')} "
          f"({hhmm(tonight.actual_time_in_bed_seconds)} in bed)")
    print()

    print("Loading existing calendar...")
    existing_calendar = load_existing_calendar(ICAL_OUTPUT_PATH)

    calendar = generate_bedtime_calendar(
        plans,
        profile,
        wake_schedule,
        existing_calendar,
        bed_event_duration_minutes=setting("BED_EVENT_DURATION_MINUTES", 15),
        bed_alarm_minutes_before=setting("BED_ALARM_MINUTES_BEFORE", 15),
        wake_event=setting("WAKE_EVENT", True),
        wake_alarm_minutes_before=setting("WAKE_ALARM_MINUTES_BEFORE", 0),
    )

    print(f"Saving calendar to {ICAL_OUTPUT_PATH}...")
    save_calendar(calendar, ICAL_OUTPUT_PATH)

    print("Done.")


if __name__ == "__main__":
    main()
