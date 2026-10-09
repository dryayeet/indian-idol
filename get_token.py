"""Mint a Spotify refresh token and write it into .env. Run whenever the scopes change.

    python get_token.py

The app's redirect URI in the Spotify dashboard must be exactly:
    http://127.0.0.1:8888/callback

The flow pieces (authorize URL, code exchange, scope set, .env merge) live in
spotify_oauth.py, which the UI login also drives.
"""

import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import spotify_oauth
from dotenv import load_dotenv

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV)

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


def main() -> None:
    cid = os.environ.get("SPOTIFY_CLIENT_ID") or input("Client ID: ").strip()
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET") or input("Client secret: ").strip()
    state = spotify_oauth.new_state()

    url = spotify_oauth.authorize_url(state, cid)
    print("\nApprove access in the browser. If nothing opened, visit:\n" + url + "\n")
    webbrowser.open(url)

    server = HTTPServer(("127.0.0.1", 8888), _Callback)
    while not result:
        server.handle_request()  # ignores stray requests like /favicon.ico

    if "error" in result:
        raise SystemExit(f"Spotify said: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("state mismatch, someone else's redirect hit this port")

    token = spotify_oauth.exchange(result["code"], client_id=cid, client_secret=secret)

    spotify_oauth.merge_env(
        ENV,
        {
            "SPOTIFY_CLIENT_ID": cid,
            "SPOTIFY_CLIENT_SECRET": secret,
            "SPOTIFY_REFRESH_TOKEN": token["refresh_token"],
        },
    )
    print(f"\nWrote the client id, secret, and refresh token to {ENV}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import tempfile

        p = os.path.join(tempfile.mkdtemp(), ".env")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# comment\nOTHER=keep\nSPOTIFY_CLIENT_ID=old\n")
        spotify_oauth.merge_env(p, {"SPOTIFY_CLIENT_ID": "new", "SPOTIFY_REFRESH_TOKEN": "tok"})
        got = dict(line.split("=", 1) for line in open(p, encoding="utf-8").read().splitlines())
        assert got == {"OTHER": "keep", "SPOTIFY_CLIENT_ID": "new", "SPOTIFY_REFRESH_TOKEN": "tok"}, got
        assert "playlist-read-private" in spotify_oauth.SCOPES
        print("ok")
    else:
        main()
