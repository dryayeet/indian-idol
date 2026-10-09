# Architecture

This page describes the current implementation. The original proposal is preserved
separately because durable psychological profiles and a closed feedback loop are not
implemented yet.

## System

```text
Streamlit UI or CLI
        |
        v
name login + Spotify connect (UI)
        |
        v
agent.py (LangGraph ReAct loop, OpenRouter model, in-process conversation state)
        |
        +---------------- MCP over stdio ----------------+
        |                                                |
        v                                                v
spotify_mcp.py (22 tools)                     psych_mcp.py (2 tools)
        |                                                |
        +--> Spotify Accounts and Web APIs               +--> Hugging Face Inference
        +--> LRCLIB
        +--> ReccoBeats
        +--> Last.fm / MusicBrainz
        +--> DuckDuckGo
```

`agent.py` starts both MCP servers with the current Python interpreter and keeps their
sessions open for one turn. The UI passes the signed-in profile's refresh token into
the Spotify subprocess's environment, so agent calls run on that profile's account;
the CLIs spawn without an override and keep the `.env` account. The servers are
stateless. Streamlit stores conversation state in an `InMemorySaver`, so state survives
Streamlit reruns but not a process restart. A standalone CLI invocation starts
without prior memory.

Streamlit's Tools mode calls both FastMCP applications in-process with the signed-in
profile's token bound only for each tool invocation (a ContextVar). `run_tool.py`
calls Spotify tools in-process on the `.env` account. Image and PDF uploads bypass
MCP and are converted into model context by `agent.py`.

## Components

| Path | Responsibility |
|---|---|
| [`agent.py`](../agent.py) | Model client, MCP sessions, prompt, approval modes, uploads, streaming, and loop brakes. |
| [`spotify_mcp.py`](../spotify_mcp.py) | Spotify authentication and 22 music, library, search, analysis, and playlist tools. |
| [`spotify_oauth.py`](../spotify_oauth.py) | Authorization Code flow: authorize URL, code exchange, `/me`, paste parser, and the local redirect listener. |
| [`psych_mcp.py`](../psych_mcp.py) | Paired-label zero-shot Big Five signals and GoEmotions classification exposed as two MCP tools. |
| [`streamlit_app.py`](../streamlit_app.py) | Name login, Spotify connect, agent chat, and schema-generated tool forms. |
| [`store.py`](../store.py) | Named profiles and their encrypted per-profile Spotify bindings in `data/users.db`. |
| [`run_tool.py`](../run_tool.py) | Interactive and one-shot Spotify tool runner on the `.env` account. |
| [`get_token.py`](../get_token.py) | Local Spotify OAuth callback that mints the `.env` account's refresh token. |
| [`benchmarks/`](../benchmarks/) | Live model and tool-selection evaluation harnesses. |
| [`tests/`](../tests/) | Headless Streamlit gate and control checks. |

## Data and State

- Local secrets are loaded from `.env`; the UI also reads configured Streamlit
  secrets for hosted runs. Hosted SQLite needs a persistent volume and the same
  `USERS_DB_KEY` across restarts.
- Spotify access tokens are refreshed from the long-lived refresh token and cached in
  memory, one cache entry per refresh token. The playlist-name cache uses the same
  account key, so switching profiles cannot reuse another's access token or playlists.
- Per-profile refresh tokens are Fernet-encrypted in `data/users.db` (gitignored)
  under `USERS_DB_KEY`; session state has identity and conversation state, but
  never tokens or encrypted token columns.
- Conversation checkpoints are process-local and are not durable.
- Benchmark JSON and logs are generated under `artifacts/benchmarks/`, which is ignored.
- The application has no passwords: a name selects a profile, it does not prove one.

## Safety Boundary

`create_playlist` is the only Spotify write tool. Manual mode gates every tool call.
AFK mode gates `my_playlists`, `playlist_tracks`, and `create_playlist` while other
tools run automatically. Auto mode runs all calls. A refused refresh token surfaces
as a `Spotify disconnected` marker, and the UI answers it with a reconnect screen
rather than a retry. The deployment binds several named profiles to one Spotify
developer app, so it still must not be exposed publicly.

## External Constraints

- Spotify no longer exposes recommendations or audio features to this app. ReccoBeats supplies partial audio-feature coverage.
- Spotify does not expose lyrics. LRCLIB supplies them.
- Similar artists and detailed genres require Last.fm; MusicBrainz is the slower genre fallback.
- Spotify-owned and other users' playlists are unavailable to this app's development-mode credentials.
- The `mcp` dependency remains below 2.0 for compatibility with `langchain-mcp-adapters`.

## Known Gaps

- Conversation state does not survive process restarts; SQLite profiles survive
  on a persistent disk.
- No durable weekly trait trajectory exists.
- No feedback signal measures whether generated playlists change later listening.
- Psychological model outputs are estimates, not clinical or diagnostic measurements.
- Current Big Five values are not comparable with values from the retired Minej model.
- Long Big Five inputs are sampled across the text with explicit character coverage.

See the [roadmap](roadmap.md) for planned work and the
[development log](history/development-log.md) for historical decisions.
