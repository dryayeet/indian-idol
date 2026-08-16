"""Mint a Spotify refresh token and write it into .env. Run whenever the scopes change.

    python get_token.py

The app's redirect URI in the Spotify dashboard must be exactly:
    http://127.0.0.1:8888/callback
"""

import os
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = (
    "user-read-recently-played "
    "user-top-read "
    "playlist-modify-private "
    "playlist-read-private "  # needed to read any playlist's tracks, public ones included
    "user-follow-read "  # followed_artists
    "user-library-read "  # liked_songs, saved_albums, saved_podcasts
    "user-read-playback-state"  # now_playing
)
ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

result: dict[str, str] = {}


class _Callback(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        result.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h3>Done. Close this tab and go back to the terminal.</h3>")

    def log_message(self, *args):
        pass


def _merge_env(path: str, values: dict[str, str]) -> None:
    """Rewrite path with values applied, keeping any other keys already there."""
    keep = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                keep[k.strip()] = v.strip()
    keep.update(values)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{k}={v}" for k, v in keep.items()) + "\n")


def main() -> None:
    cid = os.environ.get("SPOTIFY_CLIENT_ID") or input("Client ID: ").strip()
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET") or input("Client secret: ").strip()
    state = secrets.token_urlsafe(16)

    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": SCOPES,
            "state": state,
        }
    )
    print("\nApprove access in the browser. If nothing opened, visit:\n" + url + "\n")
    webbrowser.open(url)

    server = HTTPServer(("127.0.0.1", 8888), _Callback)
    while not result:
        server.handle_request()  # ignores stray requests like /favicon.ico

    if "error" in result:
        raise SystemExit(f"Spotify said: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("state mismatch, someone else's redirect hit this port")

    # retries=3 because accounts.spotify.com intermittently drops the TLS handshake here
    client = httpx.Client(timeout=20, transport=httpx.HTTPTransport(retries=3))
    r = client.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT,
        },
        auth=(cid, secret),
    )
    if r.status_code != 200:
        raise SystemExit(f"token exchange failed ({r.status_code}): {r.text}")
    token = r.json()

    missing = set(SCOPES.split()) - set(token.get("scope", "").split())
    if missing:
        print(f"WARNING: scopes not granted: {' '.join(sorted(missing))}")

    _merge_env(
        ENV,
        {
            "SPOTIFY_CLIENT_ID": cid,
            "SPOTIFY_CLIENT_SECRET": secret,
            "SPOTIFY_REFRESH_TOKEN": token["refresh_token"],
        },
    )
    print(f"\nWrote the client id, secret, and refresh token to {ENV}")


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        import tempfile

        p = os.path.join(tempfile.mkdtemp(), ".env")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# comment\nOTHER=keep\nSPOTIFY_CLIENT_ID=old\n")
        _merge_env(p, {"SPOTIFY_CLIENT_ID": "new", "SPOTIFY_REFRESH_TOKEN": "tok"})
        got = dict(line.split("=", 1) for line in open(p, encoding="utf-8").read().splitlines())
        assert got == {"OTHER": "keep", "SPOTIFY_CLIENT_ID": "new", "SPOTIFY_REFRESH_TOKEN": "tok"}, got
        assert "playlist-read-private" in SCOPES
        print("ok")
    else:
        main()
