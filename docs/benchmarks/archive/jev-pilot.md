# Jev Pilot: Tool Selection as Classification

First run 2026-09-21 with
[`benchmarks/bakeoff_jev.py`](../../../benchmarks/bakeoff_jev.py). This pilot is
superseded by the completed [41-case evaluation](../jev-tool-selection.md).

## The question

The model bakeoff found that every serious model picks the right tool. The spread
between models was persistence, not selection. Picking one tool out of a fixed
inventory from a user utterance is classification, and classification is what
Jev (`typesafe/jev-1.13`, TypeSafe's System One decision model) is for. So: can
Jev do the selection part at System 1 cost, and where does it break?

How Jev works, in one line: it reads a `state` and answers typed questions
(`choice`, `score`, `noul`) in one forward pass, returning probability
distributions instead of text. Reference: [Jev notes](../../research/jev/reference.md), written
for a separate project and used here only as documentation of the API.

## Method

Jev ran over the bakeoff's own six cases, imported from `bakeoff.CASES` so the
wording cannot drift. Seven turn-decisions total (follow-up has two turns).

Parity with the LLM runs, which were **not re-run**:

- The LLMs saw every tool from both MCP servers, so Jev's `choice` criteria are
  the same full inventory: 24 tools plus a `none` option, each carrying its tool
  description.
- The LLM prompt carries the user's playlist names; they went into Jev's state too.
- The LLM results come from the recorded 2026-08-16 run (`artifacts/benchmarks/logs/bakeoff2.log`, plus the
  reasoning-off qwen rerun in the archived benchmark results). Per-case cost does not exist
  for them: the bakeoff recorded cost per run and flagged the counter unreliable.
  The cost column below is therefore Jev measured against published rates, not
  like for like.
- A scoring difference to keep in mind: the LLMs were scored on behavior in the
  loop (call sequences, retries, replies). Jev is scored on the classification
  alone. Recovery's "rewording" behavior has no Jev equivalent; only its turn-one
  tool choice is compared.

## Results, 2026-09-21

| decision | want | jev got | conf | margin | jev secs | llm secs (mini / qwen) |
|---|---|---|---|---|---|---|
| affective search | search_by_feel | search_by_feel | 0.97 | 0.97 | 0.5 | 12.2 / 17.5 |
| lyrics search | search_by_lyrics | search_by_lyrics | 0.84 | 0.74 | 0.4 | 27.1 / 27.9 |
| read my listening | listening_lyrics | **recently_played** | 0.88 | 0.83 | 0.4 | 8.5 / 18.8 |
| restraint | none | none | 0.98 | 0.98 | 0.4 | 4.5 / 4.6 |
| recovery | search_by_lyrics | search_by_lyrics | 0.80 | 0.67 | 0.6 | 16.5 / 24.2 |
| follow-up t1 | feel or lyrics | search_by_lyrics | 0.45 | 0.21 | 0.4 | 13.9 / 12.9 |
| follow-up t2 | create_playlist | **search_by_lyrics** | 0.41 | 0.09 | 0.4 | 13.9 / 12.9 |

LLM seconds are the full in-loop case time (selection plus execution). Jev
seconds are one decisions call. Totals: Jev 5/7 turns in 3.1s for a measured
$0.0004; gpt-5.4-mini 6/6 cases in 83s; qwen3.5-flash-02-23 6/6 in 106s.

## The two misses, and what they say

**read my listening.** Jev picked `recently_played` over `listening_lyrics`. On
the bare words "what have i been listening to lately", `recently_played` is a
defensible read; the rubric prefers `listening_lyrics` because it pulls lyrics in
one call and the case asks about mood. This miss is a rubric strictness problem
as much as a Jev problem, and a reworded criterion ("prefer the tool that
answers the whole ask in one call") might fix it. Not yet tried.

**follow-up t2.** Jev answered "put those in a playlist called Rain" with another
lyrics search instead of `create_playlist`. This is the real failure mode. Two
likely causes: the state compressed turn one into one line with no sense that
tracks had actually been found, and the instruction "pick the tool for the
latest request" does not tell Jev that "those" refers to turn one. The LLMs get
this from conversation state that Jev, whose questions are independent and see
only the state you send, never had.

## Margin looks like a usable routing signal

The correct decisions sat at margin 0.21 to 0.98. The wrong one sat at 0.09. The
follow-up t2 miss is exactly the case the cascade pattern exists for: Jev flags
low margin, the state (or the whole conversation) goes to a System 2 model, and
the LLM gets it right. With the threshold anywhere below 0.20, this run's
cascade would score 6/7 at a fraction of LLM-only cost. Seven decisions is far
too few to trust a threshold; the 40-case follow-up exists to establish where
the dead zones actually are, per the threshold lessons in the Jev reference notes.

## What Jev is and is not, for this agent

- It classifies a first tool choice in 0.4s for about $0.00006 per decision.
  The LLMs need 8 to 28 seconds per case because they also read results and
  write answers; that comparison is unfair to them by design, and the honest
  statement is: Jev cannot replace the loop, it can only front it.
- The follow-up miss shows the state packaging matters more than the model.
  Jev saw a summary; the LLMs saw a live conversation. The next experiment
  should give Jev the same conversation object the agent keeps, not a summary.
- Accuracy on a 25-way choice is respectable (5/7, one miss half-defensible),
  and the guide warns the practical sweet spot is 4 to 10 options. The full
  inventory did not break it.

## Harness weaknesses

- Seven decisions, one run each. Nothing here measures consistency, and the
  margin threshold is a hypothesis, not a finding.
- The LLM reference is a single recorded run on possibly different network
  conditions; its timing absorbed Spotify 429s.
- The labels are the rubric's opinion. The `recently_played` miss is the proof:
  a classifier can be marked wrong by a stricter grader than a human would use.
- Cost comparison mixes measured Jev spend with published LLM rates.

## Re-running it

```
python benchmarks/bakeoff_jev.py --selfcheck     scoring logic, no API calls
python benchmarks/bakeoff_jev.py                 seven decisions calls, under a cent
```

Needs `OPENROUTER_API_KEY` in `.env`. Full per-turn distributions land in the
ignored `artifacts/benchmarks/bakeoff_jev_results.json` file.
