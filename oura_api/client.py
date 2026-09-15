import requests
from datetime import datetime, timedelta

from . import cache

OURA_API_BASE = "https://api.ouraring.com/v2/usercollection"


def _download(token: str, endpoint: str, days_back: int):
    """
    Fetch `days_back` days of a v2 usercollection endpoint.

    Returns (rows, complete). `complete` is False when the run stopped early on
    a network error, auth failure or malformed response — the caller must not
    cache a partial answer or let it displace a good one. Nothing raises, so a
    flaky connection never takes the whole run down.
    """
    headers = {"Authorization": f"Bearer {token}"}

    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)
    params = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }

    url = f"{OURA_API_BASE}/{endpoint}"
    rows = []
    seen_tokens = set()

    while True:
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"❌ Could not reach the Oura API ({endpoint}): {e}")
            return rows, False

        if response.status_code == 401:
            print("❌ Oura API rejected the access token (HTTP 401). "
                  "Check OURA_TOKEN in config.py.")
            return rows, False
        if response.status_code == 429:
            print(f"❌ Oura API rate limit hit (HTTP 429) on {endpoint}. Try again later.")
            return rows, False
        if not response.ok:
            print(f"❌ Oura API returned HTTP {response.status_code} on {endpoint}: "
                  f"{response.text[:200]}")
            return rows, False

        try:
            payload = response.json()
        except ValueError:
            print(f"❌ Oura API returned a non-JSON response on {endpoint}.")
            return rows, False

        rows.extend(payload.get("data", []))

        # Oura paginates with an opaque next_token. Guard against a server that
        # keeps handing back the same token, which would loop forever.
        next_token = payload.get("next_token")
        if not next_token or next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        params["next_token"] = next_token

    return rows, True


def _fetch(token: str, endpoint: str, days_back: int, use_cache: bool = True) -> list:
    """Fetch an endpoint, going through the on-disk cache when it is enabled."""
    if use_cache:
        hit = cache.load(endpoint, days_back, token)
        if hit is not None:
            rows, age = hit
            print(f"📦 Using cached {endpoint} ({cache.describe_age(age)}, "
                  f"{len(rows)} record(s)).")
            return rows

    rows, complete = _download(token, endpoint, days_back)
    if complete:
        cache.store(endpoint, days_back, token, rows)
        return rows

    # The API let us down. A stale entry is a better answer than none, as long
    # as the run says out loud that the numbers are not from today.
    if cache.serve_stale_on_error():
        stale = cache.load(endpoint, days_back, token, allow_stale=True)
        if stale is not None:
            cached_rows, age = stale
            print(f"⚠️  Falling back to cached {endpoint} from "
                  f"{cache.describe_age(age)} — the API call failed.")
            return cached_rows

    return rows


def fetch_sleep_data(token: str, days_back: int = 90, use_cache: bool = True) -> list:
    """Fetch individual sleep sessions (stages, efficiency, latency, timings)."""
    return _fetch(token, "sleep", days_back, use_cache=use_cache)


def fetch_daily_sleep(token: str, days_back: int = 90, use_cache: bool = True) -> list:
    """Fetch the per-day sleep score used to decide which nights were good."""
    return _fetch(token, "daily_sleep", days_back, use_cache=use_cache)


def fetch_daily_readiness(token: str, days_back: int = 30, use_cache: bool = True) -> list:
    """Fetch daily readiness, which carries Oura's own sleep_balance score."""
    return _fetch(token, "daily_readiness", days_back, use_cache=use_cache)
