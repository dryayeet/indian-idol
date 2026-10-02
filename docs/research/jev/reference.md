# Jev Reference Notes

A reference for Jev (`typesafe/jev-1.13`), the question types it accepts, the probability outputs it returns, the ways to call it, and the practical lessons learned from benchmarking it in this project.

> **Scope:** Most of this reference was imported from a separate experiment. The
> `jev.py`, `bench.py`, `results.md`, TypeSafe SDK transport, and direct TypeSafe
> credentials described below are not part of this repository. This project uses
> raw HTTP through OpenRouter in `benchmarks/bakeoff_jev.py`. The imported material
> is retained as research context, not as setup instructions.

## 1. What Jev Is

Jev is TypeSafe AI's flagship "System One" model. The Kahneman framing is the design: System 1 is fast, cheap, gut-level judgment; System 2 is slow, expensive reasoning. Jev is the System 1 layer for machines. Instead of generating text, it reads a `state` (any text blob: logs, tickets, agent tool calls) and answers a set of **typed questions** about it in a single forward pass:

- a **choice** among named options
- a position on an ordered **score** scale
- a **noul** (calibrated yes/no probability)

Every answer comes back as a typed value with a probability distribution and (for choice and score) a confidence number. There is no prompt parsing, no free text to wrangle: the output is data you can branch on with ordinary `if` statements. It is a closed, managed API in early access; the weights are not released.

The typical production shape is a cascade: Jev makes millions of cheap judgment calls and routes only the low-confidence or high-stakes cases to an expensive chat model.

## 2. How It Works

One request contains three things:

```
POST <endpoint>
{
  "model": "typesafe/jev-1.13",
  "state":   "...any text, JSON object, or array...",
  "questions": {
      "<your_name_1>": { "type": "choice", "instructions": "...", "criteria": {...} },
      "<your_name_2>": { "type": "score",  "instructions": "...", "criteria": [...] },
      "<your_name_3>": { "type": "noul",   "instructions": "..." }
  }
}
```

All questions are evaluated **in parallel against one shared copy of the state** in a single round trip. Any mix of types is allowed, any number of questions, and the order of entries is irrelevant (it is a named map; answers come back keyed by the same names). Crucially, the questions are **independent, not chained**: `q2` never sees `q1`'s answer, only the state. Any "risk score → block decision" dependency is your application code's job, not Jev's. The response contains an `answers` map plus a `usage` block (`cost`, `input_tokens`, `output_tokens`).

Because the state is shared, batching everything into one call is the intended usage, not an optimization: TypeSafe's fan-out cookbook measured 13 questions in one request at **12.2x cheaper and 10x faster** than asking them separately, with no change in answers.

## 3. The Three Primitives

| Type | Request fields | Response fields |
|---|---|---|
| `choice` | `type`, `instructions`, `criteria` (map: option name to description; values may be `null`) | `type`, `choice` (winning label), `confidence` (0-1), `probabilities` (per option) |
| `score` | `type`, `instructions`, `criteria` (ordered array of 2-10 level descriptions, low to high) | `type`, `score` (continuous double), `confidence` (0-1), `legend` (level index to description), `probabilities` (per level) |
| `noul` | `type`, `instructions` (optionally `criteria` with exactly `true`/`false` keys) | `type`, `noul` (0-1 probability; no confidence by design) |

Real wire examples from the source experiment (one call, three types):

```json
{
  "routing_stance": {
    "type": "choice",
    "instructions": "Classify the safety stance the system should take on this agent's proposed tool call.",
    "criteria": {
      "auto_allow": "Action is strictly read-only, fully idempotent, and safely handles edge cases.",
      "human_escalation": "Action involves destructive data deletion, schema changes, or real financial transactions."
    }
  },
  "blast_radius": {
    "type": "score",
    "instructions": "Score how much damage this tool call could do if it goes wrong.",
    "criteria": [
      "Fully isolated. Running this 100 times results in the same safe state.",
      "Modifies localized state but relies on soft-deletes or version history.",
      "Overwrites production data. Recovery would require pulling from daily backups.",
      "Irreversible external side-effect (e.g. dropping unbacked-up tables, sending a client email)."
    ]
  },
  "constraint_breach": {
    "type": "noul",
    "instructions": "Did the agent propose a tool action that risks violating a negative constraint ('do not do X') explicitly mentioned in the User Request?"
  }
}
```

And the corresponding answers as actually returned:

```json
{
  "routing_stance":  { "type": "choice", "choice": "human_escalation", "confidence": 0.92,
                       "probabilities": { "human_escalation": 0.94, "hard_block": 0.06, "dry_run_only": 0, "auto_allow": 0 } },
  "blast_radius":    { "type": "score", "score": 2.09, "confidence": 0.90,
                       "legend": { "0": "Fully isolated...", "1": "Modifies localized state...", "2": "Overwrites production data...", "3": "Irreversible external side-effect..." },
                       "probabilities": { "0": 0, "1": 0, "2": 0.9, "3": 0.1 } },
  "constraint_breach": { "type": "noul", "noul": 0.47 }
}
```

## 4. Score Semantics

The score scale is **not 0 to 1**. Levels are positions in the `criteria` array, **0-indexed**, and the returned `score` is a continuous double spread across those positions (effectively the expected position over the per-level probability distribution).

- 2 levels: a 0-to-1 scale
- 4 levels (ours): 0 to 3, where `2.09` means "solidly level 2, slight pull toward level 3" (observed split: 0.9 on level 2, 0.1 on level 3)
- 10 levels: 0 to 9

You control the resolution by choosing the number of levels. Levels are defined by their position and description, not by numeric bands. There is no `min`/`max`/`scale` knob: the rubric length *is* the scale (2 levels → 0-1, 6 levels → 0-5). If you want a 0-100 number, normalize in your application (`score / (len(criteria) - 1) * 100`); that math is app-side, not Jev's.

## 5. Probability Outputs and Confidence

What each answer exposes:

- **choice**: full `probabilities` map (one value per option; options the model deems structurally impossible can sit at exactly 0.000), plus `confidence`
- **score**: `probabilities` per level (string keys "0".."N-1"), the `legend` mapping indices back to your descriptions, plus `confidence`
- **noul**: the returned number *is* the probability; nothing else is exposed

**Confidence is not the winner's probability.** Per the official docs, `confidence` is "a statistic computed from the probability distribution the answer already gives you": it collapses the *shape* of the whole distribution into one number from 0 to 1 (their illustrative approximation for three options is `(3 x largest_probability - 1) / 2`). A flat distribution means low confidence regardless of which option leads. In the source experiment's 20-run benchmark, stance confidence averaged ~0.027 *below* the winner's probability, systematically, on every run, while blast confidence tracked the winner's mass within 0.01. Treat them as two different signals and pick deliberately.

Official guidance on using them:

- A common pattern is three bands: high confidence acts automatically, medium proceeds with caution or flags for review, low routes to a human.
- "A confidence threshold is not one number": stakes should move the threshold (their example: 0.5 floor for ordinary checks, >0.9 for destructive transfers).
- You are never locked into their metric: raw `probabilities` always ship with the answer, so you can compute your own (margins, entropy, etc.).
- Calibration is a statistics game: "start with conservative thresholds, test with your own data, and adjust as you observe results."

Empirical lesson from the source experiment (`results.md`): the single biggest instability source is placing a threshold *at the mean* of a sampling distribution. The demo's `confidence > 0.90` freeze rule sat exactly at the observed mean (~0.90) and fired 14/30 times across three batches (6, 3, 5 per batch, i.e. a coin flip), while `P(winner) > 0.90` fired 93% and `confidence >= 0.95` was a 0/30 dead zone. Branch on the probability margin (winner minus runner-up never left the 0.80-0.90 range across 30 runs) and keep thresholds in dead zones outside the observed band. That policy was validated 30/30 in the source experiment; its `jev.py` and `results.md` are not in this repository.

## 6. Request Reference

Top-level fields:

| Field | Required | Notes |
|---|---|---|
| `model` | yes | e.g. `typesafe/jev-1.13` (OpenRouter) or `jev-1.13.0` (TypeSafe direct) |
| `state` | yes | plain string, JSON object with named fields, or array of text items; text only, no images/audio/video |
| `questions` | yes | named map of question objects; any count, any mix of types, order irrelevant |
| `user` | no | metadata, max 256 chars |
| `session_id` | no | metadata, max 256 chars |
| `trace` | no | tracing info |
| `provider` | no | routing hints (OpenRouter) |

Limits (documented):

- **Token budget**: 64k tokens total for state + all questions combined; the binding constraint is state + the longest single question at 32k tokens. Exceeding it fails with `max_tokens_exceeded`.
- **choice**: up to 255 options (a technical maximum; 4-10 mutually distinguishable options is the practical sweet spot, and anything measuring a continuous property belongs in `score` instead)
- **score**: 2 to 10 levels (OpenRouter's gateway schema technically allows `minItems: 1`, but the native docs say 2-10)
- **noul**: `criteria`, if provided, must have exactly the keys `true` and `false`
- **Account rate limits**: 250,000 tokens/second and 1,200 requests/minute
- HTTP 413 ("Request payload exceeds size limits") as the hard transport cap

Limit caveat: third-party write-ups disagree with the official schema (one claims 16 questions per request and an 8KB body cap; the playground reportedly exposes at most 8 questions). The official API reference puts no cap on question count. Don't hard-code a question-count assumption without testing the exact endpoint you call.

Accuracy note from the docs: answers degrade as the state fills with unnecessary detail. Dense beats long. And `state` has no magic keys: whether you send a string or a JSON object, the field names inside it carry no built-in semantics to the model (the same is true of question IDs). Descriptive naming helps *you* and reads naturally, but the meaning lives in the content, exactly like a prompt.

## 7. Response Reference

Top level: `{ "model": ..., "answers": { ... }, "usage": { "cost": ..., "input_tokens": ..., "output_tokens": ... } }`

Answer shapes keyed by your question names:

| Answer type | Always present | Optional |
|---|---|---|
| choice | `type`, `choice` | `confidence`, `probabilities` |
| score | `type`, `score` | `confidence`, `legend`, `probabilities` |
| noul | `type`, `noul` | (nothing else) |

## 8. Access Routes and Model IDs

| Route | Endpoint | Model IDs | Auth |
|---|---|---|---|
| OpenRouter (alpha tier) | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` (pinned), `~typesafe/jev-latest` (alias) | `OPENROUTER_API_KEY` as Bearer |
| TypeSafe direct | `POST https://api.typesafe.ai/v1/systemone` | `jev-1.13.0` (pinned), `jev-latest`, `jev-preview` (aliases currently resolve to 1.13.0) | `TYPESAFE_API_KEY` as Bearer (keys at console.typesafe.ai/settings/keys) |

Two gotchas: the identifiers differ per route (a model string valid on one 404s on the other), and the OpenRouter endpoint lives under `/api/alpha/`, outside the usual `/api/v1` prefix. Pin explicit versions anywhere thresholds matter; aliases move between releases.

## 9. SDKs

**Python: `typesafe-sdk`** (official, v0.7.0, MIT, Python 3.10+, repo `github.com/typesafe-ai/typesafe-sdk-python`). Aimed at the TypeSafe direct route; auth via the `TYPESAFE_API_KEY` env var, base URL via `TYPESAFE_BASE_URL` (default `https://api.typesafe.ai`), default model via `TYPESAFE_DEFAULT_MODEL` (default `jev-latest`).

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

with TypeSafeClient() as client:
    response = client.system_one(
        state={"document": "I was charged twice. Please fix this ASAP."},
        questions={
            "category": Choice(
                instructions="What is this ticket about?",
                criteria={"billing": None, "technical": None, "other": None},
            ),
            "billing_issue": Noul(instructions="Is this ticket about billing?"),
            "urgency": Score(instructions="How urgent is this ticket?", criteria=["can wait", "this week", "today"]),
        },
    )

print(response.choices["category"].choice)    # e.g. "billing"
print(response.nouls["billing_issue"].noul)   # e.g. 0.97
print(response.scores["urgency"].score)       # e.g. 1.04
```

Note the response groups answers by type (`response.choices`, `response.nouls`, `response.scores`) rather than one flat map. `result.request_id` and `result.raw_http_response` (whose `.json()["answers"]` is the raw wire answers map) are also exposed, and errors raise `TypeSafeAPIError` carrying `.status` and `.request_id`. `RetryPolicy(max_retries=3, backoff_max=0.2, timeout=1.0)` is built in. Questions can also be passed as raw dicts (`{"type": "noul", "instructions": "..."}`), which is how this project calls it. Caveats: the docs only demonstrate `Choice` criteria values as `None` (our description strings are schema-valid on the wire, but untested through the SDK objects), and confidence/probabilities are not documented as SDK attributes, so read them from `raw_http_response` if you need them. There is also an `AsyncTypeSafeClient`.

**Source experiment implementation (`jev.py`)**: if `TYPESAFE_API_KEY` is set, `decide()` calls the SDK (`TypeSafeClient(model="jev-1.13.0")`, raw-dict questions, `result.raw_http_response.json()["answers"]`); otherwise it falls back to raw HTTP against OpenRouter. Both paths return the identical flat `answers` map. That dual transport is not part of this repository.

**TypeScript: `@typesafe-ai/sdk`** (official, Node 20+): `TypeSafeClient` plus a declarative question DSL (`choice`, `noul`, `score` helpers) that infers response types from the question map.

**OpenRouter TypeScript: `@openrouter/sdk`**: supports `decisions.alpha.decisions.create`, but you must pass `serverURL: 'https://openrouter.ai'` explicitly or the call 404s.

**No official Python SDK for the OpenRouter route** (the SDK is hard-wired to the TypeSafe direct route: same auth header shape, different base URL and path, so pointing `TYPESAFE_BASE_URL` at OpenRouter does not work). Raw HTTP is the pattern there (urllib or requests). The source experiment's `jev.py` used this fallback transport:

```python
req = urllib.request.Request(
    "https://openrouter.ai/api/alpha/decisions",
    data=json.dumps({"model": MODEL, "state": state, "questions": questions}).encode(),
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    answers = json.load(resp)["answers"]
```

**Other integrations**: Pydantic AI has a TypeSafe model integration (bool fields become noul, Literal/Enum become choice, constrained floats become score, and lists of Literals fan out into one yes/no per option); typia offers `@typia/jev` for TypeScript question conversion; Vercel's AI Gateway can front the API.

**Warning**: the PyPI package named `typesafe` (v0.9.1, "formal type asserting decorators") is a completely unrelated old library. The real package is `typesafe-sdk`. The source experiment's original `jev.py` was built against the wrong API (`from typesafe import Jev, ...`) and failed with an ImportError; its rewrite talks to the wire format directly.

## 10. Pricing

On OpenRouter, Jev 1.13 is listed at **$0.042 per 1M input tokens, $0 output**, with a 32k context window. For scale: the source experiment's deviation benchmark (20 evaluation calls plus verification runs) cost a fraction of a cent total. This is the economic case for the cascade pattern: cheap System 1 judgment everywhere, expensive System 2 reasoning only where confidence or stakes demand it.

## 11. Practical Tips

1. Keep questions atomic "gut-check" judgments; decompose complex evaluations into several primitives and combine the answers in code (the official Patterns guidance).
2. Put the nuance in `criteria` descriptions, keep `instructions` to one precise sentence.
3. Batch all questions into one call; the state is shared and the round trip is free.
4. Prefer structured `state` objects (named fields) when the evidence has natural parts; it lets criteria reference specific fields.
5. Pin the model version in anything with thresholds.
6. Never place an action threshold at the observed mean of a sampling metric; place it in a dead zone outside the band, or branch on the winner's margin. The source experiment used `margin > 0.5`, warned at breach probability `0.60`, and alerted at blast score `>= 2`.
7. Benchmark deviation on your own inputs with a fixed harness before trusting any single-call demo: bands, dead zones, and calibration quirks are input-specific.
8. Define noul boundaries tightly and neutrally: state what counts as true, don't infer constraints the user never stated, and never bake the desired answer into the question. The source experiment's breach question ("risks violating") left the model genuinely ambivalent (~0.46 mean, never above 0.50 in 30 runs) while stance was 93% sure: a real calibration finding about question wording, not noise.

## 12. Links

Official:

- Docs home and introduction: https://docs.typesafe.ai (full index at https://docs.typesafe.ai/llms.txt)
- Primitives (choice/score/noul): https://docs.typesafe.ai/primitives
- Confidence: https://docs.typesafe.ai/confidence
- Python SDK: https://docs.typesafe.ai/sdk/python/ , https://pypi.org/project/typesafe-sdk/ , https://github.com/typesafe-ai/typesafe-sdk-python
- API keys (direct route): https://console.typesafe.ai/settings/keys
- TypeSafe blog (System One announcement): https://typesafe.ai/blog/introducing-system-one-models-and-jev

OpenRouter:

- Model page and pricing: https://openrouter.ai/typesafe/jev-1.13
- Decisions API schema: https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request
- Jev-verified cascade cookbook: https://openrouter.ai/docs/cookbook/evaluate-and-optimize/jev-verified-cascade

Integrations and third-party write-ups:

- Pydantic AI integration: https://pydantic.dev/docs/ai/models/typesafe/
- Cloudflare AI model page: https://developers.cloudflare.com/ai/models/typesafe/jev/
- typia Jev conversion: https://typia.io/docs/utilization/jev/
- Vercel AI Gateway: https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe
- Concept overview (Samelogic): https://samelogic.com/blog/typesafe-ai-jev-classification
- TS SDK overview mirror: https://deepwiki.com/typesafe-ai/typesafe-sdk-js

## 13. Use in This Project

- [`benchmarks/bakeoff_jev.py`](../../../benchmarks/bakeoff_jev.py) calls the OpenRouter Decisions endpoint with raw HTTP.
- [`benchmarks/bakeoff_jev_40.py`](../../../benchmarks/bakeoff_jev_40.py) compares Jev with dry-prompt and full-agent routing.
- [`docs/benchmarks/jev-tool-selection.md`](../../benchmarks/jev-tool-selection.md) contains the current 41-case result.
- Only `OPENROUTER_API_KEY` is used for these benchmarks. The TypeSafe SDK and direct-route environment variables are not project dependencies.
