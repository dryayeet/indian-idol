# Architecture

This page describes the current implementation. The original proposal is preserved
separately because durable psychological profiles and a closed feedback loop are not
implemented yet.

## System

```text
Streamlit UI or CLI
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
sessions open for one turn. The servers are stateless. Streamlit stores conversation
state in an `InMemorySaver`, so state survives Streamlit reruns but not a process
restart. A standalone CLI invocation starts without prior memory.

Streamlit's Tools mode and `run_tool.py` call the FastMCP applications in-process
instead of using the stdio transport. Image and PDF uploads also bypass MCP: they are
converted into model context by `agent.py`.

## Components

| Path | Responsibility |
|---|---|
| [`agent.py`](../agent.py) | Model client, MCP sessions, prompt, approval modes, uploads, streaming, and loop brakes. |
| [`spotify_mcp.py`](../spotify_mcp.py) | Spotify authentication and 22 music, library, search, analysis, and playlist tools. |
| [`psych_mcp.py`](../psych_mcp.py) | Big Five and emotion classifiers exposed as two MCP tools. |
| [`streamlit_app.py`](../streamlit_app.py) | Agent chat and schema-generated tool forms. |
| [`run_tool.py`](../run_tool.py) | Interactive and one-shot Spotify tool runner. |
| [`get_token.py`](../get_token.py) | Local Spotify OAuth callback and refresh-token setup. |
| [`benchmarks/`](../benchmarks/) | Live model and tool-selection evaluation harnesses. |
| [`tests/`](../tests/) | Headless Streamlit checks. |

## Data and State

- Secrets are loaded from the root `.env` file.
- Spotify access tokens are refreshed from the long-lived refresh token and cached in memory.
- Conversation checkpoints are process-local and are not durable.
- Benchmark JSON and logs are generated under `artifacts/benchmarks/`, which is ignored.
- The application has no database and no per-user authentication.

## Safety Boundary

`create_playlist` is the only Spotify write tool. Manual mode gates every tool call.
AFK mode gates `my_playlists`, `playlist_tracks`, and `create_playlist` while other
tools run automatically. Auto mode runs all calls. The deployment controls one
Spotify account, so it must not be exposed publicly.

## External Constraints

- Spotify no longer exposes recommendations or audio features to this app. ReccoBeats supplies partial audio-feature coverage.
- Spotify does not expose lyrics. LRCLIB supplies them.
- Similar artists and detailed genres require Last.fm; MusicBrainz is the slower genre fallback.
- Spotify-owned and other users' playlists are unavailable to this app's development-mode credentials.
- The `mcp` dependency remains below 2.0 for compatibility with `langchain-mcp-adapters`.

## Known Gaps

- Conversation and profile state do not survive process restarts.
- No durable weekly trait trajectory exists.
- No feedback signal measures whether generated playlists change later listening.
- Psychological model outputs are estimates, not clinical or diagnostic measurements.
- The Big Five endpoint returned HTTP 410 during the latest recorded Jev benchmark and needs revalidation.

See the [roadmap](roadmap.md) for planned work and the
[development log](history/development-log.md) for historical decisions.
