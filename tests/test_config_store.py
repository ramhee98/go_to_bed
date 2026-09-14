"""Tests for reading and writing config.py from the Settings page.

The risky parts are all here: the file is imported as Python, so a bad write
is an execution hole rather than a cosmetic bug, and a clumsy one destroys the
user's comments.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_store import (
    SETTINGS,
    ValidationError,
    coerce,
    parse_minutes_list,
    serialize,
    write_values,
)

SAMPLE = '''# A comment that must survive
OURA_TOKEN = "SECRET-TOKEN-VALUE"

CALENDAR_URLS = [
    # "https://example.com/commented-out.ics",
]
TRAILING_SETTING = 1

# Wake times
WAKE_TIME_WEEKDAY = "06:20"
WAKE_TIME_MONDAY = None
DAYS_AHEAD = 14
WAKE_EVENT = True
BED_ALARM_MINUTES_BEFORE = (60, 15)
'''


def _sample(tmp_name="cfg_sample.py"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), tmp_name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE)
    return path


def _field(key):
    return next(f for f in SETTINGS if f.key == key)


def test_write_preserves_comments_and_the_token():
    path = _sample()
    try:
        write_values(path, {"DAYS_AHEAD": 21})
        written = open(path).read()
        assert "# A comment that must survive" in written
        assert "# Wake times" in written
        assert 'OURA_TOKEN = "SECRET-TOKEN-VALUE"' in written
        assert "DAYS_AHEAD = 21" in written
    finally:
        os.remove(path)
        if os.path.exists(path + ".bak"):
            os.remove(path + ".bak")


def test_multiline_list_is_replaced_whole():
    # A list spread over several lines must be replaced in full. Rewriting
    # only its first line would leave the tail behind and produce a file that
    # no longer parses.
    import ast as ast_module
    path = _sample("cfg_multiline.py")
    try:
        write_values(path, {"CALENDAR_URLS": ["https://a.example/x.ics",
                                              "https://b.example/y.ics"]})
        written = open(path).read()
        ast_module.parse(written)                     # must still be valid Python
        assert "TRAILING_SETTING = 1" in written      # nothing after it was eaten
        assert "commented-out" not in written         # old body fully replaced
    finally:
        for suffix in ("", ".bak"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def test_multiline_list_round_trips_and_can_shrink():
    import importlib.util
    path = _sample("cfg_shrink.py")
    try:
        write_values(path, {"CALENDAR_URLS": ["https://a.example/x.ics"]})
        write_values(path, {"CALENDAR_URLS": []})
        spec = importlib.util.spec_from_file_location("cfg_shrink", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.CALENDAR_URLS == []
        assert module.TRAILING_SETTING == 1
    finally:
        for suffix in ("", ".bak"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def test_brackets_in_strings_and_comments_do_not_confuse_the_scanner():
    from config_store import _bracket_delta
    assert _bracket_delta('X = "a ] b"\n') == 0
    assert _bracket_delta("X = 1  # ] ) }\n") == 0
    assert _bracket_delta("X = [\n") == 1
    assert _bracket_delta("]\n") == -1


def test_write_refuses_the_token():
    path = _sample()
    try:
        write_values(path, {"OURA_TOKEN": "stolen"})
        raise AssertionError("writing OURA_TOKEN should have been refused")
    except ValidationError:
        pass
    finally:
        os.remove(path)


def test_unchanged_values_are_not_rewritten():
    # A save that changes nothing must not churn the file, so quote style and
    # spacing have to be compared semantically rather than as text.
    path = _sample()
    try:
        changed = write_values(path, {
            "DAYS_AHEAD": 14,
            "WAKE_TIME_WEEKDAY": "06:20",
            "WAKE_EVENT": True,
            "BED_ALARM_MINUTES_BEFORE": (60, 15),
        })
        assert changed == [], changed
        assert not os.path.exists(path + ".bak")
    finally:
        os.remove(path)


def test_written_file_is_still_importable():
    import importlib.util
    path = _sample("cfg_import.py")
    try:
        write_values(path, {"WAKE_TIME_WEEKDAY": "05:45",
                            "BED_ALARM_MINUTES_BEFORE": (90, 30)})
        spec = importlib.util.spec_from_file_location("cfg_import", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.WAKE_TIME_WEEKDAY == "05:45"
        assert module.BED_ALARM_MINUTES_BEFORE == (90, 30)
    finally:
        for suffix in ("", ".bak"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def test_text_input_cannot_inject_code():
    evil = '"; import os; os.system("rm -rf /") #'
    rendered = serialize(coerce(_field("ICAL_OUTPUT_PATH"), evil))
    # The payload must come back as one quoted literal, not executable syntax.
    import ast
    assert ast.literal_eval(rendered) == evil


def test_strings_are_written_with_double_quotes():
    assert serialize("06:20") == '"06:20"'


def test_minutes_field_accepts_the_documented_forms():
    assert parse_minutes_list("", "X") is None
    assert parse_minutes_list("15", "X") == 15
    assert parse_minutes_list("60, 15", "X") == (60, 15)
    assert parse_minutes_list("15,60,15", "X") == (60, 15)
    assert parse_minutes_list(0, "X") == 0


def test_minutes_field_rejects_junk():
    for bad in ("soon", "-5", "1.5"):
        try:
            parse_minutes_list(bad, "X")
            raise AssertionError(f"{bad!r} should have been rejected")
        except ValidationError:
            pass


def test_numeric_bounds_are_enforced():
    for key, value in [("GOOD_SLEEP_SCORE", 101), ("HISTORY_DAYS", 3),
                       ("DAYS_AHEAD", 0)]:
        try:
            coerce(_field(key), value)
            raise AssertionError(f"{key}={value} should have been rejected")
        except ValidationError:
            pass


def test_bad_time_is_rejected():
    for bad in ("25:00", "half past six", "6.30"):
        try:
            coerce(_field("WAKE_TIME_WEEKDAY"), bad)
            raise AssertionError(f"{bad!r} should have been rejected")
        except ValidationError:
            pass


def test_blank_optional_fields_become_none():
    assert coerce(_field("WAKE_TIME_MONDAY"), "") is None
    assert coerce(_field("TIMEZONE"), "  ") is None


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
