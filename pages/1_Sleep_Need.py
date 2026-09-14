import altair as alt
import pandas as pd
import streamlit as st

from model.bedtime import hhmm
from utils import require_state, setting, sidebar, style_axes, theme

st.set_page_config(page_title="Sleep Need · go_to_bed", page_icon="🛏️", layout="wide")
st.title("Sleep need")
st.caption("How much sleep you personally need, and the nights that prove it")

state = require_state()
sidebar(state)

tokens = theme()
profile = state["profile"]
good_score = setting("GOOD_SLEEP_SCORE", 80)

rows = [
    {
        "Night": n["day"],
        "Total sleep": n["total_sleep_seconds"] / 3600,
        "Score": n["score"],
        "Efficiency": (n["efficiency"] or 0) * 100,
        "Quality": "Good night" if (n["score"] or 0) >= good_score else "Other night",
        "Slept": hhmm(n["total_sleep_seconds"]),
    }
    for n in state["nights"]
    if n["score"] is not None
]

if not rows:
    st.info("No scored nights in the history window yet.")
    st.stop()

df = pd.DataFrame(rows)

col1, col2, col3 = st.columns(3)
col1.metric("Sleep need", hhmm(profile.sleep_need_seconds))
col2.metric("Median efficiency", f"{profile.efficiency * 100:.0f}%")
col3.metric("Time in bed needed", hhmm(profile.time_in_bed_seconds))

st.subheader(f"Sleep score against time asleep")
st.caption(
    f"Your sleep need is the median time asleep on nights scoring {good_score}+ "
    f"— the vertical line. Nights below that bar are shown for context."
)

# Two categories → legend always present; identity is never color alone, so the
# table view below carries the same split.
color = alt.Color(
    "Quality:N",
    title=None,
    scale=alt.Scale(
        domain=["Good night", "Other night"],
        range=[tokens["series_1"], tokens["series_2"]],
    ),
    legend=alt.Legend(orient="top", symbolSize=120),
)

points = (
    alt.Chart(df)
    .mark_circle(size=110, opacity=0.85, stroke=tokens["surface"], strokeWidth=2)
    .encode(
        x=alt.X("Total sleep:Q", title="Time asleep (hours)",
                scale=alt.Scale(zero=False, nice=True),
                axis=alt.Axis(tickCount=8)),
        y=alt.Y("Score:Q", title="Oura sleep score",
                scale=alt.Scale(zero=False, nice=True),
                axis=alt.Axis(tickCount=6)),
        color=color,
        tooltip=[
            alt.Tooltip("Night:N"),
            alt.Tooltip("Slept:N", title="Time asleep"),
            alt.Tooltip("Score:Q"),
            alt.Tooltip("Efficiency:Q", format=".0f", title="Efficiency %"),
        ],
    )
)

need_rule = (
    alt.Chart(pd.DataFrame({"need": [profile.sleep_need_seconds / 3600]}))
    .mark_rule(color=tokens["text"], strokeWidth=2, strokeDash=[4, 3])
    .encode(x="need:Q")
)

need_label = (
    alt.Chart(pd.DataFrame({
        "need": [profile.sleep_need_seconds / 3600],
        "label": [f"Sleep need {hhmm(profile.sleep_need_seconds)}"],
    }))
    .mark_text(align="left", dx=6, dy=-6, baseline="top",
               color=tokens["text"], fontSize=11)
    .encode(x="need:Q", y=alt.value(6), text="label:N")
)

score_rule = (
    alt.Chart(pd.DataFrame({"bar": [good_score]}))
    .mark_rule(color=tokens["axis"], strokeWidth=1)
    .encode(y="bar:Q")
)

chart = (points + score_rule + need_rule + need_label).properties(height=380)
st.altair_chart(style_axes(chart, tokens), use_container_width=True, theme=None)

if profile.source == "fallback":
    st.warning(
        f"Not enough good nights yet, so the configured fallback of "
        f"{setting('FALLBACK_SLEEP_NEED_HOURS', 8.0)}h is in use. "
        f"The estimate becomes personal once "
        f"{setting('MIN_GOOD_NIGHTS', 5)} nights clear the bar."
    )
for note in profile.notes:
    st.info(note)

with st.expander("Table view"):
    st.dataframe(
        df[["Night", "Slept", "Score", "Efficiency", "Quality"]]
        .sort_values("Night", ascending=False),
        width="stretch",
        hide_index=True,
    )
