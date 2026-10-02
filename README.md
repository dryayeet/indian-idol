# Spotify Agent

An affect-aware music assistant built with LangGraph, FastMCP, Streamlit, and the
Spotify Web API. It combines 22 music and account tools with two Hugging Face
psychology tools, then exposes them through an agent chat, direct tool forms, and a
command-line client.

The project can inspect listening history, search by mood or lyrics, analyze tracks
and playlists, read images and PDFs, and create private playlists with approval
controls. It is a personal project for one Spotify account, not a multi-user service
or a clinical assessment tool.

## Architecture

```text
Streamlit or CLI
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

Agent mode starts both MCP servers as subprocesses. Streamlit's Tools mode and
`run_tool.py` call the same FastMCP applications in-process. Conversation state is
kept in memory and is lost when the process restarts.

See [Architecture](docs/architecture.md) for the full system description.

## Requirements

- Python 3.10 or newer. The dev container uses Python 3.11.
- A Spotify developer application and refresh token.
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

Fill `.env`, mint the Spotify refresh token, then start the UI:

```powershell
python get_token.py
python -m streamlit run streamlit_app.py
```

The OAuth helper listens on `http://127.0.0.1:8888/callback` and writes the
Spotify credentials to the root `.env` file. Register that exact redirect URI in
the Spotify dashboard. Spotify rejects `localhost` for this flow.

## Configuration

| Variable | Required | Used for |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | Yes | Spotify OAuth and API access. |
| `SPOTIFY_CLIENT_SECRET` | Yes | Spotify OAuth and token refresh. |
| `SPOTIFY_REFRESH_TOKEN` | Yes | Long-lived Spotify account authorization. |
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
|-- psych_mcp.py             # 2 Hugging Face tools
|-- streamlit_app.py         # chat and direct tool UI
|-- run_tool.py              # direct Spotify tool CLI
|-- get_token.py             # Spotify OAuth helper
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
python -m unittest -v tests.test_psych
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

- The app controls one Spotify account and has no per-user login.
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

Do not expose this application publicly. Anyone who can open the UI can read the
configured Spotify account and can select auto mode to create playlists. They can
also spend the configured model-provider credits.

For Streamlit Community Cloud, configure all required Spotify and OpenRouter secrets,
plus `HF_TOKEN` if psych tools are enabled, and restrict the application to trusted
viewers. See [SECURITY.md](SECURITY.md) for reporting and handling guidance.
