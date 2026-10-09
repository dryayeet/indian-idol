"""Light user store: display names and encrypted Spotify bindings.

Identity is the generated user_id, never the name: two people can be called Sam,
and Spotify auth binds by spotify_user_id. The database is data/users.db
(gitignored). Refresh tokens are stored encrypted (Fernet, key USERS_DB_KEY in
.env), so every read and write goes through this module alone.

    python -c "import store; print(store.find_users('lemon'))"
"""

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "users.db")
KEY_VAR = "USERS_DB_KEY"
ENV = os.path.join(HERE, ".env")
load_dotenv(ENV)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  spotify_user_id TEXT UNIQUE,
  created_at      TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,
  refresh_enc     TEXT,
  scopes          TEXT,
  spotify_name    TEXT,
  auth_at         TEXT
)
"""
# CREATE IF NOT EXISTS will not extend a table made before these columns, so
# ALTER them in; PRAGMA table_info says which are already there
_ADD = (
    ("refresh_enc", "TEXT"),
    ("scopes", "TEXT"),
    ("spotify_name", "TEXT"),
    ("auth_at", "TEXT"),
)
_PUBLIC = (
    "id, display_name, spotify_user_id, created_at, last_seen_at, spotify_name, "
    "CASE WHEN refresh_enc IS NOT NULL THEN 1 ELSE 0 END AS connected"
)


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
    have = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
    for col, decl in _ADD:
        if col not in have:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
    return con


def _fernet() -> Fernet:
    """The store's cipher. The key is USERS_DB_KEY, generated into .env on first
    use so the database and key stay a pair across machines."""
    key = os.environ.get(KEY_VAR)
    if not key:
        from spotify_oauth import merge_env

        key = Fernet.generate_key().decode()
        merge_env(ENV, {KEY_VAR: key})
        os.environ[KEY_VAR] = key
    return Fernet(key.encode())


def find_users(name: str) -> list[dict]:
    """Every profile using this display name, case-insensitive, oldest first."""
    with closing(_connect()) as con:
        rows = con.execute(
            f"SELECT {_PUBLIC} FROM users WHERE lower(display_name) = lower(?) "
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
        "refresh_enc": None,
        "scopes": None,
        "spotify_name": None,
        "auth_at": None,
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
        row = con.execute(f"SELECT {_PUBLIC} FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def touch(user_id: str) -> None:
    """Mark a profile as just seen, so the confirmation card can say when."""
    with closing(_connect()) as con:
        con.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (_now(), user_id))


def link_spotify(user_id: str, spotify_user_id: str) -> None:
    """Bind a profile to one Spotify account. The token is written by
    save_spotify_auth; this is only the account id half of the binding."""
    with closing(_connect()) as con:
        con.execute(
            "UPDATE users SET spotify_user_id = ? WHERE id = ?",
            (spotify_user_id, user_id),
        )


def save_spotify_auth(
    user_id: str,
    refresh_token: str,
    *,
    scopes: str,
    spotify_user_id: str,
    spotify_name: str,
) -> None:
    """Bind the profile to one Spotify account, token encrypted at rest here
    and decrypted by get_spotify_auth nowhere else."""
    enc = _fernet().encrypt(refresh_token.encode()).decode()
    with closing(_connect()) as con:
        con.execute(
            "UPDATE users SET refresh_enc = ?, scopes = ?, spotify_user_id = ?, "
            "spotify_name = ?, auth_at = ? WHERE id = ?",
            (enc, scopes, spotify_user_id, spotify_name, _now(), user_id),
        )


def create_spotify_user(
    name: str, refresh_token: str, *, spotify_user_id: str, spotify_name: str, scopes: str
) -> tuple[dict, bool]:
    """Create and bind a new profile in one transaction.

    Another viewer can connect the same Spotify account concurrently. Return
    its existing profile without overwriting its token or leaving an unbound
    name behind. The bool says whether a new profile was created.
    """
    clean = " ".join(name.split())
    if not clean or not spotify_user_id or not refresh_token:
        raise ValueError("name, Spotify id, and refresh token are required")
    enc = _fernet().encrypt(refresh_token.encode()).decode()
    now = _now()
    with closing(_connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            existing = con.execute(
                f"SELECT {_PUBLIC} FROM users WHERE spotify_user_id = ?", (spotify_user_id,)
            ).fetchone()
            if existing:
                con.execute("COMMIT")
                return dict(existing), False
            user_id = uuid.uuid4().hex
            con.execute(
                "INSERT INTO users (id, display_name, spotify_user_id, created_at, "
                "last_seen_at, refresh_enc, scopes, spotify_name, auth_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, clean, spotify_user_id, now, now, enc, scopes, spotify_name, now),
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return get_user(user_id), True


def get_spotify_auth(user_id: str) -> dict | None:
    """The binding as plaintext, or None when the profile has no token."""
    with closing(_connect()) as con:
        row = con.execute(
            "SELECT refresh_enc, scopes, spotify_user_id, spotify_name, auth_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row or not row["refresh_enc"]:
        return None
    try:
        token = _fernet().decrypt(row["refresh_enc"].encode()).decode()
    except InvalidToken:
        raise RuntimeError(
            f"the stored token will not decrypt under {KEY_VAR}; rotate the key "
            "and reconnect the account"
        ) from None
    return {
        "refresh_token": token,
        "scopes": row["scopes"],
        "spotify_user_id": row["spotify_user_id"],
        "spotify_name": row["spotify_name"],
        "auth_at": row["auth_at"],
    }


def find_by_spotify_id(spotify_user_id: str) -> dict | None:
    """The profile bound to this Spotify account, if any. Spotify ids are the
    strong identity, so a bind collision resolves through here."""
    with closing(_connect()) as con:
        row = con.execute(
            f"SELECT {_PUBLIC} FROM users WHERE spotify_user_id = ?", (spotify_user_id,)
        ).fetchone()
    return dict(row) if row else None
