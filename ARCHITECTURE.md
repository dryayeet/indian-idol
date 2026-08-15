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
        │  search_by_feel    search_by_lyrics            │
        │  my_playlists      playlist_tracks             │
        │  listening_lyrics  create_playlist             │
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
| [spotify_mcp.py](spotify_mcp.py) | MCP server. Nine tools, token refresh, HTTP retries. | `python spotify_mcp.py` (stdio) |
| [agent.py](agent.py) | LangGraph `create_react_agent`. Launches the server over stdio, reads its tool list, loops model ↔ tools. | `python agent.py "..."` |
| [run_tool.py](run_tool.py) | Manual tool runner. Lists tools, prompts for fields, prints results. | `python run_tool.py` |
| [streamlit_app.py](streamlit_app.py) | Web UI. Agent mode runs `agent.collect()`; Tools mode generates widgets from each tool's `inputSchema`. | `streamlit run streamlit_app.py` |
| [.env.example](.env.example) | The five environment variables. | copy to `.env` |

Adding a tool to `spotify_mcp.py` makes it appear in all three clients with no other
edit. That is the main reason the schemas are read at runtime rather than hardcoded.

## Working data flow: affective retrieval

1. User gives `agent.py` a request phrased as a feeling, not a genre.
2. The model writes a `description` of two to five title-like words, plus optional
   `valence`, `energy`, and `acousticness` from 0 to 1.
3. `search_by_feel` appends one word for the strongest axis and calls
   `GET /v1/search`, paging in tens. For requests about what a song *says*,
   `search_by_lyrics` reranks those candidates on their actual lyrics instead.
4. The model reads the tracks and either reports them or calls `create_playlist`.

The other workflow in the abstract (weekly trait drift) is not built. See Gaps.

## Decisions

**MCP over stdio rather than importing the functions.** The agent could import
`spotify_mcp` directly and skip the transport. It does not, because the abstract's
premise is that the tools are reusable by any MCP client. The cost is a subprocess
per run and a version pin (below).

**`httpx` directly, not `spotipy`.** Each call needed is one line. A
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
`openai/gpt-5.4-mini`. Any model chosen must support tool calling.

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
| Playlist reads are scoped | `GET /playlists/{id}/items` returns 403 without `playlist-read-private`, **even for public playlists**, and `GET /playlists/{id}` no longer carries track items. There is no unscoped way to read a playlist's contents. |
| `/v1/search` caps `limit` at 10 | A limit of 11 or more returns `400 Invalid limit`. `offset` paging still works, so `_search_tracks` pages in tens. This broke `search_by_feel`'s own default of 20 until it was found. |
| LRCLIB does not search lyrics | `/api/search?q=` matches track, artist, and album names. A verbatim lyric line returns zero hits, so there is no lyric-text search anywhere in the stack. |
| TLS drops on this network | `accounts.spotify.com` intermittently drops the handshake. Every HTTP client is built with `httpx.HTTPTransport(retries=3)`. |
| A file named `mcp.py` | Shadows the installed `mcp` package and breaks the import. The server file must not be named that. |

## Gaps

Ordered by how much they block the abstract. The actionable form of this list,
with everything else still owed, is [TODO.md](TODO.md).

1. **No psych/emotion MCP server.** `get_big_five()` and `get_emotion_labels()` do
   not exist, so no trait or emotion inference happens anywhere. The weekly drift
   workflow is blocked entirely on this.
2. **Memory does not outlive the process.** The Streamlit chat remembers a
   conversation through an `InMemorySaver`, but a restart loses it and the CLI is
   stateless. A trait trajectory across weeks needs durable storage.
3. **No feedback signal.** Nothing measures whether a playlist moved anything, so
   the loop in the abstract is open, not closed.

## Known risks

- **The deployed Streamlit app runs on one Spotify account.** There is no per-user
  login. Anyone who opens the public URL reads that account's history and writes to
  its library. If the OpenRouter key is added to the app's secrets, they also spend
  its credits. Restrict the app to named viewers.
- **Model estimates are not measurements.** This applies to every psychological
  claim the project makes, as the abstract states.

## Changelog

- **2026-08-15** — Added `listening_lyrics`, which collects recent or top tracks
  and their lyrics in one call, deduplicated and concurrently fetched. It removes
  the round-trip-per-song cost that blocked lyric-based profiling, and caps each
  lyric so a week of listening does not flood the context.
- **2026-08-15** — Added `search_by_lyrics`: Spotify supplies candidates, LRCLIB
  supplies the lyrics, ranking happens locally on term coverage. It is the first
  path that matches what a song says rather than what it is called, and the first
  that can honestly return nothing. Also found and fixed a live bug: `/v1/search`
  now rejects any `limit` above 10, so `search_by_feel`'s default of 20 was failing;
  searches now page by tens through `_search_tracks`.
- **2026-08-15** — Added `my_playlists` and `playlist_tracks`. Both need the
  `playlist-read-private` scope, which the existing refresh token does not carry,
  so `get_token.py` is restored with that scope added and must be run again.
- **2026-08-15** — Told the model the truth about the search engine. Probing
  `/v1/search` showed it matches track, artist, and album names only, ignores most
  of a long phrase, and never returns empty (gibberish still yields tracks). The
  `description` docstring and the system prompt now ask for two to five title-like
  words and warn that results need judging.
- **2026-08-15** — `agent.run(question, on_part, ...)` streams each tool call and
  reply through a callback as it happens; `collect()` is now a buffered wrapper on
  it. The Streamlit chat renders each step on arrival instead of after the run.
- **2026-08-15** — Conversation memory and a chat UI. `agent.collect()` accepts a
  checkpointer and a `thread_id`; the Streamlit app holds one `InMemorySaver` per
  process (`@st.cache_resource`) and one thread per browser session, so follow-ups
  resolve against earlier turns. History is lost on restart, by choice. Tracks now
  carry a `url`, so links come from the tool instead of being built by the model.
- **2026-08-15** — `search_by_feel` now takes a required `description` that carries
  the search, with the numbers demoted to a single modifier word. Removed the
  `"music"` fallback: mid-range values used to produce an empty query, so the tool
  searched the literal word "music". Default model is `openai/gpt-5.4-mini`.
  Added [TODO.md](TODO.md).
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
