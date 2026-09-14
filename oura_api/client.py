import requests
from datetime import datetime, timedelta

OURA_API_BASE = "https://api.ouraring.com/v2/usercollection"


def _fetch(token: str, endpoint: str, days_back: int) -> list:
    """
    Fetch `days_back` days of a v2 usercollection endpoint.

    Returns an empty list (and prints a friendly message) on network errors,
    auth failures, or malformed responses, instead of raising, so a flaky
    connection never takes the whole run down.
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
            return rows

        if response.status_code == 401:
            print("❌ Oura API rejected the access token (HTTP 401). "
                  "Check OURA_TOKEN in config.py.")
            return rows
        if response.status_code == 429:
            print(f"❌ Oura API rate limit hit (HTTP 429) on {endpoint}. Try again later.")
            return rows
        if not response.ok:
            print(f"❌ Oura API returned HTTP {response.status_code} on {endpoint}: "
                  f"{response.text[:200]}")
            return rows

        try:
            payload = response.json()
        except ValueError:
            print(f"❌ Oura API returned a non-JSON response on {endpoint}.")
            return rows

        rows.extend(payload.get("data", []))

        # Oura paginates with an opaque next_token. Guard against a server that
        # keeps handing back the same token, which would loop forever.
        next_token = payload.get("next_token")
        if not next_token or next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        params["next_token"] = next_token

    return rows


def fetch_sleep_data(token: str, days_back: int = 90) -> list:
    """Fetch individual sleep sessions (stages, efficiency, latency, timings)."""
    return _fetch(token, "sleep", days_back)


def fetch_daily_sleep(token: str, days_back: int = 90) -> list:
    """Fetch the per-day sleep score used to decide which nights were good."""
    return _fetch(token, "daily_sleep", days_back)
