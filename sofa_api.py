"""API-Football (api-sports.io) client.

Uses a single pooled, keep-alive requests.Session so the TCP/TLS connection is
established once and reused across calls. This avoids a fresh DNS lookup + connect
on every request (the stdlib-urllib approach did one per call, which hung
repeatedly under network flakiness and stalled the poller). Bounded connect/read
timeouts + retries keep any single call from wedging.
"""
import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

API_HOST = "v3.football.api-sports.io"
BASE_URL = f"https://{API_HOST}"
CONNECT_TIMEOUT = 8   # seconds to establish a connection
READ_TIMEOUT = 20     # seconds to read the response

_session = None


def _get_session():
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(
            total=2, connect=2, read=2, backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"], respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _session = s
    return _session


def _get(path, params, key):
    r = _get_session().get(
        BASE_URL + path, params=params,
        headers={"x-apisports-key": key},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    r.raise_for_status()
    return r.json()


def live_fixtures(key):
    """All in-play matches."""
    return _get("/fixtures", {"live": "all"}, key).get("response", [])


def statistics(key, fixture_id):
    """Per-team statistics for one fixture (empty list if none yet)."""
    return _get("/fixtures/statistics", {"fixture": fixture_id}, key).get("response", [])


def account_status(key):
    return _get("/status", {}, key).get("response", {})
