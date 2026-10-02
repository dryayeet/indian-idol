# Jev Tool Selection: 41 Labeled Cases

Second run 2026-09-21 with
[`benchmarks/bakeoff_jev_40.py`](../../benchmarks/bakeoff_jev_40.py). This is the
follow-up to the [six-case pilot](archive/jev-pilot.md). Raw per-case data is
generated locally at `artifacts/benchmarks/jev_40_results.json` and is ignored.

## The cases

Forty hand-labeled utterances plus one built at run time from the user's real
first playlist name (the trap the original bakeoff kept failing: a playlist name
that reads as a song title). Coverage: feel search, lyric search, deliberate
ambiguity, listening analysis, single-track lyrics, history reads, library
reads, playlist reads, artist and track detail, both psych tools, web lookups,
five no-tool cases, and two create-playlist cases that ran in `afk` mode so the
write was held for approval and never executed.

## Results

First tool choice per case, 41 cases, all three arms live, 2026-09-21:

| arm | selection | total time | avg per case | measured cost |
|---|---|---|---|---|
| Jev (`typesafe/jev-1.13`) | **33/41** (80%) | 16.6s | 0.4s | $0.0019 |
| qwen3.5-flash, dry prompt | **38/41** (93%) | 30.0s | 0.7s | ~$0.003 |
| gpt-5.4-mini, dry prompt | 33/41 (80%) | 54.0s | 1.3s | ~$0.03 |
| qwen3.5-flash, real agent | 35/41 (85%) | 536.6s | 13.1s | ~cents |
| gpt-5.4-mini, real agent | 35/41 (85%) | 466.5s | 11.4s | ~tens of cents |

Cost figures for the LLM arms are estimates from published rates; only Jev's
spend is metered. Dry means one classification prompt with the full 25-option
inventory; real agent means the full ReAct loop against live Spotify, scored on
the first tool call. Read cases used auto mode; the two write cases used AFK so
playlist creation stopped for approval.

## What the misses say

**Every arm missed the same two cases.** "What have i been listening to lately"
and "analyze the themes across my recent listening" pulled `recently_played`
from Jev, dry qwen, and in-loop qwen (the mini loop missed one the same way).
The rubric wants `listening_lyrics` because the asks are about mood and themes.
Three of five arms reading it as a history pull says the utterances are genuinely
ambiguous, not that the classifiers are broken. A rubric fix, or an utterance
that says "in the words of the songs", settles it.

**Jev's remaining misses were concentration, not confusion.** Playlist cases
pulled `my_playlists` before the specific read (a defensible first step, wrong
per label), and two cases got `none` where a tool existed. Nothing went to a
wildly wrong tool except `amb-rain`, which Jev sent to `web_search` at margin
0.03. It is also the only case where the utterance is two words with no tool
signal.

**The dry LLM arms failed in opposite directions.** gpt-5.4-mini answered
`none` on six cases that needed tools (lyrics, web, a playlist read) while
getting all five true no-tool cases right; qwen did the reverse, answering
`none` on the one web case it should have searched and calling tools it should
not. In-loop, the mini agent called tools on **all five** no-tool cases
(`now_playing` twice for "hey, what's up"): the same ungrounded-tool-spam the
bakeoff documented, worse on chit-chat than any bakeoff case showed. Jev got 5/5
on restraint, as it did in the six-case run.

**The agent loop is not better at selection than the same model with one
prompt.** qwen in-loop scored 35 against its own 38 dry. The loop's system
prompt, playlist preamble, and tool schemas made selection worse, not better.

## The cascade verdict, now with numbers

Margin routing, Jev first tier, low-margin cases handed to a dry LLM:

| threshold | routed to LLM | Jev keeps | cascade accuracy (qwen fallback) |
|---|---|---|---|
| margin < 0.2 | 3 | 32/38 | 35/41 |
| margin < 0.3 | 6 | 31/35 | 37/41 |
| margin < 0.4 | 7 | 31/34 | 38/41 |
| margin < 0.5 | 10 | 28/31 | 37/41 |

No threshold beats the dry LLM alone (38/41), because two of Jev's eight misses
sit at margin 0.86 and 0.88, above every usable threshold, and a third sits at
0.67. Margin separates Jev's wrong guesses about tools, not its wrong reads of
the request.

The cascade pays off only against the full agent loop: Jev plus qwen fallback
on the 7 lowest-margin cases is 38/41 in roughly 105 seconds, against the
agent's 35/41 in 537. Faster and more accurate. But that comparison is really
saying "one clean prompt beats the full loop", and the LLM can be that first
tier by itself.

## Conclusion

- Tool selection is not a solved classification problem for Jev on a 25-way
  inventory: 80% against 93% for the cheapest LLM with the same information.
- Jev is 0.4s and effectively free, but the dry LLM is 0.7s and also
  effectively free, so System 1 economics buy nothing here. The expensive tier
  this architecture was built to avoid does not exist at this scale.
- Where Jev is genuinely strong is restraint: 5/5 on no-tool cases in both
  runs, the one place the real agents bleed.
- The honest use, if any: Jev as a gate that answers "does this need a tool and
  roughly which", with anything ambiguous going straight to the loop. The data
  here does not show it earning that slot.

## Harness notes

- One run per case per arm; consistency unmeasured, thresholds are from a
  single sample.
- Labels are my authorship; the shared `recently_played` miss is the standing
  warning that two of the forty labels are stricter than a fair grader.
- In-loop runs hit live Spotify and DuckDuckGo, so their timings carry network
  noise the other arms do not.
- The Hugging Face Big Five endpoint returned 410 Gone during both in-loop runs;
  the cases still scored because the tool call itself was correct. Worth a look
  separately: `psych_mcp.py`'s model may need a replacement.

## Re-running it

```
python benchmarks/bakeoff_jev_40.py --selfcheck
python benchmarks/bakeoff_jev_40.py jev
python benchmarks/bakeoff_jev_40.py dry --model qwen/qwen3.5-flash-02-23
python benchmarks/bakeoff_jev_40.py inloop --model qwen/qwen3.5-flash-02-23
```

Arms resume from `artifacts/benchmarks/jev_40_results.json`, so an interrupted
in-loop run continues where it stopped.
