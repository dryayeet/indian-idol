"""Light user store: display names now, Spotify bindings later.

Identity is the generated user_id, never the name: two people can be called Sam,
and when Spotify auth lands it will bind by spotify_user_id. The database is
data/users.db (gitignored). When tokens arrive they will be stored encrypted, so
every read and write goes through this module alone.

    python -c "import store; print(store.find_users('lemon'))"
"""

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "users.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  spotify_user_id TEXT UNIQUE,
  created_at      TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    """A fresh autocommit connection; SQLite is cheap to open and this keeps
    concurrent Streamlit sessions from sharing a connection across threads."""
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(_SCHEMA)
    return con


def find_users(name: str) -> list[dict]:
    """Every profile using this display name, case-insensitive, oldest first."""
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT * FROM users WHERE lower(display_name) = lower(?) "
            "ORDER BY created_at, rowid",
            (name.strip(),),
        ).fetchall()
    return [dict(row) for row in rows]


def create_user(name: str) -> dict:
    clean = " ".join(name.split())
    if not clean:
        raise ValueError("a display name is required")
    user = {
        "id": uuid.uuid4().hex,
        "display_name": clean,
        "spotify_user_id": None,
        "created_at": _now(),
        "last_seen_at": _now(),
    }
    with closing(_connect()) as con:
        con.execute(
            "INSERT INTO users (id, display_name, spotify_user_id, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user["id"], user["display_name"], None, user["created_at"], user["last_seen_at"]),
        )
    return user


def get_user(user_id: str) -> dict | None:
    with closing(_connect()) as con:
        row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def touch(user_id: str) -> None:
    """Mark a profile as just seen, so the confirmation card can say when."""
    with closing(_connect()) as con:
        con.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (_now(), user_id))


def link_spotify(user_id: str, spotify_user_id: str) -> None:
    """Bind a profile to one Spotify account. When token storage arrives, the
    encrypted tokens are written through this seam and nowhere else."""
    with closing(_connect()) as con:
        con.execute(
            "UPDATE users SET spotify_user_id = ? WHERE id = ?",
            (spotify_user_id, user_id),
        )
