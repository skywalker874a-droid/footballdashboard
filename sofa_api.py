"""API-Football (api-sports.io) client.

Two robustness measures, both learned from Render:
1. A pooled, keep-alive requests.Session (connection reused across calls).
2. A DNS-over-HTTPS fallback for the API host. Render's system resolver has been
   observed to hang indefinitely on `v3.football.api-sports.io` (while resolving
   everything else fine), which wedged the poller. We resolve that host via
   Cloudflare/Google DoH — contacted by literal IP, so it needs no system DNS —
   and feed the IP into socket.getaddrinfo transparently. TLS SNI and certificate
   verification still use the hostname, so nothing else changes.
"""
import socket
import time

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

API_HOST = "v3.football.api-sports.io"
BASE_URL = f"https://{API_HOST}"
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 12

# ---- resolver bypass for the API host ----
# Known-good Cloudflare IPs (verified) used instantly as the primary answer, so we
# never touch Render's hanging resolver for this host. DoH refreshes them in case
# Cloudflare's anycast IPs change over time.
STATIC_IPS = ["104.20.39.36", "172.66.164.245"]
_doh_ips = []
_doh_ts = 0.0
_DOH_TTL = 600  # refresh at most every 10 min


def _resolve_doh(host):
    for url in ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve"):
        try:
            r = requests.get(
                url, params={"name": host, "type": "A"},
                headers={"Accept": "application/dns-json"}, timeout=(3, 4),
            )
            answers = r.json().get("Answer", [])
            ips = [a["data"] for a in answers if a.get("type") == 1]
            if ips:
                return ips
        except Exception:
            continue
    return []


def _host_ips():
    global _doh_ips, _doh_ts
    now = time.time()
    if now - _doh_ts >= _DOH_TTL:
        _doh_ts = now  # set first so a failing DoH doesn't retry until next TTL
        ips = _resolve_doh(API_HOST)
        if ips:
            _doh_ips = ips
    return _doh_ips or STATIC_IPS


_orig_getaddrinfo = socket.getaddrinfo


def _is_ipv4(h):
    try:
        socket.inet_aton(h)
        return isinstance(h, str) and h.count(".") == 3
    except (OSError, TypeError):
        return False


def _manual(ip, args, kwargs):
    # build a getaddrinfo-style result WITHOUT calling the system resolver, so a
    # hung/broken resolver (seen on Render) can't block us even for numeric IPs
    port = args[0] if args else kwargs.get("port", 0)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 443
    typ = args[2] if len(args) > 2 else kwargs.get("type", socket.SOCK_STREAM)
    proto = args[3] if len(args) > 3 else kwargs.get("proto", 0)
    return [(socket.AF_INET, typ or socket.SOCK_STREAM, proto, "", (ip, port))]


def _patched_getaddrinfo(host, *args, **kwargs):
    # our API host -> pinned Cloudflare IPs, built by hand (no system resolver)
    if host == API_HOST:
        res = []
        for ip in _host_ips():
            res += _manual(ip, args, kwargs)
        if res:
            return res
    # any numeric IP (e.g. the DoH servers) -> build by hand, bypass the resolver
    if _is_ipv4(host):
        return _manual(host, args, kwargs)
    # everything else (e.g. Supabase) uses the normal resolver
    return _orig_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo

# ---- pooled session ----
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


def live_odds(key, fixture_id):
    """Live in-play odds for one fixture (empty list if none)."""
    return _get("/odds/live", {"fixture": fixture_id}, key).get("response", [])


def account_status(key):
    return _get("/status", {}, key).get("response", {})
