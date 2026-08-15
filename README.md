# Spotify MCP Server

This is the Spotify tool server for the autonomous Spotify agent.
It is an MCP server. It gives an agent nine tools for the Spotify Web API and LRCLIB.
For the intent, read [SPOTIFY_AGENT_ABSTRACT.md](SPOTIFY_AGENT_ABSTRACT.md).
For what is built and why, read [ARCHITECTURE.md](ARCHITECTURE.md).
For what is still owed, read [TODO.md](TODO.md).

## Tools

| Tool | Function |
|---|---|
| `recently_played(limit)` | Get the tracks that you played last. The maximum is 50. |
| `top_tracks(limit, time_range)` | Get the tracks that you played most. |
| `get_lyrics(track, artist)` | Get the lyrics from LRCLIB. This tool does not use Spotify. |
| `search_by_feel(description, valence, energy, acousticness, limit)` | Find tracks. The description does the searching. |
| `listening_lyrics(source, limit, chars)` | Get the lyrics of recent or top tracks in one call. |
| `search_by_lyrics(phrase, search_terms, candidates, limit)` | Find tracks whose lyrics match. Slower. Can return nothing. |
| `my_playlists(limit)` | List the playlists the user owns or follows. |
| `playlist_tracks(playlist, limit)` | Read the tracks in a playlist. |
| `create_playlist(name, track_uris, description)` | Make a private playlist and add the tracks. |

`create_playlist` accepts a track URI, a Spotify link, or an ID.

## Files

| File | Function |
|---|---|
| `spotify_mcp.py` | The MCP server. Start it with `python spotify_mcp.py`. |
| `agent.py` | The LangGraph agent. It calls the tools through MCP. |
| `get_token.py` | Mints the refresh token. Run it again when the scopes change. |
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

There are two providers. Choose one with `LLM_PROVIDER` in the `.env` file:

| `LLM_PROVIDER` | Key | Model variable | Default |
|---|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` | `openai/gpt-5.4-mini` |
| `gemini` | `GEMINI_API_KEY` | `GEMINI_MODEL` | `gemini-3.6-flash` |

The model must support tool calls.

## Credentials

The server reads three Spotify variables from a `.env` file.
The agent reads its provider key from the same file.
Use `.env.example` as the pattern.

1. Open the [Spotify dashboard](https://developer.spotify.com/dashboard).
2. Make an app. Write down the client ID and the client secret.
3. Set the redirect URI to `http://127.0.0.1:8888/callback`.
   Do not use `localhost`. Spotify refuses it.
4. Get a refresh token with these scopes:
   `user-read-recently-played`, `user-top-read`, `playlist-modify-private`,
   and `playlist-read-private`. Run `python get_token.py` to do this.
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

## Tests

Each file has an internal test. Give the `--selfcheck` argument:

```
python spotify_mcp.py --selfcheck
python run_tool.py --selfcheck
python agent.py --selfcheck
```

The agent test lists the tools through MCP. It does not call the language model.

## Limits

- The server uses one Spotify account. It has no login for each user.
  A person who opens the web interface has full control of that account.
- Spotify stopped the audio-feature endpoints on 27 November 2024.
  New apps cannot use `/v1/audio-features` or `/v1/recommendations`.
  Thus `search_by_feel` uses keywords, not audio-feature targets.
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
- The connection to `accounts.spotify.com` can fail.
  The HTTP client then tries again three times.

## Deployment

You can deploy the web interface to Streamlit Community Cloud.
Put the three environment variables in the app secrets.
Make the app private. The app gives full control of your Spotify account.
