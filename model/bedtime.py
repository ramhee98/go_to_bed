"""Turn a sleep profile plus a wake time into tonight's bedtime."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from .sleep_need import SleepProfile, collect_nights


@dataclass
class BedtimePlan:
    """One night's plan: when to go to bed, when to get up, and why."""

    day: date                       # the day you wake up on
    bedtime: datetime
    wake_time: datetime
    time_in_bed_seconds: float      # target, after any debt adjustment
    debt_adjustment_seconds: float  # how much earlier debt pulled bedtime
    clamped: bool                   # True if a guard rail moved the bedtime
    reasons: List[str] = field(default_factory=list)

    @property
    def actual_time_in_bed_seconds(self) -> float:
        """The window the plan really leaves, after guard-rail clamping."""
        return (self.wake_time - self.bedtime).total_seconds()


def hhmm(seconds: float) -> str:
    """Format a duration as H:MM, matching the oura-sleep-ical event style."""
    minutes, _ = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return "%d:%02d" % (hours, minutes)


def sleep_debt_seconds(
    sessions: List[Dict],
    daily_sleep: List[Dict],
    profile: SleepProfile,
    window_days: int = 14,
    today: Optional[date] = None,
) -> float:
    """Accumulated shortfall against your sleep need over the recent window.

    Only shortfalls count: sleeping *more* than you need on Saturday does not
    cancel out Tuesday's deficit, because a long lie-in doesn't undo the
    cognitive cost of a short night.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=window_days)
    debt = 0.0

    for night in collect_nights(sessions, daily_sleep):
        try:
            night_day = date.fromisoformat(night["day"])
        except (ValueError, TypeError):
            continue
        if night_day <= cutoff or night_day > today:
            continue
        shortfall = profile.sleep_need_seconds - night["total_sleep_seconds"]
        if shortfall > 0:
            debt += shortfall

    return debt


def _parse_hhmm(value: str, label: str) -> Optional[int]:
    """Parse "HH:MM" into minutes past midnight, or None if unusable."""
    if not value:
        return None
    try:
        hours, minutes = str(value).strip().split(":")
        hours, minutes = int(hours), int(minutes)
    except (ValueError, AttributeError):
        print(f"⚠️  Could not read {label} ('{value}'); guard rail ignored.")
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        print(f"⚠️  {label} ('{value}') is not a valid time; guard rail ignored.")
        return None
    return hours * 60 + minutes


def _clamp_bedtime(
    bedtime: datetime,
    wake_time: datetime,
    earliest: Optional[str],
    latest: Optional[str],
) -> (datetime, Optional[str]):
    """Hold the bedtime inside the configured window.

    Both guard rails are resolved relative to the wake morning: EARLIEST_BEDTIME
    is the evening before, and LATEST_BEDTIME is whichever side of midnight it
    falls on. A LATEST_BEDTIME of "01:00" therefore means 1am on the wake day,
    while "23:30" means the evening before.
    """
    evening = (wake_time - timedelta(days=1)).date()

    earliest_minutes = _parse_hhmm(earliest, "EARLIEST_BEDTIME")
    latest_minutes = _parse_hhmm(latest, "LATEST_BEDTIME")

    def resolve(minutes: int) -> datetime:
        candidate = datetime.combine(evening, datetime.min.time()) + timedelta(minutes=minutes)
        candidate = candidate.replace(tzinfo=wake_time.tzinfo)
        # A small-hours guard rail (e.g. 01:00) belongs to the wake day, not
        # the evening before it.
        if minutes < 12 * 60:
            candidate += timedelta(days=1)
        return candidate

    if earliest_minutes is not None:
        floor = resolve(earliest_minutes)
        if bedtime < floor:
            return floor, f"Held to EARLIEST_BEDTIME ({earliest})."

    if latest_minutes is not None:
        ceiling = resolve(latest_minutes)
        if bedtime > ceiling:
            return ceiling, f"Held to LATEST_BEDTIME ({latest})."

    return bedtime, None


def plan_night(
    wake_time: datetime,
    profile: SleepProfile,
    debt_seconds: float = 0.0,
    debt_recovery_nights: int = 7,
    max_debt_adjustment_minutes: int = 45,
    earliest_bedtime: Optional[str] = None,
    latest_bedtime: Optional[str] = None,
) -> BedtimePlan:
    """Back-calculate a bedtime from a wake time.

    bedtime = wake - (sleep need / efficiency) - a share of any sleep debt,
    then held inside the configured guard rails.
    """
    reasons = []

    base_tib = profile.time_in_bed_seconds
    reasons.append(
        f"Sleep need {hhmm(profile.sleep_need_seconds)} at "
        f"{profile.efficiency * 100:.0f}% efficiency → {hhmm(base_tib)} in bed."
    )
    if profile.source == "fallback":
        reasons.append("Using the fallback sleep need — not enough good nights yet.")

    adjustment = 0.0
    if debt_seconds > 0 and debt_recovery_nights > 0:
        adjustment = debt_seconds / debt_recovery_nights
        cap = max_debt_adjustment_minutes * 60
        if adjustment > cap:
            adjustment = cap
            reasons.append(
                f"Sleep debt {hhmm(debt_seconds)} → capped at "
                f"{max_debt_adjustment_minutes}m earlier."
            )
        else:
            reasons.append(
                f"Sleep debt {hhmm(debt_seconds)} spread over "
                f"{debt_recovery_nights} nights → {hhmm(adjustment)} earlier."
            )

    target_tib = base_tib + adjustment
    bedtime = wake_time - timedelta(seconds=target_tib)
    # A recommendation accurate to the second is false precision, and reads
    # badly in a calendar event.
    bedtime = bedtime.replace(second=0, microsecond=0)

    bedtime, clamp_reason = _clamp_bedtime(
        bedtime, wake_time, earliest_bedtime, latest_bedtime
    )
    if clamp_reason:
        reasons.append(clamp_reason)

    return BedtimePlan(
        day=wake_time.date(),
        bedtime=bedtime,
        wake_time=wake_time,
        time_in_bed_seconds=target_tib,
        debt_adjustment_seconds=adjustment,
        clamped=clamp_reason is not None,
        reasons=reasons,
    )


def plan_nights(
    wake_schedule,
    profile: SleepProfile,
    days_ahead: int,
    debt_seconds: float = 0.0,
    start_day: Optional[date] = None,
    **kwargs,
) -> List[BedtimePlan]:
    """Plan the next `days_ahead` nights using any wake-time source.

    Days are wake days, and planning starts with tomorrow: the bedtime for
    waking up *today* is already in the past, so the first plan returned is
    always tonight's.

    `wake_schedule` is anything implementing `wake.base.WakeSchedule`, so
    swapping the fixed times for a calendar-driven source changes nothing here.
    """
    start_day = start_day or (date.today() + timedelta(days=1))
    plans = []

    for offset in range(days_ahead):
        day = start_day + timedelta(days=offset)
        wake_time = wake_schedule.wake_time_for(day)
        if wake_time is None:
            continue
        plans.append(plan_night(wake_time, profile, debt_seconds, **kwargs))

    return plans
