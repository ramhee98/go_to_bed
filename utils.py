"""Shared helpers for the Streamlit pages: data loading, theming, formatting.

Every page loads data through here so the UI and `main.py` always agree on the
numbers — a page that recomputed the profile its own way would eventually
disagree with the calendar, which is the one thing that must not happen.
"""

import streamlit as st

import config
from ical.generator import resolve_timezone
from model.bedtime import hhmm, plan_nights, sleep_debt_seconds
from model.sleep_need import build_profile, collect_nights
from oura_api.client import fetch_daily_sleep, fetch_sleep_data
from wake import build_wake_schedule

# Chart colors, taken unchanged from the validated reference palette.
# Light/dark are selected steps of the same hues, not an automatic flip.
PALETTE = {
    "light": {
        "surface": "#fcfcfb",
        "text": "#0b0b0b",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series_1": "#2a78d6",   # categorical slot 1 — blue
        "series_2": "#eb6834",   # categorical slot 2 — orange
        "diverging_low": "#d03b3b",
        "diverging_high": "#2a78d6",
    },
    "dark": {
        "surface": "#1a1a19",
        "text": "#ffffff",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series_1": "#3987e5",
        "series_2": "#d95926",
        "diverging_low": "#e66767",
        "diverging_high": "#3987e5",
    },
}


def setting(name, default):
    """Read a config value, tolerating older config.py files without it."""
    return getattr(config, name, default)


def theme() -> dict:
    """Chart tokens for the viewer's current Streamlit theme."""
    base = "light"
    try:
        base = st.context.theme.type or "light"
    except Exception:
        try:
            base = st.get_option("theme.base") or "light"
        except Exception:
            base = "light"
    return PALETTE.get(str(base).lower(), PALETTE["light"])


def style_axes(chart, tokens: dict):
    """Apply recessive grid and axis chrome to an Altair chart."""
    return (
        chart
        .configure_view(strokeWidth=0, fill=tokens["surface"])
        .configure_axis(
            grid=True,
            gridColor=tokens["grid"],
            gridWidth=1,
            domainColor=tokens["axis"],
            tickColor=tokens["axis"],
            labelColor=tokens["muted"],
            titleColor=tokens["muted"],
            labelFontSize=11,
            titleFontSize=11,
            titleFontWeight="normal",
        )
        .configure_legend(
            labelColor=tokens["text"],
            titleColor=tokens["muted"],
            labelFontSize=11,
            titleFontSize=11,
        )
    )


@st.cache_data(ttl=3600, show_spinner="Fetching your Oura history...")
def load_oura(history_days: int):
    """Fetch sleep sessions and daily scores, cached for an hour."""
    token = setting("OURA_TOKEN", "")
    sessions = fetch_sleep_data(token, days_back=history_days)
    daily = fetch_daily_sleep(token, days_back=history_days)
    return sessions, daily


def load_state(history_days=None):
    """Load everything a page needs: raw data, profile, debt, plans.

    This mirrors `main.py` step for step, so what a page shows is exactly what
    the next `python3 main.py` run will write into the calendar.
    """
    history_days = history_days or setting("HISTORY_DAYS", 90)
    sessions, daily = load_oura(history_days)

    if not sessions:
        return None

    profile = build_profile(
        sessions,
        daily,
        good_sleep_score=setting("GOOD_SLEEP_SCORE", 80),
        min_good_nights=setting("MIN_GOOD_NIGHTS", 5),
        fallback_sleep_need_hours=setting("FALLBACK_SLEEP_NEED_HOURS", 8.0),
    )

    debt = sleep_debt_seconds(
        sessions, daily, profile,
        window_days=setting("DEBT_WINDOW_DAYS", 14),
    )

    tz = resolve_timezone(setting("TIMEZONE", None))
    wake_schedule = build_wake_schedule(config, tz)

    plans = plan_nights(
        wake_schedule,
        profile,
        days_ahead=setting("DAYS_AHEAD", 14),
        debt_seconds=debt,
        debt_recovery_nights=setting("DEBT_RECOVERY_NIGHTS", 7),
        max_debt_adjustment_minutes=setting("MAX_DEBT_ADJUSTMENT_MINUTES", 45),
        earliest_bedtime=setting("EARLIEST_BEDTIME", None),
        latest_bedtime=setting("LATEST_BEDTIME", None),
    )

    return {
        "sessions": sessions,
        "daily": daily,
        "nights": collect_nights(sessions, daily),
        "profile": profile,
        "debt": debt,
        "plans": plans,
        "wake_schedule": wake_schedule,
    }


def require_state():
    """Load state or stop the page with a readable message."""
    state = load_state()
    if state is None:
        st.error(
            "No sleep data came back from Oura. Check `OURA_TOKEN` in "
            "`config.py`, then use **Refresh data** in the sidebar."
        )
        st.stop()
    return state


def sidebar(state):
    """The controls shared by every page."""
    with st.sidebar:
        st.markdown("### go_to_bed")
        if st.button("🔄 Refresh data", use_container_width=True):
            load_oura.clear()
            st.rerun()

        profile = state["profile"]
        st.caption(
            f"Sleep need **{hhmm(profile.sleep_need_seconds)}** · "
            f"efficiency **{profile.efficiency * 100:.0f}%**"
        )
        st.caption(
            f"Learned from {profile.good_nights} good nights "
            f"of {profile.nights_analyzed} analysed."
        )
        if profile.source == "fallback":
            st.warning("Using the configured fallback sleep need.")
