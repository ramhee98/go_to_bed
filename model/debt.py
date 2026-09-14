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

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from .bedtime import hhmm, sleep_debt_seconds
from .sleep_need import SleepProfile

# Oura reports sleep_balance on a 1-100 scale where 100 means "in balance".
BALANCE_BEST = 100


@dataclass
class DebtAssessment:
    """How much earlier to go to bed, and why."""

    adjustment_seconds: float
    source: str                          # "computed" | "oura" | "none"
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
            adjustment_seconds=0.0, source="oura", balance=None,
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

    return DebtAssessment(adjustment_seconds=adjustment, source="oura",
                          balance=balance, debt_seconds=debt_seconds,
                          reasons=reasons)


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
        # The duration is local arithmetic, so it costs nothing to work out and
        # gives the UI a minutes value to show beside Oura's score.
        tally = sleep_debt_seconds(sessions, daily_sleep, profile,
                                   window_days=window_days, today=today)
        return oura_assessment(readiness, max_adjustment_minutes,
                               debt_seconds=tally)

    if name != "computed":
        print(f"⚠️  Unknown DEBT_SOURCE '{name}' (known: computed, oura, none); "
              f"using 'computed'.")

    return computed_assessment(sessions, daily_sleep, profile, window_days,
                               recovery_nights, max_adjustment_minutes, today)
