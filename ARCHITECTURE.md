# Architecture

Living document. Updated 2026-08-16.

For the project's intent and psychological framing, read
[SPOTIFY_AGENT_ABSTRACT.md](SPOTIFY_AGENT_ABSTRACT.md). This file records what is
actually built, why it is built that way, and what the environment forces.

## Current shape

```
                      ┌───────────────────────────┐
                      │  agent.py                 │
   OpenRouter ◀───────│  LangGraph ReAct agent    │
   (the LLM)          │  model ↔ tools loop       │
                      └─────────────┬─────────────┘
                                    │ MCP, one stdio session per run
                                    ▼
        ┌───────────────────────────────────────────────┐
        │  spotify_mcp.py    FastMCP server, stateless   │
        │                                               │
        │  recently_played   top_tracks   get_lyrics     │
        │  search_by_feel    search_by_lyrics            │
        │  my_playlists      playlist_tracks             │
        │  followed_artists  saved_podcasts              │
        │  similar_artists   playlist_vibe   web_search  │
        │  listening_lyrics  create_playlist             │
        └──────────┬─────────────────────────┬──────────┘
                   │ OAuth refresh token     │ no auth
                   ▼                         ▼
          Spotify Web API              LRCLIB (lyrics)

   Other clients of the same tools:
     run_tool.py        CLI, interactive or key=value, no LLM
     streamlit_app.py   two modes: the agent above, or the tools as forms
```

The agent holds all state: conversation memory in the Streamlit chat, nothing in the
CLI. The MCP server is stateless, so every tool call is self-contained and the server
stays usable by any MCP client, not just this agent.

## Components

| File | Role | Entry point |
|---|---|---|
| [spotify_mcp.py](spotify_mcp.py) | MCP server. The tools, token refresh, HTTP retries. | `python spotify_mcp.py` (stdio) |
| [agent.py](agent.py) | LangGraph `create_react_agent`. Launches the server over stdio, reads its tool list, loops model ↔ tools. | `python agent.py "..."` |
| [ui_check.py](ui_check.py) | Runs the Streamlit app under its own test harness and asserts the mode controls agree. No model calls. | `python ui_check.py` |
| [bakeoff.py](bakeoff.py) | Scores models on the four cases that matter here: right tool, no forbidden writes, no repeated calls, usable answer. | `python bakeoff.py` |
| [run_tool.py](run_tool.py) | Manual tool runner. Lists tools, prompts for fields, prints results. | `python run_tool.py` |
| [streamlit_app.py](streamlit_app.py) | Web UI. Agent mode streams `agent.run()` into a chat; Tools mode generates widgets from each tool's `inputSchema`. | `streamlit run streamlit_app.py` |
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
per run and a version pin (below). One session is held open for the whole turn; the
alternative, connecting per tool call, measured 3.3s of overhead on 0.4s of work.

**`httpx` directly, not `spotipy`.** Each call needed is one line. A
client library would add a dependency and its own auth model for no gain.

**Keyword search instead of audio-feature targets.** Spotify deprecated
`/v1/audio-features` and `/v1/recommendations` on 2024-11-27 for apps created after
that date, with no replacement. `target_valence` is therefore unavailable to this
app, and `search_by_feel` approximates it with mood keywords. This is the largest
gap between the abstract and the implementation.

**LRCLIB for lyrics.** Spotify has no public lyrics endpoint. LRCLIB needs no key
and no auth.

**One LLM provider: OpenRouter.** A second provider (Gemini) was carried for a day and removed: it bought nothing that OpenRouter's own catalogue does not, since Gemini models are sold there too, and it doubled every code path that touched the model. The model is swappable through `OPENROUTER_MODEL` without touching code.

**Approval as an interrupt, not a wrapper around each tool.** Gating happens in the
graph (`interrupt_before=["tools"]`), not inside the tool functions, so the pause
survives a Streamlit rerun: state lives in the checkpointer and the graph is rebuilt
on each script run. Wrapping the tools instead would have needed the UI round trip to
happen inside a running coroutine, which Streamlit's execution model cannot do.

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
| The same rename hit three layers | The endpoint, each row's payload (`track` → `item`), and the playlist object's count field (`tracks` → `items`) all moved. Each one failed silently as an empty list or a `None`, so they were found one at a time. |
| Spotify-owned playlists are gone | Blends, Daily Mix, and Discover Weekly are absent from `/me/playlists` and answer 404 by id, even with a user token. Extended quota mode is the only route and has needed 250k monthly active users since May 2025. Another user's playlist answers 403. |
| Artist and show objects were trimmed | Genres, popularity, and follower counts are no longer on the artist object, `GET /artists` is 403 in development mode, and a show no longer carries its publisher. `followed_artists` returns names because names are what exists. |
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

- **2026-08-18** — The psych MCP server, which is the abstract's missing half.
  `psych_mcp.py` exposes `get_big_five` and `get_emotion_labels` over HF Inference,
  wired into the agent as a second stdio server (one `SERVERS` entry plus a second
  session in `build()`), and the Tools page routes each tool to its home server.
  Three things the build surfaced. The abstract's Big Five model
  (`vladinc/bigfive-regression-model`) is served by no provider, so Big Five runs on
  `Minej/bert-base-personality`, which is warm; checked by probing the hub's
  `inference` field, not by reading docs. That model ships no label names, the API
  answers `LABEL_0..4`, and the order comes from its model card. And the API's default
  softmax was silently wrong for it: the card's five traits are independent sigmoids,
  so the scores summed to 1 and competed; `function_to_apply: "sigmoid"` fixes it.
  Text is scored in ~1,500-char chunks and mean-pooled with the chunk count reported,
  because BERT truncates at 512 tokens and a single call would score one song and call
  it the week. Honest reading of the first live numbers: emotions discriminate
  sharply (sadness 0.898 on a sad text, joy 0.855 on a euphoric one), Big Five barely
  moves on short text, which is what trait models are supposed to do; it needs the
  long aggregates the abstract intended. Workflow 1 now runs end to end:
  `listening_lyrics → get_emotion_labels → get_big_five → interpretation`, three of
  three runs after the prompt's tool-first rule was re-sharpened. The dedupe had left
  it leading with links, and a "where's my head at" reply needs no links, so half the
  runs answered with zero tools and an invented reading. The rule now opens with
  "every answer about this person starts with a tool call this turn".
- **2026-08-18** — The prompt deduplicated against the tool docstrings, 1,157 tokens
  to 885. Docstrings ride the schemas into every request, and three blocks of SYSTEM
  restated them nearly verbatim: search_by_feel's mechanics, search_by_lyrics' whole
  paragraph, and all of the web block. One real contradiction fell out: the prompt
  bounded search retries at two or three phrasings while search_by_feel's docstring
  still said "try different words rather than presenting nothing", unbounded, arguing
  for last week's loop. The bound now lives in the docstrings. What stayed is
  everything traceable to a fixed bug: the voice rules, the tool-first and link rules,
  the playlist-name rule, the artist-routing rule, the attachment assertion. The four
  protected behaviours were re-run after the cut and all hold, including the best
  version of the "more dodie songs I don't listen to" turn yet seen: it read the
  user's liked, top and recent tracks, then dodie's albums, and diffed them, which is
  the request actually answered.
- **2026-08-18** — `track_features`: the numbers for one track. Asked for the valence
  of a named song, the agent correctly said no tool covered a single track, then
  guessed anyway, "likely 0.4-0.5" for a song whose measured valence is 0.167, and
  invented advice about the Spotify web player displaying features. The measurement
  machinery existed (`_features` over ReccoBeats) but only playlists could use it. The
  tool resolves a name through search with the same relevance guard as
  `search_by_feel`, since Spotify answers even nonsense with arbitrary tracks and a
  wrong match here returns a real song's numbers for a song that does not exist. An
  unmeasured track answers `measured: false` with an instruction not to guess, which
  is ordinary for non-Western catalogues, not an error.
- **2026-08-18** — The search loop had three fuels, and the exact-repeat brake only
  cut one. Results always fed back to the model verbatim; what was missing was anything
  evaluative. Fuel one was an instruction: "never tell the user there is nothing after
  one empty search" had no upper bound, so every empty result was an instructed retry.
  Now bounded at two, at most three phrasings. Fuel two was error shape: a rate-limited
  `web_search` surfaced as a bare `Error: ...`, which reads as "try again", and
  retrying a rate limit deepens it; the tool now reports the failure as final for the
  turn, the same move as the playlist 403. Fuel three was rewording: each retry changes
  one adjective, so the exact-repeat check never fires; `_drive` now also counts calls
  per tool and blocks the seventh call to any one tool (six, because fanning
  similar_artists over followed artists legitimately runs five). Tested with a request
  built to miss on every phrasing: search_by_feel stopped at exactly 6, the model
  pivoted tools, and the reply admitted what it could not find. The soft prompt bound
  was ignored (it used all six), which is the expected shape: prompts nudge, brakes
  enforce.
- **2026-08-18** — A PDF upload was refused by the model that was holding its text.
  "Look at what I attached" got "I can't read attached files directly", and the fix
  hunt went down the wrong ladder: extraction worked (3,952 chars from the real file),
  the message carried both blocks, the raw model read the same blocks fine. The failure
  was a belief, not a pipe: qwen carries a trained prior that PDF attachments are
  unreadable binaries, and in two of three runs it recited that disclaimer over text
  sitting in its own context, sometimes refusing outright, sometimes disclaiming and
  then using the content anyway. Verbatim per-run variance, nothing deterministic.
  The fix is framing: the block now opens with 'The user attached "name". It has
  already been read; its complete extracted text is:' and the prompt says refusing an
  attachment is refusing text you already have. Three of three runs now use the
  content, though one still prefixed a reflexive disclaimer before answering. Worth
  remembering alongside the playlist-name lesson: this time the model HAD the
  information and disbelieved it, so the fix was assertion, not information.
- **2026-08-18** — Uploads: images and PDFs in the chat. `qwen3.5-flash` takes image
  input natively (verified live: a synthetic playlist screenshot transcribed
  character-perfect through the same `_llm()` the agent uses), so no second model and
  no routing. The design is read-once: `prepare_upload` makes one vision call whose
  full reading rides the message's `additional_kwargs`; the first turn sends real
  pixels, and the shrink hook thereafter swaps the image block for the reading, so a
  screenshot costs its tokens once, not every turn. Verified: turn two answered "what
  did the bottom line say" exactly, with zero image blocks resent. PDFs are pypdf text
  per page, and a page with no text but an embedded image (any phone scan) is
  vision-read instead of silently dropped, which is what `extract_text()` alone does;
  capped at twelve scanned pages. Uploads never touch the MCP server: they are
  conversation context, not tools. One hallucination surfaced in testing: given a
  screenshot, the model claimed measurements without calling any tool, so the prompt
  now says an attachment is what the user showed you, not a tool result, and numbers
  still come from tools. After that it read the screenshot, named the playlist, and
  measured the real one with `playlist_vibe`. The main use is the API wall: a Blend or
  a friend's playlist is unreadable through Spotify but perfectly readable as a
  screenshot.
- **2026-08-18** — A brake on tool loops, and a Stop button. "more dodie songs I
  don't listen to" ran 50+ near-identical `search_by_feel` calls: the searches came
  back empty, the prompt says never to give up after one empty search, and nothing
  braked it, because LangGraph's own 25-step recursion limit resets every time a gated
  graph resumes, which in afk mode is every auto-approved read. `_drive` now keys each
  round of calls (order-insensitive) and, on an exact repeat or past 20 rounds, answers
  the calls with "stop and conclude" instead of results, the same inject-a-ToolMessage
  mechanism decline uses; five rounds past that it cuts the turn off. Ungated runs get
  the built-in recursion error caught and reported instead of raised. Fixing this
  exposed a display bug that had been there all along: resuming re-emits the paused
  state, so every auto-approved call was reported twice; tool reports are now deduped
  by message id. The prompt also learned that "more music by an artist" is
  `artist_albums`/`similar_artists`, not a feel search on the artist's name. The
  Streamlit chat got a Stop button, which works by Streamlit's own interruption model:
  it is rendered before the blocking call, clicking it halts the script at the next
  `st` call, and the handler files what had already streamed as "_stopped_" rather
  than losing it. The exception handlers had to learn to re-raise Streamlit's
  control-flow exceptions instead of printing them as errors.
- **2026-08-17** — Last.fm, found by surveying comparable agents rather than by reading
  Spotify's documentation. `artist.getSimilar` is a working replacement for
  `/related-artists`, which has been 403 here since Feb 2026, so `similar_artists` puts
  "more like this" back. `track.getTopTags` gives a tag per track where MusicBrainz can
  only give one per artist, and has no one-per-second limit. Measured, not predicted:
  a 46-track rap playlist went from 7.7s to 3.9s and its tags got sharper, naming
  `conscious hip hop` and `west coast hip hop` rather than the artists' whole careers.
  A 15-track Sufi and Bollywood playlist got *slower*, 7.8s, because Last.fm has no
  tags for those tracks at all and the MusicBrainz fallback runs after the attempt.
  Same Western bias as ReccoBeats, and worth remembering before adding a third source
  that shares it. Two filters were needed on the way: Last.fm tags are user-written, so
  the artist's own name is usually the top "tag" (compared on letters only, since the
  tag reads `j cole` where the artist is `J. Cole`), and `hip-hop` and `hip hop` arrive
  as separate tags. It is the only source
  here that needs a key, so everything degrades without one: `_lastfm` returns `{}`,
  genres fall back to the MusicBrainz path unchanged, and `similar_artists` says where
  to get a key rather than failing quietly. Worth noting where this came from: every
  comparable project surveyed (PersonalAIs, Moodify, the LangGraph DJ agents) is built
  on `/audio-features` and `/recommendations` and appears not to have noticed they are
  dead. Reading their dependencies was more useful than reading their features.
- **2026-08-17** — The prompt is tagged blocks now, and the replies stopped reading
  like a press release. The analytical instruction added earlier had produced exactly
  the register it asked for, a critic filing a review: bold section headings, "a study
  in controlled tension", "paints a portrait of", "it's not just sadness, it's the
  weight of", three adjectives everywhere. Those are the documented signs of AI writing,
  so the fix was a short list of things never to write, plus a plain instruction to lead
  with what was noticed. Style costs about 90 tokens.
  A worked experiment failed on the way and is worth keeping: a few-shot block of five
  example exchanges, taken from the voice work in the lemon-ai project next door. Two
  things went wrong. Written against real playlist names, the model repeated an example
  reply word for word and then invented supporting facts around it. Worse, because the
  examples showed only finished replies and never the tool call that earned them, the
  model stopped calling tools entirely: zero calls on three of three cases, and
  fabricated Spotify links like `4k5k5k5k5k5k5k5k5k5k5k` in place of real ones. Adding
  the tool step to each example fixed the tool use, but the block was still 400 tokens
  of the prompt, so it was cut altogether. The rule left behind, that nothing may be
  said about this library without a tool and no link may be written from memory, is the
  part worth having. A few-shot example teaches the whole shape of a turn, so an example
  that skips the work teaches the model to skip the work.
- **2026-08-17** — `web_search`, over DuckDuckGo through `ddgs`. Keyless, so it adds a
  package but no credential. It is for the context around music rather than for finding
  music: the docstring and the prompt both say so, because a search tool that returns
  no playable links would otherwise get used for track-finding and produce answers
  nobody can listen to. One thing checked before shipping: `ddgs` and its HTTP layer log
  every backend they try, and this server speaks MCP over stdout, so a stray print would
  corrupt the protocol. Verified stdout stays empty; the noise is stderr, and the two
  loggers are quietened anyway.
- **2026-08-17** — The agent is told what the user's playlists are called, and the
  bare-name problem goes away. "compare Unmaad and Ni || Ti" used to spend ten calls
  searching them as song titles before trying `my_playlists`; it now goes straight to
  two `playlist_vibe` calls. Three system-prompt wordings had failed at this, one
  leaving it flailing, one making it ask the user rather than look, one making it give
  up with no tool call at all. None could have worked: the model was not making a bad
  decision, it had no way to know the word was a playlist. 346 tokens of names against
  roughly 2,300 of tool schemas buys the fact outright. The lesson is the one this
  codebase keeps relearning, that a wrong tool choice is usually missing information
  rather than a missing instruction, and prompt wording is the last thing to reach for.
  `_playlists` is cached for five minutes, which also speeds every name resolution.
- **2026-08-17** — Audio features are back, from ReccoBeats rather than Spotify, and
  genres from MusicBrainz. `search_by_feel`'s three numbers used to append one keyword
  and nothing else; they now over-fetch and rank the candidates by measured distance
  whenever a dial moves off 0.5, which costs nothing when they are all left alone. New
  `playlist_vibe` reports what a playlist actually sounds like: it separates Unmaad
  (acousticness 0.44, danceability 0.56, "filmi, world fusion") from Ni || Ti
  (acousticness 0.20, danceability 0.74, "conscious hip hop") on measurement rather
  than on titles. Coverage is Western-biased, 98% on a US rap playlist against 65% on
  Hindi-heavy top tracks, so an unmeasured track sorts after the ranked ones instead of
  being dropped, and `measured` is returned so the model can weigh the average.
  `playlist_tracks` now returns the playlist's title alongside its tracks: given a bare
  list, the model announced that "Unmaad" was really called "Bhar Do Jholi Meri", which
  is its first track.
- **2026-08-17** — The whole reachable surface probed and written down in
  research/API_SURFACE.md, 37 live requests rather than a reading of the documentation. Six
  more tools from what answered: `liked_songs`, `saved_albums`, `top_artists`,
  `album_tracks`, `artist_albums`, `now_playing`. The probe also found that the
  limit-10 cap is not only on `/search`: `/artists/{id}/albums` shares it, so `_pages`
  takes a page size. Seventeen tools now, which makes the tool-selection item in the
  TODO more pressing rather than less.
- **2026-08-17** — Paging, two new tools, and a third instance of the same rename.
  `playlist_tracks` read the first 50 tracks and silently dropped the rest, so a
  240-track playlist was analysed from a third of itself. One `_pages` helper now
  serves playlists, tracks and shows. `my_playlists` reported `tracks: None` for all
  69, because the playlist object's `tracks` field became `items` in the same Feb 2026
  migration that renamed the endpoint and the row payload. Added `followed_artists`
  (cursor-paged, the only such endpoint here) and `saved_podcasts`, needing the new
  `user-follow-read` and `user-library-read` scopes. Both return fewer fields than
  planned: artist genres, popularity and follower counts, and a show's publisher, are
  no longer sent at all, and shipping them would have been three more permanent nulls. A 403 on someone else's playlist
  now says what cannot be fixed, since a bare 403 made the model retry forever.
  Blends are settled: they never appear in `my_playlists`, and a blend id answers 404
  even with a user token from the Authorization Code flow. Extended quota mode is the
  only route and has required 250k monthly active users since May 2025, so this is
  permanent, not a bug to fix. `playlist_tracks` reports it as such rather than
  retrying, and the same message covers Discover Weekly and Daily Mix.
- **2026-08-17** — The prompt asks for analysis rather than a list: state the pattern,
  say where it breaks, name the tracks that carry the claim, and keep the reply one
  argument. It also asks the model to read an open request more than one way before
  concluding. Costs roughly 90 tokens per call and one extra search on vague requests.
- **2026-08-17** — `playlist_tracks` takes a playlist name, and `my_playlists` pages.
  Told "look at Unmaad", the model passed the name as an id, got a 404, and asked the
  user for a link instead of calling `my_playlists`. Telling it to chain the two calls
  in the docstring worked sometimes, which is worse than not working. `playlist_tracks`
  now resolves a name itself, so the failure cannot happen. Separately `my_playlists`
  asked for 50 of 70 playlists and never paged, so a fifth of them did not exist as far
  as the agent was concerned. Name matching is exact, then every word of the request
  present in the name, then substring; "name inside request" is deliberately absent
  because a playlist called "Ti" sits inside "ni ti" and would win.
- **2026-08-17** — `playlist_tracks` read every playlist as empty. The Feb 2026
  migration renamed each row's payload from `track` to `item` as well as the
  endpoint, so the `if i.get("track")` filter dropped all 46 rows of a 46-track
  playlist. The symptom looked like an approval bug: the model got `[]`, was told
  by the prompt to try again after an empty result, and asked for approval on the
  same call forever. Episodes share that field and carry no artists, so the row
  type is now checked. The docstring also says a name is not an id, because the
  model was passing "Ni || Ti" straight in instead of calling `my_playlists`.
- **2026-08-17** — Gemini removed as a provider, everywhere: no `LLM_PROVIDER`, no
  `_pick_provider`, no `provider` argument through `_llm`/`build`/`run`/`turn`/
  `decide`/`bakeoff`, no `langchain-google-genai`. Gemini models remain reachable
  through OpenRouter, so nothing was lost but a fork in every model code path.
- **2026-08-17** — One streaming loop instead of two. `run()` and `_drive()` each
  carried a copy of the astream loop, so a streaming fix could land in one and not
  the other. `_drive()` now takes a `gated` flag: ungated callers skip the state
  inspection entirely, which is also required rather than merely faster, because
  `aget_state` needs a checkpointer and `run()` is usually called without one.
  `build()` now rejects gating without a checkpointer, since a paused graph has
  nowhere to live.
- **2026-08-16** — Default OpenRouter model is now `qwen/qwen3.5-flash-02-23`,
  chosen by a 14-model bake-off: it ties the previous default `openai/gpt-5.4-mini`
  at 29/30 within one tool call and 28% of the latency, for a tenth of the input
  price. Full results and method in [MODEL_BAKEOFF.md](MODEL_BAKEOFF.md); the
  reasoning about running several models at once is in
  [MULTI_MODEL.md](research/MULTI_MODEL.md). A rubric bug in the first round (demanding
  `search_by_feel` for "songs about rain", where the lyric search is a fair read)
  had masked the winner, so the fix came before the choice. The model needs
  `extra_body={"reasoning": {"enabled": False}}` on OpenRouter: it otherwise streams
  its deliberation as the visible reply, which the rubric did not catch. Disabling
  it also cut the model from 16 tool calls to 13.
- **2026-08-16** — Cut what the conversation carries, in two steps. (1) `_track`
  now returns name, artist, url only: the uri and id were the same identifier three
  times, and `_uri()` reconstructs a uri from the url when a playlist is built.
  Measured 23-30% off every track-bearing tool, plus lower defaults
  (`recently_played` 50→20, `search_by_feel` 20→10, `listening_lyrics` 20 tracks
  ×1200 chars → 12×800). (2) A `pre_model_hook` stubs tool results from earlier
  turns before the model sees them, keeping the assistant's summary of each. Stored
  history is untouched, so the UI still shows everything. A third turn sent 2,523
  chars instead of 4,507, and a "name one more like it" follow-up still resolved.
- **2026-08-16** — Bake-off run on OpenRouter, $0.15 for five models over four cases.
  Four tied at 20/20 (gpt-5.4-mini, qwen3.7-flash, qwen3.7-max, qwen3.7-plus);
  ling-3.0-flash took 19/20. **No model change**: the score tied, and the incumbent
  `openai/gpt-5.4-mini` also won both tiebreakers by a wide margin, 6 tool calls and
  46.6s against 13/105s for the next best. The rubric saturated, so efficiency, not
  correctness, is what separated these models.
- **2026-08-16** — Mode buttons added beside the chat bar. First attempt crashed:
  Streamlit forbids assigning to a widget's key once that widget has rendered, and
  the chat input is handled after the buttons, so a slash command blew up trying to
  move them. The key now carries the mode (`mode_picker_<mode>`), so changing mode
  builds a fresh widget that reads the new default. `ui_check.py` covers it.
- **2026-08-16** — Three approval modes (`manual`, `afk`, `auto`), set by buttons
  above the chat bar or by slash command; both write `session_state.mode`, and the
  command also writes the widget's key so the buttons never show a stale mode. Built on `interrupt_before=["tools"]` plus the
  checkpointer: the graph parks before the tool node, `_drive()` auto-resumes calls
  the mode permits and returns the rest for approval, and a decline writes a
  `ToolMessage` so the model reacts instead of hanging. Verified that a gated call
  leaves zero tool results in state, so nothing runs before approval.
- **2026-08-16** — Added `bakeoff.py`: a mechanical scorer for model choice, run
  against the real MCP tools rather than a synthetic benchmark. Scoring is 5 points
  per case and a forbidden tool call (writing a playlist unasked) zeroes the case
  outright. `agent.run/build/_llm` now take `model` and `provider` overrides so one
  process can compare several.
- **2026-08-16** — Provider selection is automatic when `LLM_PROVIDER` is blank:
  OpenRouter if its key is set, else Gemini. Prompted by hitting both a credit wall
  on one provider and a 20-requests-per-day-per-model free-tier cap on the other;
  swapping is now a key edit rather than a config edit.
- **2026-08-16** — `agent.build()` is now an async context manager holding ONE MCP
  session for the whole turn. Every tool call previously opened its own stdio
  connection: measured 3.6-4.0s per call against 0.39s for the same work called
  directly, and it did not improve on repeat, because each call spawned a fresh
  interpreter. Inside a session the same calls take 0.30-0.41s.
- **2026-08-16** — Replies stream token by token. `astream` now runs with
  `stream_mode=["values", "messages"]`: tool calls come from the values stream,
  reply text from the messages stream, so `on_part` receives a "token" kind.
  `collect()` rejoins tokens so buffered callers are unaffected.
- **2026-08-16** — Default Gemini model moved from `gemini-3.7-flash` to
  `gemini-3.6-flash` after repeated free-tier rate limiting on 3.7. Same list price
  ($0.75/$3.75 per 1M), one generation older, agentic scores close (83.0%
  OSWorld-Verified). The lesson worth keeping: on a free tier the newest model is
  the most quota-constrained, so availability beats benchmark position.
- **2026-08-15** — Gemini added as a second provider behind `LLM_PROVIDER`, with its
  own key and model variables. Default `gemini-3.7-flash`, verified against the live
  key: it exists, takes 1M in / 65K out, and completed a real tool-calling run.
  `gemini-3.1-flash` does **not** exist; at 3.1 Google serves only `-flash-lite` and
  `-pro`. Google's direct price for 3.7 Flash is $0.75/$3.75 per 1M through
  2026-12-31, then $1.50/$7.50 — the same as 3.6 Flash, not cheaper. (The lower
  $0.38/$1.88 figure seen earlier was OpenRouter's resale price, not Google's.)
  Every Flash model has a free tier, which is why Gemini is the fallback when
  OpenRouter credits run out.
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
