import altair as alt
import pandas as pd
import streamlit as st

from model.bedtime import hhmm
from utils import require_state, setting, sidebar, style_axes, theme

st.set_page_config(page_title="History · go_to_bed", page_icon="🛏️", layout="wide")
st.title("History")
st.caption("How each night landed against your sleep need")

state = require_state()
sidebar(state)

tokens = theme()
profile = state["profile"]
need_hours = profile.sleep_need_seconds / 3600

rows = []
for night in state["nights"]:
    slept = night["total_sleep_seconds"] / 3600
    rows.append({
        "Night": night["day"],
        "Slept": slept,
        "Delta": slept - need_hours,
        "Score": night["score"],
        "Slept label": hhmm(night["total_sleep_seconds"]),
        "Delta label": ("+" if slept >= need_hours else "−")
                       + hhmm(abs(slept - need_hours) * 3600),
    })

if not rows:
    st.info("No nights in the history window yet.")
    st.stop()

df = pd.DataFrame(rows).sort_values("Night")
df["Night"] = pd.to_datetime(df["Night"])

window = setting("DEBT_WINDOW_DAYS", 14)
short_nights = int((df.tail(window)["Delta"] < 0).sum())

CHOICES = {"Last 14 nights": 14, "Last 30 nights": 30,
           "Last 90 nights": 90, "Everything": None}
choice = st.radio("Window", list(CHOICES), index=1, horizontal=True,
                  label_visibility="collapsed")
span = CHOICES[choice]
view = df if span is None else df.tail(span)

col1, col2, col3 = st.columns(3)
col1.metric("Sleep debt", hhmm(state["debt"]), delta=f"last {window} days",
            delta_color="off")
col2.metric("Short nights", f"{short_nights} of {min(window, len(df))}")
col3.metric("Median night", hhmm(df["Slept"].median() * 3600))

st.subheader("Nightly shortfall and surplus")
st.caption(
    f"Distance from your {hhmm(profile.sleep_need_seconds)} sleep need. "
    f"Only shortfalls accumulate as debt — a long weekend night does not repay "
    f"a short Tuesday."
)

# Polarity around zero → diverging pair with a neutral zero baseline.
dense = len(view) > 40
deltas = (
    alt.Chart(view)
    .mark_bar(
        cornerRadius=0 if dense else 4,
        stroke=None if dense else tokens["surface"],
        strokeWidth=0 if dense else 2,
    )
    .encode(
        x=alt.X("Night:T", title=None, axis=alt.Axis(format="%d %b")),
        y=alt.Y("Delta:Q", title="Hours vs sleep need"),
        color=alt.condition(
            alt.datum.Delta >= 0,
            alt.value(tokens["diverging_high"]),
            alt.value(tokens["diverging_low"]),
        ),
        tooltip=[
            alt.Tooltip("Night:T", format="%a %d %b"),
            alt.Tooltip("Slept label:N", title="Time asleep"),
            alt.Tooltip("Delta label:N", title="vs need"),
            alt.Tooltip("Score:Q"),
        ],
    )
    .properties(height=300)
)

zero = (
    alt.Chart(pd.DataFrame({"y": [0]}))
    .mark_rule(color=tokens["axis"], strokeWidth=1)
    .encode(y="y:Q")
)

st.altair_chart(style_axes(deltas + zero, tokens),
                use_container_width=True, theme=None)
st.markdown(
    f"<span style='color:{tokens['diverging_high']}'>■</span> Met or beat your "
    f"sleep need &nbsp;&nbsp; "
    f"<span style='color:{tokens['diverging_low']}'>■</span> Fell short",
    unsafe_allow_html=True,
)

st.subheader("Time asleep over the window")

line = (
    alt.Chart(view)
    .mark_line(
        strokeWidth=2,
        color=tokens["series_1"],
        # Markers on every one of 80 points is noise; drop them when dense.
        point=(False if dense else
               alt.OverlayMarkDef(size=60, filled=True,
                                  stroke=tokens["surface"], strokeWidth=2)),
    )
    .encode(
        x=alt.X("Night:T", title=None, axis=alt.Axis(format="%d %b")),
        y=alt.Y("Slept:Q", title="Hours asleep", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("Night:T", format="%a %d %b"),
            alt.Tooltip("Slept label:N", title="Time asleep"),
            alt.Tooltip("Score:Q"),
        ],
    )
    .properties(height=280)
)

need_rule = (
    alt.Chart(pd.DataFrame({"need": [need_hours]}))
    .mark_rule(color=tokens["text"], strokeWidth=2, strokeDash=[4, 3])
    .encode(y="need:Q")
)

st.altair_chart(style_axes(line + need_rule, tokens),
                use_container_width=True, theme=None)
st.caption(f"Dashed line: your sleep need, {hhmm(profile.sleep_need_seconds)}.")

with st.expander("Table view"):
    table = view.copy()
    table["Night"] = table["Night"].dt.strftime("%a %d %b %Y")
    st.dataframe(
        table[["Night", "Slept label", "Delta label", "Score"]]
        .rename(columns={"Slept label": "Time asleep", "Delta label": "vs need"})
        .iloc[::-1],
        use_container_width=True,
        hide_index=True,
    )
