"""Read and write config.py from the Settings page.

Two rules shape this module:

* **OURA_TOKEN is never read, shown or written.** It is the one secret in the
  file, and a settings form has no business round-tripping it.
* **Values are re-serialised, never pasted.** config.py is imported as Python,
  so writing raw form input into it would be an execution hole. Every value
  goes through a typed serialiser, and anything that fails validation is
  rejected before the file is touched.

Edits are surgical: only the assignment line for a changed setting is
rewritten, so comments, ordering, spacing and any hand-added settings survive.
"""

import ast
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Callable, List, Optional

# Never surfaced in the UI, never written back.
SECRET_KEYS = {"OURA_TOKEN"}


@dataclass
class Field:
    key: str
    label: str
    kind: str                     # text | int | float | bool | time | opt_time | minutes | choice | opt_text
    help: str = ""
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[List[str]] = None
    section: str = "General"


SETTINGS: List[Field] = [
    Field("ICAL_OUTPUT_PATH", "Calendar output path", "text", section="Calendar",
          help="Where the generated .ics is written.", default="./bedtime.ics"),
    Field("DAYS_AHEAD", "Nights to plan ahead", "int", section="Calendar",
          help="How many nights each run plans.", default=14, minimum=1, maximum=365),
    Field("TIMEZONE", "Timezone", "opt_text", section="Calendar",
          help="IANA name such as Europe/Zurich. Leave blank for the machine's local zone.",
          default=None),

    Field("WAKE_TIME_WEEKDAY", "Monday–Friday", "time", section="Wake times",
          help="Used for any weekday without its own override below.", default="06:30"),
    Field("WAKE_TIME_SATURDAY", "Saturday", "time", section="Wake times", default="08:00"),
    Field("WAKE_TIME_SUNDAY", "Sunday", "time", section="Wake times", default="08:00"),
    Field("WAKE_TIME_MONDAY", "Monday override", "opt_time", section="Wake times",
          help="Blank means use the Monday–Friday time."),
    Field("WAKE_TIME_TUESDAY", "Tuesday override", "opt_time", section="Wake times"),
    Field("WAKE_TIME_WEDNESDAY", "Wednesday override", "opt_time", section="Wake times"),
    Field("WAKE_TIME_THURSDAY", "Thursday override", "opt_time", section="Wake times"),
    Field("WAKE_TIME_FRIDAY", "Friday override", "opt_time", section="Wake times"),

    Field("WAKE_SOURCE", "Wake time source", "choice", section="Calendar feeds",
          choices=["fixed", "calendar"], default="fixed",
          help="'fixed' uses the configured wake times. 'calendar' reads the "
               "feeds below and can pull the alarm earlier."),
    Field("CALENDAR_URLS", "Calendar feeds", "lines", section="Calendar feeds",
          help="One .ics URL or local path per line.", default=[]),
    Field("CALENDAR_LEAD_MINUTES", "Lead time (minutes)", "int",
          section="Calendar feeds",
          help="Getting ready plus travel before the first event.",
          default=90, minimum=0, maximum=600),
    Field("CALENDAR_ONLY_EARLIER", "Only ever wake earlier", "bool",
          section="Calendar feeds",
          help="On: a late first event keeps your usual wake time.", default=True),
    Field("CALENDAR_EARLIEST_WAKE", "Earliest calendar wake", "time",
          section="Calendar feeds",
          help="The calendar may never derive a wake time before this.",
          default="05:00"),
    Field("CALENDAR_SKIP_ALL_DAY", "Ignore all-day events", "bool",
          section="Calendar feeds", default=True),
    Field("CALENDAR_SKIP_FREE", "Ignore free and cancelled events", "bool",
          section="Calendar feeds", default=True),

    Field("BED_SOURCE", "Bedtime source", "choice", section="Bedtimes",
          choices=["computed", "fixed"], default="computed",
          help="'computed' back-calculates from your wake time and sleep need. "
               "'fixed' uses the times below."),
    Field("BED_TIME_WEEKDAY", "Monday–Friday nights", "time", section="Bedtimes",
          help="The evening you turn in, so Monday pairs with Tuesday's wake time.",
          default="23:00"),
    Field("BED_TIME_SATURDAY", "Saturday night", "time", section="Bedtimes",
          default="23:30"),
    Field("BED_TIME_SUNDAY", "Sunday night", "time", section="Bedtimes",
          default="22:30"),
    Field("BED_TIME_MONDAY", "Monday night override", "opt_time", section="Bedtimes",
          help="Blank means use the Monday–Friday time."),
    Field("BED_TIME_TUESDAY", "Tuesday night override", "opt_time", section="Bedtimes"),
    Field("BED_TIME_WEDNESDAY", "Wednesday night override", "opt_time", section="Bedtimes"),
    Field("BED_TIME_THURSDAY", "Thursday night override", "opt_time", section="Bedtimes"),
    Field("BED_TIME_FRIDAY", "Friday night override", "opt_time", section="Bedtimes"),

    Field("HISTORY_DAYS", "History window (days)", "int", section="Sleep need",
          help="How much Oura history to learn from. 60–90 is usually stable.",
          default=90, minimum=7, maximum=365),
    Field("GOOD_SLEEP_SCORE", "Good-night score", "int", section="Sleep need",
          help="Nights scoring at least this set your sleep need.",
          default=80, minimum=1, maximum=100),
    Field("MIN_GOOD_NIGHTS", "Minimum good nights", "int", section="Sleep need",
          help="Below this, the fallback below is used instead.",
          default=5, minimum=1, maximum=100),
    Field("FALLBACK_SLEEP_NEED_HOURS", "Fallback sleep need (hours)", "float",
          section="Sleep need", default=8.0, minimum=1.0, maximum=14.0),

    Field("DEBT_WINDOW_DAYS", "Debt window (days)", "int", section="Sleep debt",
          default=14, minimum=1, maximum=90),
    Field("DEBT_RECOVERY_NIGHTS", "Recovery nights", "int", section="Sleep debt",
          help="Debt is repaid across this many nights rather than all at once.",
          default=7, minimum=1, maximum=60),
    Field("MAX_DEBT_ADJUSTMENT_MINUTES", "Max debt adjustment (minutes)", "int",
          section="Sleep debt",
          help="Cap on how much earlier debt may pull your bedtime.",
          default=45, minimum=0, maximum=240),

    Field("EARLIEST_BEDTIME", "Earliest bedtime", "time", section="Guard rails",
          help="Never recommend a bedtime before this.", default="21:00"),
    Field("LATEST_BEDTIME", "Latest bedtime", "time", section="Guard rails",
          help="Never recommend a bedtime after this. May be after midnight.",
          default="01:00"),

    Field("BED_EVENT_DURATION_MINUTES", "Bedtime event length (minutes)", "int",
          section="Reminders", default=15, minimum=1, maximum=240),
    Field("BED_ALARM_MINUTES_BEFORE", "Bedtime reminders", "minutes",
          section="Reminders",
          help="Minutes before bedtime. Comma-separate for several, e.g. 60, 15. Blank for none.",
          default=(60, 15)),
    Field("WAKE_EVENT", "Add a wake-up event", "bool", section="Reminders",
          default=True),
    Field("WAKE_ALARM_MINUTES_BEFORE", "Wake reminders", "minutes",
          section="Reminders",
          help="Minutes before the wake time. 0 fires exactly at it.", default=0),
]

SECTIONS = list(dict.fromkeys(f.section for f in SETTINGS))


class ValidationError(ValueError):
    """A submitted value that must not reach config.py."""


def parse_hhmm(value: str, label: str) -> str:
    """Validate an "HH:MM" string, returning it normalised."""
    try:
        hours, minutes = str(value).strip().split(":")
        parsed = time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        raise ValidationError(f"{label}: '{value}' is not a time like 07:30.")
    return parsed.strftime("%H:%M")


def parse_minutes_list(value, label: str):
    """Parse "60, 15" into (60, 15). Returns None when blank."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        items = text.replace(";", ",").split(",")

    minutes = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        try:
            number = int(item)
        except ValueError:
            raise ValidationError(f"{label}: '{item}' is not a whole number of minutes.")
        if number < 0:
            raise ValidationError(f"{label}: '{item}' cannot be negative.")
        minutes.append(number)

    if not minutes:
        return None
    ordered = sorted(set(minutes), reverse=True)
    return ordered[0] if len(ordered) == 1 else tuple(ordered)


def coerce(field: Field, raw) -> Any:
    """Validate and convert one submitted value into what config.py should hold."""
    label = field.label

    if field.kind in ("text",):
        text = str(raw).strip()
        if not text:
            raise ValidationError(f"{label}: cannot be empty.")
        return text

    if field.kind == "opt_text":
        text = str(raw or "").strip()
        return text or None

    if field.kind == "lines":
        if isinstance(raw, (list, tuple)):
            items = [str(item).strip() for item in raw]
        else:
            items = [line.strip() for line in str(raw or "").splitlines()]
        return [item for item in items if item]

    if field.kind == "time":
        return parse_hhmm(raw, label)

    if field.kind == "opt_time":
        text = str(raw or "").strip()
        return parse_hhmm(text, label) if text else None

    if field.kind == "bool":
        return bool(raw)

    if field.kind == "minutes":
        return parse_minutes_list(raw, label)

    if field.kind in ("int", "float"):
        try:
            number = int(raw) if field.kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise ValidationError(f"{label}: '{raw}' is not a number.")
        if field.minimum is not None and number < field.minimum:
            raise ValidationError(f"{label}: must be at least {field.minimum}.")
        if field.maximum is not None and number > field.maximum:
            raise ValidationError(f"{label}: must be at most {field.maximum}.")
        return number

    if field.kind == "choice":
        if raw not in (field.choices or []):
            raise ValidationError(f"{label}: '{raw}' is not an allowed value.")
        return raw

    raise ValidationError(f"{label}: unsupported field kind '{field.kind}'.")


def serialize(value) -> str:
    """Render a validated value as a Python literal.

    Only these types are ever produced by `coerce`, so nothing arbitrary can
    reach the file: strings go through repr(), which quotes and escapes them.
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        # Double quotes to match the style config.py.template is written in,
        # so saving doesn't rewrite every string line just to flip the quotes.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, tuple):
        if not all(isinstance(item, int) for item in value):
            raise ValidationError("Only whole numbers are allowed in a list setting.")
        return "(" + ", ".join(str(item) for item in value) + ")"
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ValidationError("Only text entries are allowed in a list setting.")
        if not value:
            return "[]"
        inner = ",\n".join(f"    {serialize(item)}" for item in value)
        return "[\n" + inner + ",\n]"
    raise ValidationError(f"Cannot write a {type(value).__name__} to config.py.")


def read_values(cfg) -> dict:
    """Current values for every setting, secrets excluded."""
    return {
        f.key: getattr(cfg, f.key, f.default)
        for f in SETTINGS
        if f.key not in SECRET_KEYS
    }


def _bracket_delta(line: str) -> int:
    """Net bracket depth a line adds, ignoring strings and comments.

    Brackets inside a quoted URL or a commented-out example must not count, or
    a multi-line list would be measured wrongly.
    """
    depth = 0
    quote = None
    position = 0

    while position < len(line):
        character = line[position]
        if quote:
            if character == "\\":
                position += 2
                continue
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character == "#":
            break
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        position += 1

    return depth


def _assignment_end(lines: List[str], start: int) -> int:
    """Index just past the assignment that begins at `lines[start]`.

    A single-line setting returns start + 1; a list spread over several lines
    returns the index after its closing bracket.
    """
    depth = 0
    index = start

    while index < len(lines):
        depth += _bracket_delta(lines[index])
        index += 1
        if depth <= 0:
            break

    return index


def write_values(path: str, updates: dict) -> List[str]:
    """Apply `updates` to config.py in place. Returns the keys actually changed.

    A .bak copy is taken first and the new file is written to a temporary path
    then moved into place, so an interrupted write cannot leave a half-file
    that fails to import.
    """
    for key in updates:
        if key in SECRET_KEYS:
            raise ValidationError(f"{key} must not be written from the UI.")

    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    changed = []
    remaining = dict(updates)

    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", lines[index])
        if not match:
            index += 1
            continue

        key = match.group(1)
        end = _assignment_end(lines, index)

        if key not in remaining:
            index = end
            continue

        new_value = remaining.pop(key)

        # Compare the parsed value, not the rendered text: otherwise a
        # difference in quote style or spacing counts as a change and the
        # save rewrites lines the user never touched.
        existing_text = "".join(lines[index:end]).split("=", 1)[1].strip()
        try:
            unchanged = ast.literal_eval(existing_text) == new_value
        except (ValueError, SyntaxError):
            unchanged = False

        if unchanged:
            index = end
            continue

        # The whole assignment is replaced, not just its first line — a list
        # written across several lines would otherwise leave its tail behind
        # and turn config.py into a syntax error.
        lines[index:end] = [f"{key} = {serialize(new_value)}\n"]
        changed.append(key)
        index += 1

    # A setting the file never had (an older config.py) is appended rather than
    # silently dropped, so the UI can introduce new options.
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n# --- Added by the Settings page ---\n")
        for key, value in remaining.items():
            lines.append(f"{key} = {serialize(value)}\n")
            changed.append(key)

    if not changed:
        return []

    shutil.copyfile(path, path + ".bak")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    os.replace(temporary, path)
    return changed
