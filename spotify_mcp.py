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
from mcp.server.fastmcp import FastMCP

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

API = "https://api.spotify.com/v1"
app = FastMCP("spotify")
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
    if r.status_code != 200:
        raise RuntimeError(
            f"token refresh failed ({r.status_code}): {r.text[:200]}. "
            "Check SPOTIFY_CLIENT_ID / SECRET / REFRESH_TOKEN are set correctly."
        )
    d = r.json()
    _tok.update(value=d["access_token"], expires=time.time() + d["expires_in"])
    return _tok["value"]


def _call(method: str, path: str, **kw) -> dict:
    r = _http.request(method, API + path, headers={"Authorization": f"Bearer {_token()}"}, **kw)
    if r.status_code == 429:
        raise RuntimeError(f"rate limited, retry after {r.headers.get('Retry-After', '?')}s")
    if r.status_code >= 400:
        # Spotify puts the actual reason in the body; raise_for_status alone hides it
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
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


def _feel_query(description: str, valence: float, energy: float, acousticness: float) -> str:
    """The description carries the search; the strongest mood axis adds one word.

    Spotify's search matches text, not feeling, so a pile of mood adjectives just
    matches song titles containing those adjectives. One word is the useful dose.
    """
    axes = {
        "valence": (valence, "sad", "happy"),
        "energy": (energy, "calm", "energetic"),
        "acousticness": (acousticness, "electronic", "acoustic"),
    }
    _, (value, low, high) = max(axes.items(), key=lambda kv: abs(kv[1][0] - 0.5))
    description = description.strip()
    if abs(value - 0.5) < 0.15:  # nothing stands out; the description is enough
        return description
    return f"{description} {high if value > 0.5 else low}"


@app.tool()
def search_by_feel(
    description: str,
    valence: float = 0.5,
    energy: float = 0.5,
    acousticness: float = 0.5,
    limit: int = 20,
) -> list[dict]:
    """Find tracks matching a mood.

    description: what the music should feel like or be about, in a few evocative words
        ("driving away at night", "heartbreak in a hotel room"). This does the work, so
        write it carefully. Genre, era, and artist go here too.
    valence: 0 sad to 1 happy. energy: 0 calm to 1 intense.
    acousticness: 0 produced to 1 acoustic. Leave one at 0.5 if it does not matter.
    """
    # ponytail: keyword search, not /v1/recommendations + target_valence — that endpoint and
    # /v1/audio-features were deprecated for new apps on 2024-11-27 and return 403. Swap back
    # to real feature targeting only if this app gets extended-mode access.
    if not description.strip():
        raise ValueError("description is required — say what the music should feel like")
    q = _feel_query(description, valence, energy, acousticness)
    params = {"q": q, "type": "track", "limit": min(limit, 50)}
    return [_track(t) for t in _call("GET", "/search", params=params)["tracks"]["items"]]


def _uri(item: str) -> str:
    """Take a track URI, an open.spotify.com link, or a bare id, and return a URI."""
    item = item.strip()
    if item.startswith("spotify:"):
        return item
    if "open.spotify.com" in item:
        item = item.rsplit("/", 1)[-1].split("?")[0]
    return f"spotify:track:{item}"


@app.tool()
def create_playlist(name: str, track_uris: list[str], description: str = "") -> dict:
    """Create a private playlist and add tracks. Accepts track URIs, links, or bare ids."""
    # /users/{id}/playlists returns a bare 403 since the Feb 2026 migration; /me/playlists is the
    # replacement, and it saves the /me lookup that the old path needed.
    pl = _call(
        "POST", "/me/playlists", json={"name": name, "description": description, "public": False}
    )
    uris = [_uri(u) for u in track_uris]
    for i in range(0, len(uris), 100):
        # /items, not /tracks — same Feb 2026 migration renamed it
        _call("POST", f"/playlists/{pl['id']}/items", json={"uris": uris[i : i + 100]})
    return {"id": pl["id"], "url": pl["external_urls"]["spotify"], "added": len(track_uris)}


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        assert _feel_query("leaving home", 0.2, 0.5, 0.5) == "leaving home sad"
        assert _feel_query("rave at 3am", 0.5, 0.9, 0.5) == "rave at 3am energetic"
        assert _feel_query("campfire", 0.4, 0.6, 0.95) == "campfire acoustic"  # strongest axis
        assert _feel_query("leaving home", 0.5, 0.5, 0.5) == "leaving home"  # no "music" fallback
        assert _uri("1bMkimTb47umgNP6xCi4A1") == "spotify:track:1bMkimTb47umgNP6xCi4A1"
        assert _uri("spotify:track:abc") == "spotify:track:abc"
        assert _uri("https://open.spotify.com/track/xyz?si=1") == "spotify:track:xyz"
        _tok.update(value="cached", expires=time.time() + 3600)
        assert _token() == "cached"  # no network call when unexpired
        print("ok")
    else:
        app.run()
