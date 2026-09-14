import altair as alt
import pandas as pd
import streamlit as st

from model.bedtime import hhmm
from utils import require_state, setting, sidebar, style_axes, theme

st.set_page_config(page_title="Schedule · go_to_bed", page_icon="🛏️", layout="wide")
st.title("Schedule")
st.caption("The nights ahead, and the .ics that carries them to your phone")

state = require_state()
sidebar(state)

tokens = theme()
plans = state["plans"]

if not plans:
    st.info("No nights planned.")
    st.stop()

# Hours since the previous noon, so an evening bedtime and a morning wake time
# sit on one continuous axis instead of wrapping around midnight.
def hours_from_noon(stamp, day):
    anchor = stamp.replace(hour=12, minute=0, second=0, microsecond=0)
    if stamp.hour < 12:
        anchor -= pd.Timedelta(days=1)
    return (stamp - anchor).total_seconds() / 3600 + 12


rows = []
for plan in plans:
    rows.append({
        "Day": plan.day.strftime("%a %d %b"),
        "Weekday": plan.day.strftime("%A"),
        "Bed": plan.bedtime.strftime("%H:%M"),
        "Wake": plan.wake_time.strftime("%H:%M"),
        "In bed": hhmm(plan.actual_time_in_bed_seconds),
        "start": hours_from_noon(plan.bedtime, plan.day),
        "end": hours_from_noon(plan.wake_time, plan.day),
        "Source": state["wake_schedule"].describe(plan.day),
    })

df = pd.DataFrame(rows)

# Fit the axis to the plan rather than to a fixed window, so the bars fill the
# plot whatever the configured wake times are. Whole hours keep ticks readable.
axis_start = int(df["start"].min()) - 1
axis_end = int(df["end"].max()) + 2

st.subheader("Next nights")
st.caption("Each bar runs from lights-out to alarm. Hover for the detail.")

# One series → no legend; the title names it.
bars = (
    alt.Chart(df)
    .mark_bar(height=14, cornerRadius=4, color=tokens["series_1"],
              stroke=tokens["surface"], strokeWidth=2)
    .encode(
        x=alt.X("start:Q", title="Time of day",
                scale=alt.Scale(domain=[axis_start, axis_end], nice=False),
                axis=alt.Axis(
                    values=list(range(axis_start, axis_end + 1)),
                    labelExpr="datum.value % 24 + ':00'",
                )),
        x2="end:Q",
        y=alt.Y("Day:N", title=None, sort=list(df["Day"]),
                axis=alt.Axis(labelFontSize=12)),
        tooltip=[
            alt.Tooltip("Day:N"),
            alt.Tooltip("Bed:N", title="Go to bed"),
            alt.Tooltip("Wake:N", title="Wake up"),
            alt.Tooltip("In bed:N", title="Time in bed"),
            alt.Tooltip("Source:N", title="Wake time from"),
        ],
    )
    .properties(height=max(240, 28 * len(df)))
)

labels = (
    alt.Chart(df)
    .mark_text(align="left", dx=8, color=tokens["surface"], fontSize=11,
               fontWeight="bold")
    .encode(x="start:Q", y=alt.Y("Day:N", sort=list(df["Day"])), text="Bed:N")
)

midnight = (
    alt.Chart(pd.DataFrame({"t": [24]}))
    .mark_rule(color=tokens["axis"], strokeWidth=1, strokeDash=[3, 3])
    .encode(x="t:Q")
)

st.altair_chart(style_axes(bars + midnight + labels, tokens),
                use_container_width=True, theme=None)

st.dataframe(
    df[["Day", "Bed", "Wake", "In bed", "Source"]],
    use_container_width=True,
    hide_index=True,
)

st.subheader("Calendar file")
ics_path = setting("ICAL_OUTPUT_PATH", "./bedtime.ics")
st.markdown(
    f"Run `python3 main.py` to write these plans to `{ics_path}`, then subscribe "
    f"to that file from your calendar client so the reminder reaches your phone."
)

try:
    with open(ics_path, "rb") as f:
        st.download_button(
            "⬇️ Download bedtime.ics",
            data=f.read(),
            file_name="bedtime.ics",
            mime="text/calendar",
        )
except FileNotFoundError:
    st.info(f"No `{ics_path}` yet — run `python3 main.py` to generate it.")
