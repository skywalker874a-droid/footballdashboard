"""Account store + password hashing for dashboard access.

Accounts are created/deleted only by the admin (no self-registration). Stored in
Postgres (shared across workers) when DATABASE_URL is set; otherwise a local
SQLite file is used so the app works in local dev without the production DB.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash, check_password_hash

import db

_SQLITE_PATH = os.environ.get("AUTH_DB", "auth_users.db")
_ready = False
_lock = threading.Lock()


def _pg():
    return bool(db.DATABASE_URL and db.psycopg2)


def ensure_table():
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        try:
            if _pg():
                conn = db._connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute("CREATE TABLE IF NOT EXISTS users ("
                                    "username TEXT PRIMARY KEY, pwhash TEXT NOT NULL, "
                                    "created TIMESTAMPTZ DEFAULT now())")
                    conn.commit()
                finally:
                    conn.close()
            else:
                c = sqlite3.connect(_SQLITE_PATH)
                try:
                    c.execute("CREATE TABLE IF NOT EXISTS users ("
                              "username TEXT PRIMARY KEY, pwhash TEXT NOT NULL, created TEXT)")
                    c.commit()
                finally:
                    c.close()
            _ready = True
        except Exception as e:  # noqa: BLE001
            print(f"[auth] ensure_table failed: {e}", flush=True)


def create_user(username, password):
    username = (username or "").strip()
    if not username or not password:
        return False, "Username and password are required."
    ensure_table()
    h = generate_password_hash(password, method="pbkdf2:sha256")
    try:
        if _pg():
            conn = db._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO users (username, pwhash, created) "
                                "VALUES (%s, %s, now()) ON CONFLICT (username) DO NOTHING "
                                "RETURNING username", (username, h))
                    ok = cur.fetchone() is not None
                conn.commit()
            finally:
                conn.close()
        else:
            c = sqlite3.connect(_SQLITE_PATH)
            try:
                cur = c.execute("INSERT OR IGNORE INTO users (username, pwhash, created) "
                                "VALUES (?, ?, ?)",
                                (username, h, datetime.now(timezone.utc).isoformat()))
                ok = cur.rowcount > 0
                c.commit()
            finally:
                c.close()
        return (True, None) if ok else (False, "That username already exists.")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def delete_user(username):
    ensure_table()
    try:
        if _pg():
            conn = db._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM users WHERE username = %s", (username,))
                conn.commit()
            finally:
                conn.close()
        else:
            c = sqlite3.connect(_SQLITE_PATH)
            try:
                c.execute("DELETE FROM users WHERE username = ?", (username,))
                c.commit()
            finally:
                c.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def list_users():
    ensure_table()
    try:
        if _pg():
            conn = db._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT username, created FROM users ORDER BY username")
                    rows = cur.fetchall()
            finally:
                conn.close()
        else:
            c = sqlite3.connect(_SQLITE_PATH)
            try:
                rows = c.execute("SELECT username, created FROM users ORDER BY username").fetchall()
            finally:
                c.close()
        return [{"username": r[0], "created": str(r[1]) if r[1] else None} for r in rows]
    except Exception:  # noqa: BLE001
        return []


def verify(username, password):
    ensure_table()
    try:
        if _pg():
            conn = db._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pwhash FROM users WHERE username = %s", (username,))
                    r = cur.fetchone()
            finally:
                conn.close()
        else:
            c = sqlite3.connect(_SQLITE_PATH)
            try:
                r = c.execute("SELECT pwhash FROM users WHERE username = ?", (username,)).fetchone()
            finally:
                c.close()
        return bool(r and check_password_hash(r[0], password))
    except Exception:  # noqa: BLE001
        return False
