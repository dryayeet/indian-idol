"""Sync data/users.db to a private GitHub gist, so a hosted deploy keeps profiles.

Streamlit Community Cloud containers are ephemeral: a reboot or redeploy wipes
the filesystem, and profiles stored only in data/users.db die with the
container. This module checkpoints the database (the refresh tokens inside it
are already Fernet-encrypted) and pushes it to a private gist after every
identity-relevant write, and restores it at startup when the local copy is
missing. Profile names, Spotify ids, and join dates are readable in the gist,
so it stays private and the token is scoped to gists alone.

Inactive unless DB_SYNC_GIST_ID and DB_SYNC_TOKEN are both set, so local
development and the CLIs never touch the network. The store is the only caller;
this module reads the database path from it, so a test that redirects store.DB
redirects the sync too.

    python db_sync.py --seed    # once: create the private gist from the local db
    python db_sync.py --check   # provisioning check: can the gist be read back?
"""

import argparse
import base64
import os
import sqlite3
import threading
import time

import httpx
from dotenv import load_dotenv

GIST_ID_VAR = "DB_SYNC_GIST_ID"
TOKEN_VAR = "DB_SYNC_TOKEN"
FILE_NAME = "users.db"
ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_API = "https://api.github.com/gists"
_THROTTLE_SECONDS = 60.0

load_dotenv(ENV)

_lock = threading.Lock()
_last_push = 0.0
_last_error: str | None = None


def enabled() -> bool:
    """True when both secrets are present; False leaves the app fully local."""
    return bool(os.environ.get(GIST_ID_VAR) and os.environ.get(TOKEN_VAR))


def status() -> str | None:
    """The last sync failure, for a quiet sidebar line; None when healthy."""
    return _last_error


def _db_path() -> str:
    # the store owns the path; importing lazily keeps the two modules loadable
    # in either order (store imports this module at its own top)
    import store

    return store.DB


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ[TOKEN_VAR]}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _checkpoint(path: str) -> None:
    """Fold the write-ahead log into the main file, so the bytes read from disk
    are the whole database and not all of it."""
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def restore() -> bool:
    """Pull the gist into the database when the local copy is missing.

    Returns True when a database was restored. Never raises: a failed sync
    means the app runs like an unsynced one, not that it stops.
    """
    global _last_error
    if not enabled():
        return False
    path = _db_path()
    if os.path.exists(path):
        return False
    try:
        with httpx.Client(timeout=15) as client:
            gist = client.get(f"{_API}/{os.environ[GIST_ID_VAR]}", headers=_headers())
            gist.raise_for_status()
        content = gist.json()["files"][FILE_NAME]["content"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(content))
        _last_error = None
        return True
    except Exception as exc:  # noqa: BLE001 - sync must never break the app
        _last_error = f"restore failed: {exc}"
        return False


def push(urgent: bool = True) -> bool:
    """Send the database to the gist; True when it was updated. Never raises.

    Urgent writes (a profile created or a binding changed) go out immediately;
    the throttle holds back the rest, so a `touch` on every login does not mean
    a request on every login.
    """
    global _last_push, _last_error
    if not enabled():
        return False
    with _lock:
        if not urgent and (time.monotonic() - _last_push) < _THROTTLE_SECONDS:
            return False
        path = _db_path()
        if not os.path.exists(path):
            return False
        try:
            _checkpoint(path)
            with open(path, "rb") as f:
                content = base64.b64encode(f.read()).decode()
            with httpx.Client(timeout=15) as client:
                response = client.patch(
                    f"{_API}/{os.environ[GIST_ID_VAR]}",
                    headers=_headers(),
                    json={"files": {FILE_NAME: {"content": content}}},
                )
                response.raise_for_status()
            _last_push = time.monotonic()
            _last_error = None
            return True
        except Exception as exc:  # noqa: BLE001 - sync must never break the app
            _last_error = f"push failed: {exc}"
            return False


def _seed() -> int:
    """One-time: create the private gist from the local database."""
    if not os.environ.get(TOKEN_VAR):
        print(f"set {TOKEN_VAR} first: a GitHub token scoped to gists")
        return 1
    path = _db_path()
    if not os.path.exists(path):
        print(f"no database at {path}; connect a profile first or copy one in")
        return 1
    _checkpoint(path)
    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    with httpx.Client(timeout=15) as client:
        response = client.post(
            _API,
            headers=_headers(),
            json={
                "description": "spotify-agent users.db sync (private)",
                "public": False,
                "files": {FILE_NAME: {"content": content}},
            },
        )
        response.raise_for_status()
        gist_id = response.json()["id"]
    print(f"created private gist {gist_id}")
    print(f"put {GIST_ID_VAR}={gist_id} in the same place as the other secrets")
    return 0


def _check() -> int:
    """Provisioning check: the gist exists and its payload decodes."""
    if not enabled():
        print(f"set {GIST_ID_VAR} and {TOKEN_VAR} first")
        return 1
    try:
        with httpx.Client(timeout=15) as client:
            gist = client.get(f"{_API}/{os.environ[GIST_ID_VAR]}", headers=_headers())
            gist.raise_for_status()
        content = gist.json()["files"][FILE_NAME]["content"]
        size = len(base64.b64decode(content))
        print(f"gist readable: {FILE_NAME} decodes to {size} bytes")
        return 0
    except Exception as exc:
        print(f"check failed: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sync users.db to a private gist")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--seed", action="store_true", help="create the gist from the local db")
    group.add_argument("--check", action="store_true", help="verify the gist reads back")
    args = parser.parse_args()
    if args.seed:
        raise SystemExit(_seed())
    if args.check:
        raise SystemExit(_check())
    parser.print_help()
