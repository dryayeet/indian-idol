# Architecture

Living document. Updated 2026-08-15.

For the project's intent and psychological framing, read
[SPOTIFY_AGENT_ABSTRACT.md](SPOTIFY_AGENT_ABSTRACT.md). This file records what is
actually built, why it is built that way, and what the environment forces.

## Current shape

```
                      ┌───────────────────────────┐
                      │  agent.py                 │
   OpenRouter ◀───────│  LangGraph ReAct agent    │
   (LLM, tool calls)  │  model ↔ tools loop       │
                      └─────────────┬─────────────┘
                                    │ MCP, stdio subprocess
                                    ▼
        ┌───────────────────────────────────────────────┐
        │  spotify_mcp.py    FastMCP server, stateless   │
        │                                               │
        │  recently_played   top_tracks   get_lyrics     │
        │  search_by_feel    create_playlist             │
        └──────────┬─────────────────────────┬──────────┘
                   │ OAuth refresh token     │ no auth
                   ▼                         ▼
          Spotify Web API              LRCLIB (lyrics)

   Other clients of the same tools:
     run_tool.py        CLI, interactive or key=value, no LLM
     streamlit_app.py   two modes: the agent above, or the tools as forms
```

The agent holds all state (currently only within a single run). The MCP server is
stateless: every tool call is self-contained, which keeps the server usable by any
MCP client, not just this agent.

## Components

| File | Role | Entry point |
|---|---|---|
| [spotify_mcp.py](spotify_mcp.py) | MCP server. Five tools, token refresh, HTTP retries. | `python spotify_mcp.py` (stdio) |
| [agent.py](agent.py) | LangGraph `create_react_agent`. Launches the server over stdio, reads its tool list, loops model ↔ tools. | `python agent.py "..."` |
| [run_tool.py](run_tool.py) | Manual tool runner. Lists tools, prompts for fields, prints results. | `python run_tool.py` |
| [streamlit_app.py](streamlit_app.py) | Web UI. Agent mode runs `agent.collect()`; Tools mode generates widgets from each tool's `inputSchema`. | `streamlit run streamlit_app.py` |
| [.env.example](.env.example) | The five environment variables. | copy to `.env` |

Adding a tool to `spotify_mcp.py` makes it appear in all three clients with no other
edit. That is the main reason the schemas are read at runtime rather than hardcoded.

## Working data flow: affective retrieval

1. User gives `agent.py` a request phrased as a feeling, not a genre.
2. The model maps it onto `search_by_feel`'s coordinates: `valence`, `energy`,
   `acousticness`, each 0 to 1, plus free text in `extra_query`.
3. `search_by_feel` turns those numbers into search keywords and calls
   `GET /v1/search`.
4. The model reads the tracks and either reports them or calls `create_playlist`.

The other workflow in the abstract (weekly trait drift) is not built. See Gaps.

## Decisions

**MCP over stdio rather than importing the functions.** The agent could import
`spotify_mcp` directly and skip the transport. It does not, because the abstract's
premise is that the tools are reusable by any MCP client. The cost is a subprocess
per run and a version pin (below).

**`httpx` directly, not `spotipy`.** The five calls needed are one line each. A
client library would add a dependency and its own auth model for no gain.

**Keyword search instead of audio-feature targets.** Spotify deprecated
`/v1/audio-features` and `/v1/recommendations` on 2024-11-27 for apps created after
that date, with no replacement. `target_valence` is therefore unavailable to this
app, and `search_by_feel` approximates it with mood keywords. This is the largest
gap between the abstract and the implementation.

**LRCLIB for lyrics.** Spotify has no public lyrics endpoint. LRCLIB needs no key
and no auth.

**OpenRouter as the LLM provider.** One key covers many models, and the model is
swappable through `OPENROUTER_MODEL` without touching code. Default is
`openai/gpt-4o-mini`. Any model chosen must support tool calling.

**Refresh token rather than an interactive login.** The server is launched by an
agent, not by a person, so it cannot open a browser. The refresh token is minted
once by hand and exchanged for access tokens on demand, cached in memory until 60
seconds before expiry.

## Environment constraints

These are all things the environment forces, not preferences. Each one cost a
debugging cycle.

| Constraint | Effect |
|---|---|
| `mcp` must stay `<2.0.0` | `langchain-mcp-adapters` does not support 2.0. This decides the whole API surface below. |
| mcp 1.x vs 2.0 API | 1.x is `FastMCP` and `Tool.inputSchema`; 2.0 is `MCPServer` and `Tool.input_schema`. Moving between them touches every client. |
| `call_tool()` return shape | 1.x returns a `(blocks, structured)` tuple, 2.0 returns an object with `.content`. Clients unpack the tuple. |
| Spotify endpoints moved 2026-02 | `POST /me/playlists` replaced `POST /users/{id}/playlists`; playlist `/tracks` became `/items`. The old paths return a bare 403 with no reason. |
| Redirect URI rules | Plain HTTP is allowed only on a literal loopback IP. Use `http://127.0.0.1:8888/callback`, never `localhost`. |
| TLS drops on this network | `accounts.spotify.com` intermittently drops the handshake. Every HTTP client is built with `httpx.HTTPTransport(retries=3)`. |
| A file named `mcp.py` | Shadows the installed `mcp` package and breaks the import. The server file must not be named that. |

## Gaps

Ordered by how much they block the abstract.

1. **No psych/emotion MCP server.** `get_big_five()` and `get_emotion_labels()` do
   not exist, so no trait or emotion inference happens anywhere. The weekly drift
   workflow is blocked entirely on this.
2. **No memory.** Each `agent.py` run is a fresh conversation. Tracking a profile
   across weeks needs a checkpointer for the thread and a separate store for the
   trait trajectory.
3. **`get_lyrics` is single-track.** Profiling a week of listening means one model
   round trip per track. A batch tool is needed before lyric-based inference is
   practical on free-tier limits.
4. **No feedback signal.** Nothing measures whether a playlist moved anything, so
   the loop in the abstract is open, not closed.

## Known risks

- **The deployed Streamlit app runs on one Spotify account.** There is no per-user
  login. Anyone who opens the public URL reads that account's history and writes to
  its library. If the OpenRouter key is added to the app's secrets, they also spend
  its credits. Restrict the app to named viewers.
- **Model estimates are not measurements.** This applies to every psychological
  claim the project makes, as the abstract states.

## Changelog

- **2026-08-15** — Added an Agent mode to the Streamlit app. `agent.collect()` is
  now the shared entry point for the CLI and the UI. The agent's MCP subprocess
  runs correctly from Streamlit's worker thread, which was the open risk.
- **2026-08-15** — Reverted the server to `FastMCP` and pinned `mcp<2.0.0` so
  `langchain-mcp-adapters` works. Agent now reaches the tools over MCP stdio
  instead of importing them. LLM provider switched from Anthropic direct to
  OpenRouter. Added `agent.py`, this document.
- **2026-08-14** — Built the MCP server, `run_tool.py`, and `streamlit_app.py`.
  Fixed the Feb 2026 playlist endpoint moves. Added URI normalisation and error
  bodies on failed Spotify calls.
