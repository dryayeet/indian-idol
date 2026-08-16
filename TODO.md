# To do

Living list. Updated 2026-08-16. Newest decisions go in
[ARCHITECTURE.md](ARCHITECTURE.md); this file is what is still owed.

Ordered by what blocks the most. Check items off, do not delete them, so the
Done section records what was actually finished.

## Blocking the abstract

- [ ] **Build the psych/emotion MCP server.** `get_big_five(text)` and
  `get_emotion_labels(text)` over the Hugging Face Serverless Inference API
  (`vladinc/bigfive-regression-model`, `SamLowe/roberta-base-go_emotions`).
  Nothing about trait or emotion inference exists yet, so the entire vibe-drift
  workflow is blocked on this one item. `listening_lyrics` already returns the
  text it needs, so this is the last missing piece of workflow 1.
- [ ] **Give the agent durable memory.** Conversation memory exists but is
  in-process only, so it dies with the Streamlit process. The trait trajectory
  needs a store that outlives every thread and restart, or "next week's reading
  measures last week's action" cannot happen. Upgrade path: swap `InMemorySaver`
  for a SQLite checkpointer (`langgraph-checkpoint-sqlite` + `aiosqlite`), then
  add a separate store for profiles.
- [ ] **Add a feedback signal.** Nothing measures whether a playlist moved
  anything, so the loop stays open and the intervention claim stays unproven.
- [ ] **Add the unified personality profile** as a group of md files, or
  instructions, or prompt insertion/context, exposed as a tool. Needs the psych
  server first for anything to write into it, but the shape can be designed now:
  what a profile holds, who writes it, and what reads it back.
- [ ] **Ranking logic for song fetching: personalise it properly.** Results are
  whatever Spotify's text search returns, in its order, with no reference to the
  user at all. Their own history (`top_tracks`, `recently_played`) is already
  available and unused for ranking.
- [ ] **Spotify auth for other users.** Today one refresh token means one account,
  which is also the reason the deployed app cannot be shared. Real per-user auth
  means an OAuth callback, per-user token storage, and a server that is no longer
  stateless. Worth designing before it is built.

## Quality

- [ ] **Web searches for better context engineering** of tool usage and history
  passing. Some of this is now done (leaner tool payloads, stubbed old tool
  results), but the reading has not been done systematically.
- [ ] **Score the reply, not just the tool calls.** The bake-off gave the new
  default 29/30 while it was streaming "The user is asking for..." and stray
  `</think>` tags to the user. Add a check for first-person deliberation and
  thinking tags in the reply. See MODEL_BAKEOFF.md.
- [ ] **Measure model consistency, not just one run.** The bake-off runs each case
  once, so a model that succeeds four times in five looks identical to one that
  always succeeds. Repeat each case k times and score the worst run.
- [ ] **Fix the bake-off cost column.** Six of fourteen models read $0.0000 because
  OpenRouter's usage counter lags past the 25s settle. Either poll until it moves
  or compute cost from token usage and published prices.
- [ ] **Make the bake-off discriminate.** Four of five models scored a perfect
  20/20 on 2026-08-16, so the rubric no longer separates them. What actually
  differed was efficiency: 6 tool calls and 46.6s for gpt-5.4-mini against 22
  calls and 287s for qwen3.7-plus. Score calls and seconds directly, and add
  harder cases: an ambiguous request, a tool error to recover from, and a
  multi-turn follow-up that depends on the previous answer.
- [ ] **Embeddings for lyric ranking.** The highest-value multi-model move, and it
  is not an LLM: `_lyric_score` counts shared words, so "headlights" never matches
  "high beams". Lyrics never change, so embeddings are computed once and cached.
  See [MULTI_MODEL.md](MULTI_MODEL.md).
- [ ] **A relevance judge over search results.** `_relevant()` is a four-character
  substring check; it kills gibberish but cannot tell that three "Rainy Days"
  tracks match the words and miss the request. A cheap second model scoring each
  candidate, with no access to the conversation, is the verifier pattern. See
  [MULTI_MODEL.md](MULTI_MODEL.md).
- [ ] **Failover on 402 and 429.** Everything runs through one OpenRouter key, so a
  dead key or an empty balance stops the agent. Falling back to another model or a
  second key would have rescued two dead sessions already.
- [ ] **Improve lyric ranking.** `search_by_lyrics` scores on the share of query
  terms present in the lyrics, so it rewards literal wording and misses
  paraphrase ("headlights" will not match "high beams"). Embeddings would fix
  that; the emotion classifier from the psych server would fix it better.
- [ ] **Detect empty search results.** Spotify's search never returns nothing: a
  meaningless query still yields arbitrary tracks (verified with
  `q="qwertypoiu zxcvbnm asdfgh"`). The agent cannot tell a good match from
  noise, so a bad description is presented as a real answer.
- [ ] **Retry on Spotify 429.** `_call` raises with the `Retry-After` value but
  does not wait and retry. A week of listening scored in one run will hit it.
- [ ] **Test beyond the selfchecks.** `ui_check.py` now covers the mode controls
  end to end, but the HTTP paths are still untested against recorded responses,
  so an endpoint moving under us is only found by running the thing.
- [ ] **Give the CLI a memory, or say plainly that it has none.** The chat trims
  and keeps history; `python agent.py "..."` passes no checkpointer, so every run
  starts blank. Either add a thread-id flag or document it in the help text.
- [ ] **Grow the slash commands.** The chat bar takes `/manual`, `/afk`, `/auto`,
  `/mode`, `/help`. Worth adding: `/new` for a fresh thread, `/model` to switch
  model mid-conversation, `/tools` to list what the server exposes, and the same
  commands in the CLI, which has none. Each is a branch in `_command()`.
- [ ] **Reuse the MCP session across Streamlit messages.** One session now covers
  a turn, but each chat message rebuilds the graph and pays the ~3.5s subprocess
  spawn. Sessions are bound to their event loop and Streamlit calls `asyncio.run`
  per message, so this needs a loop held in a background thread.

## Housekeeping

- [ ] **Move to `langchain.agents.create_agent`.** `create_react_agent` is
  deprecated in LangGraph v1 and goes away in v2. Deliberately deferred: the
  new import needs the whole `langchain` package, which is a real dependency
  for a warning that changes nothing today.
- [ ] **Revisit the `mcp<2.0.0` pin** when `langchain-mcp-adapters` supports 2.0.
  Going back means `MCPServer`, `Tool.input_schema`, and a `.content` object
  instead of a tuple. See Environment constraints in ARCHITECTURE.md.
- [ ] **Lock down the deployed Streamlit app.** It runs on one Spotify account
  with no per-user login, so anyone with the URL controls that account and
  spends the provider key. Restrict to named viewers, or add a shared password
  gate. The approval modes help but do not solve it: `auto` is one click away,
  and reads leak the account's history in every mode.
- [ ] **Reword the abstract's workflow 2.** It claims `target_valence` and
  `target_acousticness`, which no new Spotify app can use. What is built is
  keyword search over a model-written description, plus lyric reranking.

## Done

- [X] **2026-08-16** — Context trimmed twice over: leaner tool payloads (23-30%
  per track-bearing call, `_track` down to name/artist/url) and a pre-model hook
  that stubs earlier turns' tool results (44% less sent on a third turn).
- [X] **2026-08-16** — Bake-off built and run on five OpenRouter models for $0.15.
  Four-way tie on score, so the default model was left alone; `gpt-5.4-mini` won
  both tiebreakers anyway (6 calls, 46.6s).
- [X] **2026-08-16** — Approval modes (`manual`, `afk`, `auto`) with slash
  commands and buttons that stay in step, plus `ui_check.py` to prove it.
- [X] **2026-08-16** — One MCP session per run: tool calls went from 3.6-4.0s
  each to 0.30-0.41s. Replies stream token by token.
- [X] **2026-08-16** — Gemini added as a second provider; provider now
  auto-selected from whichever key is present, OpenRouter preferred.
- [X] **2026-08-15** — `listening_lyrics`: recent or top tracks plus their lyrics
  in one call, deduplicated and concurrent. Playlist read tools (`my_playlists`,
  `playlist_tracks`) with the scope they need.
- [X] **2026-08-15** — `search_by_lyrics`: fetch-and-rerank over LRCLIB lyrics,
  with concurrent fetching and honest empty results. Fixed the `/v1/search`
  limit-10 cap by paging.
- [X] **2026-08-15** — Chat UI with conversation memory (in-process), streaming
  steps, and track links returned by the tool rather than assembled by the model.
- [X] **2026-08-15** — Fixed the mood-query dead zone. Mid-range values produced
  an empty query that fell back to searching the word "music". `description`
  is now a required argument and carries the search; the strongest axis adds
  one word.
- [X] **2026-08-15** — Agent runs end to end over MCP stdio, in the CLI and in
  the Streamlit app's Agent mode.
- [X] **2026-08-14** — MCP server, CLI runner, Streamlit tool forms, refresh-token
  auth, Feb 2026 Spotify endpoint migration, URI normalisation.
