"""Spotify MCP server: recently played, top tracks, lyrics, mood search, playlist create.

Env: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
Scopes the refresh token needs:
    user-read-recently-played user-top-read playlist-modify-private
    playlist-read-private   (required to read any playlist's contents, even public ones)

Run:  python spotify_mcp.py           (stdio)
Check: python spotify_mcp.py --selfcheck
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

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
        # given, not left for the caller to build from the id, which invites made-up links
        "url": f"https://open.spotify.com/track/{t['id']}",
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


SEARCH_PAGE = 10  # /v1/search rejects limit > 10 outright; page with offset instead


def _search_tracks(q: str, want: int) -> list[dict]:
    """Search, paging past the 10-result ceiling until `want` tracks or results run out."""
    out: list[dict] = []
    for offset in range(0, min(want, 50), SEARCH_PAGE):
        page = _call(
            "GET",
            "/search",
            params={"q": q, "type": "track", "limit": SEARCH_PAGE, "offset": offset},
        )["tracks"]["items"]
        out += [_track(t) for t in page if t]
        if len(page) < SEARCH_PAGE:
            break
    return out[:want]


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

    description: two to five words that could plausibly appear in a song or album
        title, such as "leaving home", "midnight drive", "hotel heartbreak". Spotify
        matches this against track, artist, and album names only, never against
        lyrics or mood, so long poetic phrases are mostly ignored. Genre, era, and
        artist names work well here.
    valence: 0 sad to 1 happy. energy: 0 calm to 1 intense.
    acousticness: 0 produced to 1 acoustic. Leave one at 0.5 if it does not matter.

    The search never returns nothing: an unmatched query still yields arbitrary
    tracks. Judge whether the results actually fit before presenting them.
    """
    # ponytail: keyword search, not /v1/recommendations + target_valence — that endpoint and
    # /v1/audio-features were deprecated for new apps on 2024-11-27 and return 403. Swap back
    # to real feature targeting only if this app gets extended-mode access.
    if not description.strip():
        raise ValueError("description is required — say what the music should feel like")
    return _search_tracks(_feel_query(description, valence, energy, acousticness), limit)


_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with",
    "is", "am", "are", "was", "were", "be", "been", "it", "its", "this", "that", "as",
    "my", "your", "his", "her", "their", "our", "me", "you", "he", "she", "they", "we",
    "song", "songs", "music", "track", "tracks", "about", "like", "feel", "feels",
}


def _terms(text: str) -> list[str]:
    """Content words worth matching on, lowercased."""
    return [w for w in re.findall(r"[a-z']{3,}", text.lower()) if w not in _STOP]


def _lyric_score(terms: list[str], lyrics: str) -> tuple[float, str]:
    """Share of the terms present in the lyrics, plus the line that carries the most."""
    if not terms or not lyrics:
        return 0.0, ""
    low = lyrics.lower()
    hits = {t for t in terms if t in low}
    if not hits:
        return 0.0, ""
    line = max(lyrics.splitlines(), key=lambda ln: sum(t in ln.lower() for t in hits))
    return round(len(hits) / len(terms), 3), line.strip()


def _lyrics_for(track: dict) -> str:
    """Lyrics for one track, empty string if LRCLIB has none. Safe to call in threads."""
    try:
        r = _http.get(
            "https://lrclib.net/api/get",
            params={"track_name": track["name"], "artist_name": track["artist"].split(",")[0]},
            headers={"User-Agent": "spotify-agent (https://github.com/)"},
        )
        return r.json().get("plainLyrics") or "" if r.status_code == 200 else ""
    except httpx.HTTPError:
        return ""


@app.tool()
def search_by_lyrics(
    phrase: str,
    search_terms: str = "",
    candidates: int = 30,
    limit: int = 10,
) -> list[dict]:
    """Find tracks whose LYRICS match a phrase, not whose titles do.

    phrase: the words or ideas that should appear in the words of the song, e.g.
        "leaving town headlights never coming back".
    search_terms: optional title-like words used to pull the candidate pool from
        Spotify. Defaults to the phrase. Widen this if results come back empty.
    candidates: how many Spotify results to fetch lyrics for. More is slower.

    Slow: it reads the lyrics of every candidate, one request each. Unlike
    search_by_feel this can legitimately return nothing, which means no candidate's
    lyrics matched. Each result carries the score and the matching line.
    """
    if not phrase.strip():
        raise ValueError("phrase is required — say what the words of the song should say")
    pool = _search_tracks(search_terms.strip() or phrase, min(candidates, 50))
    terms = _terms(phrase)

    with ThreadPoolExecutor(max_workers=6) as pool_exec:  # 30 serial fetches is a minute
        lyrics = list(pool_exec.map(_lyrics_for, pool))

    scored = []
    for track, words in zip(pool, lyrics):
        score, line = _lyric_score(terms, words)
        if score:
            scored.append(track | {"lyric_score": score, "matched_line": line})
    scored.sort(key=lambda t: t["lyric_score"], reverse=True)
    return scored[:limit]


def _playlist_id(item: str) -> str:
    """Take a playlist id, URI, or open.spotify.com link and return the id."""
    item = item.strip().rstrip("/")
    if item.startswith("spotify:"):
        return item.rsplit(":", 1)[-1]
    if "open.spotify.com" in item:
        return item.rsplit("/", 1)[-1].split("?")[0]
    return item


@app.tool()
def my_playlists(limit: int = 50) -> list[dict]:
    """List the playlists the user owns or follows, with their ids and track counts."""
    items = _call("GET", "/me/playlists", params={"limit": min(limit, 50)})["items"]
    return [
        {
            "name": p["name"],
            "id": p["id"],
            "tracks": (p.get("tracks") or {}).get("total"),
            "owner": (p.get("owner") or {}).get("display_name"),
            "url": p["external_urls"]["spotify"],
        }
        for p in items
        if p
    ]


@app.tool()
def playlist_tracks(playlist: str, limit: int = 50) -> list[dict]:
    """Read the tracks in a playlist. Accepts a playlist id, URI, or link."""
    path = f"/playlists/{_playlist_id(playlist)}/items"
    items = _call("GET", path, params={"limit": min(limit, 50)})["items"]
    return [_track(i["track"]) for i in items if i.get("track")]


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
        track = _track({"name": "n", "artists": [{"name": "a"}], "uri": "spotify:track:ID", "id": "ID"})
        assert track["url"] == "https://open.spotify.com/track/ID", track
        assert _uri("1bMkimTb47umgNP6xCi4A1") == "spotify:track:1bMkimTb47umgNP6xCi4A1"
        assert _uri("spotify:track:abc") == "spotify:track:abc"
        assert _uri("https://open.spotify.com/track/xyz?si=1") == "spotify:track:xyz"
        assert _playlist_id("spotify:playlist:PID") == "PID"
        assert _playlist_id("https://open.spotify.com/playlist/PID?si=2") == "PID"
        assert _playlist_id(" PID ") == "PID"
        assert _terms("Songs about leaving my hometown") == ["leaving", "hometown"]
        # the winning line is the one carrying the most distinct terms, not the first hit
        score, line = _lyric_score(["leaving", "hometown"], "I am leaving\nleaving my hometown tonight")
        assert (score, line) == (1.0, "leaving my hometown tonight"), (score, line)
        assert _lyric_score(["leaving", "hometown"], "just leaving")[0] == 0.5  # partial
        assert _lyric_score(["leaving"], "no match here") == (0.0, "")  # honest empty
        _tok.update(value="cached", expires=time.time() + 3600)
        assert _token() == "cached"  # no network call when unexpired
        print("ok")
    else:
        app.run()
