# What Spotify still gives us, and what other Spotify MCP servers give

Two surveys, done 2026-08-17. The first is what the Web API answers for **this app**,
probed live with the project's own token rather than read from documentation. The
second is every tool other Spotify MCP servers expose, as a source of ideas.

Read the first before adding a tool. Half the obvious ideas are impossible now.

## 1. The live surface

Probed with 37 requests against the real account. `OK` means it answered with data.

### Works

| Endpoint | Tool here | Notes |
|---|---|---|
| `GET /me` | — | Profile. `country`, `email`, `product` were removed Feb 2026. |
| `GET /me/tracks` | `liked_songs` | Liked Songs. Pages 50. |
| `GET /me/albums` | `saved_albums` | Pages 50. |
| `GET /me/shows` | `saved_podcasts` | Pages 50. |
| `GET /me/episodes` | — | Saved episodes. Trivial for this account (1). |
| `GET /me/audiobooks` | — | Answers, but empty here. |
| `GET /me/following?type=artist` | `followed_artists` | Cursor-paged, not offset-paged. |
| `GET /me/top/tracks` | `top_tracks` | Three time ranges. |
| `GET /me/top/artists` | `top_artists` | Three time ranges. |
| `GET /me/player/recently-played` | `recently_played` | Tracks only, never episodes. Max 50. |
| `GET /me/playlists` | `my_playlists` | Pages 50. Excludes everything Spotify owns. |
| `GET /playlists/{id}` | — | Own playlists only. |
| `GET /playlists/{id}/items` | `playlist_tracks` | Pages 50. Rows carry `item`, not `track`. |
| `GET /tracks/{id}` | — | Single track. The batch `/tracks` is gone. |
| `GET /artists/{id}` | — | Name and images only now. |
| `GET /artists/{id}/albums` | `artist_albums` | **Caps a page at 10**, like `/search`. |
| `GET /albums/{id}` | `album_tracks` | `genres` is present but always empty. |
| `GET /albums/{id}/tracks` | `album_tracks` | |
| `GET /shows/{id}`, `/shows/{id}/episodes` | — | Work with a real id. |
| `GET /search` | `search_by_feel`, `search_by_lyrics` | **Caps `limit` at 10**, default 5. Offset paging works. |

### Needs a scope we now request

| Endpoint | Tool | Scope |
|---|---|---|
| `GET /me/player/currently-playing` | `now_playing` | `user-read-playback-state` |
| `GET /me/player`, `/devices`, `/queue` | — | Same scope. Not built. |

### Gone, permanently

| Endpoint | What happened |
|---|---|
| `GET /audio-features/{id}` | 403. Deprecated 2024-11-27. This is why `search_by_feel` matches names, not acoustics. |
| `GET /recommendations` | 404. Same deprecation. There is no seed-based recommender. |
| `GET /artists/{id}/related-artists` | 403. No "artists like this". |
| `GET /artists/{id}/top-tracks` | 403. Removed Feb 2026. |
| `GET /tracks`, `/artists`, `/albums`, `/shows`, `/episodes` (batch) | 403. Removed Feb 2026. One id per call now. |
| `GET /browse/new-releases`, `/browse/categories`, `/browse/featured-playlists` | 403. No editorial browse. |
| `GET /markets` | 403. |
| `GET /users/{id}`, `GET /users/{id}/playlists` | Removed Feb 2026. |
| Anything Spotify owns | Blends, Daily Mix, Discover Weekly, Your Top Songs 2024, Release Radar. Absent from `/me/playlists`, 404 by id. Extended quota mode is the only route and needs 250k monthly active users. |
| Another user's playlist | 403. |
| Search history | Never existed in the Web API. |
| Artist genres, popularity, followers | Removed from the artist object Feb 2026, and the batch lookup that carried them is gone too. |
| Podcast publisher | Removed Feb 2026. |

### Writes we do not do

`PUT /me/library` and `DELETE /me/library` replaced every per-type save, unsave,
follow, and unfollow endpoint. `PUT /playlists/{id}/items` reorders. `DELETE
/playlists/{id}/items` removes. None are built: this agent reads and creates, and
nothing else. See the TODO for what a write surface would cost.

## 2. What other Spotify MCP servers expose

Surveyed for ideas, not to copy. Most of these servers are playback remotes: their
value is "pause the music from your editor". This project is not that, so most of
their tools are irrelevant here, and a few are already impossible.

| Server | Shape |
|---|---|
| [gupta-kush/spotify-mcp](https://github.com/gupta-kush/spotify-mcp) | ~100 tools in four toolsets: core, discovery, power, destructive. The most complete. |
| [jamiew/spotify-mcp](https://github.com/jamiew/spotify-mcp) | 31 tools, explicitly token-efficient, batches up to 100 tracks per call. |
| [marcelmarais/spotify-mcp-server](https://github.com/marcelmarais/spotify-mcp-server) | Deliberately lightweight: search, playback, playlists. |
| [NathanPr03](https://github.com/NathanPr03/spotify-mcp), [garywwh](https://github.com/garywwh/spotify_mcp_server/), [Carrieukie](https://github.com/Carrieukie/spotify-mcp-server), [tylerpina](https://github.com/tylerpina/spotify-mcp) | Playback plus search, in various languages. |

### Their tools, grouped

**Playback** (none built here, all need `user-modify-playback-state` and a Premium
account with an active device): play, pause, resume, skip next, skip previous, seek,
set volume, set repeat, toggle shuffle, add to queue, get queue, list devices,
transfer playback, now playing, playback state.

**Library reads** (mostly built): saved tracks, saved albums, saved shows, top tracks,
top artists, recently played, user profile, playlist list, playlist details, playlist
tracks, track details, album details, artist details.

**Library writes** (none built): save and unsave tracks, albums, shows; follow and
unfollow artists, users, playlists.

**Playlist writes** (only `create_playlist` built): create, add tracks, remove tracks,
reorder, rename, change description and visibility, merge two playlists, split by
artist, deduplicate.

**Discovery** (mostly impossible now): related artists, discover by artist, genre
explorer, deep cuts, radio from a seed. These lean on `/recommendations` and
`/related-artists`, both dead for new apps. `spotify_discover_by_mood` and
`spotify_find_vibe_matches` are the same problem `search_by_feel` solves, and any
server still claiming acoustic matching is either grandfathered or guessing.

**Analysis** (the interesting column): playlist vibe, artist deep dive, artist
network, library stats, query library, sync library to a local index. This is the only
group that overlaps with what this project is for. Section 4 takes each one apart.

## 4. The analysis tools, checked one by one

Researched and probed 2026-08-17. Three of the six are impossible here, two are free,
and one turns out to reopen a door this project had written off.

| Tool | Verdict here |
|---|---|
| `spotify_artist_network` | **Impossible.** Built on `/related-artists`, which is 403 for us. That server is grandfathered or its README is stale. |
| `spotify_playlist_vibe` | **Impossible as built.** Its own README says it "estimates energy from genre data rather than audio-features", and genres left the artist object in Feb 2026. Possible against a third-party genre source, see below. |
| `spotify_artist_deep_dive` | **Half.** `artist_albums` works; `/artists/{id}/top-tracks` is 403 and the artist object is down to a name and images. |
| `spotify_sync_library` | **Free.** Everything it indexes is already reachable. |
| `spotify_library_stats` | **Free.** Pure aggregation over the above. |
| `spotify_query_library` | **Free.** Filtering over a local index, no endpoint at all. |

### The find: audio features are available again, from someone else (now built)

[ReccoBeats](https://api.reccobeats.com) answers with **Spotify track ids** and returns
the exact schema Spotify deprecated: `valence`, `energy`, `acousticness`,
`danceability`, `instrumentalness`, `liveness`, `speechiness`, `loudness`, `tempo`,
`key`, `mode`. No key, no account.

Probed against this account's real tracks:

- Batch cap **40 ids**; 50 answers `400 size must be...`.
- 49 tracks in **0.3s** over two requests.
- Coverage is **not** universal, and the gap is not random:

| Sample | Covered |
|---|---|
| A US rap playlist | 45/46 (98%) |
| A Sufi and Bollywood playlist | 13/15 (87%) |
| This account's top tracks, Hindi-heavy | 13/20 (65%) |

The catalogue is Western-biased, and this user's listening is not, so a third of the
most relevant tracks have no features. Any use of it has to treat a miss as normal
rather than as an error.

That matters because `search_by_feel`'s three numbers currently only nudge one keyword
onto a text search: they cannot filter, because there was nothing to filter on. With
ReccoBeats they could rank a candidate list for real. Whether that is worth an extra
request per search, given a third of the results would be unranked, is the open
question. Measure before building.

**Built 2026-08-17.**  ranks by real features whenever a dial is moved
off 0.5, and  reports what a playlist actually sounds like.

[MusicBrainz](https://musicbrainz.org/ws/2/artist) is the matching answer for genres:
keyless, and it returned eight usable tags for a test artist. It is the only route to
anything genre-shaped now that Spotify sends none.

## 2b. Agents doing the same job

Surveyed 2026-08-17. The finding that matters: **almost all of them are built on
endpoints that no longer answer**, and most have not noticed.

| Project | What it is | State |
|---|---|---|
| [PersonalAIs](https://www.orfium.com/data-science/%F0%9F%8E%B6-personalais-an-ai-music-recommendation-system-for-personalized-mood-aware-listening/) (Orfium, GSoC 2025) | The closest thing to this project. Spotify API plus MCP plus Qwen2.5-7B, a Next.js chat, a valence/energy emotion map, and mood transitions. Supplements with Last.fm. | Its valence and energy come from `/audio-features` and its picks from `/recommendations`. Both are dead for new apps. |
| [udossa/spotify-ai-agent](https://github.com/udossa/spotify-ai-agent) | LangGraph, LangChain, MCP, ChromaDB, spotipy. Playlists from a natural-language intent. | Nearest architecture to this one. |
| [Astropomeai's AI DJ](https://medium.com/@astropomeai/ai-music-curation-creating-an-ai-dj-assistant-with-langgraph-studio-and-spotify-api-560a492b7c2b) | LangGraph Studio plus the Spotify API, mood playlists from valence and energy. | Same dead dependency. |
| [Moodify](https://github.com/mahnoorshafi/Moodify) | Mood playlists from listening history, using danceability, energy, valence. | Same dead dependency. |
| [prathami1/spotify-analysis](https://github.com/prathami1/spotify-analysis) | Flask, predicts mood from listening habits, Google NLP for emotion. | Closest in intent to the abstract. |
| Spotify itself | Semantic IDs: each track as quantised tokens, an LLM fine-tuned to generate them for playlisting. | Not public API. |

What this project has that they do not: a tested map of what still answers, and
replacements wired in for what does not (ReccoBeats, MusicBrainz). Lyric search by
fetch-and-rerank. Approval modes. They are recommenders; this one is built to explain.

What they have that this does not: **mood transitions**. "I'm sad and I want to be
happy" is a request for an arc, and every tool here returns a flat set.

## 3. What is worth taking

In the TODO, in order. Briefly:

1. **Playlist writes** beyond create: add, remove, reorder, rename. The agent can
   build a playlist but cannot revise one, so "drop the last three" is impossible.
2. **Library stats and query**, the one genuinely good idea in the other servers:
   fold the library into one summary rather than making the model read four lists.
3. **Deduplicate and merge**, which are pure local logic over data already reachable.
4. **Playback control**, if the agent should ever act on the mood it infers rather
   than only describe it. Needs Premium and a live device, so it is untestable in CI.

Deliberately not taken: anything built on `/recommendations`, `/related-artists`, or
audio features, because they do not answer for this app and no amount of code fixes
that.
