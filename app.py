from datetime import datetime

import streamlit as st

from model.bedtime import hhmm
from utils import require_state, setting, sidebar

st.set_page_config(page_title="go_to_bed", page_icon="🛏️", layout="wide")
st.title("🛏️ go_to_bed")
st.caption("When to go to bed and when to get up, from your own Oura history")

state = require_state()
sidebar(state)

profile = state["profile"]
plans = state["plans"]

if not plans:
    st.warning("No nights planned. Check `DAYS_AHEAD` and `WAKE_SOURCE` in `config.py`.")
    st.stop()

tonight = plans[0]

# --- Tonight ----------------------------------------------------------------
# A single headline is a stat tile, not a chart.

st.subheader("Tonight")

now = datetime.now(tonight.bedtime.tzinfo)
until_bed = (tonight.bedtime - now).total_seconds()

col1, col2, col3, col4 = st.columns(4)
col1.metric(
    "Go to bed",
    tonight.bedtime.strftime("%H:%M"),
    delta=(f"in {hhmm(until_bed)}" if until_bed > 0 else f"{hhmm(-until_bed)} ago"),
    delta_color="off",
)
col2.metric(
    "Wake up",
    tonight.wake_time.strftime("%H:%M"),
    delta=tonight.wake_time.strftime("%A"),
    delta_color="off",
)
col3.metric("Time in bed", hhmm(tonight.actual_time_in_bed_seconds))
debt = state["debt"]
col4.metric("Sleep debt", debt.display, delta=debt.caption, delta_color="off")

if until_bed > 0:
    st.success(
        f"**{hhmm(until_bed)}** until bedtime — "
        f"lights out at **{tonight.bedtime.strftime('%H:%M')}**, "
        f"up at **{tonight.wake_time.strftime('%H:%M')}**."
    )
else:
    st.warning(
        f"Bedtime was **{tonight.bedtime.strftime('%H:%M')}**, "
        f"{hhmm(-until_bed)} ago. Go to bed."
    )

# --- Why --------------------------------------------------------------------

st.subheader("Why this time")

st.markdown(f"- {state['wake_schedule'].describe(tonight.day)}")
for reason in tonight.reasons:
    st.markdown(f"- {reason}")
for note in profile.notes:
    st.markdown(f"- ⚠️ {note}")

with st.expander("What the model does"):
    st.markdown(
        f"""
Oura's own `sleep_time` endpoint returns `optimal_bedtime: null` for most days,
so the recommendation is derived from your history instead:

1. **Sleep need** — the median time you actually slept on nights scoring
   {setting("GOOD_SLEEP_SCORE", 80)} or better:
   **{hhmm(profile.sleep_need_seconds)}**.
2. **Time in bed** — sleep need divided by your median efficiency
   ({profile.efficiency * 100:.0f}%), which folds in the
   {hhmm(profile.latency_seconds)} you typically take to fall asleep and the
   time you spend awake mid-night: **{hhmm(profile.time_in_bed_seconds)}**.
3. **Sleep debt** — shortfalls over the last
   {setting("DEBT_WINDOW_DAYS", 14)} days, repaid across
   {setting("DEBT_RECOVERY_NIGHTS", 7)} nights and capped at
   {setting("MAX_DEBT_ADJUSTMENT_MINUTES", 45)} minutes so recovery never
   demands one brutal early night.
4. **Guard rails** — the result is held between
   {setting("EARLIEST_BEDTIME", "—")} and {setting("LATEST_BEDTIME", "—")}.

Run `python3 main.py` to write these plans to
`{setting("ICAL_OUTPUT_PATH", "./bedtime.ics")}`.
"""
    )
