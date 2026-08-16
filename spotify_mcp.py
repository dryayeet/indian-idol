"""Spotify MCP server: recently played, top tracks, lyrics, mood search, playlist create.

Env: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
Scopes the refresh token needs:
    user-read-recently-played user-top-read playlist-modify-private
    playlist-read-private   (required to read any playlist's contents, even public ones)
    user-follow-read user-library-read user-read-playback-state

What the API still answers, and what it no longer does: API_SURFACE.md

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


RATE_LIMIT_TRIES = 3
RATE_LIMIT_MAX_WAIT = 30  # a longer Retry-After means a long ban; fail instead of hanging


def _call(method: str, path: str, **kw) -> dict:
    for attempt in range(RATE_LIMIT_TRIES):
        r = _http.request(
            method, API + path, headers={"Authorization": f"Bearer {_token()}"}, **kw
        )
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", 1))
            last = attempt == RATE_LIMIT_TRIES - 1
            if last or wait > RATE_LIMIT_MAX_WAIT:
                raise RuntimeError(
                    f"rate limited on {path}, Spotify asked for {wait:g}s"
                    + ("" if last else " which is longer than we wait")
                )
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            # Spotify puts the actual reason in the body; raise_for_status alone hides it
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}
    raise RuntimeError(f"rate limited on {path} after {RATE_LIMIT_TRIES} tries")


def _track(t: dict) -> dict:
    """Name, artist, link. Nothing else.

    The uri and id used to ride along too, but they are the same identifier three
    times: `_uri()` turns this url back into a uri when a playlist is built. Dropping
    them cuts 41% off every track, and tracks are most of what the model resends.
    """
    return {
        "name": t["name"],
        "artist": ", ".join(a["name"] for a in t["artists"]),
        # given, not left for the caller to build from the id, which invites made-up links
        "url": f"https://open.spotify.com/track/{t['id']}",
    }


@app.tool()
def recently_played(limit: int = 20) -> list[dict]:
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


def _relevant(track: dict, terms: list[str]) -> bool:
    """Whether the track has anything to do with the query.

    Spotify's search never returns nothing: an unmatched query still yields
    arbitrary tracks, which the agent would then present as an answer. Requiring a
    query word in the title or artist turns that into an honest empty result. The
    first four characters are compared, not the whole word, so "driving" still
    matches "drivers" the way Spotify's own fuzzy matching intends.
    """
    hay = f"{track['name']} {track['artist']}".lower()
    return any(term[:4] in hay for term in terms)


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


RECCO = "https://api.reccobeats.com/v1/audio-features"
RECCO_BATCH = 40  # 50 answers 400 "size must be ..."
FEEL_FLOOR = 0.15  # below this a number is not a request, it is the middle of the dial


def _features(tracks: list[dict]) -> dict[str, dict]:
    """Audio features by track id, from ReccoBeats, keyed on the Spotify id.

    Spotify deprecated /audio-features for new apps on 2024-11-27; ReccoBeats answers
    the same schema from the same track ids. Coverage is not complete and the gap is
    Western-biased: 98% on a US rap playlist here, 65% on Hindi-heavy top tracks. A
    missing track is ordinary, so it is simply absent from the result, and an outage
    at a third party must never fail a Spotify search.
    """
    ids = [t["url"].rsplit("/", 1)[-1] for t in tracks]
    out: dict[str, dict] = {}
    for i in range(0, len(ids), RECCO_BATCH):
        try:
            r = _http.get(RECCO, params={"ids": ",".join(ids[i : i + RECCO_BATCH])}, timeout=20)
            r.raise_for_status()
        except httpx.HTTPError:
            continue
        for x in r.json().get("content") or []:
            out[x["href"].rsplit("/", 1)[-1]] = x
    return out


def _asked_for(valence: float, energy: float, acousticness: float) -> dict[str, float]:
    """The axes the caller actually moved. All three at 0.5 means "no preference"."""
    axes = {"valence": valence, "energy": energy, "acousticness": acousticness}
    return {k: v for k, v in axes.items() if abs(v - 0.5) >= FEEL_FLOOR}


def _by_feel(tracks: list[dict], wanted: dict[str, float], feats=None) -> list[dict]:
    """Sort by distance from the requested mood. Tracks with no features keep their order.

    feats is passed in only by the self-check, so the sort can be tested without a network.
    """
    feats = _features(tracks) if feats is None else feats

    def key(item: tuple[int, dict]) -> tuple[int, float]:
        i, t = item
        f = feats.get(t["url"].rsplit("/", 1)[-1])
        if not f:  # unmeasured, so it cannot be ranked: after the ranked ones, in search order
            return (1, i)
        return (0, sum(abs(f[k] - v) for k, v in wanted.items()))

    return [t for _, t in sorted(enumerate(tracks), key=key)]


@app.tool()
def search_by_feel(
    description: str,
    valence: float = 0.5,
    energy: float = 0.5,
    acousticness: float = 0.5,
    limit: int = 10,
) -> list[dict]:
    """Find tracks matching a mood.

    description: two to five words that could plausibly appear in a song or album
        title, such as "leaving home", "midnight drive", "hotel heartbreak". Spotify
        matches this against track, artist, and album names only, never against
        lyrics or mood, so long poetic phrases are mostly ignored. Genre, era, and
        artist names work well here.
    valence: 0 sad to 1 happy. energy: 0 calm to 1 intense.
    acousticness: 0 produced to 1 acoustic. Leave one at 0.5 if it does not matter.

    Moving any of the three away from 0.5 makes this measure the candidates and rank
    them by how close they actually sound, so the numbers are worth setting.

    Results are filtered to tracks whose title or artist actually carries a word
    from the description, because Spotify answers even a nonsense query with
    arbitrary tracks. An empty list therefore means nothing matched: try different
    words rather than presenting nothing.
    """
    # Spotify's own /audio-features and /recommendations are 403 for new apps since
    # 2024-11-27, so the description still does the searching. ReccoBeats then ranks
    # what came back, which is the part that used to be impossible.
    if not description.strip():
        raise ValueError("description is required — say what the music should feel like")
    wanted = _asked_for(valence, energy, acousticness)
    # over-fetch only when there is something to rank by, since each page is a request
    want = min(limit * 3, RECCO_BATCH) if wanted else limit
    found = _search_tracks(_feel_query(description, valence, energy, acousticness), want)
    terms = _terms(description)
    hits = [t for t in found if _relevant(t, terms)] if terms else found
    return (_by_feel(hits, wanted) if wanted else hits)[:limit]


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


def _dedupe(tracks: list[dict]) -> list[dict]:
    """Recently played repeats the same song; lyrics only need fetching once."""
    seen, out = set(), []
    for t in tracks:
        if t["url"] not in seen:  # url, not id: _track no longer carries an id
            seen.add(t["url"])
            out.append(t)
    return out


@app.tool()
def listening_lyrics(source: str = "recent", limit: int = 12, chars: int = 800) -> dict:
    """Lyrics for what the user has been listening to, fetched in one call.

    source: "recent" for recently played, "top" for most played.
    limit: how many distinct tracks. Repeats of the same song are collapsed.
    chars: truncate each lyric to this many characters, 0 for the whole thing.
        Twenty full lyrics is a large amount of context; the default keeps roughly
        the opening verse and chorus, which is enough to read the mood.

    Returns {"tracks": [...], "missing": [...]}, where missing names the tracks
    LRCLIB had no lyrics for, usually instrumentals and very new releases.

    Use this instead of calling get_lyrics once per track.
    """
    if source not in ("recent", "top"):
        raise ValueError("source must be 'recent' or 'top'")
    found = (
        recently_played(min(limit * 2, 50)) if source == "recent" else top_tracks(limit)
    )
    picked = _dedupe(found)[:limit]

    with ThreadPoolExecutor(max_workers=6) as pool:
        lyrics = list(pool.map(_lyrics_for, picked))

    tracks, missing = [], []
    for track, words in zip(picked, lyrics):
        if not words:
            missing.append(f"{track['name']} - {track['artist']}")
            continue
        tracks.append(
            {
                "name": track["name"],
                "artist": track["artist"],
                "url": track["url"],
                "lyrics": words[:chars] if chars else words,
            }
        )
    return {"tracks": tracks, "missing": missing}


def _bare_id(item: str) -> str:
    """Take an id, a URI, or an open.spotify.com link, and return the id.

    Playlists, albums and artists all encode ids the same way, so this serves all three.
    """
    item = item.strip().rstrip("/")
    if item.startswith("spotify:"):
        return item.rsplit(":", 1)[-1]
    if "open.spotify.com" in item:
        return item.rsplit("/", 1)[-1].split("?")[0]
    return item


def _pages(path: str, limit: int, page: int = 50, **params) -> list[dict]:
    """Items up to limit, `page` at a time.

    Most list endpoints cap a page at 50, but /search and /artists/{id}/albums cap at
    10 and answer 400 "Invalid limit" for anything larger, so the size is a parameter.
    """
    out: list[dict] = []
    while len(out) < limit:
        got = _call("GET", path, params={"limit": page, "offset": len(out), **params})["items"]
        out += got
        if len(got) < page:  # last page
            break
    return out[:limit]


PLAYLIST_TTL = 300  # a playlist made mid-conversation should show up without a restart
_cached: dict = {"at": 0.0, "value": []}


def _playlists(limit: int = 200) -> list[dict]:
    """Every playlist the user owns or follows. Spotify sends the odd null row.

    Cached for a few minutes: two requests each time, and this is now read on every
    name resolution and once per agent turn to tell the model what the user's
    playlists are called.
    """
    if _cached["value"] and time.time() - _cached["at"] < PLAYLIST_TTL:
        return _cached["value"][:limit]
    fresh = [p for p in _pages("/me/playlists", 200) if p]
    _cached.update(at=time.time(), value=fresh)
    return fresh[:limit]


@app.tool()
def playlist_names() -> list[str]:
    """The names of the user's playlists. Cheap: no track data, just the titles."""
    return [p["name"] for p in _playlists()]


_IS_ID = re.compile(r"[0-9A-Za-z]{22}").fullmatch


def _playlist_of(playlist: str) -> str:
    """Whatever the caller passed, as a playlist id."""
    pid = _bare_id(playlist)
    return pid if _IS_ID(pid) else _resolve(pid)


def _find(kind: str, name: str) -> str:
    """A name to an id, through search. The model is handed names, so it passes names."""
    found = _call("GET", "/search", params={"q": name, "type": kind, "limit": 1})
    items = (found.get(f"{kind}s") or {}).get("items") or []
    if not items:
        raise RuntimeError(f"no {kind} called {name!r}")
    return items[0]["id"]


def _resolve(name: str) -> str:
    """A playlist name to its id. Users say names, so the agent is handed names."""
    want = name.strip().lower()
    words = set(re.findall(r"\w+", want))
    mine = _playlists()
    # exact, then every word of the request present in the name, then plain substring.
    # Not "name in request": a playlist called "Ti" is inside "ni ti" and would win.
    for hit in (
        lambda n: n == want,
        lambda n: words <= set(re.findall(r"\w+", n)),
        lambda n: want in n,
    ):
        for p in mine:
            if hit(p["name"].strip().lower()):
                return p["id"]
    # name the alternatives, so the caller can retry without another round trip
    raise RuntimeError(f"no playlist called {name!r}. Yours: {', '.join(p['name'] for p in mine)}")


@app.tool()
def my_playlists(limit: int = 200) -> list[dict]:
    """List the playlists the user owns or follows, with their ids and track counts."""
    items = _playlists(limit)
    return [
        {
            "name": p["name"],
            "id": p["id"],
            # "items", not "tracks": the Feb 2026 rename hit the playlist object too,
            # so this read None for every playlist
            "tracks": (p.get("items") or {}).get("total"),
            "owner": (p.get("owner") or {}).get("display_name"),
            "url": p["external_urls"]["spotify"],
        }
        for p in items
        if p
    ]


@app.tool()
def playlist_tracks(playlist: str, limit: int = 200) -> dict:
    """Read the tracks in a playlist, by name. Also takes an id, URI, or link.

    Pass the name the user said, exactly as they said it: this resolves it. There is no
    need to call my_playlists first, and casing and punctuation do not have to match.

    The playlist's real name comes back with the tracks. It is the playlist's title, not
    the first track's; do not rename it from what is inside it.
    """
    # a name is resolved here rather than left to the model, which was calling this
    # with "Unmaad", getting a 404, and asking the user for a link instead of looking
    # the name up in my_playlists
    pid = _playlist_of(playlist)
    path = f"/playlists/{pid}/items"
    try:
        items = _pages(path, limit)
    except RuntimeError as e:
        # a bare 403 or 404 made the model retry the same call; say what cannot be fixed.
        # A Blend answers 404 and another user's playlist 403, both permanently.
        if not any(code in str(e) for code in ("403", "404")):
            raise
        raise RuntimeError(
            "Spotify refuses this playlist in development mode: only the user's own "
            "playlists can be read, not ones owned by other users or by Spotify itself "
            "(Blends, Discover Weekly, Daily Mix). Do not retry; say so instead."
        ) from None
    # the same Feb 2026 migration renamed each row's payload from "track" to "item".
    # Reading "track" gives an empty list for every playlist. Podcast episodes sit in
    # the same field and carry no artists, so type has to be checked.
    return {
        # the model announced that "Unmaad" was "actually titled Bhar Do Jholi Meri", its
        # first track, because a bare list gave it nothing else to call the playlist
        "playlist": _call("GET", f"/playlists/{pid}").get("name"),
        "tracks": [_track(i["item"]) for i in items if (i.get("item") or {}).get("type") == "track"],
    }


MB = "https://musicbrainz.org/ws/2/artist"
MB_AGENT = "spotify-agent/0.1 (https://github.com/)"
MB_ARTISTS = 5  # MusicBrainz asks for one request a second, so this is 5s of waiting


def _genres(names: list[str]) -> list[str]:
    """Genre tags for artists, from MusicBrainz, commonest first.

    Spotify stopped sending genres in the Feb 2026 migration and the batch lookup that
    carried them is gone, so this is the only remaining route to anything genre-shaped.
    """
    counts: dict[str, int] = {}
    for i, name in enumerate(names[:MB_ARTISTS]):
        if i:
            time.sleep(1)  # their published rate limit, not a guess
        try:
            r = _http.get(
                MB,
                params={"query": name, "fmt": "json", "limit": 1},
                headers={"User-Agent": MB_AGENT},
                timeout=20,
            )
            r.raise_for_status()
            artists = r.json().get("artists") or []
        except (httpx.HTTPError, ValueError):
            continue
        if not artists:
            continue
        for tag in (artists[0].get("tags") or []) + (artists[0].get("genres") or []):
            counts[tag["name"]] = counts.get(tag["name"], 0) + int(tag.get("count") or 1)
    return sorted(counts, key=counts.get, reverse=True)[:10]


@app.tool()
def playlist_vibe(playlist: str, genres: bool = True) -> dict:
    """Measure how a playlist actually sounds: its mood, its spread, and its genres.

    Takes a playlist name, id, URI, or link. Returns the mean and the range of valence,
    energy, acousticness and danceability over the tracks that could be measured, plus
    the artists it leans on. Set genres to false to skip the slower genre lookup.

    `measured` says how many tracks carried features. Coverage is weaker outside the
    Western catalogue, so read the averages against that number, not as the whole truth.
    """
    read = playlist_tracks(playlist)
    name, tracks = read["playlist"], read["tracks"]
    feats = _features(tracks)
    axes = ("valence", "energy", "acousticness", "danceability")
    values = {a: [f[a] for f in feats.values() if f.get(a) is not None] for a in axes}
    mood = {
        a: {
            "mean": round(sum(v) / len(v), 3),
            "low": round(min(v), 3),
            "high": round(max(v), 3),
        }
        for a, v in values.items()
        if v
    }
    top: dict[str, int] = {}
    for t in tracks:
        for who in t["artist"].split(", "):  # not `name`: that holds the playlist's own
            top[who] = top.get(who, 0) + 1
    leaders = sorted(top, key=top.get, reverse=True)[:MB_ARTISTS]
    return {
        "playlist": name,
        "tracks": len(tracks),
        "measured": len(feats),
        "mood": mood or "no track in this playlist could be measured",
        "artists": leaders,
        "genres": _genres(leaders) if genres else [],
    }


@app.tool()
def followed_artists(limit: int = 100) -> list[dict]:
    """Artists the user follows. Taste as names, where top_tracks is taste as plays.

    Names only: the Feb 2026 migration stripped genres, popularity and follower counts
    from the artist object, and /artists is 403 in development mode.
    """
    # the only cursor-paged endpoint here: it hands back the id to carry on from
    out: list[dict] = []
    after: str | None = None
    while len(out) < limit:
        params = {"type": "artist", "limit": 50} | ({"after": after} if after else {})
        page = _call("GET", "/me/following", params=params)["artists"]
        out += page["items"]
        after = (page.get("cursors") or {}).get("after")
        if not after or not page["items"]:
            break
    return [{"name": a["name"], "url": a["external_urls"]["spotify"]} for a in out[:limit]]


@app.tool()
def top_artists(limit: int = 20, time_range: str = "short_term") -> list[dict]:
    """Most-played artists. time_range: short_term (~4 weeks), medium_term, long_term."""
    params = {"limit": min(limit, 50), "time_range": time_range}
    return [
        {"name": a["name"], "url": a["external_urls"]["spotify"]}
        for a in _call("GET", "/me/top/artists", params=params)["items"]
    ]


@app.tool()
def album_tracks(album: str) -> dict:
    """The tracks on an album, with its year. Takes an album name, id, URI, or link.

    Pass the album name the user said. Adding the artist to it sharpens the search.
    """
    aid = _bare_id(album)
    if not _IS_ID(aid):
        aid = _find("album", aid)
    a = _call("GET", f"/albums/{aid}")
    return {
        "name": a["name"],
        "artist": ", ".join(x["name"] for x in a["artists"]),
        "released": a.get("release_date"),
        "tracks": [_track(t) for t in _pages(f"/albums/{aid}/tracks", 100)],
    }


@app.tool()
def artist_albums(artist: str, limit: int = 30) -> list[dict]:
    """An artist's releases. Takes an artist name, id, URI, or link."""
    aid = _bare_id(artist)
    if not _IS_ID(aid):
        aid = _find("artist", aid)
    return [
        {
            "name": a["name"],
            "released": a.get("release_date"),
            "type": a.get("album_type"),
            "tracks": a.get("total_tracks"),
            "url": a["external_urls"]["spotify"],
        }
        for a in _pages(f"/artists/{aid}/albums", limit, page=10)
    ]


@app.tool()
def now_playing() -> dict:
    """What is playing right now, if anything. Needs the user-read-playback-state scope."""
    r = _call("GET", "/me/player/currently-playing")
    item = (r or {}).get("item")
    if not item:
        return {"playing": False}
    if item.get("type") != "track":  # a podcast episode has no artists to unpack
        return {"playing": True, "name": item.get("name"), "kind": item.get("type")}
    return _track(item) | {"playing": bool(r.get("is_playing"))}


@app.tool()
def liked_songs(limit: int = 200) -> list[dict]:
    """The user's Liked Songs, newest first. Their library proper, not a playlist."""
    return [
        _track(i["track"]) | {"added": (i.get("added_at") or "")[:10]}
        for i in _pages("/me/tracks", limit)
        if i.get("track")
    ]


@app.tool()
def saved_albums(limit: int = 100) -> list[dict]:
    """Albums in the user's library. A saved album is a stronger signal than a liked song."""
    return [
        {
            "name": a["album"]["name"],
            "artist": ", ".join(x["name"] for x in a["album"]["artists"]),
            "released": a["album"].get("release_date"),
            "tracks": a["album"].get("total_tracks"),
            "url": a["album"]["external_urls"]["spotify"],
        }
        for a in _pages("/me/albums", limit)
        if a.get("album")
    ]


@app.tool()
def saved_podcasts(limit: int = 50) -> list[dict]:
    """Podcasts in the user's library. Spotify exposes saved shows, not listening history."""
    return [
        {
            "name": s["show"]["name"],
            # publisher went the way of artist genres in the Feb 2026 migration
            "episodes": s["show"].get("total_episodes"),
            "description": (s["show"].get("description") or "")[:300],
            "url": s["show"]["external_urls"]["spotify"],
        }
        for s in _pages("/me/shows", limit)
        if s.get("show")
    ]


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
        assert track == {"name": "n", "artist": "a", "url": "https://open.spotify.com/track/ID"}, track
        assert _uri("1bMkimTb47umgNP6xCi4A1") == "spotify:track:1bMkimTb47umgNP6xCi4A1"
        assert _uri("spotify:track:abc") == "spotify:track:abc"
        assert _uri("https://open.spotify.com/track/xyz?si=1") == "spotify:track:xyz"
        assert _bare_id("spotify:playlist:PID") == "PID"
        assert _bare_id("https://open.spotify.com/playlist/PID?si=2") == "PID"
        assert _bare_id(" PID ") == "PID"
        # a bare name must not be mistaken for an id; a real id must not be looked up
        assert _IS_ID("4hWbZFjKdKAZWMTiTYUW41") and not _IS_ID("Unmaad")
        assert not _IS_ID("Ni || Ti") and not _IS_ID("my 22 character playlist")
        # the three dials: only a moved one is a request, and 0.5 must cost no request
        assert _asked_for(0.5, 0.5, 0.5) == {}
        assert _asked_for(0.5, 0.6, 0.5) == {}, "0.1 is inside the dead zone"
        assert _asked_for(0.05, 0.5, 0.9) == {"valence": 0.05, "acousticness": 0.9}
        sad = {"name": "s", "artist": "a", "url": "https://open.spotify.com/track/AAA"}
        glad = {"name": "g", "artist": "a", "url": "https://open.spotify.com/track/BBB"}
        mute = {"name": "m", "artist": "a", "url": "https://open.spotify.com/track/CCC"}
        known = {"AAA": {"valence": 0.1}, "BBB": {"valence": 0.9}}  # CCC unmeasured
        assert _by_feel([glad, mute, sad], {"valence": 0.0}, known) == [sad, glad, mute]
        assert _by_feel([sad, mute, glad], {"valence": 1.0}, known) == [glad, sad, mute]
        assert _terms("Songs about leaving my hometown") == ["leaving", "hometown"]
        keep = {"name": "Leaving Home", "artist": "Indian Ocean"}
        junk = {"name": "Dame Tu Cosita", "artist": "El Chombo"}
        assert _relevant(keep, ["leaving", "home"]) and not _relevant(junk, ["leaving"])
        # four-character prefix, so Spotify's own fuzzy hits survive the filter
        assert _relevant({"name": "drivers license", "artist": "x"}, ["driving"])

        # 429 is retried, not raised, when Spotify says to wait a moment
        seen = {"n": 0}
        real_request, real_token = _http.request, globals()["_token"]

        class _Resp:
            def __init__(self, status):
                self.status_code, self.headers = status, {"Retry-After": "0"}
                self.content, self.text = b"{}", "{}"

            def json(self):
                return {"ok": True}

        _http.request = lambda *a, **k: (
            seen.update(n=seen["n"] + 1) or _Resp(429 if seen["n"] < 3 else 200)
        )
        globals()["_token"] = lambda: "t"
        try:
            assert _call("GET", "/x") == {"ok": True}, "should succeed after retries"
            assert seen["n"] == 3, seen
        finally:
            _http.request, globals()["_token"] = real_request, real_token
        # the winning line is the one carrying the most distinct terms, not the first hit
        score, line = _lyric_score(["leaving", "hometown"], "I am leaving\nleaving my hometown tonight")
        assert (score, line) == (1.0, "leaving my hometown tonight"), (score, line)
        assert _lyric_score(["leaving", "hometown"], "just leaving")[0] == 0.5  # partial
        rows = [{"url": "a"}, {"url": "b"}, {"url": "a"}]
        assert [t["url"] for t in _dedupe(rows)] == ["a", "b"]
        assert _lyric_score(["leaving"], "no match here") == (0.0, "")  # honest empty
        _tok.update(value="cached", expires=time.time() + 3600)
        assert _token() == "cached"  # no network call when unexpired
        print("ok")
    else:
        app.run()
