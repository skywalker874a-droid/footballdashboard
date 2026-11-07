#!/usr/bin/env python3
"""
Live football statistics collector (API-Football / api-sports.io).

Polls all in-play matches, pulls the per-match statistics panel
(ball possession, shots, corners, passes, etc.) and appends every
snapshot to a SQLite database and a CSV file.

Usage:
    export APISPORTS_KEY=your_key_here
    python3 collector.py                  # poll every 60s, all live matches
    python3 collector.py --interval 30    # custom poll interval (seconds)
    python3 collector.py --once           # single pass then exit
    python3 collector.py --league 39      # only this league id (e.g. 39 = Premier League)

Get a free key at https://dashboard.api-football.com (free tier: 100 requests/day).
Each poll uses 1 request for the live list + 1 request per live match.
"""

import argparse
import csv
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_HOST = "v3.football.api-sports.io"
BASE_URL = f"https://{API_HOST}"
DB_PATH = "stats.db"
CSV_PATH = "stats.csv"

# Statistic names API-Football returns, mapped to tidy column names.
STAT_FIELDS = {
    "Ball Possession": "possession",
    "Total Shots": "total_shots",
    "Shots on Goal": "shots_on_target",
    "Shots off Goal": "shots_off_target",
    "Blocked Shots": "blocked_shots",
    "Shots insidebox": "shots_insidebox",
    "Shots outsidebox": "shots_outsidebox",
    "Corner Kicks": "corners",
    "Offsides": "offsides",
    "Fouls": "fouls",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
    "Goalkeeper Saves": "gk_saves",
    "Total passes": "passes",
    "Passes accurate": "passes_accurate",
    "Passes %": "passes_pct",
    "expected_goals": "xg",
}

_running = True


def _stop(*_):
    global _running
    _running = False
    print("\nStopping after current cycle...", file=sys.stderr)


def api_get(path, params, key):
    """GET a JSON endpoint. Returns parsed dict or raises."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE_URL}{path}?{qs}" if qs else f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"x-apisports-key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    errs = data.get("errors")
    if errs:
        # API-Football returns errors as {} when none, or a dict/list with messages.
        if isinstance(errs, dict) and errs:
            raise RuntimeError(f"API error: {errs}")
        if isinstance(errs, list) and errs:
            raise RuntimeError(f"API error: {errs}")
    return data


def get_live_fixtures(key, league=None):
    params = {"live": "all"}
    if league:
        params = {"live": "all", "league": league}
    data = api_get("/fixtures", params, key)
    return data.get("response", [])


def get_statistics(key, fixture_id, retries=1, pause=8):
    """Fetch stats for one fixture; retry once on 429 (rate limit)."""
    for attempt in range(retries + 1):
        try:
            data = api_get("/fixtures/statistics", {"fixture": fixture_id}, key)
            return data.get("response", [])
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(pause)
                continue
            raise


def parse_stats(stats_response):
    """stats_response is a 2-element list (home team block, away team block)."""
    out = {}
    for block in stats_response:
        team = block.get("team", {})
        side = None  # determined by caller via order; we key by team id
        parsed = {}
        for item in block.get("statistics", []):
            name = item.get("type")
            val = item.get("value")
            col = STAT_FIELDS.get(name)
            if col:
                parsed[col] = val
        out[team.get("id")] = {"team_name": team.get("name"), "stats": parsed}
    return out


def init_db(conn):
    cols = ", ".join(f"{c} TEXT" for c in STAT_FIELDS.values())
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS stats (
            ts TEXT,
            fixture_id INTEGER,
            minute INTEGER,
            status TEXT,
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            home_score INTEGER,
            away_score INTEGER,
            side TEXT,
            team_name TEXT,
            {cols}
        )
        """
    )
    conn.commit()


def csv_header():
    return (
        ["ts", "fixture_id", "minute", "status", "league", "home_team",
         "away_team", "home_score", "away_score", "side", "team_name"]
        + list(STAT_FIELDS.values())
    )


def ensure_csv(path):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(csv_header())


def record_rows(conn, csv_path, rows):
    header = csv_header()
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([r.get(c, "") for c in header])
    placeholders = ", ".join("?" for _ in header)
    conn.executemany(
        f"INSERT INTO stats ({', '.join(header)}) VALUES ({placeholders})",
        [[r.get(c) for c in header] for r in rows],
    )
    conn.commit()


def poll_once(conn, key, league=None, max_matches=None, req_delay=0.0):
    ts = datetime.now(timezone.utc).isoformat()
    fixtures = get_live_fixtures(key, league)
    total_live = len(fixtures)
    if max_matches:
        fixtures = fixtures[:max_matches]
    print(f"[{ts}] {total_live} live match(es)"
          + (f", collecting {len(fixtures)}" if max_matches else ""))
    all_rows = []
    for i, fx in enumerate(fixtures):
        if req_delay and i > 0:
            time.sleep(req_delay)
        fixture = fx.get("fixture", {})
        fid = fixture.get("id")
        teams = fx.get("teams", {})
        goals = fx.get("goals", {})
        league_name = fx.get("league", {}).get("name")
        home = teams.get("home", {})
        away = teams.get("away", {})
        minute = (fixture.get("status") or {}).get("elapsed")
        status = (fixture.get("status") or {}).get("short")

        try:
            stats_resp = get_statistics(key, fid)
        except urllib.error.HTTPError as e:
            print(f"  fixture {fid}: HTTP {e.code}", file=sys.stderr)
            continue
        parsed = parse_stats(stats_resp)

        base = {
            "ts": ts, "fixture_id": fid, "minute": minute, "status": status,
            "league": league_name, "home_team": home.get("name"),
            "away_team": away.get("name"), "home_score": goals.get("home"),
            "away_score": goals.get("away"),
        }
        for side, team in (("home", home), ("away", away)):
            tid = team.get("id")
            pdata = parsed.get(tid, {})
            row = dict(base)
            row["side"] = side
            row["team_name"] = pdata.get("team_name", team.get("name"))
            row.update(pdata.get("stats", {}))
            all_rows.append(row)

        hp = parsed.get(home.get("id"), {}).get("stats", {}).get("possession", "?")
        print(f"  {home.get('name')} {goals.get('home')}-{goals.get('away')} "
              f"{away.get('name')}  [{minute}'] poss {hp}")

    if all_rows:
        record_rows(conn, CSV_PATH, all_rows)
        print(f"  saved {len(all_rows)} rows -> {DB_PATH}, {CSV_PATH}")
    return len(fixtures)


def main():
    ap = argparse.ArgumentParser(description="Live football stats collector (API-Football)")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls")
    ap.add_argument("--once", action="store_true", help="single pass then exit")
    ap.add_argument("--league", type=int, default=None, help="restrict to a league id")
    ap.add_argument("--max-matches", type=int, default=None,
                    help="only collect the first N live matches per poll (saves requests)")
    ap.add_argument("--req-delay", type=float, default=7.0,
                    help="seconds between per-match requests (free tier allows ~10/min; default 7)")
    args = ap.parse_args()

    key = os.environ.get("APISPORTS_KEY")
    if not key:
        print("ERROR: set APISPORTS_KEY environment variable with your api-sports.io key.",
              file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    ensure_csv(CSV_PATH)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.once:
        poll_once(conn, key, args.league, args.max_matches, args.req_delay)
        conn.close()
        return

    print(f"Polling every {args.interval}s. Ctrl-C to stop.")
    while _running:
        try:
            poll_once(conn, key, args.league, args.max_matches, args.req_delay)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            print(f"HTTP {e.code}: {body}", file=sys.stderr)
            if e.code in (429, 499):
                print("Rate limited — backing off 120s.", file=sys.stderr)
                time.sleep(120)
        except Exception as e:  # noqa: BLE001
            print(f"Error: {e}", file=sys.stderr)
        # Interruptible sleep
        for _ in range(args.interval):
            if not _running:
                break
            time.sleep(1)

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
