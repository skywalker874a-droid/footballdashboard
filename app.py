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

import html as _html
import socket

from flask import Flask, jsonify, redirect, request, send_from_directory, session

import auth
import core
import db
import notify
import sofa_api

VERSION = "v10-accounts"  # bump each deploy so /api/debug confirms what's live

# Account system (admin via env; viewing the dashboard requires a login)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
ADMIN_USER = os.environ.get("ADMIN_USER")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

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
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

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
    sh = entry.get("stats_home", {})
    sa = entry.get("stats_away", {})

    def g(k):
        return sh.get(k, "-"), sa.get(k, "-")

    poss, xg, ts, son, cor = g("Ball Possession"), g("expected_goals"), \
        g("Total Shots"), g("Shots on Goal"), g("Corner Kicks")
    stats_line = (f"Poss {poss[0]}-{poss[1]} · xG {xg[0]}-{xg[1]} · "
                  f"Shots {ts[0]}-{ts[1]} (on {son[0]}-{son[1]}) · Corners {cor[0]}-{cor[1]}")
    return (f"⚡ Dominance alert — {lead} on top\n"
            f"{h} {hs}-{aw} {a}   {entry['minute']}'  ({entry.get('league') or ''})\n"
            f"Dominance {round(pct)}% vs {round(100 - pct)}%\n"
            f"{stats_line}")


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


# ---------- auth ----------
@app.before_request
def _ensure_poller_running():
    # Guarantees the poller runs in the gunicorn worker that serves requests,
    # regardless of fork/preload timing. Idempotent and cheap.
    if not _poller_alive():
        start_poller()


@app.before_request
def _auth_gate():
    """Login required to view the dashboard and its data endpoints. Admin pages
    require the admin session. /login and /logout are public."""
    p = request.path
    # public: login flow + health check (Render pings /api/meta; it exposes only counts)
    if p in ("/login", "/logout", "/healthz", "/api/meta"):
        return
    if p.startswith("/admin"):
        if not session.get("is_admin"):
            return redirect("/login")
        return
    if not session.get("user"):
        if p.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect("/login")


def _page(title, body):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:radial-gradient(900px 500px at 50% -10%,#12203a,#0b1120 55%,#090d17);
    color:#eef2fb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}}
  .box{{width:100%;max-width:380px;background:#141c2e;border:1px solid #283454;border-radius:16px;
    padding:26px;box-shadow:0 10px 40px rgba(0,0,0,.5)}}
  .wide{{max-width:640px}}
  h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#8893b1;font-size:13px;margin:0 0 18px}}
  label{{display:block;font-size:12px;color:#8893b1;margin:12px 0 5px}}
  input{{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #283454;background:#0c1322;
    color:#eef2fb;font-size:14px;box-sizing:border-box}}
  input:focus{{outline:none;border-color:#2dd4bf}}
  button{{margin-top:18px;width:100%;padding:11px;border:0;border-radius:9px;cursor:pointer;
    background:linear-gradient(90deg,#0f9e8e,#2dd4bf);color:#042a26;font-weight:800;font-size:14px}}
  .err{{background:rgba(251,113,133,.14);color:#fda4af;border:1px solid #4a2230;border-radius:8px;
    padding:9px 11px;font-size:13px;margin-bottom:6px}}
  table{{width:100%;border-collapse:collapse;margin-top:6px}}
  td,th{{text-align:left;padding:9px 6px;border-bottom:1px solid #222e4a;font-size:13px}}
  th{{color:#8893b1;font-size:11px;text-transform:uppercase;letter-spacing:.4px}}
  .row2{{display:flex;gap:8px}} .row2 input{{flex:1}}
  .row2 button{{margin-top:0;width:auto;padding:11px 16px}}
  .del{{background:#2a1620;color:#fda4af;border:1px solid #4a2230;border-radius:7px;padding:5px 10px;
    cursor:pointer;font-size:12px;font-weight:700}}
  .top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}}
  a.logout{{color:#8893b1;font-size:13px;text-decoration:none}}
</style></head><body><div class="box {('wide' if title=='Admin' else '')}">{body}</div></body></html>"""


def _login_html(error=""):
    err = f'<div class="err">{_html.escape(error)}</div>' if error else ""
    return _page("Sign in", f"""
      <h1>Live Momentum</h1><p class="sub">Sign in to view the dashboard</p>
      {err}
      <form method="post" action="/login">
        <label>Username</label><input name="username" autofocus autocomplete="username">
        <label>Password</label><input name="password" type="password" autocomplete="current-password">
        <button type="submit">Sign in</button>
      </form>""")


def _admin_html(msg=""):
    users = auth.list_users()
    rows = "".join(
        f"<tr><td>{_html.escape(u['username'])}</td>"
        f"<td style='color:#8893b1'>{_html.escape((u['created'] or '')[:19])}</td>"
        f"<td style='text-align:right'><form method='post' action='/admin/delete' "
        f"onsubmit=\"return confirm('Delete {_html.escape(u['username'])}?')\" style='margin:0'>"
        f"<input type='hidden' name='username' value='{_html.escape(u['username'])}'>"
        f"<button class='del'>Delete</button></form></td></tr>"
        for u in users) or "<tr><td colspan='3' style='color:#8893b1'>No accounts yet.</td></tr>"
    note = f'<div class="err" style="background:rgba(45,212,191,.12);color:#5eead4;border-color:#1d4a44">{_html.escape(msg)}</div>' if msg else ""
    return _page("Admin", f"""
      <div class="top"><h1>Accounts</h1><a class="logout" href="/logout">Log out</a></div>
      <p class="sub">Create and remove accounts that can view the dashboard.</p>
      {note}
      <form method="post" action="/admin/create" class="row2">
        <input name="username" placeholder="username" autocomplete="off">
        <input name="password" placeholder="password" autocomplete="off">
        <button type="submit">Create</button>
      </form>
      <table><tr><th>Username</th><th>Created</th><th></th></tr>{rows}</table>""")


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        if ADMIN_USER and u == ADMIN_USER and pw == ADMIN_PASSWORD:
            session["user"] = u
            session["is_admin"] = True
            return redirect("/admin")
        if auth.verify(u, pw):
            session["user"] = u
            session["is_admin"] = False
            return redirect("/")
        return _login_html("Invalid username or password."), 401
    if session.get("is_admin"):
        return redirect("/admin")
    if session.get("user"):
        return redirect("/")
    return _login_html()


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/admin")
def admin_panel():
    return _admin_html()


@app.route("/admin/create", methods=["POST"])
def admin_create():
    ok, err = auth.create_user(request.form.get("username"), request.form.get("password"))
    return _admin_html("Account created." if ok else (err or "Could not create account."))


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    auth.delete_user((request.form.get("username") or "").strip())
    return _admin_html("Account deleted.")


# ---------- API ----------


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
