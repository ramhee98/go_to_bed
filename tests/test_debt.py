"""Tests for the two sleep-debt sources.

The Oura source deserves care: Oura publishes a 0-100 score, not hours, so the
mapping from score to minutes is ours and must stay predictable.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.debt import (
    DebtAssessment,
    assess,
    computed_assessment,
    latest_sleep_balance,
    oura_assessment,
)
from model.sleep_need import SleepProfile


def _profile(hours=8.0):
    return SleepProfile(sleep_need_seconds=hours * 3600, efficiency=1.0,
                        latency_seconds=0, nights_analyzed=30, good_nights=10,
                        source="personal")


def _night(day, hours):
    return {"id": day, "day": day, "type": "long_sleep",
            "total_sleep_duration": hours * 3600, "time_in_bed": hours * 3600,
            "efficiency": 95, "latency": 600, "awake_time": 300,
            "bedtime_start": f"{day}T23:00:00+02:00",
            "bedtime_end": f"{day}T07:00:00+02:00"}


def _readiness(day, balance):
    return {"day": day, "contributors": {"sleep_balance": balance}}


# -- computed ---------------------------------------------------------------

def test_computed_sums_shortfalls_and_spreads_them():
    sessions = [_night("2026-09-10", 6.0), _night("2026-09-11", 6.0)]
    daily = [{"day": n["day"], "score": 85} for n in sessions]
    result = computed_assessment(sessions, daily, _profile(8.0), window_days=14,
                                 recovery_nights=4, max_adjustment_minutes=60,
                                 today=date(2026, 9, 14))
    assert result.debt_seconds == 4 * 3600          # two nights, two hours each
    assert result.adjustment_seconds == 3600        # 4h over 4 nights
    assert result.source == "computed"


def test_computed_respects_the_cap():
    sessions = [_night("2026-09-1%d" % i, 2.0) for i in range(4)]
    daily = [{"day": n["day"], "score": 85} for n in sessions]
    result = computed_assessment(sessions, daily, _profile(8.0), window_days=14,
                                 recovery_nights=7, max_adjustment_minutes=45,
                                 today=date(2026, 9, 14))
    assert result.adjustment_seconds == 45 * 60
    assert "capped" in " ".join(result.reasons)


# -- oura -------------------------------------------------------------------

def test_oura_uses_the_most_recent_balance():
    readiness = [_readiness("2026-09-10", 40), _readiness("2026-09-14", 86),
                 _readiness("2026-09-12", 60)]
    assert latest_sleep_balance(readiness) == 86


def test_oura_maps_the_balance_onto_the_maximum():
    # 100 asks for nothing; the shortfall from 100 scales the adjustment.
    assert oura_assessment([_readiness("2026-09-14", 100)], 60).adjustment_seconds == 0
    assert oura_assessment([_readiness("2026-09-14", 50)], 60).adjustment_seconds == 30 * 60
    assert oura_assessment([_readiness("2026-09-14", 0)], 60).adjustment_seconds == 60 * 60
    # The real value from the account this was built against.
    assert round(oura_assessment([_readiness("2026-09-14", 86)], 60)
                 .adjustment_seconds) == round(0.14 * 60 * 60)


def test_oura_reports_a_score_not_a_duration():
    result = oura_assessment([_readiness("2026-09-14", 86)], 60)
    assert result.balance == 86
    assert result.debt_seconds is None       # Oura publishes no hours owed
    assert result.display == "86/100"


def test_oura_source_also_reports_a_duration():
    # Oura gives no hours, so the shortfall is computed alongside and shown
    # for reference. It must not change the adjustment, which comes from the
    # score.
    sessions = [_night("2026-09-10", 6.0), _night("2026-09-11", 6.0)]
    daily = [{"day": n["day"], "score": 85} for n in sessions]
    result = assess("oura", sessions, daily, [_readiness("2026-09-14", 50)],
                    _profile(8.0), window_days=14, max_adjustment_minutes=60,
                    today=date(2026, 9, 14))
    assert result.balance == 50
    assert result.debt_seconds == 4 * 3600          # the computed tally
    assert result.adjustment_seconds == 30 * 60     # still from the score
    assert result.display == "4:00"                 # minutes are visible
    assert "balance 50/100" in result.caption
    assert any("Oura publishes no duration" in r for r in result.reasons)


def test_captions_name_the_source():
    assert "disabled" in assess("none", [], [], [], _profile()).caption
    computed = computed_assessment([], [], _profile(), today=date(2026, 9, 14))
    assert "bedtime" in computed.caption


def test_oura_without_a_balance_degrades_to_no_adjustment():
    for readiness in ([], [{"day": "2026-09-14", "contributors": {}}],
                      [{"day": "2026-09-14"}]):
        result = oura_assessment(readiness, 60)
        assert result.adjustment_seconds == 0
        assert result.balance is None


def test_oura_ignores_a_malformed_balance():
    assert latest_sleep_balance([_readiness("2026-09-14", "nonsense")]) is None


# -- dispatch ---------------------------------------------------------------

def test_none_disables_the_adjustment():
    result = assess("none", [], [], [], _profile())
    assert result.adjustment_seconds == 0
    assert result.source == "none"


def test_unknown_source_falls_back_to_computed():
    result = assess("nonsense", [], [], [], _profile())
    assert result.source == "computed"


def test_dispatch_selects_oura():
    result = assess("oura", [], [], [_readiness("2026-09-14", 50)], _profile(),
                    max_adjustment_minutes=60)
    assert result.source == "oura"
    assert result.adjustment_seconds == 30 * 60


def test_display_falls_back_gracefully():
    assert DebtAssessment(0.0, "none").display == "0:00"
    assert DebtAssessment(0.0, "computed", debt_seconds=3600).display == "1:00"


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
