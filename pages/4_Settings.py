import importlib
from datetime import time as dtime

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
                            placeholder="https://example.com/calendar.ics",
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
