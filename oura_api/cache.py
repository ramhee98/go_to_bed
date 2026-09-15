"""An on-disk cache for Oura API responses, shared by the CLI and the app.

Every run used to hit the API for the full history, which is wasteful when the
calendar is regenerated from cron and the app is reloaded all day: yesterday's
nights do not change. Responses are stored as JSON under `CACHE_DIR` and reused
until `CACHE_TTL_MINUTES` has passed.

Two properties matter more than hit rate:

* **A cache failure is never a run failure.** Every read and write is wrapped;
  an unwritable directory or a truncated file degrades to a plain API call.
* **A failed fetch never overwrites a good entry**, and with
  `CACHE_SERVE_STALE_ON_ERROR` an expired entry is still served when the API is
  unreachable — a cron run on a flaky connection keeps the last good data
  rather than writing a calendar from nothing.
"""

import hashlib
import json
import os
import tempfile
import time

DEFAULT_DIR = "./.cache/oura"
DEFAULT_TTL_MINUTES = 60


def _setting(name, default):
    """Read a config value, tolerating a missing or older config.py."""
    try:
        import config
    except ImportError:
        return default
    return getattr(config, name, default)


def enabled() -> bool:
    return bool(_setting("CACHE_ENABLED", True))


def ttl_seconds() -> float:
    """How long a cached response stays fresh. 0 disables reuse."""
    try:
        minutes = float(_setting("CACHE_TTL_MINUTES", DEFAULT_TTL_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_TTL_MINUTES
    return max(0.0, minutes) * 60


def directory() -> str:
    return str(_setting("CACHE_DIR", DEFAULT_DIR) or DEFAULT_DIR)


def serve_stale_on_error() -> bool:
    return bool(_setting("CACHE_SERVE_STALE_ON_ERROR", True))


def _fingerprint(token: str) -> str:
    """A short, non-reversible tag for the token.

    The token never lands in the cache — only this tag — but it has to take
    part in the key: pointing the app at a second account must not serve the
    first account's nights back.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:8]


def path_for(endpoint: str, days_back: int, token: str) -> str:
    return os.path.join(directory(),
                        f"{endpoint}-{int(days_back)}d-{_fingerprint(token)}.json")


def load(endpoint: str, days_back: int, token: str, allow_stale: bool = False):
    """Return (rows, age_seconds) for a usable entry, or None.

    With `allow_stale`, an entry past its TTL is returned too — for the case
    where the alternative is no data at all.
    """
    if not enabled():
        return None

    ttl = ttl_seconds()
    if ttl <= 0 and not allow_stale:
        return None

    try:
        with open(path_for(endpoint, days_back, token), "r", encoding="utf-8") as f:
            entry = json.load(f)
        rows = entry["rows"]
        fetched_at = float(entry["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None

    if not isinstance(rows, list):
        return None

    age = max(0.0, time.time() - fetched_at)
    if age > ttl and not allow_stale:
        return None
    return rows, age


def store(endpoint: str, days_back: int, token: str, rows: list) -> bool:
    """Write a fresh response. Returns whether it was actually stored."""
    if not enabled() or not isinstance(rows, list):
        return False

    path = path_for(endpoint, days_back, token)
    entry = {
        "endpoint": endpoint,
        "days_back": int(days_back),
        "fetched_at": time.time(),
        "rows": rows,
    }

    temporary = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Written alongside and renamed, so a crash mid-write cannot leave a
        # half-file that the next run would read as a valid entry.
        # mkstemp creates at 0600 and os.replace keeps the mode, so a night's
        # worth of personal data is not left world-readable.
        handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                             suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        # Take the half-written file with us, or a directory that keeps failing
        # to write slowly fills with them.
        if temporary is not None:
            try:
                os.remove(temporary)
            except OSError:
                pass
        return False
    return True


def clear() -> int:
    """Delete every cached response. Returns how many files went."""
    removed = 0
    try:
        names = os.listdir(directory())
    except OSError:
        return 0

    for name in names:
        # .tmp catches anything an interrupted write left behind.
        if not name.endswith((".json", ".tmp")):
            continue
        try:
            os.remove(os.path.join(directory(), name))
            removed += 1
        except OSError:
            continue
    return removed


def describe_age(seconds: float) -> str:
    """'just now', '12m ago', '3h ago', '2d ago' — for the run's own output."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"
