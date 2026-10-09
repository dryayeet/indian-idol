# Spotify Agent

An affect-aware music assistant built with LangGraph, FastMCP, Streamlit, and the
Spotify Web API. It combines 22 music and account tools with two Hugging Face
psychology tools, then exposes them through an agent chat, direct tool forms, and a
command-line client.

The UI gates every session behind a name login and a Spotify connect: each profile
carries its own encrypted Spotify authorization, and every call runs on the signed-in
profile's account. The CLIs keep using the `.env` account.

The project can inspect listening history, search by mood or lyrics, analyze tracks
and playlists, read images and PDFs, and create private playlists with approval
controls. It is a personal project for a small set of named profiles on one Spotify
developer app, not a multi-user service or a clinical assessment tool.

## Architecture

```text
Streamlit or CLI
      |
      v
Name login + Spotify connect (UI only)
      |
      v
LangGraph agent + OpenRouter
      |
      +---------------- MCP over stdio ----------------+
      |                                                |
      v                                                v
Spotify MCP server (22 tools)               Psych MCP server (2 tools)
      |                                                |
      + Spotify, LRCLIB, ReccoBeats                    + Hugging Face
      + Last.fm, MusicBrainz, DuckDuckGo
```

Agent mode starts both MCP servers as subprocesses, with the signed-in profile's
refresh token injected into the Spotify server's environment per turn. Streamlit's
Tools mode and `run_tool.py` call the same FastMCP applications in-process. In the
UI, conversation state is kept in memory and is lost when the process restarts;
profile bindings and encrypted tokens live in `data/users.db`.

See [Architecture](docs/architecture.md) for the full system description.

## Requirements

- Python 3.10 or newer. The dev container uses Python 3.11.
- A Spotify developer application; the UI obtains each viewer's refresh token
  during connection. The CLI additionally needs one in `.env`.
- An OpenRouter API key for the conversational agent.
- A Hugging Face token for the two psych tools.
- An optional Last.fm key for similar artists and better genre tags.

Run all commands from the repository root.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env`, register
`http://127.0.0.1:8888/callback` in the Spotify dashboard, then start the UI:

```powershell
python -m streamlit run streamlit_app.py
```

To use `agent.py` or `run_tool.py` directly, run `python get_token.py` once to
write their separate `.env` refresh token. Spotify rejects `localhost` for this
flow; use the exact `127.0.0.1` redirect above.

## Configuration

| Variable | Required | Used for |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | Yes | Spotify OAuth and API access. |
| `SPOTIFY_CLIENT_SECRET` | Yes | Spotify OAuth and token refresh. |
| `SPOTIFY_REFRESH_TOKEN` | CLIs | The `.env` account for `agent.py` and `run_tool.py`; the UI uses per-profile tokens instead. |
| `USERS_DB_KEY` | Auto locally; required for a durable hosted deployment | Fernet key encrypting per-profile refresh tokens in `data/users.db`; generated into `.env` on first local connect. |
| `OPENROUTER_API_KEY` | Agent mode | Conversational model and live benchmarks. |
| `OPENROUTER_MODEL` | No | Tool-capable OpenRouter model override. |
| `HF_TOKEN` | Psych tools | Big Five and emotion inference; needs Inference Providers permission. |
| `LASTFM_API_KEY` | No | Similar artists and track-level genre tags. |

The default model and selection rationale are documented in the
[model bake-off](docs/benchmarks/model-bakeoff.md).

The Spotify refresh token requests these scopes:

- `user-read-recently-played`
- `user-top-read`
- `playlist-modify-private`
- `playlist-read-private`
- `user-follow-read`
- `user-library-read`
- `user-read-playback-state`

Never commit `.env` or Streamlit secrets. The repository ignore rules cover both.

## Running the Project

Start the web application:

```powershell
python -m streamlit run streamlit_app.py
```

Ask the agent one question from the CLI:

```powershell
python agent.py "songs that feel like driving away from my hometown"
```

Call a Spotify tool directly:

```powershell
python run_tool.py get_lyrics track="Motion Sickness" artist="Phoebe Bridgers"
```

Run `python run_tool.py` with no arguments for interactive tool selection. Starting
`spotify_mcp.py` or `psych_mcp.py` directly opens an MCP stdio server, so waiting
silently for a client is expected.

## Profiles and Spotify Login

The UI opens on a name screen. A name nobody uses yet creates nothing: the profile
is only created after Spotify authorizes, because the Spotify account id is the
strong identity. A name someone already uses shows "Is this your account?" cards
(joined date, last seen, connected-as); "Yes, it's me" enters that profile, and "No,
this is a new account" runs the connect.

The connect screen sends the viewer to the Spotify authorize page with a per-attempt
`state`. Locally, a listener on `127.0.0.1:8888` catches the redirect; return to
the app and click **I approved it, check again**. Alternatively, paste the **full**
redirected URL and click **Use pasted redirect**. The paste option also works
with a hosted UI, where the redirect hits the viewer's own machine and the code
is visible in the browser address bar even if that page cannot load. The URL
must point to the registered callback, and the code and state must match this
attempt. Missing scopes or tokens abort the connect without creating a profile.

- Tokens are Fernet-encrypted in `data/users.db` (gitignored), keyed by
  `USERS_DB_KEY`; session state never holds them or their ciphertext.
- Connecting an id that another profile already owns offers that profile instead of
  creating a duplicate.
- A refused refresh, repeated 401, or unreadable stored token leads to the
  reconnect screen; the sidebar's Reconnect button reaches it any time.
- Name confirmation is a convenience, **not proof of identity**. Anyone with
  access to the UI can select a profile and use its stored Spotify connection.
- For hosted Streamlit, configure client credentials and a stable `USERS_DB_KEY`
  as secrets. Persist `data/users.db` separately: ephemeral hosting storage
  loses profiles on restart.
- Logout, local revoke, and the 6-month re-auth banner are still on the roadmap.

## Approval Modes

| Mode | Behavior |
|---|---|
| `/manual` | Every tool call waits for approval. |
| `/afk` | Most reads run automatically; `my_playlists`, `playlist_tracks`, and `create_playlist` wait. |
| `/auto` | All tools run without approval. |

The UI starts in AFK mode. `/mode` shows the current mode and `/help` lists the
available commands. `create_playlist` is the only Spotify write tool.

## Tool Inventory

### Music, Search, and Analysis

| Tool | Purpose |
|---|---|
| `search_by_feel` | Search by a short emotional description and optional feature targets. |
| `search_by_lyrics` | Fetch and rank candidate lyrics for a phrase or theme. |
| `web_search` | Find music context that Spotify does not provide. |
| `get_lyrics` | Retrieve one track's lyrics from LRCLIB. |
| `listening_lyrics` | Collect lyrics from recent or top listening in one call. |
| `track_features` | Read ReccoBeats audio features for one track. |
| `playlist_vibe` | Summarize playlist audio features, artists, and optional genres. |
| `similar_artists` | Find similar artists through Last.fm. |
| `artist_albums` | List an artist's releases. |
| `album_tracks` | List tracks from an album. |

### Spotify Account and Library

| Tool | Purpose |
|---|---|
| `recently_played` | Read recently played tracks. |
| `top_tracks` | Read top tracks for a Spotify time range. |
| `top_artists` | Read top artists for a Spotify time range. |
| `followed_artists` | List followed artists. |
| `liked_songs` | List saved tracks. |
| `saved_albums` | List saved albums. |
| `saved_podcasts` | List saved podcasts. |
| `now_playing` | Read the currently playing track. |
| `my_playlists` | List owned and followed playlists visible to the app. |
| `playlist_names` | Return playlist names for agent context. |
| `playlist_tracks` | Read an accessible playlist by name, ID, URI, or URL. |
| `create_playlist` | Create a private playlist and add tracks. |

### Psychology

| Tool | Purpose |
|---|---|
| `get_big_five` | Estimate five heuristic OCEAN trait signals from text. |
| `get_emotion_labels` | Estimate probabilities over 28 emotion labels. |

Psychological outputs are model estimates, not diagnoses. Big Five uses the hosted
`facebook/bart-large-mnli` zero-shot classifier with paired high/low labels for each
trait. The resulting values are heuristic evidence, not calibrated psychometric
scores, and values from the retired Minej model are not longitudinally comparable.
Long inputs use up to 12 evenly distributed safe-size samples and report scored versus
total characters instead of silently truncating text. Sample results are weighted by
their character coverage.

## Uploads

The Streamlit chat accepts PNG, JPEG, WebP, and PDF files. Images are resized, sent
through a dedicated vision-reading call, and also attached to the first agent turn.
Later turns receive only the stored reading. Text PDFs are extracted with `pypdf`.
Image-only pages are sent through vision, with a limit of 12 scanned pages.

Uploads are model context, not MCP tools. They are useful for screenshots of Spotify
content the API cannot access, such as Blends or another user's playlist.

## Project Layout

```text
.
|-- agent.py                 # LangGraph agent and upload handling
|-- spotify_mcp.py           # 22 music and Spotify tools
|-- spotify_oauth.py          # Authorization Code flow shared by UI and CLI
|-- psych_mcp.py             # 2 Hugging Face tools
|-- streamlit_app.py         # login, chat, and direct tool UI
|-- store.py                 # profiles and encrypted Spotify bindings
|-- run_tool.py              # direct Spotify tool CLI
|-- get_token.py             # Spotify OAuth helper for the .env account
|-- benchmarks/              # live evaluation harnesses
|-- tests/                   # headless UI checks
|-- docs/                    # current, historical, and research documentation
|-- artifacts/               # ignored benchmark JSON and logs
|-- .env.example             # configuration template
`-- requirements.txt
```

Core runtime modules remain at the root because they launch and import each other
directly. Benchmarks resolve the root explicitly and write generated output only to
`artifacts/benchmarks/`.

## Documentation

| Document | Purpose |
|---|---|
| [Architecture](docs/architecture.md) | Current components, data flow, safety boundary, and gaps. |
| [Roadmap](docs/roadmap.md) | Planned work, technical debt, and completed milestones. |
| [Original project proposal](docs/project-abstract.md) | Intended psychological framing and closed-loop design. |
| [Development log](docs/history/development-log.md) | Historical architecture snapshot and implementation diary. |
| [Model bake-off](docs/benchmarks/model-bakeoff.md) | Main model evaluation and default-model rationale. |
| [Jev tool-selection study](docs/benchmarks/jev-tool-selection.md) | Current 41-case routing comparison. |
| [Jev pilot](docs/benchmarks/archive/jev-pilot.md) | Superseded six-case pilot retained for history. |
| [Multi-model design](docs/design/multi-model.md) | Embedding, verifier, failover, and cascade analysis. |
| [Spotify API survey](docs/research/spotify/api-surface.md) | Live endpoint survey and third-party replacements. |
| [Jev reference notes](docs/research/jev/reference.md) | Imported API research with repository-specific scope notes. |
| [LinkedIn draft](docs/outreach/linkedin-jev-draft.md) | Outreach draft based on the Jev experiments. |
| [Intelligence levels](docs/research/models/intelligence-levels.pdf) | Research notes on reasoning budgets and agent loops. |
| [Image generation](docs/research/chat-ui/image-generation.pdf) | Research notes on image tools and rendering. |
| [Charts and graphs](docs/research/chat-ui/charts-and-graphs.pdf) | Research notes on chart rendering. |
| [Document artifacts](docs/research/chat-ui/document-artifacts.pdf) | Research notes on PDF and editable artifact pipelines. |
| [Quote replies](docs/research/chat-ui/quote-replies.pdf) | Research notes on reply context. |
| [Message editing](docs/research/chat-ui/message-editing-and-branching.pdf) | Research notes on branching conversation storage. |

The research PDFs describe possible chat-product features. They are not claims about
features implemented in this repository.

## Benchmarks

The harnesses call live services and may cost money unless run with `--selfcheck`.

```powershell
python benchmarks/bakeoff.py --selfcheck
python benchmarks/bakeoff_jev.py --selfcheck
python benchmarks/bakeoff_jev_40.py --selfcheck
```

Live results and logs are written under `artifacts/benchmarks/`. That directory is
local-only and ignored because results are generated, environment-specific, and can
contain workstation paths or HTTP traces. Curated findings belong in `docs/benchmarks/`.

## Checks

```powershell
python spotify_mcp.py --selfcheck
python psych_mcp.py --selfcheck
python spotify_oauth.py --selfcheck
python get_token.py --selfcheck
python -m unittest -v tests.test_psych
python -m unittest -v tests.test_store
python -m unittest -v tests.test_auth
python run_tool.py --selfcheck
python agent.py --selfcheck
python benchmarks/bakeoff.py --selfcheck
python benchmarks/bakeoff_jev.py --selfcheck
python benchmarks/bakeoff_jev_40.py --selfcheck
python tests/test_ui.py
```

Most self-checks avoid model calls. `agent.py --selfcheck` starts both MCP servers,
reads Spotify playlist names, and asserts at least one is visible. It requires valid
Spotify credentials and network access. The UI check uses Streamlit's headless test
harness and makes no model calls.

## Current Limitations

- The UI serves named profiles with per-profile Spotify bindings, but has no
  passwords: anyone who can open it can use any profile, so keep it local or
  restrict it to trusted viewers.
- Conversation state is in memory and disappears on restart.
- Spotify's recommendation and audio-feature endpoints are unavailable to this app.
- ReccoBeats restores partial audio features but has uneven catalogue coverage.
- LRCLIB supplies lyrics because Spotify has no public lyrics endpoint.
- Spotify-owned playlists and other users' playlists are not readable with this app's credentials.
- Last.fm coverage is uneven outside Western catalogues; MusicBrainz fallback is slower.
- DuckDuckGo can rate-limit bursts of web searches.
- Big Five values are zero-shot trait signals rather than validated psychometric scores.
- Durable weekly profiles and intervention feedback from the original proposal are not implemented.
- `mcp` must remain below 2.0 until `langchain-mcp-adapters` supports it.

## Deployment and Security

Do not expose this application publicly. Anyone who can open the UI can pick any
profile and spend the configured model-provider credits; approval modes help, but
`auto` is one click away. Name login is a lookup handle, not a password.

For Streamlit Community Cloud, configure all required Spotify and OpenRouter secrets,
plus `HF_TOKEN` if psych tools are enabled, and restrict the application to trusted
viewers. See [SECURITY.md](SECURITY.md) for reporting and handling guidance.
