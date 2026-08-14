# Spotify MCP Server

This is the Spotify tool server for the autonomous Spotify agent.
It is an MCP server. It gives an agent five tools for the Spotify Web API.
For the full design, read [SPOTIFY_AGENT_ABSTRACT.md](SPOTIFY_AGENT_ABSTRACT.md).

## Tools

| Tool | Function |
|---|---|
| `recently_played(limit)` | Get the tracks that you played last. The maximum is 50. |
| `top_tracks(limit, time_range)` | Get the tracks that you played most. |
| `get_lyrics(track, artist)` | Get the lyrics from LRCLIB. This tool does not use Spotify. |
| `search_by_feel(valence, energy, acousticness, extra_query, limit)` | Find tracks with mood values from 0 to 1. |
| `create_playlist(name, track_uris, description)` | Make a private playlist and add the tracks. |

`create_playlist` accepts a track URI, a Spotify link, or an ID.

## Files

| File | Function |
|---|---|
| `spotify_mcp.py` | The MCP server. Start it with `python spotify_mcp.py`. |
| `agent.py` | The LangGraph agent. It calls the tools through MCP. |
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
Set `OPENROUTER_MODEL` to change the model. The model must support tool calls.

## Credentials

The server reads three Spotify variables from a `.env` file.
The agent reads `OPENROUTER_API_KEY` from the same file.
Use `.env.example` as the pattern.

1. Open the [Spotify dashboard](https://developer.spotify.com/dashboard).
2. Make an app. Write down the client ID and the client secret.
3. Set the redirect URI to `http://127.0.0.1:8888/callback`.
   Do not use `localhost`. Spotify refuses it.
4. Get a refresh token with these scopes:
   `user-read-recently-played`, `user-top-read`, and `playlist-modify-private`.
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
  The server uses `POST /me/playlists` and `POST /playlists/{id}/items`.
- Spotify has no lyrics endpoint. The lyrics come from LRCLIB.
- The `mcp` package must stay below version 2.0.
  `langchain-mcp-adapters` does not support version 2.0 yet.
- The agent has no memory. Each run starts a new conversation.
- The connection to `accounts.spotify.com` can fail.
  The HTTP client then tries again three times.

## Deployment

You can deploy the web interface to Streamlit Community Cloud.
Put the three environment variables in the app secrets.
Make the app private. The app gives full control of your Spotify account.
