"""Thin API-Football (api-sports.io) client — standard library only."""
import json
import socket
import time
import urllib.error
import urllib.request

API_HOST = "v3.football.api-sports.io"
BASE_URL = f"https://{API_HOST}"

# Global safety net so no socket op blocks indefinitely (note: does not bound DNS;
# the poller runs each cycle under a hard deadline for that).
socket.setdefaulttimeout(20)
REQ_TIMEOUT = 15


def _get(path, params, key, retries=2, pause=6):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE_URL}{path}?{qs}" if qs else f"{BASE_URL}{path}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"x-apisports-key": key})
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(pause)
                continue
            raise


def live_fixtures(key):
    """All in-play matches."""
    return _get("/fixtures", {"live": "all"}, key).get("response", [])


def statistics(key, fixture_id):
    """Per-team statistics for one fixture (empty list if none yet)."""
    return _get("/fixtures/statistics", {"fixture": fixture_id}, key).get("response", [])


def account_status(key):
    return _get("/status", {}, key).get("response", {})
