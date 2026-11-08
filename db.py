"""Supabase/Postgres archival writer for 5-minute scan snapshots.

One wide row per match per scan (matches with stats only). Every scan is stored
(regular 5-min grid for analysis) with a `stats_changed` flag marking the scans
where the feed actually moved. Writes are best-effort: a DB problem is recorded
in STATUS but never breaks the live dashboard.

Set DATABASE_URL (Supabase Transaction pooler URI) to enable. If unset, archival
is silently disabled.
"""
import os
from datetime import datetime, timezone

import core

try:
    import psycopg2
    from psycopg2.extras import execute_values
except Exception:  # driver not installed yet
    psycopg2 = None

DATABASE_URL = os.environ.get("DATABASE_URL")

STATUS = {
    "enabled": bool(DATABASE_URL),
    "driver": bool(psycopg2),
    "table_ready": False,
    "rows_written": 0,
    "last_write": None,
    "last_error": None,
}

# fixed columns, then home_<c> and away_<c> for every stat in core.FULL_STATS
_BASE_COLS = [
    "scan_ts", "fixture_id", "league", "country", "home_team", "away_team",
    "home_id", "away_id", "minute", "status", "home_score", "away_score",
    "coverage", "dominance_home", "momentum_home", "stats_changed",
]
_STAT_COLS = [f"home_{c}" for c, _ in core.FULL_STATS] + [f"away_{c}" for c, _ in core.FULL_STATS]
_ODDS_COLS = ["odd_home", "odd_draw", "odd_away"]  # live 1X2 (Fulltime Result)
ALL_COLS = _BASE_COLS + _STAT_COLS + _ODDS_COLS


def _connect():
    # Pass sslmode as a real connection parameter (psycopg2 merges it into the URI)
    # rather than string-appending to the URL — appending breaks when the password
    # contains characters like '?', which would corrupt the parsed database name.
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)


def _coltype(col):
    if col in ("scan_ts",):
        return "TIMESTAMPTZ"
    if col in ("fixture_id", "home_id", "away_id"):
        return "BIGINT"
    if col in ("minute", "home_score", "away_score"):
        return "INTEGER"
    if col in ("stats_changed",):
        return "BOOLEAN"
    if col in ("league", "country", "home_team", "away_team", "status", "coverage"):
        return "TEXT"
    return "NUMERIC"  # all stat columns + dominance/momentum


def init_db():
    """Create the snapshots table + indexes if needed."""
    if not (DATABASE_URL and psycopg2):
        STATUS["last_error"] = "DATABASE_URL or psycopg2 missing"
        return False
    cols_sql = ",\n  ".join(f"{c} {_coltype(c)}" for c in ALL_COLS)
    # ADD COLUMN IF NOT EXISTS makes this idempotent AND evolves an existing table
    # (e.g. adds the odds columns to a table created before they existed).
    alters = "\n".join(
        f"ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS {c} {_coltype(c)};" for c in ALL_COLS)
    ddl = f"""
    CREATE TABLE IF NOT EXISTS snapshots (
      id BIGSERIAL PRIMARY KEY,
      {cols_sql}
    );
    {alters}
    CREATE INDEX IF NOT EXISTS idx_snap_fixture_ts ON snapshots (fixture_id, scan_ts);
    CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots (scan_ts);
    CREATE INDEX IF NOT EXISTS idx_snap_league ON snapshots (league);
    """
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        STATUS["table_ready"] = True
        STATUS["last_error"] = None
        print("[db] snapshots table ready", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        STATUS["last_error"] = f"init: {e}"
        print(f"[db] init failed: {e}", flush=True)
        return False
    finally:
        if conn is not None:
            conn.close()


def row_from(entry, home_stats, away_stats, changed, odds=None):
    """Build an ordered row tuple matching ALL_COLS from a poller entry."""
    odds = odds or {}
    r = {
        "scan_ts": entry["updated_at"],
        "fixture_id": entry["id"],
        "league": entry["league"], "country": entry["country"],
        "home_team": entry["home"]["name"], "away_team": entry["away"]["name"],
        "home_id": entry.get("home_id"), "away_id": entry.get("away_id"),
        "minute": entry["minute"], "status": entry["status"],
        "home_score": entry["score"]["home"], "away_score": entry["score"]["away"],
        "coverage": entry["coverage"],
        "dominance_home": entry["dominance"], "momentum_home": entry["momentum"],
        "stats_changed": changed,
        "odd_home": odds.get("home"), "odd_draw": odds.get("draw"), "odd_away": odds.get("away"),
    }
    for c, _ in core.FULL_STATS:
        r[f"home_{c}"] = home_stats.get(c)
        r[f"away_{c}"] = away_stats.get(c)
    return tuple(r.get(c) for c in ALL_COLS)


def _query(sql, params=None):
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        STATUS["last_error"] = f"query: {e}"
        return None
    finally:
        if conn is not None:
            conn.close()


def latest_live(minutes=12):
    """Latest snapshot per fixture seen in the last `minutes` (i.e. currently live).
    Returns a list of column->value dicts, or None if the DB is unavailable."""
    if not (DATABASE_URL and psycopg2):
        return None
    sql = (f"SELECT DISTINCT ON (fixture_id) {', '.join(ALL_COLS)} "
           f"FROM snapshots WHERE scan_ts > now() - interval '{int(minutes)} minutes' "
           f"ORDER BY fixture_id, scan_ts DESC")
    rows = _query(sql)
    if rows is None:
        return None
    return [dict(zip(ALL_COLS, r)) for r in rows]


def latest_one(fixture_id):
    """Most recent snapshot for one fixture, as a column->value dict (or None)."""
    if not (DATABASE_URL and psycopg2):
        return None
    sql = (f"SELECT {', '.join(ALL_COLS)} FROM snapshots "
           f"WHERE fixture_id = %s ORDER BY scan_ts DESC LIMIT 1")
    rows = _query(sql, (fixture_id,))
    if not rows:
        return None
    return dict(zip(ALL_COLS, rows[0]))


def insert_rows(rows):
    """Bulk-insert snapshot rows. Best-effort; errors recorded, never raised."""
    if not rows or not (DATABASE_URL and psycopg2):
        return
    if not STATUS["table_ready"] and not init_db():
        return
    sql = f"INSERT INTO snapshots ({', '.join(ALL_COLS)}) VALUES %s"
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
        STATUS["rows_written"] += len(rows)
        STATUS["last_write"] = datetime.now(timezone.utc).isoformat()
        STATUS["last_error"] = None
    except Exception as e:  # noqa: BLE001
        STATUS["last_error"] = f"insert: {e}"
        print(f"[db] insert failed: {e}", flush=True)
    finally:
        if conn is not None:
            conn.close()
