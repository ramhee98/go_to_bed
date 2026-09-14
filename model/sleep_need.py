"""Learn how much sleep *you* individually need from your own Oura history.

Oura's own `sleep_time` endpoint returns `optimal_bedtime: null` for most days
(status "only_recommended_found"), so the recommendation is derived here from
the nights you actually slept well instead.
"""

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Dict, List, Optional

# Only Oura's main overnight sleep counts towards a sleep-need estimate; naps
# ("sleep", "late_nap", "rest", ...) would drag the median down. Whitelisting
# the single night type means an unrecognised secondary-sleep type is never
# mistaken for a night.
NIGHT_TYPE = "long_sleep"


@dataclass
class SleepProfile:
    """Your personal sleep parameters, learned from history."""

    sleep_need_seconds: float
    efficiency: float           # 0..1, median over good nights
    latency_seconds: float      # median time from lights-out to asleep
    nights_analyzed: int
    good_nights: int
    source: str                 # "personal" or "fallback"
    notes: List[str] = field(default_factory=list)

    @property
    def time_in_bed_seconds(self) -> float:
        """How long you need to be *in bed* to get `sleep_need_seconds` asleep.

        Dividing by efficiency folds latency and mid-night wakefulness into one
        number, which is what actually sets the bedtime.
        """
        eff = self.efficiency if 0.5 <= self.efficiency <= 1.0 else 0.9
        return self.sleep_need_seconds / eff


def is_night(session: Dict) -> bool:
    """True if an Oura session is the main overnight sleep rather than a nap."""
    normalized = str(session.get("type") or "").strip().lower() or NIGHT_TYPE
    return normalized == NIGHT_TYPE


def normalize_efficiency(raw) -> Optional[float]:
    """Oura reports efficiency as 0..100; return it as a 0..1 fraction."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1.0:
        value = value / 100.0
    return value if 0.0 < value <= 1.0 else None


def score_by_day(daily_sleep: List[Dict]) -> Dict[str, int]:
    """Index the daily sleep score by its `day` string."""
    scores = {}
    for row in daily_sleep or []:
        day = row.get("day")
        score = row.get("score")
        if day and score is not None:
            try:
                scores[str(day)] = int(score)
            except (TypeError, ValueError):
                continue
    return scores


def collect_nights(sessions: List[Dict], daily_sleep: List[Dict]) -> List[Dict]:
    """Flatten Oura sessions into the night records the model works on.

    Each record carries the fields the rest of the model needs and nothing
    else, so the Streamlit pages and the CLI see exactly the same view.
    """
    scores = score_by_day(daily_sleep)
    nights = []

    for session in sessions or []:
        if not is_night(session):
            continue

        total_sleep = session.get("total_sleep_duration")
        if not total_sleep:
            continue

        day = str(session.get("day") or "")
        bedtime_start = session.get("bedtime_start")
        bedtime_end = session.get("bedtime_end")

        nights.append({
            "day": day,
            "id": session.get("id"),
            "total_sleep_seconds": float(total_sleep),
            "time_in_bed_seconds": float(session.get("time_in_bed") or 0) or None,
            "efficiency": normalize_efficiency(session.get("efficiency")),
            "latency_seconds": (float(session["latency"])
                                if session.get("latency") is not None else None),
            "awake_seconds": (float(session["awake_time"])
                              if session.get("awake_time") is not None else None),
            "score": scores.get(day),
            "bedtime_start": _parse_iso(bedtime_start),
            "bedtime_end": _parse_iso(bedtime_end),
        })

    nights.sort(key=lambda n: n["day"])
    return nights


def _parse_iso(value) -> Optional[datetime]:
    """Parse an Oura ISO timestamp, tolerating a trailing 'Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _median_of(nights: List[Dict], key: str) -> Optional[float]:
    values = [n[key] for n in nights if n.get(key) is not None]
    return median(values) if values else None


def build_profile(
    sessions: List[Dict],
    daily_sleep: List[Dict],
    good_sleep_score: int = 80,
    min_good_nights: int = 5,
    fallback_sleep_need_hours: float = 8.0,
) -> SleepProfile:
    """Build a personal sleep profile from Oura history.

    Your sleep need is the median time you actually slept on nights that scored
    at least `good_sleep_score`. If too few nights clear that bar, the bar is
    lowered to the top quartile of the scores you do have, and only if that
    still isn't enough does it fall back to the configured default.
    """
    nights = collect_nights(sessions, daily_sleep)
    notes = []

    scored = [n for n in nights if n.get("score") is not None]
    good = [n for n in scored if n["score"] >= good_sleep_score]

    if len(good) < min_good_nights and len(scored) >= min_good_nights:
        # Not enough nights cleared the configured bar. Rather than give up on
        # a personal estimate, use this person's own best nights: the top
        # quartile of the scores actually recorded.
        cutoff = sorted(n["score"] for n in scored)[int(len(scored) * 0.75)]
        relaxed = [n for n in scored if n["score"] >= cutoff]
        if len(relaxed) >= min_good_nights:
            good = relaxed
            notes.append(
                f"Fewer than {min_good_nights} nights scored {good_sleep_score}+, "
                f"so your best nights (score {cutoff}+) were used instead."
            )

    if len(good) >= min_good_nights:
        sleep_need = _median_of(good, "total_sleep_seconds")
        efficiency = _median_of(good, "efficiency") or _median_of(nights, "efficiency") or 0.90
        latency = _median_of(good, "latency_seconds")
        if latency is None:
            latency = _median_of(nights, "latency_seconds") or 0.0
        return SleepProfile(
            sleep_need_seconds=sleep_need,
            efficiency=efficiency,
            latency_seconds=latency,
            nights_analyzed=len(nights),
            good_nights=len(good),
            source="personal",
            notes=notes,
        )

    notes.append(
        f"Only {len(good)} good night(s) in {len(nights)} analysed — "
        f"need {min_good_nights}. Using the configured fallback until more "
        f"history accumulates."
    )
    return SleepProfile(
        sleep_need_seconds=fallback_sleep_need_hours * 3600.0,
        efficiency=_median_of(nights, "efficiency") or 0.90,
        latency_seconds=_median_of(nights, "latency_seconds") or 0.0,
        nights_analyzed=len(nights),
        good_nights=len(good),
        source="fallback",
        notes=notes,
    )
