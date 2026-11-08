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
import db
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
POLL_DBG = {"loop_iters": 0, "phase": "init", "started_at": None,
            "finished_at": None, "match_i": 0, "n": 0}


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
    POLL_DBG["phase"] = "fetch_live"
    fixtures = sofa_api.live_fixtures(KEY)
    live_ids = set()
    counts = {"full": 0, "partial": 0, "checking": 0, "skipped": 0}
    archive_rows = []
    POLL_DBG["n"] = len(fixtures)
    POLL_DBG["match_i"] = 0
    for i, fx in enumerate(fixtures):
        POLL_DBG["match_i"] = i
        POLL_DBG["phase"] = f"match {i+1}/{len(fixtures)}"
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
            "home_id": teams["home"].get("id"), "away_id": teams["away"].get("id"),
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
        changed = (prev is None) or (home_vals != prev[0]) or (away_vals != prev[1])
        archive_rows.append(
            db.row_from(entry, core.full_stats(st[0]), core.full_stats(st[1]), changed))
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
    db.insert_rows(archive_rows)  # best-effort archival; never breaks the live view


CYCLE_DEADLINE = int(os.environ.get("CYCLE_DEADLINE", "90"))  # max secs per poll cycle


def _run_poll_cycle():
    POLL_DBG["phase"] = "polling"
    POLL_DBG["started_at"] = _iso()
    try:
        poll_once()
        POLL_DBG["phase"] = "done"
    except Exception as e:  # noqa: BLE001
        META.update(error=str(e), last_poll=_iso())
        POLL_DBG["phase"] = "error: " + str(e)[:120]
    POLL_DBG["finished_at"] = _iso()


def poller_loop():
    # Run each cycle in a disposable thread with a hard deadline. A network call
    # can hang past its socket timeout (urllib's timeout does NOT cover DNS), which
    # would otherwise wedge the poller forever. If a cycle overruns, we abandon it
    # (daemon thread, harmless) and try again next interval — the poller never freezes.
    while True:
        POLL_DBG["loop_iters"] += 1
        t = threading.Thread(target=_run_poll_cycle, name="poll-cycle", daemon=True)
        t.start()
        t.join(timeout=CYCLE_DEADLINE)
        if t.is_alive():
            POLL_DBG["phase"] = f"cycle stuck >{CYCLE_DEADLINE}s (network); retrying"
            META["error"] = f"poll cycle exceeded {CYCLE_DEADLINE}s (likely DNS/network); retrying"
        time.sleep(POLL_INTERVAL)


# ---------- API ----------
@app.before_request
def _ensure_poller_running():
    # Guarantees the poller runs in the gunicorn worker that serves requests,
    # regardless of fork/preload timing. Idempotent and cheap.
    if not _poller_alive():
        start_poller()


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


@app.route("/api/debug")
def api_debug():
    names = [t.name for t in threading.enumerate()]
    return jsonify({
        "pid": os.getpid(),
        "poller_thread_alive": _poller_alive(),
        "threads": names,
        "key_present": bool(KEY),
        "key_len": len(KEY) if KEY else 0,
        "autostart_env": repr(os.environ.get("AUTOSTART_POLLER")),
        "poll_interval": POLL_INTERVAL,
        "last_poll": META.get("last_poll"),
        "error": META.get("error"),
        "poll_dbg": POLL_DBG,
    })


@app.route("/api/archive")
def api_archive():
    return jsonify(db.STATUS)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


_poller_lock = threading.Lock()


def _poller_alive():
    # Use the real thread registry (not a boolean flag): a flag set before a
    # gunicorn fork is copied into the worker, but the thread itself is not.
    return any(t.name == "poller" and t.is_alive() for t in threading.enumerate())


def start_poller():
    """Start the background poller if it isn't already running in THIS process.
    Fork-safe and idempotent — called at import, on first request, and in main()."""
    with _poller_lock:
        if _poller_alive():
            return
        if not KEY:
            print("[poller] NOT started: APISPORTS_KEY is not set", flush=True)
            return
        db.init_db()  # create the archival table if DATABASE_URL is set (no-op otherwise)
        threading.Thread(target=poller_loop, name="poller", daemon=True).start()
        print(f"[poller] started (interval {POLL_INTERVAL}s)", flush=True)


def main():
    if not KEY:
        raise SystemExit("ERROR: set APISPORTS_KEY environment variable.")
    start_poller()
    print(f"Live Football Momentum running on http://localhost:{PORT}  (poll every {POLL_INTERVAL}s)")
    app.run(host="0.0.0.0", port=PORT, threaded=True)


# When served by a WSGI server (gunicorn) the module is imported rather than run,
# so start the poller here too. It starts by default; set AUTOSTART_POLLER=0 to
# disable (e.g. in tests). Any value other than "0" (or unset) starts it.
if os.environ.get("AUTOSTART_POLLER", "1").strip() != "0":
    start_poller()

if __name__ == "__main__":
    main()
