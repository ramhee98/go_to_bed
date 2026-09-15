"""Tests for the on-disk Oura cache.

The failure modes worth guarding are the quiet ones: serving one account's
nights to another, letting a failed API call overwrite good data, and turning
an unreadable cache file into a crash instead of a plain API call.
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oura_api import cache
from oura_api import client

ROWS = [{"day": "2026-09-13", "total_sleep_duration": 27000}]


class _Settings:
    """Stand in for config.py so a test never reads the real one."""

    def __init__(self, directory, **overrides):
        self.values = {
            "CACHE_ENABLED": True,
            "CACHE_TTL_MINUTES": 60,
            "CACHE_DIR": directory,
            "CACHE_SERVE_STALE_ON_ERROR": True,
        }
        self.values.update(overrides)

    def __call__(self, name, default):
        return self.values.get(name, default)


def _sandbox(**overrides):
    """A temp cache directory with cache._setting pointed at it."""
    directory = tempfile.mkdtemp(prefix="gtb-cache-")
    original = cache._setting
    cache._setting = _Settings(directory, **overrides)
    return directory, original


def _cleanup(directory, original):
    cache._setting = original
    shutil.rmtree(directory, ignore_errors=True)


def test_store_then_load_returns_the_rows():
    directory, original = _sandbox()
    try:
        assert cache.store("sleep", 90, "token", ROWS) is True
        hit = cache.load("sleep", 90, "token")
        assert hit is not None
        rows, age = hit
        assert rows == ROWS
        assert age < 5
    finally:
        _cleanup(directory, original)


def test_expired_entry_is_a_miss_but_still_available_as_stale():
    directory, original = _sandbox(CACHE_TTL_MINUTES=1)
    try:
        cache.store("sleep", 90, "token", ROWS)

        # Backdate the entry rather than waiting a minute for it to age.
        path = cache.path_for("sleep", 90, "token")
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        entry["fetched_at"] = time.time() - 3600
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f)

        assert cache.load("sleep", 90, "token") is None
        stale = cache.load("sleep", 90, "token", allow_stale=True)
        assert stale is not None and stale[0] == ROWS
        assert stale[1] > 3000
    finally:
        _cleanup(directory, original)


def test_zero_ttl_never_serves_a_fresh_hit():
    directory, original = _sandbox(CACHE_TTL_MINUTES=0)
    try:
        cache.store("sleep", 90, "token", ROWS)
        assert cache.load("sleep", 90, "token") is None
        # Still readable as a last resort when the API is down.
        assert cache.load("sleep", 90, "token", allow_stale=True) is not None
    finally:
        _cleanup(directory, original)


def test_disabled_cache_neither_writes_nor_reads():
    directory, original = _sandbox()
    try:
        cache.store("sleep", 90, "token", ROWS)
        cache._setting.values["CACHE_ENABLED"] = False
        assert cache.load("sleep", 90, "token") is None
        assert cache.store("sleep", 90, "token", ROWS) is False
    finally:
        _cleanup(directory, original)


def test_a_second_token_gets_its_own_entry():
    directory, original = _sandbox()
    try:
        cache.store("sleep", 90, "token-a", ROWS)
        assert cache.load("sleep", 90, "token-b") is None
        assert cache.load("sleep", 90, "token-a")[0] == ROWS
    finally:
        _cleanup(directory, original)


def test_the_days_back_window_is_part_of_the_key():
    directory, original = _sandbox()
    try:
        cache.store("sleep", 90, "token", ROWS)
        assert cache.load("sleep", 30, "token") is None
    finally:
        _cleanup(directory, original)


def test_the_token_is_never_written_to_disk():
    directory, original = _sandbox()
    try:
        cache.store("sleep", 90, "SECRET-TOKEN-VALUE", ROWS)
        for name in os.listdir(directory):
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                assert "SECRET-TOKEN-VALUE" not in f.read()
            assert "SECRET-TOKEN-VALUE" not in name
    finally:
        _cleanup(directory, original)


def test_a_corrupt_entry_is_a_miss_not_a_crash():
    directory, original = _sandbox()
    try:
        cache.store("sleep", 90, "token", ROWS)
        with open(cache.path_for("sleep", 90, "token"), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        assert cache.load("sleep", 90, "token") is None
    finally:
        _cleanup(directory, original)


def test_an_unwritable_directory_does_not_raise():
    directory, original = _sandbox(CACHE_DIR="/proc/nonexistent/go_to_bed")
    try:
        assert cache.store("sleep", 90, "token", ROWS) is False
        assert cache.load("sleep", 90, "token") is None
        assert cache.clear() == 0
    finally:
        _cleanup(directory, original)


def test_a_failed_write_leaves_nothing_behind():
    directory, original = _sandbox()
    try:
        # A set is not JSON, so the write fails partway through.
        assert cache.store("sleep", 90, "token", [{"day": {1, 2}}]) is False
        assert os.listdir(directory) == []
    finally:
        _cleanup(directory, original)


def test_clear_removes_every_entry():
    directory, original = _sandbox()
    try:
        cache.store("sleep", 90, "token", ROWS)
        cache.store("daily_sleep", 90, "token", ROWS)
        assert cache.clear() == 2
        assert cache.load("sleep", 90, "token") is None
    finally:
        _cleanup(directory, original)


def test_fetch_serves_the_cache_without_calling_the_api():
    directory, original = _sandbox()
    calls = []
    original_download = client._download
    try:
        cache.store("sleep", 90, "token", ROWS)
        client._download = lambda *a, **k: (calls.append(a) or ([], True))
        assert client._fetch("token", "sleep", 90) == ROWS
        assert calls == []
    finally:
        client._download = original_download
        _cleanup(directory, original)


def test_no_cache_bypasses_the_hit_but_still_refreshes_it():
    directory, original = _sandbox()
    fresh = [{"day": "2026-09-14"}]
    original_download = client._download
    try:
        cache.store("sleep", 90, "token", ROWS)
        client._download = lambda *a, **k: (fresh, True)
        assert client._fetch("token", "sleep", 90, use_cache=False) == fresh
        assert cache.load("sleep", 90, "token")[0] == fresh
    finally:
        client._download = original_download
        _cleanup(directory, original)


def test_a_failed_call_keeps_the_old_entry_and_serves_it():
    directory, original = _sandbox(CACHE_TTL_MINUTES=0)
    original_download = client._download
    try:
        cache.store("sleep", 90, "token", ROWS)
        # Partial rows plus complete=False is what a mid-pagination failure
        # looks like; it must not displace a whole response.
        client._download = lambda *a, **k: ([{"day": "partial"}], False)
        assert client._fetch("token", "sleep", 90) == ROWS
        assert cache.load("sleep", 90, "token", allow_stale=True)[0] == ROWS
    finally:
        client._download = original_download
        _cleanup(directory, original)


def test_stale_fallback_can_be_turned_off():
    directory, original = _sandbox(CACHE_TTL_MINUTES=0,
                                   CACHE_SERVE_STALE_ON_ERROR=False)
    original_download = client._download
    try:
        cache.store("sleep", 90, "token", ROWS)
        client._download = lambda *a, **k: ([], False)
        assert client._fetch("token", "sleep", 90) == []
    finally:
        client._download = original_download
        _cleanup(directory, original)


def test_describe_age_reads_like_a_sentence():
    assert cache.describe_age(5) == "just now"
    assert cache.describe_age(720) == "12m ago"
    assert cache.describe_age(7200) == "2h ago"
    assert cache.describe_age(172800) == "2d ago"


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
