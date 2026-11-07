#!/usr/bin/env python3
"""
Momentum + dominance tracker for one live match (API-Football).

Every INTERVAL seconds it snapshots the match and prints/logs two bars:
  - MOMENTUM  : share of the last window (delta vs previous snapshot)
  - DOMINANCE : share from kickoff to now (cumulative totals)

Both use the same weight set (WEIGHTS below). Stops when the match finishes.

Usage:
  APISPORTS_KEY=... python3 momentum_track.py <fixture_id> [interval_seconds]
"""
import os, sys, time, collector
from datetime import datetime

LOG = "momentum_log.txt"

# component -> (weight, invert?)   weights sum to 1.0
WEIGHTS = {
    "xG":            (0.20, False),
    "Possession":    (0.20, False),
    "Shots on goal": (0.10, False),
    "Shots off goal":(0.07, False),
    "Corners":       (0.20, False),
    "Shots in/out box":(0.05, False),
    "Fouls (neg)":   (0.05, True),
    "Total passes":  (0.10, False),
    "Blocked shots": (0.015, False),
    "Offsides":      (0.015, False),
}

def num(v):
    if v in (None, ""): return 0.0
    if isinstance(v, str) and v.endswith("%"): return float(v[:-1])
    try: return float(v)
    except: return 0.0

def vals(block):
    d = {s["type"]: s["value"] for s in block["statistics"]}
    return {
        "xG": num(d.get("expected_goals")),
        "Possession": num(d.get("Ball Possession")),
        "Shots on goal": num(d.get("Shots on Goal")),
        "Shots off goal": num(d.get("Shots off Goal")),
        "Corners": num(d.get("Corner Kicks")),
        "Shots in/out box": num(d.get("Shots insidebox")) + num(d.get("Shots outsidebox")),
        "Fouls (neg)": num(d.get("Fouls")),
        "Total passes": num(d.get("Total passes")),
        "Blocked shots": num(d.get("Blocked Shots")),
        "Offsides": num(d.get("Offsides")),
    }

def share(hv, av):
    t = hv + av
    return 0.5 if t <= 0 else hv / t

def weighted(hvals, avals, delta_from=None):
    """Return home dominance fraction. If delta_from given, use deltas (momentum)."""
    dom = 0.0
    for k, (w, inv) in WEIGHTS.items():
        if delta_from is not None:
            if k in ("Possession", "Total passes"):
                # possession in delta mode = share of passes gained this window
                dh = hvals["Total passes"] - delta_from[0]["Total passes"]
                da = avals["Total passes"] - delta_from[1]["Total passes"]
            else:
                dh = max(hvals[k] - delta_from[0][k], 0)
                da = max(avals[k] - delta_from[1][k], 0)
            s = share(dh, da)
        else:
            s = share(hvals[k], avals[k])
        if inv: s = 1 - s
        dom += w * s
    return dom * 100

def bar(pct):
    b = int(round(pct / 5))
    return "#" * b + "-" * (20 - b)

def log(line):
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def fetch(key, fid):
    st = collector.get_statistics(key, fid)
    fx = [x for x in collector.get_live_fixtures(key) if x["fixture"]["id"] == fid]
    meta = fx[0] if fx else None
    if not st or not st[0].get("statistics"):
        return None, meta
    return (vals(st[0]), vals(st[1]), st[0]["team"]["name"], st[1]["team"]["name"]), meta

def main():
    key = os.environ["APISPORTS_KEY"]
    fid = int(sys.argv[1])
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    prev = None
    log(f"\n=== momentum tracking started {datetime.now():%H:%M:%S} | fixture {fid} | every {interval}s ===")
    ticks = 0
    while True:
        snap, meta = fetch(key, fid)
        status = (meta["fixture"]["status"]["short"] if meta else "?")
        el = (meta["fixture"]["status"]["elapsed"] if meta else "?")
        goals = (meta["goals"] if meta else {})
        ts = datetime.now().strftime("%H:%M:%S")
        if snap is None:
            log(f"[{ts}] @{el}' [{status}] no stats available yet")
        else:
            h, a, hn, an = snap
            dom = weighted(h, a)
            if prev is not None:
                mom = weighted(h, a, delta_from=(prev[0], prev[1]))
                mline = f"MOMENTUM  {hn} {mom:3.0f}% [{bar(mom)}] {100-mom:3.0f}% {an}"
            else:
                mline = "MOMENTUM  (baseline set — needs next tick)"
            log(f"[{ts}] @{el}' [{status}]  {hn} {goals.get('home')}-{goals.get('away')} {an}")
            log(f"          DOMINANCE {hn} {dom:3.0f}% [{bar(dom)}] {100-dom:3.0f}% {an}")
            log(f"          {mline}")
            prev = (h, a)
        ticks += 1
        if status in ("FT", "AET", "PEN"):
            log(f"[{ts}] match finished ({status}). tracking stopped after {ticks} ticks.")
            break
        if meta is None and ticks > 2:
            log(f"[{ts}] fixture no longer live. stopping.")
            break
        time.sleep(interval)

if __name__ == "__main__":
    main()
