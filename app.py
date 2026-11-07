#!/usr/bin/env python3
"""
Live Football Momentum — Flask backend + 5-minute poller.

Polls all in-play matches that have statistics, computes DOMINANCE (cumulative)
and MOMENTUM (last-window delta), and serves a responsive web UI.

Only the latest reading per match is kept in memory; a match drops off the list
as soon as it leaves the live feed. No archival (by design, for now).

Run:
    export APISPORTS_KEY=your_ultra_key
    python3 app.py            # http://localhost:8000
    PORT=5000 python3 app.py
"""
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory

import core
import sofa_api

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))   # seconds (5 min)
PORT = int(os.environ.get("PORT", "8000"))
KEY = os.environ.get("APISPORTS_KEY")
# after this many consecutive "no stats" checks, stop polling a match for good
MAX_NONE_CHECKS = int(os.environ.get("MAX_NONE_CHECKS", "2"))

app = Flask(__name__, static_folder="static")

_lock = threading.Lock()
STORE = {}          # fid -> match dict (latest only; only full/partial matches)
PREV = {}           # fid -> (home_vals, away_vals) from previous tick, for momentum
NONE_STREAK = {}    # fid -> consecutive "no stats" checks so far
SKIP = set()        # fids confirmed to have no stats — never polled again
META = {"last_poll": None, "live_count": 0, "with_stats": 0,
        "counts": {"full": 0, "partial": 0, "checking": 0, "skipped": 0},
        "requests_remaining": None, "error": None}


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat()


def classify(st):
    """Return (coverage, d0, d1): 'none' | 'partial' | 'full'.
    none    = no usable statistics
    partial = stats present but no xG (expected_goals) — limited coverage
    full    = stats present including xG — richest feed
    """
    if not st or len(st) < 2 or not st[0].get("statistics"):
        return "none", None, None
    d0 = {s["type"]: s["value"] for s in st[0].get("statistics", [])}
    d1 = {s["type"]: s["value"] for s in st[1].get("statistics", [])}
    present = sum(1 for v in d0.values() if v not in (None, ""))
    if present == 0:
        return "none", d0, d1
    has_xg = (d0.get("expected_goals") not in (None, "")) or (d1.get("expected_goals") not in (None, ""))
    return ("full" if has_xg else "partial"), d0, d1


def poll_once():
    """One polling cycle. Only matches with stats are polled and stored; a match
    confirmed to have no stats (after MAX_NONE_CHECKS) is blacklisted and never
    polled again, so it costs no further API requests."""
    fixtures = sofa_api.live_fixtures(KEY)
    live_ids = set()
    counts = {"full": 0, "partial": 0, "checking": 0, "skipped": 0}
    for fx in fixtures:
        fid = fx["fixture"]["id"]
        live_ids.add(fid)

        if fid in SKIP:                 # known no-stats — don't spend a request
            counts["skipped"] += 1
            continue

        try:
            st = sofa_api.statistics(KEY, fid)
        except Exception:
            st = None
        coverage, _, _ = classify(st)

        if coverage == "none":
            NONE_STREAK[fid] = NONE_STREAK.get(fid, 0) + 1
            if NONE_STREAK[fid] >= MAX_NONE_CHECKS:
                SKIP.add(fid)           # give up on this match
                NONE_STREAK.pop(fid, None)
                counts["skipped"] += 1
            else:
                counts["checking"] += 1  # still giving it a chance to populate
            with _lock:
                STORE.pop(fid, None)
            PREV.pop(fid, None)
            continue

        NONE_STREAK.pop(fid, None)
        counts[coverage] += 1
        teams, goals = fx["teams"], fx["goals"]
        league = fx["league"]
        status = fx["fixture"]["status"]
        home_vals, away_vals = core.vals(st[0]), core.vals(st[1])
        prev = PREV.get(fid)
        dom_pct, dom_bd = core.dominance(home_vals, away_vals)
        mom_pct, mom_bd = core.momentum(
            home_vals, away_vals, prev[0] if prev else None, prev[1] if prev else None)
        entry = {
            "id": fid,
            "home": {"name": teams["home"]["name"], "logo": teams["home"].get("logo")},
            "away": {"name": teams["away"]["name"], "logo": teams["away"].get("logo")},
            "score": {"home": goals.get("home"), "away": goals.get("away")},
            "minute": status.get("elapsed"),
            "status": status.get("short"),
            "league": league.get("name"),
            "country": league.get("country"),
            "league_logo": league.get("logo"),
            "coverage": coverage,
            "dominance": dom_pct, "dominance_breakdown": dom_bd,
            "momentum": mom_pct, "momentum_breakdown": mom_bd,
            "stats_home": core.raw_table(st[0]), "stats_away": core.raw_table(st[1]),
            "updated_at": _iso(),
        }
        with _lock:
            STORE[fid] = entry
        PREV[fid] = (home_vals, away_vals)

    # forget everything about matches that are no longer live
    with _lock:
        for fid in list(STORE.keys()):
            if fid not in live_ids:
                STORE.pop(fid, None)
                PREV.pop(fid, None)
    for fid in list(NONE_STREAK.keys()):
        if fid not in live_ids:
            NONE_STREAK.pop(fid, None)
    SKIP.intersection_update(live_ids)
    META.update(last_poll=_iso(), live_count=len(fixtures),
                with_stats=counts["full"] + counts["partial"], counts=counts, error=None)


def poller_loop():
    while True:
        try:
            poll_once()
        except Exception as e:  # noqa: BLE001
            META.update(error=str(e), last_poll=_iso())
        time.sleep(POLL_INTERVAL)


# ---------- API ----------
@app.route("/api/matches")
def api_matches():
    with _lock:
        items = [{
            "id": m["id"], "home": m["home"], "away": m["away"], "score": m["score"],
            "minute": m["minute"], "status": m["status"], "league": m["league"],
            "country": m["country"], "league_logo": m["league_logo"],
            "coverage": m["coverage"],
            "dominance": m["dominance"], "momentum": m["momentum"],
            "updated_at": m["updated_at"],
        } for m in STORE.values()]
    # full first, then partial, then none; within a tier most lopsided first
    rank = {"full": 0, "partial": 1, "none": 2}
    items.sort(key=lambda x: (rank.get(x["coverage"], 3), -abs((x["dominance"] or 50) - 50)))
    return jsonify({"matches": items, "meta": META, "interval": POLL_INTERVAL})


@app.route("/api/match/<int:fid>")
def api_match(fid):
    with _lock:
        m = STORE.get(fid)
    if not m:
        return jsonify({"error": "not found or no longer live"}), 404
    return jsonify(m)


@app.route("/api/meta")
def api_meta():
    return jsonify({"meta": META, "interval": POLL_INTERVAL, "tracked": len(STORE)})


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


_poller_started = False
_poller_lock = threading.Lock()


def start_poller():
    """Start the background poller once (idempotent). Safe to call from both
    `main()` (local run) and module import (gunicorn/WSGI)."""
    global _poller_started
    with _poller_lock:
        if _poller_started or not KEY:
            return
        _poller_started = True
    threading.Thread(target=poller_loop, daemon=True).start()


def main():
    if not KEY:
        raise SystemExit("ERROR: set APISPORTS_KEY environment variable.")
    start_poller()
    print(f"Live Football Momentum running on http://localhost:{PORT}  (poll every {POLL_INTERVAL}s)")
    app.run(host="0.0.0.0", port=PORT, threaded=True)


# When served by a WSGI server (gunicorn) the module is imported rather than run,
# so start the poller here too. Set AUTOSTART_POLLER=0 to disable (e.g. in tests).
if os.environ.get("AUTOSTART_POLLER", "1") == "1":
    start_poller()

if __name__ == "__main__":
    main()
