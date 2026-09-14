import argparse
import importlib
from datetime import date as _date, timedelta as _timedelta

import config
from config import OURA_TOKEN, ICAL_OUTPUT_PATH
from config_store import sync_with_template

from oura_api.client import (fetch_sleep_data, fetch_daily_sleep,
                              fetch_daily_readiness)
from model.sleep_need import build_profile
from model.bedtime import plan_nights, hhmm
from model import debt as debt_model
from wake import build_wake_schedule
from bed import build_bed_schedule
from ical.generator import (
    load_existing_calendar,
    generate_bedtime_calendar,
    resolve_timezone,
    save_calendar,
)


def setting(name, default):
    """Read a config value, tolerating older config.py files without it."""
    return getattr(config, name, default)


def sync_config():
    """Add settings the template has and config.py lacks.

    A config written by an older version is missing whatever has been added
    since, and hunting those down by hand is how a deployment ends up with new
    code reading settings that aren't there. Existing values are never touched.
    """
    if not setting("AUTO_ADD_MISSING_SETTINGS", True):
        return

    try:
        added = sync_with_template(config.__file__)
    except OSError as error:
        print(f"⚠️  Could not update config.py from the template: {error}")
        return

    if added:
        print(f"Added {len(added)} new setting(s) from config.py.template: "
              f"{', '.join(added)}")
        print(f"   Previous version kept at {config.__file__}.bak")
        importlib.reload(config)


def sleep_need_override():
    """Oura's own sleep need in seconds, or None to derive it from history."""
    if str(setting("SLEEP_NEED_SOURCE", "computed")).strip().lower() != "oura":
        return None
    hours = setting("OURA_SLEEP_NEED_HOURS", None)
    if not hours:
        print("⚠️  SLEEP_NEED_SOURCE is 'oura' but OURA_SLEEP_NEED_HOURS is "
              "unset; deriving the sleep need from your history instead.")
        return None
    return float(hours) * 3600

def baseline_seconds():
    """The configured Oura baseline in seconds, or None to use the profile."""
    hours = setting("OURA_BASELINE_NEED_HOURS", None)
    if hours:
        return float(hours) * 3600
    # Not calibrated: the stated Oura need is a better guess than a need
    # derived from this app's own definition of a good night.
    return sleep_need_override()


def calibrate(observed_minutes):
    """Recover the baseline need that reproduces a debt seen in the Oura app."""
    history_days = setting("HISTORY_DAYS", 90)
    include_naps = setting("OURA_DEBT_INCLUDE_NAPS", False)

    print(f"Fetching sleep history for the past {history_days} days...")
    sessions = fetch_sleep_data(OURA_TOKEN, days_back=history_days)
    daily_sleep = fetch_daily_sleep(OURA_TOKEN, days_back=history_days)

    if not sessions:
        print("No sleep data found.")
        return

    found = debt_model.calibrate_baseline(
        sessions, daily_sleep, observed_minutes,
        window_days=setting("DEBT_WINDOW_DAYS", 14),
        include_naps=include_naps,
    )

    if found is None:
        print("Could not solve for a baseline from that value. A debt of 0 is "
              "satisfied by any low baseline, so try a day showing a non-zero "
              "figure.")
        return

    shown = ", ".join(f"{v:.0f}" for v in observed_minutes)
    print()
    print(f"  Observed in the Oura app (today first): {shown} min")
    print(f"  Baseline that reproduces it: {hhmm(found)} "
          f"({found / 3600:.3f} hours, naps "
          f"{'included' if include_naps else 'excluded'})")
    print()
    print("  Fit against each observation:")

    error = 0
    for days_ago, observed in enumerate(observed_minutes):
        anchor = _date.today() - _timedelta(days=days_ago)
        modelled = debt_model.oura_debt_seconds(
            sessions, daily_sleep, None, today=anchor, baseline_seconds=found,
            window_days=setting("DEBT_WINDOW_DAYS", 14), include_naps=include_naps)
        got = int(modelled / 60)
        error += abs(got - int(observed))
        mark = "match" if got == int(observed) else f"off {got - int(observed):+}"
        print(f"    {anchor}  model {got:3}m   app {int(observed):3}m   {mark}")

    if error:
        print()
        print(f"  Total error {error}m. The 14-day weights sum to ~9.1, so the "
              f"result moves ~9 minutes for every minute of baseline error and "
              f"is then rounded to 10 — an exact match across days is unlikely "
              f"unless Oura's own baseline is constant.")

    print()
    print("  Put this in config.py:")
    print(f"      OURA_BASELINE_NEED_HOURS = {found / 3600:.3f}")


def main():
    sync_config()

    history_days = setting("HISTORY_DAYS", 90)

    print(f"Fetching sleep history for the past {history_days} days...")
    sessions = fetch_sleep_data(OURA_TOKEN, days_back=history_days)
    daily_sleep = fetch_daily_sleep(OURA_TOKEN, days_back=history_days)

    debt_source = str(setting("DEBT_SOURCE", "computed")).strip().lower()
    readiness = (fetch_daily_readiness(OURA_TOKEN, days_back=30)
                 if debt_source == "oura" else [])

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
        sleep_need_override=sleep_need_override(),
    )
    if profile.source == "oura":
        origin = "from Oura, not derived"
    else:
        origin = (f"{profile.source}, from {profile.good_nights}"
                  f"/{profile.nights_analyzed} nights")
    print(f"  Sleep need: {hhmm(profile.sleep_need_seconds)} ({origin})")
    print(f"  Efficiency: {profile.efficiency * 100:.0f}% "
          f"→ {hhmm(profile.time_in_bed_seconds)} in bed")
    for note in profile.notes:
        print(f"  ⚠️  {note}")

    debt = debt_model.assess(
        debt_source,
        sessions,
        daily_sleep,
        readiness,
        profile,
        window_days=setting("DEBT_WINDOW_DAYS", 14),
        recovery_nights=setting("DEBT_RECOVERY_NIGHTS", 7),
        max_adjustment_minutes=setting("MAX_DEBT_ADJUSTMENT_MINUTES", 45),
        baseline_seconds=baseline_seconds(),
        include_naps=setting("OURA_DEBT_INCLUDE_NAPS", True),
    )
    for line in debt.reasons:
        print(f"  {line}")
    if not debt.reasons:
        print("  Sleep debt: none")

    tz = resolve_timezone(setting("TIMEZONE", None))
    wake_schedule = build_wake_schedule(config, tz)
    bed_schedule = build_bed_schedule(config, tz)
    if bed_schedule is not None:
        print("  Bedtimes: fixed from config, not computed")

    days_ahead = setting("DAYS_AHEAD", 14)
    print(f"Planning the next {days_ahead} nights...")
    plans = plan_nights(
        wake_schedule,
        profile,
        days_ahead=days_ahead,
        debt=debt,
        bed_schedule=bed_schedule,
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
        bed_schedule=bed_schedule,
        bed_event_duration_minutes=setting("BED_EVENT_DURATION_MINUTES", 15),
        bed_alarm_minutes_before=setting("BED_ALARM_MINUTES_BEFORE", 15),
        wake_event=setting("WAKE_EVENT", True),
        wake_alarm_minutes_before=setting("WAKE_ALARM_MINUTES_BEFORE", 0),
    )

    print(f"Saving calendar to {ICAL_OUTPUT_PATH}...")
    save_calendar(calendar, ICAL_OUTPUT_PATH)

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plan tonight's bedtime.")
    parser.add_argument(
        "--calibrate-debt", metavar="MINUTES",
        help="Recover the baseline sleep need from the debt the Oura app "
             "shows. Give today's figure, or several comma-separated starting "
             "with today (e.g. 10,20,30) for a steadier fit. Exits without "
             "writing the calendar.")
    args = parser.parse_args()

    if args.calibrate_debt is not None:
        sync_config()
        try:
            observed = [float(part) for part in args.calibrate_debt.split(",")
                        if part.strip()]
        except ValueError:
            parser.error("--calibrate-debt takes numbers of minutes, "
                         "e.g. 10 or 10,20,30")
        calibrate(observed)
    else:
        main()
