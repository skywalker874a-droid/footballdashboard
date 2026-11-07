# Live Football Momentum

A web/mobile dashboard for live football matches. The main page lists every live
match (that has stats) with a **Dominance** bar; tap one to see **Dominance +
Momentum** and the two teams' **live stats**. Everything refreshes on a
5-minute poll. Data source: **API-Football** (api-sports.io).

## What it shows

- **Dominance** — who's been on top across the *whole match* (cumulative stats).
- **Momentum** — who's on top in the *last 5-minute window* (change since the
  previous poll).
- **Live stats** — the raw per-team numbers (possession, xG, shots, passes, …).

Only the latest reading per match is kept; a match disappears from the list once
it's no longer live. No archival yet (planned later).

## Setup

```bash
pip install -r requirements.txt
export APISPORTS_KEY=your_api_football_key
python3 app.py
```

Open **http://localhost:8000** (works on phone browsers too — responsive).

### Config (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `APISPORTS_KEY` | — | your API-Football key (required) |
| `POLL_INTERVAL` | `300` | seconds between polls (300 = 5 min) |
| `PORT` | `8000` | web server port |

## The model

Dominance and momentum both use the same weighted scheme (`core.py` → `WEIGHTS`):

| Component | Weight |
|---|---:|
| xG | 20% |
| Possession | 20% |
| Corners | 20% |
| Total passes | 10% |
| Shots on target | 10% |
| Shots off target | 7% |
| Fouls *(negative)* | 5% |
| Shots in/out box | 5% |
| Blocked shots | 1.5% |
| Offsides | 1.5% |

Each component is split between the two teams by their share of that stat (fouls
inverted — fewer is better); the shares are weighted and summed to a 0–100% split.
**Dominance** applies this to cumulative totals; **momentum** applies it to the
deltas since the previous poll. Edit `WEIGHTS` to retune — both bars update together.

## Files

- `app.py` — Flask server + background 5-min poller + JSON API
- `core.py` — weights + dominance/momentum math
- `sofa_api.py` — API-Football client (stdlib only)
- `static/index.html` — responsive UI (list + detail)

## Request budget

Each poll costs `1 + (live matches with stats)` requests. At 5-min intervals a
busy weekend (~hundreds live) runs into the tens of thousands/day — sized for the
**Ultra** plan. Raise `POLL_INTERVAL` to 600 to roughly halve it (the feed only
refreshes every ~10 min anyway).
