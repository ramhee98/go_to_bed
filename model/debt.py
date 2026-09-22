"""How much sleep debt is pulling tonight's bedtime earlier.

Two sources, selected by `DEBT_SOURCE`:

* ``"computed"`` — sum your own shortfalls against your personal sleep need
  over a rolling window, then repay them gradually. A real duration.
* ``"oura"`` — use Oura's own ``sleep_balance``, the readiness contributor that
  reflects whether the last two weeks of sleep match your needs.

A caveat worth stating plainly: **Oura does not publish a sleep debt duration.**
`sleep_time` returns `optimal_bedtime: null` on most days, and nothing in the v2
API gives hours owed. `sleep_balance` is a 0-100 score, so mapping it to minutes
of earlier bedtime is this app's interpretation, not a figure from Oura.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

from .bedtime import hhmm, sleep_debt_seconds
from .sleep_need import SleepProfile, collect_nights

# Oura reports sleep_balance on a 1-100 scale where 100 means "in balance".
BALANCE_BEST = 100

# Oura's own sleep debt weights each night by 0.93^n, n=0 being today, so a
# short night three weeks ago barely registers while last night dominates.
OURA_DECAY = 0.93
OURA_WINDOW_DAYS = 14
OURA_ROUNDING_MINUTES = 10


@dataclass
class DebtAssessment:
    """How much earlier to go to bed, and why."""

    adjustment_seconds: float
    source: str      # "computed" | "oura" | "oura_balance" | "none"
    debt_seconds: Optional[float] = None  # a real duration, when there is one
    balance: Optional[int] = None         # Oura's score, when that's the source
    reasons: List[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        """A short value for a metric tile — a duration wherever there is one."""
        if self.debt_seconds is not None:
            return hhmm(self.debt_seconds)
        if self.balance is not None:
            return f"{self.balance}/100"
        return "0:00"

    @property
    def caption(self) -> str:
        """The sub-label under the tile, naming the source and its effect."""
        if self.source == "oura":
            score = (f" · balance {self.balance}/100"
                     if self.balance is not None else "")
            return f"Oura formula → -{hhmm(self.adjustment_seconds)}{score}"
        if self.source == "oura_balance":
            score = f"balance {self.balance}/100" if self.balance is not None \
                else "no balance score"
            return f"Oura {score} → -{hhmm(self.adjustment_seconds)}"
        if self.source == "none":
            return "adjustment disabled"
        return f"-{hhmm(self.adjustment_seconds)} bedtime"


def computed_assessment(
    sessions: List[Dict],
    daily_sleep: List[Dict],
    profile: SleepProfile,
    window_days: int = 14,
    recovery_nights: int = 7,
    max_adjustment_minutes: int = 45,
    today: Optional[date] = None,
) -> DebtAssessment:
    """Debt as the accumulated shortfall against your own sleep need."""
    debt = sleep_debt_seconds(sessions, daily_sleep, profile,
                              window_days=window_days, today=today)
    reasons = []
    adjustment = 0.0

    if debt > 0 and recovery_nights > 0:
        adjustment = debt / recovery_nights
        cap = max_adjustment_minutes * 60
        if adjustment > cap:
            adjustment = cap
            reasons.append(f"Sleep debt {hhmm(debt)} → capped at "
                           f"{max_adjustment_minutes}m earlier.")
        else:
            reasons.append(f"Sleep debt {hhmm(debt)} spread over "
                           f"{recovery_nights} nights → {hhmm(adjustment)} earlier.")

    return DebtAssessment(adjustment_seconds=adjustment, source="computed",
                          debt_seconds=debt, reasons=reasons)


def _slept_by_day(sessions: List[Dict], daily_sleep: List[Dict],
                  include_naps: bool) -> Dict[date, float]:
    """Total sleep per day in seconds.

    Oura's daily sleep total counts naps, so including them fits its debt
    figure better than nights alone.
    """
    totals: Dict[date, float] = {}

    if include_naps:
        for session in sessions or []:
            duration = session.get("total_sleep_duration")
            if not duration:
                continue
            try:
                day = date.fromisoformat(str(session.get("day")))
            except (ValueError, TypeError):
                continue
            totals[day] = totals.get(day, 0.0) + float(duration)
        return totals

    for night in collect_nights(sessions, daily_sleep):
        try:
            totals[date.fromisoformat(night["day"])] = night["total_sleep_seconds"]
        except (ValueError, TypeError):
            continue
    return totals


def oura_debt_seconds(
    sessions: List[Dict],
    daily_sleep: List[Dict],
    profile: SleepProfile,
    window_days: int = OURA_WINDOW_DAYS,
    today: Optional[date] = None,
    baseline_seconds: Optional[float] = None,
    include_naps: bool = False,
) -> float:
    """Oura's 14-day rolling sleep debt.

        L = baseline_need - actual_sleep          (per night, signed)
        D = sum(L_n * 0.93^n) for n = 0..13       (n=0 is today)
        D = max(D, 0), rounded to the nearest 10 minutes

    Two things differ from `sleep_debt_seconds`, and both matter:

    * **L is signed and only the total is clamped.** A night longer than your
      need genuinely offsets an earlier short one, where the computed source
      discards every surplus. This makes Oura's figure the more forgiving of
      the two.
    * **Recent nights dominate.** The 0.93 decay means last night counts fully
      while a night thirteen days ago counts about 39%.

    Nights with no data are skipped rather than counted as zero sleep, so a
    gap in wear doesn't manufacture debt.

    **The baseline dominates the result.** The weights sum to about 9.1 over
    fourteen days, so every minute of baseline error moves the debt by roughly
    nine minutes. Oura does not publish the sleep need its own app uses, so to
    match that display you must supply it — see `calibrate_baseline`.
    """
    today = today or date.today()
    baseline = (baseline_seconds if baseline_seconds is not None
                else profile.sleep_need_seconds)
    slept = _slept_by_day(sessions, daily_sleep, include_naps)

    total_seconds = 0.0
    for offset in range(max(0, window_days)):
        day = today - timedelta(days=offset)
        actual = slept.get(day)
        if actual is None:
            # No data for this night: skipped, not treated as zero sleep.
            continue
        loss = baseline - actual
        total_seconds += loss * (OURA_DECAY ** offset)

    if total_seconds < 0:
        total_seconds = 0.0

    # Half-up, not Python's banker's rounding: round(2.5) gives 2, which would
    # send a debt of exactly 25 minutes down to 20 instead of up to 30.
    step = OURA_ROUNDING_MINUTES * 60
    rounded = Decimal(total_seconds / step).quantize(Decimal("1"),
                                                     rounding=ROUND_HALF_UP)
    return int(rounded) * step


def calibrate_baseline(
    sessions: List[Dict],
    daily_sleep: List[Dict],
    observed_minutes,
    window_days: int = OURA_WINDOW_DAYS,
    today: Optional[date] = None,
    include_naps: bool = False,
) -> Optional[float]:
    """Solve for the baseline need that reproduces a debt figure you can see.

    Oura shows a sleep debt in its app but publishes neither that number nor
    the sleep need behind it. Given one observed value, this inverts the
    formula to recover the baseline, which can then be pinned in config so the
    app's figure and this one agree.

    `observed_minutes` may be a single figure for today, or a sequence where
    position is days ago — ``[10, 20, 30]`` meaning today, yesterday and the
    day before. Several observations are averaged, which matters because the
    answer is so sensitive that a single day pins the baseline poorly.

    Returns seconds, or None when nothing can be solved — an observed zero, for
    instance, is satisfied by any sufficiently low baseline and so pins nothing.
    """
    today = today or date.today()

    if not isinstance(observed_minutes, (list, tuple)):
        observed_minutes = [observed_minutes]

    observations = {today - timedelta(days=days_ago): observed
                    for days_ago, observed in enumerate(observed_minutes)}
    return calibrate_baseline_for_days(
        sessions, daily_sleep, observations,
        window_days=window_days, include_naps=include_naps)


def calibrate_baseline_for_days(
    sessions: List[Dict],
    daily_sleep: List[Dict],
    observations: Dict[date, float],
    window_days: int = OURA_WINDOW_DAYS,
    include_naps: bool = False,
) -> Optional[float]:
    """Solve for the baseline need, given debt figures on specific days.

    `observations` maps a day to the debt in minutes the Oura app showed on it.
    Unlike the positional form, the days need not be consecutive — the app only
    keeps a fortnight of history on screen, so the figures you can actually read
    off it usually have gaps.

    Each observation is inverted independently and the estimates averaged,
    which trades two errors off against each other. The app rounds to ten
    minutes, so a reading is +/-5 minutes out, worth about 0.6 minutes of
    baseline; averaging shrinks that. But the need itself moves — measured at
    roughly 1.6 minutes of baseline per week — so an old reading pulls the
    answer toward a window that has already passed.

    Drift is the larger term, so **recency beats quantity**: two to four
    readings from the last week or so beat a long history. Fitting three weeks
    of readings together did worse than fitting the most recent two. Note also
    that consecutive days share 13 of their 14 nights, so they are close to the
    same measurement — the second reading helps, the fourth barely.

    Returns seconds, or None when nothing can be solved — an observed zero is
    satisfied by any sufficiently low baseline and so pins nothing.
    """
    slept = _slept_by_day(sessions, daily_sleep, include_naps)

    estimates = []
    for anchor, observed in observations.items():
        if observed is None or observed <= 0:
            continue

        weight_sum = 0.0
        weighted_sleep = 0.0
        for offset in range(max(0, window_days)):
            actual = slept.get(anchor - timedelta(days=offset))
            if actual is None:
                continue
            weight = OURA_DECAY ** offset
            weight_sum += weight
            weighted_sleep += actual * weight

        if weight_sum <= 0:
            continue
        # D = baseline * sum(w) - sum(sleep * w)  ->  solve for baseline.
        estimates.append((observed * 60 + weighted_sleep) / weight_sum)

    return sum(estimates) / len(estimates) if estimates else None


class DurationError(ValueError):
    """A duration that could not be read."""


def parse_duration_minutes(text) -> Optional[float]:
    """Read a duration the way the Oura app prints one, returning minutes.

    Accepts "3:30", "3h30m", "3h 30", "3h", "45m" and a bare "210" (minutes),
    because those are the forms people actually copy off the screen. Returns
    None for blank input; raises DurationError for anything unreadable, so a
    typo is reported rather than silently treated as zero.
    """
    if text is None:
        return None
    cleaned = re.sub(r"\s+", "", str(text)).lower()
    if not cleaned:
        return None

    match = re.fullmatch(r"(\d+)[:h](\d{1,2})m?", cleaned)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        if minutes >= 60:
            raise DurationError(f"'{text}': {minutes} is not a number of minutes.")
        return hours * 60 + minutes

    match = re.fullmatch(r"(\d+(?:\.\d+)?)h", cleaned)
    if match:
        return float(match.group(1)) * 60

    match = re.fullmatch(r"(\d+(?:\.\d+)?)m?", cleaned)
    if match:
        return float(match.group(1))

    raise DurationError(f"'{text}': expected something like 3:30, 3h30m or 210.")


def latest_sleep_balance(readiness: List[Dict]) -> Optional[int]:
    """The most recent sleep_balance contributor, or None if absent."""
    best_day, balance = None, None

    for row in readiness or []:
        day = str(row.get("day") or "")
        value = (row.get("contributors") or {}).get("sleep_balance")
        if not day or value is None:
            continue
        if best_day is None or day > best_day:
            try:
                balance = int(value)
                best_day = day
            except (TypeError, ValueError):
                continue

    return balance


def oura_assessment(
    readiness: List[Dict],
    max_adjustment_minutes: int = 45,
    debt_seconds: Optional[float] = None,
) -> DebtAssessment:
    """Debt from Oura's sleep_balance readiness contributor.

    The score runs to 100 for "in balance", so the shortfall from 100 scales
    the adjustment: a balance of 100 asks for nothing, 50 asks for half the
    configured maximum. The scale is linear and the mapping is this app's, not
    Oura's — see the module docstring.
    """
    balance = latest_sleep_balance(readiness)

    if balance is None:
        return DebtAssessment(
            adjustment_seconds=0.0, source="oura_balance", balance=None,
            debt_seconds=debt_seconds,
            reasons=["⚠️ Oura returned no sleep_balance score; "
                     "no debt adjustment applied."],
        )

    shortfall = max(0, BALANCE_BEST - balance) / BALANCE_BEST
    adjustment = shortfall * max_adjustment_minutes * 60

    if adjustment <= 0:
        reasons = [f"Oura sleep balance {balance}/100 — in balance, "
                   f"no adjustment."]
    else:
        reasons = [f"Oura sleep balance {balance}/100 → {hhmm(adjustment)} "
                   f"earlier ({int(shortfall * 100)}% of the "
                   f"{max_adjustment_minutes}m maximum)."]

    # Oura gives no duration, so the shortfall tally is computed alongside and
    # shown for reference. It is this app's figure, not Oura's, and it does not
    # drive the adjustment when this source is selected.
    if debt_seconds is not None:
        reasons.append(f"Shortfall over the same period: {hhmm(debt_seconds)} "
                       f"(computed here — Oura publishes no duration).")

    return DebtAssessment(adjustment_seconds=adjustment, source="oura_balance",
                          balance=balance, debt_seconds=debt_seconds,
                          reasons=reasons)


def oura_debt_assessment(
    sessions: List[Dict],
    daily_sleep: List[Dict],
    readiness: List[Dict],
    profile: SleepProfile,
    window_days: int = OURA_WINDOW_DAYS,
    recovery_nights: int = 7,
    max_adjustment_minutes: int = 45,
    today: Optional[date] = None,
    baseline_seconds: Optional[float] = None,
    include_naps: bool = False,
) -> DebtAssessment:
    """Debt by Oura's own formula, repaid on this app's schedule.

    The debt is a real duration, so it is spread and capped the same way the
    computed source is. Oura's balance score is carried alongside for context
    but does not drive the adjustment here.
    """
    debt = oura_debt_seconds(sessions, daily_sleep, profile,
                             window_days=window_days, today=today,
                             baseline_seconds=baseline_seconds,
                             include_naps=include_naps)
    balance = latest_sleep_balance(readiness)
    reasons = []
    adjustment = 0.0

    if debt > 0 and recovery_nights > 0:
        adjustment = debt / recovery_nights
        cap = max_adjustment_minutes * 60
        if adjustment > cap:
            adjustment = cap
            reasons.append(f"Oura sleep debt {hhmm(debt)} → capped at "
                           f"{max_adjustment_minutes}m earlier.")
        else:
            reasons.append(f"Oura sleep debt {hhmm(debt)} spread over "
                           f"{recovery_nights} nights → {hhmm(adjustment)} earlier.")
    else:
        reasons.append(f"Oura sleep debt {hhmm(debt)} — no adjustment needed.")

    if baseline_seconds is None:
        # Without a calibrated baseline this measures against the app's own
        # derived need, which is not the number Oura uses and is amplified
        # about ninefold by the decay weights.
        print("⚠️  DEBT_SOURCE is 'oura' but OURA_BASELINE_NEED_HOURS is unset, "
              "so the debt is measured against this app's derived sleep need "
              "and will not match the Oura app. Run "
              "'python3 main.py --calibrate-debt <minutes>' to find it.")

    used = (baseline_seconds if baseline_seconds is not None
            else (profile.sleep_need_seconds if profile is not None else 0.0))
    reasons.append(f"Decay-weighted over {window_days} days against a "
                   f"{hhmm(used)} baseline"
                   + (" (from OURA_BASELINE_NEED_HOURS)" if baseline_seconds
                      is not None else " (your computed sleep need)")
                   + ("; naps included." if include_naps else "; nights only."))
    if balance is not None:
        reasons.append(f"Oura sleep balance: {balance}/100.")

    return DebtAssessment(adjustment_seconds=adjustment, source="oura",
                          debt_seconds=debt, balance=balance, reasons=reasons)


def assess(
    source: str,
    sessions: List[Dict],
    daily_sleep: List[Dict],
    readiness: List[Dict],
    profile: SleepProfile,
    window_days: int = 14,
    recovery_nights: int = 7,
    max_adjustment_minutes: int = 45,
    today: Optional[date] = None,
    baseline_seconds: Optional[float] = None,
    include_naps: bool = False,
) -> DebtAssessment:
    """Assess sleep debt using the configured source.

    An unknown source falls back to "computed" with a warning rather than
    stopping the run, matching how the rest of the app degrades.
    """
    name = str(source or "computed").strip().lower()

    if name == "none":
        return DebtAssessment(adjustment_seconds=0.0, source="none",
                              debt_seconds=None,
                              reasons=["Sleep debt adjustment disabled."])

    if name == "oura":
        return oura_debt_assessment(sessions, daily_sleep, readiness, profile,
                                    window_days=window_days,
                                    recovery_nights=recovery_nights,
                                    max_adjustment_minutes=max_adjustment_minutes,
                                    today=today,
                                    baseline_seconds=baseline_seconds,
                                    include_naps=include_naps)

    if name == "oura_balance":
        # The duration is local arithmetic, so it costs nothing to work out and
        # gives the UI a minutes value to show beside Oura's score.
        tally = sleep_debt_seconds(sessions, daily_sleep, profile,
                                   window_days=window_days, today=today)
        return oura_assessment(readiness, max_adjustment_minutes,
                               debt_seconds=tally)

    if name != "computed":
        print(f"⚠️  Unknown DEBT_SOURCE '{name}' (known: computed, oura, "
              f"oura_balance, none); using 'computed'.")

    return computed_assessment(sessions, daily_sleep, profile, window_days,
                               recovery_nights, max_adjustment_minutes, today)
