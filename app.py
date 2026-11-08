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

import socket

import core
import db
import notify
import sofa_api

VERSION = "v8-telegram-alerts"  # bump each deploy so /api/debug confirms what's live

# Telegram alert thresholds (env-overridable)
ALERT_MIN_MINUTE = int(os.environ.get("ALERT_MIN_MINUTE", "15"))   # ignore before this minute
ALERT_MAX_MARGIN = int(os.environ.get("ALERT_MAX_MARGIN", "2"))    # ignore if goal gap > this
ALERT_DOM = float(os.environ.get("ALERT_DOM", "60"))              # alert at >= this or <= 100-this
_alert_state = {}  # fid -> bool: this worker's last-seen qualifying state (transition tracking)

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


def _alert_text(entry, dom):
    h, a = entry["home"]["name"], entry["away"]["name"]
    hs = entry["score"]["home"] or 0
    aw = entry["score"]["away"] or 0
    lead = h if dom >= 50 else a
    pct = dom if dom >= 50 else 100 - dom
    return (f"⚡ Dominance alert — {lead} on top\n"
            f"{h} {hs}-{aw} {a}   {entry['minute']}'  ({entry.get('league') or ''})\n"
            f"Dominance {round(pct)}% vs {round(100 - pct)}%")


def _check_alert(entry):
    """Telegram alert when a match crosses the dominance threshold (minute/goal-gap
    filtered). Fires on the not-qualifying -> qualifying transition; DB claim makes
    it exactly once across workers; re-arms when it stops qualifying."""
    if not notify.enabled():
        return
    fid = entry["id"]
    dom = entry["dominance"]
    minute = entry["minute"]
    hs = entry["score"]["home"] or 0
    aw = entry["score"]["away"] or 0
    qualifies = (dom is not None and minute is not None and minute >= ALERT_MIN_MINUTE
                 and abs(hs - aw) <= ALERT_MAX_MARGIN
                 and (dom >= ALERT_DOM or dom <= 100 - ALERT_DOM))
    was = _alert_state.get(fid, False)
    if qualifies and not was:
        _alert_state[fid] = True
        if db.claim_alert(fid):
            notify.send(_alert_text(entry, dom))
    elif not qualifies and was:
        _alert_state[fid] = False
        db.clear_alert(fid)


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
        try:
            odds = core.match_winner_odds(sofa_api.live_odds(KEY, fid))
        except Exception:
            odds = None  # odds often unavailable for a match; store nulls
        archive_rows.append(
            db.row_from(entry, core.full_stats(st[0]), core.full_stats(st[1]), changed, odds))
        with _lock:
            STORE[fid] = entry
        PREV[fid] = (home_vals, away_vals)
        _check_alert(entry)

    # forget everything about matches that are no longer live
    with _lock:
        for fid in list(STORE.keys()):
            if fid not in live_ids:
                STORE.pop(fid, None)
                PREV.pop(fid, None)
    for fid in list(NONE_STREAK.keys()):
        if fid not in live_ids:
            NONE_STREAK.pop(fid, None)
    for fid in list(_alert_state.keys()):
        if fid not in live_ids:
            _alert_state.pop(fid, None)
    SKIP.intersection_update(live_ids)
    META.update(last_poll=_iso(), live_count=len(fixtures),
                with_stats=counts["full"] + counts["partial"], counts=counts, error=None)
    db.insert_rows(archive_rows)  # best-effort archival; never breaks the live view


CYCLE_DEADLINE = int(os.environ.get("CYCLE_DEADLINE", "45"))   # max secs per poll cycle
RETRY_INTERVAL = int(os.environ.get("RETRY_INTERVAL", "45"))   # retry sooner after a failed cycle


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
        stuck = t.is_alive()
        if stuck:
            POLL_DBG["phase"] = f"cycle stuck >{CYCLE_DEADLINE}s (network); retrying"
            META["error"] = f"poll cycle exceeded {CYCLE_DEADLINE}s (likely network); retrying soon"
        # retry soon after a failed/stuck cycle so we catch intermittent connectivity
        failed = stuck or bool(META.get("error"))
        time.sleep(RETRY_INTERVAL if failed else POLL_INTERVAL)


# ---------- API ----------
@app.before_request
def _ensure_poller_running():
    # Guarantees the poller runs in the gunicorn worker that serves requests,
    # regardless of fork/preload timing. Idempotent and cheap.
    if not _poller_alive():
        start_poller()


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_ts(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _item_from_dbrow(r):
    return {
        "id": r["fixture_id"],
        "home": {"name": r["home_team"], "logo": None},
        "away": {"name": r["away_team"], "logo": None},
        "score": {"home": r["home_score"], "away": r["away_score"]},
        "minute": r["minute"], "status": r["status"],
        "league": r["league"], "country": r["country"], "league_logo": None,
        "coverage": r["coverage"],
        "dominance": _fnum(r["dominance_home"]), "momentum": _fnum(r["momentum_home"]),
        "updated_at": _iso_ts(r["scan_ts"]),
    }


_PCT_COLS = {"possession", "passes_pct"}


def _fmt_stat(col, v):
    """Render a DB NUMERIC back the way the live feed showed it: integers without a
    trailing .0, percentages with %, xG/decimals as-is."""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if col in _PCT_COLS:
        return f"{f:g}%"
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


def _detail_from_dbrow(r):
    item = _item_from_dbrow(r)
    sh, sa = {}, {}
    for col, rawname in core.FULL_STATS:
        sh[rawname] = _fmt_stat(col, r.get(f"home_{col}"))
        sa[rawname] = _fmt_stat(col, r.get(f"away_{col}"))
    item["stats_home"], item["stats_away"] = sh, sa
    item["odds"] = {"home": _fnum(r.get("odd_home")), "draw": _fnum(r.get("odd_draw")),
                    "away": _fnum(r.get("odd_away"))}
    return item


@app.route("/api/matches")
def api_matches():
    rows = db.latest_live()  # shared DB view: works regardless of which worker polled
    if rows is not None:
        items = [_item_from_dbrow(r) for r in rows]
        from collections import Counter
        cc = Counter(i["coverage"] for i in items)
        last = max((i["updated_at"] for i in items), default=None)
        meta = {"last_poll": last, "live_count": len(items), "source": "db",
                "counts": {"full": cc.get("full", 0), "partial": cc.get("partial", 0),
                           "checking": 0, "skipped": 0}}
    else:
        with _lock:
            items = [{
                "id": m["id"], "home": m["home"], "away": m["away"], "score": m["score"],
                "minute": m["minute"], "status": m["status"], "league": m["league"],
                "country": m["country"], "league_logo": m["league_logo"],
                "coverage": m["coverage"], "dominance": m["dominance"],
                "momentum": m["momentum"], "updated_at": m["updated_at"],
            } for m in STORE.values()]
        meta = META
    rank = {"full": 0, "partial": 1, "none": 2}
    items.sort(key=lambda x: (rank.get(x["coverage"], 3), -abs((x["dominance"] or 50) - 50)))
    return jsonify({"matches": items, "meta": meta, "interval": POLL_INTERVAL})


@app.route("/api/match/<int:fid>")
def api_match(fid):
    r = db.latest_one(fid)
    if r is not None:
        return jsonify(_detail_from_dbrow(r))
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
        "version": VERSION,
        "resolver_patched": socket.getaddrinfo is sofa_api._patched_getaddrinfo,
        "static_ips": sofa_api.STATIC_IPS,
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


@app.route("/api/alerts")
def api_alerts():
    return jsonify({**notify.STATUS,
                    "thresholds": {"min_minute": ALERT_MIN_MINUTE,
                                   "max_goal_margin": ALERT_MAX_MARGIN,
                                   "dominance": ALERT_DOM}})


@app.route("/api/netdiag")
def api_netdiag():
    """Time each network layer to api-sports.io to pinpoint where it hangs."""
    import ssl
    import time as _t
    import requests as _rq
    out = {}
    for ip in sofa_api.STATIC_IPS:
        t = _t.time()
        try:
            c = socket.create_connection((ip, 443), timeout=6)
            out[f"tcp {ip}"] = f"ok {round(_t.time()-t,2)}s"
            t2 = _t.time()
            ctx = ssl.create_default_context()
            ss = ctx.wrap_socket(c, server_hostname=sofa_api.API_HOST)
            out[f"tls {ip}"] = f"ok {round(_t.time()-t2,2)}s"
            ss.close()
        except Exception as e:  # noqa: BLE001
            out[f"tcp/tls {ip}"] = f"ERR {type(e).__name__}: {str(e)[:50]} ({round(_t.time()-t,2)}s)"
    t = _t.time()
    try:
        r = _rq.get("https://1.1.1.1/dns-query",
                    params={"name": sofa_api.API_HOST, "type": "A"},
                    headers={"Accept": "application/dns-json"}, timeout=(4, 5))
        out["doh 1.1.1.1"] = f"ok {r.status_code} {round(_t.time()-t,2)}s"
    except Exception as e:  # noqa: BLE001
        out["doh 1.1.1.1"] = f"ERR {type(e).__name__} ({round(_t.time()-t,2)}s)"
    t = _t.time()
    try:
        st = sofa_api.account_status(KEY)
        out["api /status"] = f"ok {round(_t.time()-t,2)}s plan={st.get('subscription',{}).get('plan')}"
    except Exception as e:  # noqa: BLE001
        out["api /status"] = f"ERR {type(e).__name__}: {str(e)[:70]} ({round(_t.time()-t,2)}s)"
    return jsonify(out)


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
