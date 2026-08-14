"""Spotify MCP server: recently played, top tracks, lyrics, mood search, playlist create.

Env: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
Scopes the refresh token needs:
    user-read-recently-played user-top-read playlist-modify-private

Run:  python spotify_mcp.py           (stdio)
Check: python spotify_mcp.py --selfcheck
"""

import os
import time

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

API = "https://api.spotify.com/v1"
app = MCPServer("spotify")
_tok = {"value": None, "expires": 0.0}
# retries=3 because accounts.spotify.com intermittently drops the TLS handshake here
_http = httpx.Client(timeout=30, transport=httpx.HTTPTransport(retries=3))


def _token() -> str:
    if _tok["value"] and time.time() < _tok["expires"] - 60:
        return _tok["value"]
    try:
        cid, secret, refresh = (
            os.environ["SPOTIFY_CLIENT_ID"],
            os.environ["SPOTIFY_CLIENT_SECRET"],
            os.environ["SPOTIFY_REFRESH_TOKEN"],
        )
    except KeyError as e:
        raise RuntimeError(f"missing env var {e.args[0]}") from None
    r = _http.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh},
        auth=(cid, secret),
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    _tok.update(value=d["access_token"], expires=time.time() + d["expires_in"])
    return _tok["value"]


def _call(method: str, path: str, **kw) -> dict:
    r = _http.request(method, API + path, headers={"Authorization": f"Bearer {_token()}"}, **kw)
    if r.status_code == 429:
        raise RuntimeError(f"rate limited, retry after {r.headers.get('Retry-After', '?')}s")
    r.raise_for_status()
    return r.json() if r.content else {}


def _track(t: dict) -> dict:
    return {
        "name": t["name"],
        "artist": ", ".join(a["name"] for a in t["artists"]),
        "uri": t["uri"],
        "id": t["id"],
    }


@app.tool()
def recently_played(limit: int = 50) -> list[dict]:
    """Recently played tracks, newest first (max 50)."""
    items = _call("GET", "/me/player/recently-played", params={"limit": min(limit, 50)})["items"]
    return [_track(i["track"]) | {"played_at": i["played_at"]} for i in items]


@app.tool()
def top_tracks(limit: int = 20, time_range: str = "short_term") -> list[dict]:
    """Most-played tracks. time_range: short_term (~4 weeks), medium_term, long_term."""
    params = {"limit": min(limit, 50), "time_range": time_range}
    return [_track(t) for t in _call("GET", "/me/top/tracks", params=params)["items"]]


@app.tool()
def get_lyrics(track: str, artist: str) -> str:
    """Plain lyrics for a track from LRCLIB. Empty string if not found or instrumental."""
    r = _http.get(
        "https://lrclib.net/api/get",
        params={"track_name": track, "artist_name": artist},
        headers={"User-Agent": "spotify-agent (https://github.com/)"},
    )
    if r.status_code == 404:
        return ""
    r.raise_for_status()
    return r.json().get("plainLyrics") or ""


def _feel_query(valence: float, energy: float, acousticness: float, extra: str) -> str:
    words = []
    if valence < 0.35:
        words.append("sad melancholy")
    elif valence > 0.65:
        words.append("happy upbeat")
    if energy < 0.35:
        words.append("calm mellow")
    elif energy > 0.65:
        words.append("energetic intense")
    if acousticness > 0.6:
        words.append("acoustic")
    if extra:
        words.append(extra)
    return " ".join(words)


@app.tool()
def search_by_feel(
    valence: float = 0.5,
    energy: float = 0.5,
    acousticness: float = 0.5,
    extra_query: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search tracks by affective targets in [0,1] plus optional free text (genre, year, artist)."""
    # ponytail: keyword search, not /v1/recommendations + target_valence — that endpoint and
    # /v1/audio-features were deprecated for new apps on 2024-11-27 and return 403. Swap back
    # to real feature targeting only if this app gets extended-mode access.
    q = _feel_query(valence, energy, acousticness, extra_query) or "music"
    params = {"q": q, "type": "track", "limit": min(limit, 50)}
    return [_track(t) for t in _call("GET", "/search", params=params)["tracks"]["items"]]


@app.tool()
def create_playlist(name: str, track_uris: list[str], description: str = "") -> dict:
    """Create a private playlist for the current user and add tracks. Returns its URL."""
    user = _call("GET", "/me")["id"]
    pl = _call(
        "POST",
        f"/users/{user}/playlists",
        json={"name": name, "description": description, "public": False},
    )
    for i in range(0, len(track_uris), 100):
        _call("POST", f"/playlists/{pl['id']}/tracks", json={"uris": track_uris[i : i + 100]})
    return {"id": pl["id"], "url": pl["external_urls"]["spotify"], "added": len(track_uris)}


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        assert _feel_query(0.2, 0.2, 0.9, "") == "sad melancholy calm mellow acoustic"
        assert _feel_query(0.9, 0.9, 0.1, "80s") == "happy upbeat energetic intense 80s"
        assert _feel_query(0.5, 0.5, 0.5, "") == ""
        _tok.update(value="cached", expires=time.time() + 3600)
        assert _token() == "cached"  # no network call when unexpired
        print("ok")
    else:
        app.run()
