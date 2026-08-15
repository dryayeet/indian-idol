# Model bake-off

What model this agent should run on, decided by measurement rather than by
leaderboard. Last run 2026-08-16 with [bakeoff.py](bakeoff.py).

**Current default: `qwen/qwen3.5-flash-02-23`.** It ties the previous default on
score at a tenth of the input price.

## Why not just read a leaderboard

BFCL v4 and MCP-Mark rank models on generic tool calling. Neither predicted what
happened here:

- BFCL v4 does not rank `gpt-5.4-mini` or the Gemini Flash models at all, yet
  `gpt-5.4-mini` matched the models it does rank and beat them on efficiency.
- Flash-Lite models were reported weak at tool calling. On this job they picked
  the right tool every time; their weakness was persistence, not selection.
- `inclusionai/ling-3.0-flash` sits third on BFCL v4 at 73.0%. Here it needed 55
  tool calls and 276 seconds to answer the same six questions others answered in
  15 calls.

Generic benchmarks measure whether a model can call a function. This agent needs a
model that writes a good search query, knows when *not* to call anything, rewords
after an empty result, and carries turn one into turn two.

## What is scored

Six cases, five points each, all mechanical so the result is reproducible.

| case | what it tests |
|---|---|
| affective search | picks `search_by_feel`, writes a query of five words or fewer |
| lyrics search | picks `search_by_lyrics` when the ask is about what a song says |
| read my listening | picks `listening_lyrics`, not `get_lyrics` per track |
| restraint | tools available, correct move is to call none |
| recovery | first search finds nothing; a good model rewords instead of giving up |
| follow-up | two turns, where the second depends on the first |

Every case also checks: no forbidden tool (using `create_playlist` unasked zeroes
the case outright), no repeated identical calls, and a usable answer.

Three things are measured alongside the score, because a model that answers
correctly while burning ten tool calls is not the better model:

- **calls** — tool calls made
- **efficiency** — fewest calls the case needs, over calls actually made
- **cost** — real dollars read from the provider between models

## Results, 2026-08-16

Fourteen models, $0.16 total.

| model | score | calls | eff | secs | $/1M in | $/1M out |
|---|---|---|---|---|---|---|
| openai/gpt-5.4-mini | 29/30 | 15 | 0.66 | 83 | 0.75 | 4.50 |
| **qwen/qwen3.5-flash-02-23** | **29/30** | 16 | 0.63 | 106 | **0.07** | **0.26** |
| ↳ same, reasoning disabled *(shipped)* | **29/30** | **13** | **0.68** | **85** | 0.07 | 0.26 |
| qwen/qwen3.7-flash | 29/30 | 18 | 0.59 | 141 | 0.03 | 0.13 |
| z-ai/glm-4.7-flash | 29/30 | 15 | 0.64 | 207 | 0.06 | 0.40 |
| openai/gpt-5.4-nano | 29/30 | 17 | 0.67 | 75 | 0.20 | 1.25 |
| google/gemini-3.7-flash | 29/30 | 40 | 0.49 | 147 | 0.38 | 1.88 |
| nex-agi/nex-n2-mini | 28/30 | 20 | 0.50 | 108 | 0.02 | 0.10 |
| nvidia/nemotron-3.5-lightning | 28/30 | 86 | 0.49 | 509 | 0.10 | 0.25 |
| bytedance-seed/seed-1.6-flash | 27/30 | 7 | 0.94 | 124 | 0.07 | 0.30 |
| mistralai/mistral-small-3.2-24b | 26/30 | 6 | 0.78 | 64 | 0.09 | 0.25 |
| openai/gpt-oss-20b | 26/30 | 8 | 0.67 | 76 | 0.03 | 0.13 |
| qwen/qwen3-30b-a3b-instruct | 23/30 | 8 | 0.67 | 54 | 0.05 | 0.19 |
| mistralai/mistral-nemo | 19/30 | 6 | 0.78 | 68 | 0.02 | 0.03 |
| meta-llama/llama-4-scout | 11/30 | 5 | 0.83 | 32 | 0.10 | 0.30 |

Round one (2026-08-16, before the rubric fix below) also tested
`qwen/qwen3.7-plus` 27/30, `openai/gpt-oss-120b` 27/30, `upstage/solar-pro4`
26/30, `inclusionai/ling-3.0-flash` 28/30 in 55 calls, and
`deepseek/deepseek-v4-flash` 24/30 with a crash.

## Why `qwen3.5-flash-02-23` over the other models that tied

Five models tied at 29/30, so the choice came from the other columns.

- `qwen3.7-flash` is cheaper still, but 141s against 106s. A third more waiting
  for the same answer.
- `glm-4.7-flash` matches on calls but is the slowest of the group at 207s.
- `gpt-5.4-nano` is the fastest at 75s, but saves the least: $0.20/$1.25.
- `gemini-3.7-flash` needed 40 calls to reach the same score.

`qwen3.5-flash-02-23` is within one call and 28% of the latency of the previous
default, for a tenth of the input price and a seventeenth of the output price.

## The one thing the rubric missed: reasoning leaking into the answer

Within minutes of shipping `qwen3.5-flash-02-23` as the default, a live run streamed
this to the user:

    The user is asking for songs about leaving a small town at night. This sounds
    like it's more about what the songs *say*... Let me think about what phrase to
    search for... </think>

The model emits its deliberation as ordinary content, complete with stray `</think>`
tags. The bake-off scored it 29/30 anyway, because the rubric checks tool choice,
retries, links and restraint, and never asks whether the reply reads like an answer.

Two fixes were tried. `{"reasoning": {"exclude": True}}` does nothing here, because
exclusion only suppresses reasoning the provider returns in a separate field, and
this model puts it in the content. `{"reasoning": {"enabled": False}}` works.

Disabling it also made the model measurably better on this job: 13 tool calls
instead of 16, 85 seconds instead of 106, same 29/30.

Worth noting the other models that tied do not have this flaw: `gpt-5.4-nano`,
`glm-4.7-flash` and `qwen3.7-flash` all answer cleanly with no parameter at all.

**Add to the rubric before the next run:** a check that the reply contains no
first-person deliberation ("the user wants", "let me", "I should") and no thinking
tags.

## Findings that outlived the run

**A rubric bug changed the winner.** Round one required `search_by_feel` on a case
asking for "two songs about rain". Using `search_by_lyrics` there is a fair reading,
and two models were docked for it. Fixing the case moved `qwen3.7-flash` from 27 to
29. Shipping a model choice on round one's data would have been shipping a bug.

**Cheap models fail structurally, not gracefully.** `mistral-nemo` and
`qwen3-30b-a3b` raised unhandled `ExceptionGroup` crashes mid-run; `llama-4-scout`
scored 11/30. The failure to watch for is a `failed:` note, not a low score.

**Efficiency is not quality.** `seed-1.6-flash` has the best efficiency of any model
tested (0.94) and scores 27, because it makes exactly one call per case and stops.
`nemotron-3.5-lightning` scored 28 while making 86 calls in 509 seconds. Both
extremes are wrong.

**Persistence separates models, selection does not.** Almost every model scores 5/5
on the first four cases. The spread is in `recovery` and `follow-up` — whether an
empty result provokes a reword, and whether turn one survives into turn two.

**Answering with no tool call at all is the worst failure.** `mistral-small` answered
"what have I been listening to" without calling anything, and `gpt-oss-20b` did the
same on recovery. A confidently ungrounded answer is worse for this app than a low
score, and only the case-level tool assertions catch it.

## Known weaknesses of this harness

- **The cost column is unreliable.** OpenRouter's usage counter lags; six of
  fourteen models read $0.0000 including the most expensive one. Only the run total
  and the non-zero rows can be trusted. Prices in the table above are published
  rates, not measured spend.
- **Timings absorb Spotify rate limits.** Round one hit 32 Spotify 429s, all
  retried successfully, which inflates `secs` for whichever models ran during that
  window. Scores, calls, and efficiency are unaffected.
- **One run per case.** Nothing here measures consistency. A model that succeeds
  four times in five would look identical to one that always succeeds.

## Re-running it

```
python bakeoff.py --selfcheck                 scoring logic, no API calls
python bakeoff.py                             the default panel
python bakeoff.py openai/gpt-5.4-mini ...     named models
python bakeoff.py --provider gemini gemini-3.5-flash
```

Use `python -u` so scores stream instead of appearing at the end. Results land in
`bakeoff_results.json`.
