"""Dominance + momentum computation from API-Football statistics blocks.

DOMINANCE = share from kickoff to now, computed on cumulative totals.
MOMENTUM  = share of the latest window, computed on deltas vs the previous snapshot.

Both use the same weight set. Each component is split between the two teams by
their share of that stat; the shares are then weighted. Weights sum to 1.0.
"""

# component -> (weight, invert?)  — invert means "more is worse" (fouls)
WEIGHTS = {
    "xg":            (0.20, False),
    "possession":    (0.20, False),
    "shots_on":      (0.10, False),
    "shots_off":     (0.07, False),
    "corners":       (0.20, False),
    "box_shots":     (0.05, False),
    "fouls":         (0.05, True),
    "passes":        (0.10, False),
    "blocked":       (0.015, False),
    "offsides":      (0.015, False),
}

# human labels for the UI
LABELS = {
    "xg": "Expected goals (xG)", "possession": "Possession", "shots_on": "Shots on target",
    "shots_off": "Shots off target", "corners": "Corners", "box_shots": "Shots in/out box",
    "fouls": "Fouls (negative)", "passes": "Total passes", "blocked": "Blocked shots",
    "offsides": "Offsides",
}

# raw API stat names shown verbatim in the two-team stats table
RAW_STATS = [
    "Ball Possession", "expected_goals", "Total Shots", "Shots on Goal", "Shots off Goal",
    "Blocked Shots", "Shots insidebox", "Shots outsidebox", "Corner Kicks", "Offsides",
    "Fouls", "Yellow Cards", "Red Cards", "Goalkeeper Saves", "Total passes",
    "Passes accurate", "Passes %",
]


def num(v):
    if v in (None, ""):
        return 0.0
    if isinstance(v, str) and v.endswith("%"):
        try:
            return float(v[:-1])
        except ValueError:
            return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def vals(block):
    """Reduce one team's statistics block to the model's component values."""
    d = {s["type"]: s["value"] for s in block.get("statistics", [])}
    return {
        "xg": num(d.get("expected_goals")),
        "possession": num(d.get("Ball Possession")),
        "shots_on": num(d.get("Shots on Goal")),
        "shots_off": num(d.get("Shots off Goal")),
        "corners": num(d.get("Corner Kicks")),
        "box_shots": num(d.get("Shots insidebox")) + num(d.get("Shots outsidebox")),
        "fouls": num(d.get("Fouls")),
        "passes": num(d.get("Total passes")),
        "blocked": num(d.get("Blocked Shots")),
        "offsides": num(d.get("Offsides")),
    }


def raw_table(block):
    """Verbatim stat name -> value for the stats table (missing shown as '-')."""
    d = {s["type"]: s["value"] for s in block.get("statistics", [])}
    out = {}
    for name in RAW_STATS:
        v = d.get(name)
        out[name] = "-" if v in (None, "") else v
    return out


# Full 17-stat set for archival: (db_column, raw API stat name)
FULL_STATS = [
    ("possession", "Ball Possession"), ("xg", "expected_goals"),
    ("shots_total", "Total Shots"), ("shots_on", "Shots on Goal"),
    ("shots_off", "Shots off Goal"), ("blocked", "Blocked Shots"),
    ("box_in", "Shots insidebox"), ("box_out", "Shots outsidebox"),
    ("corners", "Corner Kicks"), ("offsides", "Offsides"), ("fouls", "Fouls"),
    ("yellow", "Yellow Cards"), ("red", "Red Cards"), ("gk_saves", "Goalkeeper Saves"),
    ("passes", "Total passes"), ("passes_acc", "Passes accurate"),
    ("passes_pct", "Passes %"),
]


def full_stats(block):
    """All 17 stats as {db_column: numeric} for one team (missing -> 0)."""
    d = {s["type"]: s["value"] for s in block.get("statistics", [])}
    return {col: num(d.get(name)) for col, name in FULL_STATS}


def match_winner_odds(odds_response):
    """Extract live 1X2 (Fulltime Result) odds -> {home, draw, away} (None if absent)."""
    out = {"home": None, "draw": None, "away": None}
    if not odds_response:
        return out
    for bet in odds_response[0].get("odds", []):
        if bet.get("id") == 59 or str(bet.get("name", "")).lower() in ("fulltime result", "match winner"):
            for v in bet.get("values", []):
                k = str(v.get("value", "")).lower()
                if k in out and out[k] is None:
                    try:
                        out[k] = float(v.get("odd"))
                    except (TypeError, ValueError):
                        pass
            break
    return out


def _share(h, a):
    t = h + a
    return 0.5 if t <= 0 else h / t


def _score(home, away, prev=None):
    """Return (home_pct, breakdown). If prev given -> momentum (deltas)."""
    dom = 0.0
    breakdown = []
    for k, (w, inv) in WEIGHTS.items():
        if prev is not None:
            if k in ("possession", "passes"):
                # possession in delta-mode = share of passes gained this window
                dh = home["passes"] - prev[0]["passes"]
                da = away["passes"] - prev[1]["passes"]
            else:
                dh = max(home[k] - prev[0][k], 0)
                da = max(away[k] - prev[1][k], 0)
            s = _share(dh, da)
            hv, av = dh, da
        else:
            s = _share(home[k], away[k])
            hv, av = home[k], away[k]
        if inv:
            s = 1 - s
        dom += w * s
        breakdown.append({
            "key": k, "label": LABELS[k], "weight": round(w * 100, 1),
            "home": round(hv, 2), "away": round(av, 2), "home_share": round(s * 100),
        })
    return round(dom * 100), breakdown


def dominance(home_vals, away_vals):
    return _score(home_vals, away_vals)


def momentum(home_vals, away_vals, prev_home, prev_away):
    if prev_home is None or prev_away is None:
        return None, None
    return _score(home_vals, away_vals, prev=(prev_home, prev_away))
