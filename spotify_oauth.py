"""Spotify Authorization Code flow, shared by the CLI helper and the UI login.

One place for the pieces every caller needs: the authorize URL, the code
exchange, the /me lookup that says whose account this is, and both ways the
redirect comes back (a local listener thread, and pasting the URL).

    python spotify_oauth.py --selfcheck

Env: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET.
The app's redirect URI in the Spotify dashboard must be exactly:
    http://127.0.0.1:8888/callback
"""

import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")
load_dotenv(ENV)

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = " ".join(
    (
        "user-read-recently-played",
        "user-top-read",
        "playlist-modify-private",
        # required to read any playlist's contents, even public ones
        "playlist-read-private",
        "user-follow-read",  # followed_artists
        "user-library-read",  # liked_songs, saved_albums, saved_podcasts
        "user-read-playback-state",  # now_playing
        "user-modify-playback-state",  # resume, pause, next_track, previous_track
    )
)
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
ME_URL = "https://api.spotify.com/v1/me"

# retries=3 because accounts.spotify.com intermittently drops the TLS handshake here
_http = httpx.Client(timeout=20, transport=httpx.HTTPTransport(retries=3))


def new_state() -> str:
    """A fresh CSRF guard; one per connect attempt, echoed back by Spotify."""
    return secrets.token_urlsafe(16)


def authorize_url(state: str, client_id: str | None = None) -> str:
    """The page the viewer approves on. client_id defaults to the .env one."""
    cid = client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")
    return AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": SCOPES,
            "state": state,
        }
    )


def exchange(
    code: str, *, client_id: str | None = None, client_secret: str | None = None
) -> dict:
    """An authorization code as {access_token, refresh_token, expires_in, scope}."""
    try:
        cid = client_id or os.environ["SPOTIFY_CLIENT_ID"]
        secret = client_secret or os.environ["SPOTIFY_CLIENT_SECRET"]
    except KeyError as e:
        raise RuntimeError(f"missing env var {e.args[0]}") from None
    r = _http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
        },
        auth=(cid, secret),
    )
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed ({r.status_code}): {r.text[:200]}")
    d = r.json()
    for field in ("access_token", "refresh_token"):
        if not d.get(field):
            raise ValueError(f"Spotify did not return {field}; connect again")
    missing = set(SCOPES.split()) - set(d.get("scope", "").split())
    if missing:
        raise ValueError(f"Spotify did not grant required scope(s): {', '.join(sorted(missing))}")
    return d


def me(access_token: str) -> dict:
    """Whose account this is. The id is the store's binding key."""
    r = _http.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        raise RuntimeError(f"/me failed ({r.status_code}): {r.text[:200]}")
    profile = r.json()
    if not profile.get("id"):
        raise ValueError("Spotify /me did not return an account id")
    return profile


def parse_paste_url(text: str) -> dict | None:
    """The redirected URL (or a bare query) as {code, state}, {error}, or None.

    On a cloud deploy the browser's redirect lands on the viewer's own
    machine and nothing is listening there, but the URL with the code is
    still in the address bar, so pasting it back is the transport.
    """
    text = text.strip()
    if text.startswith(("code=", "error=")):
        query = text
    else:
        url = urllib.parse.urlparse(text)
        expected = urllib.parse.urlparse(REDIRECT)
        if (url.scheme, url.netloc, url.path) != (
            expected.scheme, expected.netloc, expected.path
        ) or url.fragment:
            return None
        query = url.query
    got = urllib.parse.parse_qs(query, keep_blank_values=True)
    if any(len(values) != 1 for values in got.values()):
        return None
    fields = {key: values[0] for key, values in got.items()}
    if not fields.get("state") or not (fields.get("code") or fields.get("error")):
        return None
    return fields


def merge_env(path: str, values: dict[str, str]) -> None:
    """Rewrite path with values applied, keeping any other keys already there."""
    keep: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    keep[k.strip()] = v.strip()
    keep.update(values)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{k}={v}" for k, v in keep.items()) + "\n")


# The local listener is process-wide, but its responses are isolated by OAuth
# state. Polling one session must never consume another session's callback.
_lock = threading.Lock()
_captured: dict[str, dict] = {}
_pending: dict[str, float] = {}
_server: HTTPServer | None = None
STATE_TTL = 600


def register_state(state: str) -> None:
    """Register a single pending authorization and retire old attempts."""
    with _lock:
        now = time.monotonic()
        for old, created in list(_pending.items()):
            if now - created > STATE_TTL:
                _pending.pop(old, None)
                _captured.pop(old, None)
        _pending.setdefault(state, now)


def forget_state(state: str) -> None:
    with _lock:
        _pending.pop(state, None)
        _captured.pop(state, None)


class _Callback(BaseHTTPRequestHandler):
    def do_GET(self):
        got = parse_paste_url(f"{REDIRECT.removesuffix('/callback')}{self.path}")
        with _lock:
            created = _pending.get(got["state"]) if got else None
            valid = created is not None and time.monotonic() - created <= STATE_TTL
            if valid:
                _captured[got["state"]] = got
            elif got:
                _pending.pop(got["state"], None)
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h3>Done. Go back to the app.</h3>"
            if valid else b"<h3>Invalid or expired Spotify callback.</h3>"
        )

    def log_message(self, *args):
        pass


def start_listener() -> bool:
    """Listen on the redirect port. True when listening; False when it is taken."""
    global _server
    if _server is not None:
        return True
    try:
        srv = HTTPServer(("127.0.0.1", 8888), _Callback)
    except OSError:  # busy: get_token.py running, or another Streamlit process
        return False
    _server = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


def poll_listener(state: str) -> dict | None:
    """Return only this session's callback, once, without consuming another's."""
    with _lock:
        created = _pending.get(state)
        if created is None or time.monotonic() - created > STATE_TTL:
            _pending.pop(state, None)
            _captured.pop(state, None)
            return None
        return _captured.pop(state, None)


if __name__ == "__main__":
    import sys

    if "--selfcheck" not in sys.argv:
        raise SystemExit("nothing runs here; the UI and get_token.py drive this module")

    os.environ["SPOTIFY_CLIENT_ID"] = "selfcheck-id"
    state = new_state()
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(authorize_url(state)).query))
    assert q == {
        "client_id": "selfcheck-id",
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPES,
        "state": state,
    }, q
    assert new_state() != new_state(), "state must be per attempt"
    pasted = parse_paste_url(f"{REDIRECT}?code=abc&state={state}")
    assert pasted == {"code": "abc", "state": state}, pasted
    assert parse_paste_url(f"{REDIRECT}?error=access_denied&state={state}") == {
        "error": "access_denied", "state": state
    }
    assert parse_paste_url("code=abc&state=s") == {"code": "abc", "state": "s"}, "bare query"
    assert parse_paste_url("not a url at all") is None
    assert parse_paste_url("") is None
    # merge_env keeps unrelated keys, comments dropped, applied values win
    import tempfile

    p = os.path.join(tempfile.mkdtemp(), ".env")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# comment\nOTHER=keep\nSPOTIFY_CLIENT_ID=old\n")
    merge_env(p, {"SPOTIFY_CLIENT_ID": "new", "USERS_DB_KEY": "k"})
    got = dict(
        line.split("=", 1) for line in open(p, encoding="utf-8").read().splitlines()
    )
    assert got == {"OTHER": "keep", "SPOTIFY_CLIENT_ID": "new", "USERS_DB_KEY": "k"}, got
    print("ok")
