# Spotify MCP Server

This is the Spotify tool server for the autonomous Spotify agent.
It is an MCP server. It gives an agent twenty one tools for the Spotify Web API, LRCLIB, and the web.
For the intent, read [SPOTIFY_AGENT_ABSTRACT.md](SPOTIFY_AGENT_ABSTRACT.md).
For what is built and why, read [ARCHITECTURE.md](ARCHITECTURE.md).
For what is still owed, read [TODO.md](TODO.md).
For the model choice, read [MODEL_BAKEOFF.md](MODEL_BAKEOFF.md).
For what the API still allows, read [research/API_SURFACE.md](research/API_SURFACE.md).
For multi-model plans, read [research/MULTI_MODEL.md](research/MULTI_MODEL.md).

## Tools

| Tool | Function |
|---|---|
| `recently_played(limit)` | Get the tracks that you played last. The maximum is 50. |
| `top_tracks(limit, time_range)` | Get the tracks that you played most. |
| `web_search(query, limit)` | Search the web for context Spotify does not carry. |
| `get_lyrics(track, artist)` | Get the lyrics from LRCLIB. This tool does not use Spotify. |
| `search_by_feel(description, valence, energy, acousticness, limit)` | Find tracks. The description does the searching. |
| `listening_lyrics(source, limit, chars)` | Get the lyrics of recent or top tracks in one call. |
| `search_by_lyrics(phrase, search_terms, candidates, limit)` | Find tracks whose lyrics match. Slower. Can return nothing. |
| `my_playlists(limit)` | List the playlists the user owns or follows. |
| `playlist_names()` | Just the playlist titles. The agent reads this into its prompt. |
| `playlist_tracks(playlist, limit)` | Read a playlist by name, id, URI, or link. Returns its title and tracks. |
| `playlist_vibe(playlist, genres)` | Measure how a playlist sounds: mood, spread, artists, genres. |
| `followed_artists(limit)` | Artists the user follows. Names only. |
| `top_artists(limit, time_range)` | Most-played artists. |
| `liked_songs(limit)` | The user's Liked Songs. |
| `saved_albums(limit)` | Albums in the user's library. |
| `album_tracks(album)` | The tracks on an album. |
| `similar_artists(artist, limit)` | Artists that sound like this one. Needs a Last.fm key. |
| `artist_albums(artist, limit)` | An artist's releases, by name or id. |
| `now_playing()` | What is playing right now. |
| `saved_podcasts(limit)` | Podcasts in the user's library. |
| `create_playlist(name, track_uris, description)` | Make a private playlist and add the tracks. |

`create_playlist` accepts a track URI, a Spotify link, or an ID.

## Files

| File | Function |
|---|---|
| `spotify_mcp.py` | The MCP server. Start it with `python spotify_mcp.py`. |
| `agent.py` | The LangGraph agent. It calls the tools through MCP. |
| `get_token.py` | Mints the refresh token. Run it again when the scopes change. |
| `bakeoff.py` | Scores models on the agent's job. Run `python bakeoff.py`. |
| `MODEL_BAKEOFF.md` | The model results and what they mean. |
| `research/MULTI_MODEL.md` | Plans for using more than one model. |
| `research/API_SURFACE.md` | Every endpoint that still answers, and what other Spotify MCP servers do. |
| `ui_check.py` | Drives the web interface headlessly. Run `python ui_check.py`. |
| `run_tool.py` | A command-line client. Use it to call one tool. |
| `streamlit_app.py` | A web interface. It shows all the tools as forms. |
| `requirements.txt` | The Python packages. |

## Installation

1. Make a virtual environment.
2. Install the packages with `pip install -r requirements.txt`.

## The agent

`agent.py` is a LangGraph ReAct agent. It starts `spotify_mcp.py` as a subprocess
and reads the tool list through MCP. The language model comes from OpenRouter.

```
python agent.py "songs that feel like driving away from my hometown"
```

The agent translates the request into emotion values, then calls the tools.

The model comes from OpenRouter. Set `OPENROUTER_MODEL` to change it; it must
support tool calls. Which model and why: [MODEL_BAKEOFF.md](MODEL_BAKEOFF.md).

## Modes

The chat gives three levels of control over the tools. Change the mode with the
buttons above the chat bar, or with a slash command in the chat bar. The two stay
in step: a slash command moves the buttons, and a button press is the same as the
command.

| Mode | Effect |
|---|---|
| `/manual` | Every tool call waits for your approval. |
| `/afk` | Reads run freely. The three playlist tools wait for you. |
| `/auto` | All tools run. Nothing waits. |

`/mode` shows the current mode. `/help` lists the commands.
The mode is `afk` when the chat starts.

A tool that waits does not run. The agent stops before the tool, and shows you the
call. Approve it, and the agent continues. Decline it, and the agent is told that
you declined. It then chooses another action.

## Credentials

The server reads three Spotify variables from a `.env` file.
The agent reads `OPENROUTER_API_KEY` from the same file.
`LASTFM_API_KEY` is optional. Get one at <https://www.last.fm/api/account/create>.
Use `.env.example` as the pattern.

1. Open the [Spotify dashboard](https://developer.spotify.com/dashboard).
2. Make an app. Write down the client ID and the client secret.
3. Set the redirect URI to `http://127.0.0.1:8888/callback`.
   Do not use `localhost`. Spotify refuses it.
4. Get a refresh token with these scopes:
   `user-read-recently-played`, `user-top-read`, `playlist-modify-private`,
   `playlist-read-private`, `user-follow-read`, `user-library-read`,
   and `user-read-playback-state`.
   Run `python get_token.py` to do this.
5. Put the three values in the `.env` file.

Keep the `.env` file out of the repository. The `.gitignore` file does this.

## Operation

Start the web interface:

```
streamlit run streamlit_app.py
```

Call one tool from the command line:

```
python run_tool.py get_lyrics track="Motion Sickness" artist="Phoebe Bridgers"
```

Start `run_tool.py` with no arguments for the interactive mode.
The MCP server writes no output to the screen. This is correct.
It waits for a client on stdin.

## Uploads

The chat accepts files with the paperclip: images (png, jpg, webp) and PDF.
An image is read in full by the model once, on upload. The first turn sees the
image itself. Later turns see the stored reading instead, so the cost does not
repeat. The reading is a complete transcription, not a caption.
A PDF becomes its text, page by page. A page with no text but an embedded
image, which is what any phone scan is, gets read by the model like an
uploaded image. At most twelve scanned pages are read; the rest say so.
A screenshot of a playlist this app cannot reach through the API, such as a
Blend or a friend's playlist, becomes readable this way.

## Tests

Each file has an internal test. Give the `--selfcheck` argument:

```
python spotify_mcp.py --selfcheck
python run_tool.py --selfcheck
python agent.py --selfcheck
python bakeoff.py --selfcheck
python ui_check.py
```

The agent test lists the tools through MCP. It does not call the language model.

## Limits

- The server uses one Spotify account. It has no login for each user.
  A person who opens the web interface has full control of that account.
- Spotify stopped the audio-feature endpoints on 27 November 2024.
  New apps cannot use `/v1/audio-features` or `/v1/recommendations`.
  The description still does the searching; ReccoBeats does the ranking.
- Spotify moved two endpoints in February 2026.
  The server uses `POST /me/playlists` and the `/items` path for playlist tracks.
- Spotify has no lyrics endpoint. The lyrics come from LRCLIB.
- No service searches lyrics. `search_by_lyrics` reads the lyrics of each
  candidate track and ranks them. This costs one request for each candidate.
- The search endpoint refuses a limit above 10. The server pages with an
  offset to get more results.
- The `mcp` package must stay below version 2.0.
  `langchain-mcp-adapters` does not support version 2.0 yet.
- The agent remembers a conversation only while the web interface runs.
  A restart loses the history.
- Reading the tracks of any playlist needs the `playlist-read-private` scope.
  This is true even for public playlists.
- Only the user's own playlists can be read. Playlists that Spotify owns,
  such as a Blend, a Daily Mix, or Discover Weekly, are not in the list and
  answer 404 by id. Another user's playlist answers 403. This is permanent.
- Spotify stopped serving audio features in 2024, so `search_by_feel` and
  `playlist_vibe` get them from ReccoBeats instead. Coverage is not complete
  and the gap favours the Western catalogue: 98 percent on a US rap playlist
  here, 65 percent on Hindi-heavy top tracks. A missing track is normal.
- The agent puts the user's playlist names into its own prompt at startup.
  Without them it cannot tell a playlist name from a song title, and it
  searches for the name instead of reading the playlist. The list is cached
  for five minutes, so a new playlist appears without a restart.
- Genres come from Last.fm when `LASTFM_API_KEY` is set, one tag lookup for
  each track. Without the key they come from MusicBrainz instead, which asks
  for one request each second and can only read artists, not tracks.
- Last.fm has few tags for music outside the Western catalogue. A playlist it
  does not know falls back to MusicBrainz, which takes longer than either
  source alone.
  `playlist_vibe` then reads only five artists and takes about seven seconds.
  Pass `genres=false` for a result in one second either way.
- `similar_artists` needs the Last.fm key. Spotify's own related-artists
  endpoint answers 403 for this app, so there is no other route to it.
- Spotify no longer sends an artist's genres, popularity, or follower count,
  and it no longer sends a podcast's publisher.
- `web_search` uses DuckDuckGo through the `ddgs` package. It needs no key.
  DuckDuckGo limits how often you may search. A burst of searches can fail.
- The connection to `accounts.spotify.com` can fail.
  The HTTP client then tries again three times.

## Deployment

You can deploy the web interface to Streamlit Community Cloud.
Put the three environment variables in the app secrets.
Make the app private. The app gives full control of your Spotify account.
