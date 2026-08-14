# To do

Living list. Updated 2026-08-15. Newest decisions go in
[ARCHITECTURE.md](ARCHITECTURE.md); this file is what is still owed.

Ordered by what blocks the most. Check items off, do not delete them, so the
Done section records what was actually finished.

## Blocking the abstract

- [ ] **Build the psych/emotion MCP server.** `get_big_five(text)` and
      `get_emotion_labels(text)` over the Hugging Face Serverless Inference API
      (`vladinc/bigfive-regression-model`, `SamLowe/roberta-base-go_emotions`).
      Nothing about trait or emotion inference exists yet, so the entire vibe-drift
      workflow is blocked on this one item.
- [ ] **Add a batch lyrics tool.** `get_lyrics` is single-track, so profiling a
      week means one model round trip per song. Needed before lyric-based
      inference is practical on free-tier limits.
- [ ] **Give the agent memory.** A checkpointer for the conversation thread, plus
      a separate store for the trait trajectory that has to outlive any thread.
      Without it, "next week's reading measures last week's action" cannot happen.
- [ ] **Add a feedback signal.** Nothing measures whether a playlist moved
      anything, so the loop stays open and the intervention claim stays unproven.

## Quality

- [ ] **Run a model bake-off for tool-call accuracy.** Build a small fixed set of
      requests with known-good tool calls, then score models on: did it call the
      right tool, did it write a usable `description`, did it avoid redundant
      repeat searches. gpt-4o-mini failed the third badly; gpt-5.4-mini passed on
      one sample, which is not evidence yet.
- [ ] **Stream agent output in the Streamlit app.** `agent.collect()` returns only
      when the whole run finishes, so the UI shows a spinner and then everything
      at once. The CLI has the same shape.
- [ ] **Retry on Spotify 429.** `_call` raises with the `Retry-After` value but
      does not wait and retry. A week of listening scored in one run will hit it.
- [ ] **Test beyond the selfchecks.** They cover pure logic (query building, env
      merge, arg parsing). Nothing covers the HTTP paths against recorded
      responses.

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
      spends the OpenRouter key. Restrict to named viewers, or add a shared
      password gate.
- [ ] **Reword the abstract's workflow 2.** It claims `target_valence` and
      `target_acousticness`, which no new Spotify app can use. What is built is
      keyword search over a model-written description.

## Done

- [x] **2026-08-15** — Fixed the mood-query dead zone. Mid-range values produced
      an empty query that fell back to searching the word "music". `description`
      is now a required argument and carries the search; the strongest axis adds
      one word.
- [x] **2026-08-15** — Agent runs end to end over MCP stdio through OpenRouter,
      and in the Streamlit app's Agent mode.
- [x] **2026-08-14** — MCP server, CLI runner, Streamlit tool forms, refresh-token
      auth, Feb 2026 Spotify endpoint migration, URI normalisation.
