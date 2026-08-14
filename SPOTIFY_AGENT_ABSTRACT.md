# Autonomous Spotify Agent

**An affect-aware agent that infers psychological state from listening behaviour and acts on it.**

Prajwal Pandey · June 2026 – July 2026
LangGraph · Model Context Protocol (FastMCP) · Hugging Face Inference API · Spotify

---

## Abstract

Listening history encodes affective and dispositional signal that music platforms do not
surface. Recommendation systems optimise for engagement over collaborative-filtering
neighbourhoods; they do not model *why* a listener is drawn to particular material, and they
cannot act on a stated feeling that has no genre label.

This project implements an autonomous agent that reads a user's Spotify history, infers
psychological state from the lyrical content of what that user actually listens to, and acts
on the inference by constructing playlists. The system is built on LangGraph and coordinates
two Model Context Protocol (MCP) tool servers. The first is a purpose-written Python server
(FastMCP) that exposes two Hugging Face Serverless Inference API models as agent-callable
tools: a Big Five (OCEAN) regression model returning five trait scores in [0.0, 1.0], and a
classifier returning probabilities over 28 emotion labels. The second is a Spotify MCP that
retrieves recently played tracks and lyrics, searches by audio feature, and creates playlists.

The agent composes these tools in two directions. Reading inward, it scores the lyrics of
recent listening to estimate trait levels, maps the resulting profile onto interpretive
frameworks (cognitive-function grip states and Enneagram movement), and tracks how that
profile shifts across weeks. Acting outward, it translates natural-language affect into
retrieval parameters, converting emotion labels into Spotify audio-feature targets such as
`target_valence` and `target_acousticness`, then assembles playlists intended to move a trait
score in a chosen direction over time.

The design goal was a full agentic loop, perception through action, running entirely on
free-tier inference. All model calls are served by the Hugging Face Serverless Inference API,
with exception handling around HTTP failures and free-tier rate limits.

---

## Motivation

Two gaps motivated the build.

**Recommendation is content-blind to interiority.** Audio features describe a track's surface
(tempo, valence, energy). Collaborative filtering describes who else liked it. Neither reads
what a song is *about*, and lyrical content is where dispositional signal concentrates.

**Affective queries have no query language.** A request like "songs that feel like driving
away from your hometown for the last time" is precise to a human and unusable to a search
API. It names an emotional configuration, not a genre, artist, or decade. Something has to
translate between the two, and that translation is exactly what a language model plus an
emotion classifier can do.

---

## Architecture

```
                    ┌──────────────────────────────┐
                    │      LangGraph Agent         │
                    │  state · memory · routing    │
                    │      cyclic loops            │
                    └───────┬──────────────┬───────┘
                            │ MCP          │ MCP
              ┌─────────────▼──────┐  ┌────▼─────────────────┐
              │  Psych/Emotion     │  │    Spotify MCP       │
              │  MCP  (FastMCP)    │  │                      │
              │                    │  │ · recently played    │
              │ get_big_five()     │  │ · lyrics             │
              │ get_emotion_labels │  │ · audio-feature      │
              └─────────┬──────────┘  │   search             │
                        │             │ · playlist create    │
              ┌─────────▼──────────┐  └──────────────────────┘
              │ Hugging Face       │
              │ Serverless         │
              │ Inference API      │
              │                    │
              │ bigfive-regression │
              │ roberta-go_emotions│
              └────────────────────┘
```

The agent is the only component holding state. Both MCP servers are stateless tool providers,
which keeps the psychological inference reusable outside this agent and makes each tool
independently testable.

---

## Components

| Component | Role |
|---|---|
| **LangGraph agent** | Stateful orchestration. Holds conversation and profile memory, routes conditionally on inference results, supports cyclic loops so a reading can trigger an action that triggers another reading. |
| **Psych/Emotion MCP server** | Written for this project in Python with FastMCP. Wraps the Hugging Face `InferenceClient` and exposes two typed tools. Stateless. |
| `get_big_five(text)` | Returns five float scores (Openness, Conscientiousness, Extraversion, Agreeableness, Neuroticism) in [0.0, 1.0] via `vladinc/bigfive-regression-model`. |
| `get_emotion_labels(text)` | Returns probabilities across 28 emotion labels via `SamLowe/roberta-base-go_emotions`. |
| **Spotify MCP** | Retrieves recently played tracks and lyrics, searches the catalogue by audio-feature targets, and creates playlists. |
| **Hugging Face Inference API** | Serverless model hosting. No local model execution, no GPU requirement, free tier. |

---

## Representative workflows

### 1. Vibe drift tracking (perception → interpretation → action)

1. The agent calls the Spotify MCP for the week's most-played tracks and pulls their lyrics.
2. Lyrics go to `get_big_five()`, producing a trait vector for the period.
3. The agent interprets the vector. A profile with elevated Neuroticism and suppressed
   Extraversion is read as a **cognitive grip** (stress state) and mapped to Enneagram
   movement toward disintegration.
4. The agent acts: it queries the Spotify MCP by audio feature and assembles a playlist
   designed to move the Neuroticism estimate downward across subsequent weeks.

The loop is closed. Next week's reading measures the effect of last week's action.

### 2. Affective retrieval (natural language → query parameters)

Given a request such as *"songs that make me feel like I am driving away from my hometown for
the last time"*:

1. The agent recognises the request is affective rather than categorical and routes the text
   to `get_emotion_labels()`.
2. The classifier returns high probability on `sadness` and `nostalgia`.
3. The agent translates those labels into Spotify audio-feature targets, for example
   `target_valence: 0.3` and `target_acousticness: 0.7`.
4. The Spotify MCP executes the search and the agent returns the tracks.

---

## Design decisions

**MCP over direct API calls.** Exposing the models through MCP rather than calling them
inline makes them available to any MCP-speaking client, not just this agent. It also gives
each tool a typed signature and a docstring the agent reasons over when deciding what to call.

**Two servers rather than one.** Psychological inference and music retrieval are separate
concerns with separate failure modes and rate limits. Splitting them means an outage in one
does not take out the other, and the psych server can be reused independently.

**Serverless inference over local models.** Running the models remotely removes GPU and
local-execution requirements entirely and keeps the whole system on free tiers. The trade-off
is network dependence and rate limiting, handled with explicit exception paths around HTTP
errors and quota exhaustion.

**Lyrics as the input signal.** Audio features describe how a track sounds. Lyrics describe
what it is about. Since the Big Five model is a text regressor, lyrics are the natural input,
and they carry the dispositional signal that audio features miss.

---

## Limitations

Stated plainly, since they bound what the system can claim:

- **No ground truth.** Trait and emotion outputs are model estimates. There is no validated
  instrument in the loop and no evaluation against self-report, so outputs should be read as
  indicative, not diagnostic.
- **Lyrics are not endorsement.** Listening to a song is not agreement with it. The inference
  assumes a correlation between lyrical content and listener state that is real in aggregate
  but noisy per user.
- **Framework mapping is interpretive.** Cognitive-function grip states and Enneagram movement
  are interpretive layers applied on top of Big Five output, not measured quantities.
- **Free-tier constraints.** Serverless inference has per-hour caps, which bounds how much
  history can be scored in one run.
- **Not a clinical tool.** Nothing here is designed for or valid in any wellbeing or
  diagnostic context.

## Possible extensions

- Validate trait estimates against a real Big Five instrument taken by the user.
- Score audio features alongside lyrics and compare which predicts state better.
- Persist trait trajectories to make drift detection statistical rather than observational.
- Add an explicit feedback signal so playlist interventions can be evaluated rather than
  assumed.

---

## Stack

**Orchestration** LangGraph · **Tooling** Model Context Protocol, FastMCP ·
**Inference** Hugging Face Serverless Inference API ·
**Models** `vladinc/bigfive-regression-model`, `SamLowe/roberta-base-go_emotions` ·
**Data** Spotify Web API (recently played, lyrics, audio features, playlists) ·
**Language** Python

---

*Note on claims: this document describes implemented design. It reports no accuracy,
performance, or user-study results, because none were measured. Trait and emotion outputs are
model estimates and are described as such throughout.*
