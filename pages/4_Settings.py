import importlib
from datetime import date, timedelta
from datetime import time as dtime

import pandas as pd
import streamlit as st

import config
from config_store import (
    SECTIONS,
    SETTINGS,
    ValidationError,
    coerce,
    read_values,
    write_values,
)
from model import debt as debt_model
from utils import load_oura, setting

st.set_page_config(page_title="Settings · go_to_bed", page_icon="🛏️", layout="wide")
st.title("Settings")
st.caption("Edit config.py from here — everything except the Oura token")

CONFIG_PATH = config.__file__
current = read_values(config)


def widget(field, value):
    """Render one setting, returning whatever the user left in it."""
    label = field.label
    help_text = field.help or None
    key = f"set_{field.key}"

    if field.kind == "bool":
        return st.checkbox(label, value=bool(value), help=help_text, key=key)

    if field.kind == "choice":
        options = field.choices or []
        index = options.index(value) if value in options else 0
        return st.selectbox(label, options, index=index, help=help_text, key=key)

    if field.kind == "int":
        return st.number_input(label, value=int(value if value is not None else 0),
                               step=1, help=help_text, key=key,
                               min_value=int(field.minimum) if field.minimum is not None else None,
                               max_value=int(field.maximum) if field.maximum is not None else None)

    if field.kind == "float":
        return st.number_input(label, value=float(value if value is not None else 0.0),
                               step=0.25, help=help_text, key=key,
                               min_value=float(field.minimum) if field.minimum is not None else None,
                               max_value=float(field.maximum) if field.maximum is not None else None)

    if field.kind == "time":
        # A picker for the times that must always have a value.
        try:
            hours, minutes = str(value).split(":")
            initial = dtime(int(hours), int(minutes))
        except (ValueError, AttributeError):
            initial = dtime(6, 30)
        picked = st.time_input(label, value=initial, step=300, help=help_text, key=key)
        return picked.strftime("%H:%M") if picked else ""

    if field.kind == "opt_time":
        # Free text rather than a picker, because "unset" is a real state here
        # and a picker has no way to express it.
        return st.text_input(label, value=(value or ""), placeholder="blank = use Monday–Friday",
                             help=help_text, key=key)

    if field.kind == "lines":
        text = "\n".join(value) if isinstance(value, (list, tuple)) else (value or "")
        return st.text_area(label, value=text, height=110,
                            placeholder=field.placeholder or None,
                            help=help_text, key=key)

    if field.kind == "opt_float":
        return st.text_input(label, value=("" if value is None else str(value)),
                             placeholder=field.placeholder or "blank = unset",
                             help=help_text, key=key)

    if field.kind == "minutes":
        if value is None:
            shown = ""
        elif isinstance(value, (list, tuple)):
            shown = ", ".join(str(item) for item in value)
        else:
            shown = str(value)
        return st.text_input(label, value=shown, placeholder="e.g. 60, 15",
                             help=help_text, key=key)

    return st.text_input(label, value=("" if value is None else str(value)),
                         help=help_text, key=key)


with st.form("settings"):
    tabs = st.tabs(SECTIONS)
    raw_values = {}

    for tab, section in zip(tabs, SECTIONS):
        with tab:
            fields = [f for f in SETTINGS if f.section == section]
            columns = st.columns(2)
            for index, field in enumerate(fields):
                with columns[index % 2]:
                    raw_values[field.key] = widget(field, current.get(field.key))

    st.caption("Saves every tab at once, not just the one you're looking at.")
    submitted = st.form_submit_button("💾 Save to config.py", type="primary")

if submitted:
    updates, errors = {}, []
    for field in SETTINGS:
        try:
            updates[field.key] = coerce(field, raw_values[field.key])
        except ValidationError as error:
            errors.append(str(error))

    if errors:
        for message in errors:
            st.error(message)
    else:
        try:
            changed = write_values(CONFIG_PATH, updates)
        except (ValidationError, OSError) as error:
            st.error(f"Could not write config.py: {error}")
        else:
            if not changed:
                st.info("No changes — config.py already matches these values.")
            else:
                # The running process still holds the old module, and the Oura
                # fetch is cached on HISTORY_DAYS, so both have to be refreshed
                # before the other pages reflect the new settings.
                importlib.reload(config)
                st.cache_data.clear()
                st.success(f"Saved {len(changed)} setting(s): {', '.join(sorted(changed))}")
                st.caption(f"Previous version kept at `{CONFIG_PATH}.bak`.")

st.divider()

col1, col2 = st.columns([2, 1])
with col1:
    st.markdown(
        f"Editing `{CONFIG_PATH}`. Comments and any hand-added settings are "
        f"preserved — only the lines you change are rewritten, and a `.bak` "
        f"copy is taken before each save."
    )
    st.markdown(
        "**`OURA_TOKEN` is not editable here** and is never read into this page. "
        "Edit it in `config.py` directly."
    )
with col2:
    if st.button("🔄 Regenerate calendar now", width="stretch"):
        import main
        importlib.reload(main)
        with st.spinner("Running main.py..."):
            try:
                main.main()
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                st.error(f"Run failed: {error}")
            else:
                st.success(f"Wrote {getattr(config, 'ICAL_OUTPUT_PATH', 'the .ics')}.")

st.divider()

# -- Recalibration --------------------------------------------------------
#
# Oura publishes neither its sleep debt nor the sleep need behind it, so the
# baseline has to be recovered from figures read off the app. It also drifts,
# which is why this lives in the UI rather than staying a one-off CLI chore.

st.subheader("🎯 Recalibrate the Oura baseline")
st.caption(
    "Oura publishes neither its sleep debt nor the sleep need behind it, so "
    "`OURA_BASELINE_NEED_HOURS` is recovered from figures you read off the app "
    "— and refitted when they drift apart again."
)

CALIBRATION_ROWS = 4


def _observation_frame():
    """The blank sheet: the last few days, newest first, ready to fill in."""
    today = date.today()
    return pd.DataFrame({
        "Day": [today - timedelta(days=n) for n in range(CALIBRATION_ROWS)],
        "Sleep debt": [""] * CALIBRATION_ROWS,
    })


if "calibration_rows" not in st.session_state:
    st.session_state["calibration_rows"] = _observation_frame()

left, right = st.columns([3, 2])

with left:
    edited = st.data_editor(
        st.session_state["calibration_rows"],
        key="calibration_editor",
        hide_index=True,
        width="stretch",
        num_rows="dynamic",
        column_config={
            "Day": st.column_config.DateColumn(
                "Day", format="ddd D MMM",
                help="The day the Oura app showed this figure."),
            "Sleep debt": st.column_config.TextColumn(
                "Sleep debt",
                help="As the app writes it: 3:30, 3h30m, or 210 for minutes.",
                width="small"),
        },
    )
    st.caption(
        "Leave a row blank to skip it. Days need not be consecutive, and a "
        "debt of 0 pins nothing — any low enough baseline reproduces it."
    )

with right:
    shown_need = st.text_input(
        "Sleep need shown in the app",
        value="",
        placeholder="e.g. 7:16 — optional",
        help="Written to OURA_SLEEP_NEED_HOURS, which sets the bedtime target "
             "when SLEEP_NEED_SOURCE is 'oura'. It is a separate number from "
             "the debt baseline, though the two often agree.",
    )
    naps = bool(getattr(config, "OURA_DEBT_INCLUDE_NAPS", False))
    st.caption(
        f"Fitting with naps **{'included' if naps else 'excluded'}** and a "
        f"**{getattr(config, 'DEBT_WINDOW_DAYS', 14)}-day** window, matching "
        f"how the debt itself is computed."
    )
    fit = st.button("📐 Fit baseline", width="stretch")


def _rounded_hhmm(seconds):
    """H:MM rounded to the nearest minute, not truncated to it.

    `hhmm` truncates, which is right for a duration but wrong for a label
    sitting next to the decimal hours it came from: 7.2658 hours would read
    7:15 beside a button offering 7.2658, when the honest reading is 7:16.
    """
    minutes = int(round(seconds / 60))
    return "%d:%02d" % (minutes // 60, minutes % 60)


def _observations(frame):
    """Turn the edited sheet into {day: minutes}, reporting what won't parse."""
    observations, problems = {}, []
    for _, row in frame.iterrows():
        day, raw = row.get("Day"), row.get("Sleep debt")
        if day is None or pd.isna(day):
            continue
        try:
            minutes = debt_model.parse_duration_minutes(raw)
        except debt_model.DurationError as error:
            problems.append(str(error))
            continue
        if minutes is None:
            continue
        observations[pd.Timestamp(day).date()] = minutes
    return observations, problems


if fit:
    st.session_state["calibration_rows"] = edited
    observations, problems = _observations(edited)
    for problem in problems:
        st.error(problem)

    try:
        need_minutes = debt_model.parse_duration_minutes(shown_need)
    except debt_model.DurationError as error:
        st.error(str(error))
        need_minutes = None

    # A debt of zero is satisfied by any low enough baseline, so the solver
    # ignores those days. They must be dropped here too, or they would pad
    # "days fitted" and add their whole error to a total they never shaped.
    ignored = sorted(day for day, value in observations.items() if value <= 0)
    observations = {day: value for day, value in observations.items() if value > 0}

    if ignored:
        st.info(
            "Ignored %s showing a debt of 0 — any low enough baseline "
            "reproduces zero, so those days pin nothing."
            % ", ".join(day.strftime("%a %d %b") for day in ignored)
        )

    if not observations and not problems:
        st.warning("Enter at least one day's non-zero sleep debt to fit against.")

    if observations:
        sessions, daily, _ = load_oura(setting("HISTORY_DAYS", 90))
        if not sessions:
            st.error("No sleep data came back from Oura, so there is nothing "
                     "to fit against.")
        else:
            window = setting("DEBT_WINDOW_DAYS", 14)
            found = debt_model.calibrate_baseline_for_days(
                sessions, daily, observations,
                window_days=window, include_naps=naps)

            if found is None:
                st.error("Could not solve for a baseline from those figures. A "
                         "debt of 0 is satisfied by any low baseline, so use a "
                         "day showing a non-zero one.")
            else:
                rows, error_total = [], 0
                for day in sorted(observations, reverse=True):
                    modelled = int(debt_model.oura_debt_seconds(
                        sessions, daily, None, today=day, baseline_seconds=found,
                        window_days=window, include_naps=naps) / 60)
                    observed = int(observations[day])
                    error_total += abs(modelled - observed)
                    rows.append({
                        "Day": day.strftime("%a %d %b"),
                        "This app": f"{modelled // 60}:{modelled % 60:02d}",
                        "Oura app": f"{observed // 60}:{observed % 60:02d}",
                        "Off by": "match" if modelled == observed
                                  else f"{modelled - observed:+} min",
                    })
                st.session_state["calibration_fit"] = {
                    "hours": round(found / 3600, 4),
                    "label": _rounded_hhmm(found),
                    "rows": rows,
                    "error": error_total,
                    "count": len(rows),
                    "need_hours": (round(need_minutes / 60, 4)
                                   if need_minutes else None),
                }

result = st.session_state.get("calibration_fit")
if result:
    st.markdown("#### Fitted baseline")
    metrics = st.columns(3)
    metrics[0].metric("Baseline need", result["label"],
                      help="OURA_BASELINE_NEED_HOURS = %s" % result["hours"])
    metrics[1].metric("Total error", f"{result['error']} min",
                      help="Summed across every day fitted.")
    metrics[2].metric("Days fitted", result["count"])

    st.dataframe(pd.DataFrame(result["rows"]), hide_index=True, width="stretch")
    st.caption(
        "The 14-day weights sum to about 9.1, so the debt moves roughly nine "
        "minutes for every minute of baseline error, and is then rounded to "
        "ten. Landing within a step or two on most days is as close as the "
        "formula gets."
    )

    targets = {"OURA_BASELINE_NEED_HOURS": result["hours"]}
    if result["need_hours"]:
        targets["OURA_SLEEP_NEED_HOURS"] = result["need_hours"]
    summary = ", ".join(f"`{k} = {v}`" for k, v in targets.items())

    if st.button(f"💾 Save {summary}", type="primary", key="save_calibration"):
        try:
            changed = write_values(CONFIG_PATH, targets)
        except (ValidationError, OSError) as error:
            st.error(f"Could not write config.py: {error}")
        else:
            if not changed:
                st.info("No change — config.py already holds these values.")
            else:
                importlib.reload(config)
                st.cache_data.clear()
                st.success(f"Saved {', '.join(sorted(changed))}. "
                           f"Run the calendar again to pick it up.")
